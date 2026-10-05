from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from yuanta_broker_execution_v01 import (
    APCode,
    ExecutionIntent,
    IntentPurpose,
    PriceType,
    Side,
    TimeInForce,
)
from yuanta_live_runtime_v01.force_flat_supervisor import (
    SCHEDULE_GATE,
    TAIPEI,
    _baseline_ready,
    _launch_exit_only,
    _seconds_until_market_cutoff,
    _within_trigger_window,
    scheduler_loop,
    trigger_once,
)
from yuanta_live_runtime_v01.main import (
    _as_market_fallback,
    _authoritative_cash_long_delta,
    _baseline_is_current,
    _write_baseline,
    _write_baseline_metadata,
)


class BaselineTests(unittest.TestCase):
    def test_daily_baseline_is_account_scoped_and_reusable_only_same_day(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "position_baseline.json"
            account = "S12345678901"
            _write_baseline(path, {"2330|0": 1000})
            _write_baseline_metadata(
                path,
                account=account,
                captured_at="2026-10-02T00:30:00+00:00",
            )
            now = datetime(2026, 10, 2, 9, 0, tzinfo=TAIPEI)
            self.assertTrue(_baseline_is_current(path, account=account, now=now))
            self.assertFalse(
                _baseline_is_current(
                    path,
                    account=account,
                    now=datetime(2026, 10, 3, 9, 0, tzinfo=TAIPEI),
                )
            )
            with self.assertRaises(RuntimeError):
                _baseline_is_current(path, account="S99999999999", now=now)

    def test_supervisor_requires_today_metadata(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "position_baseline.json").write_text("{}\n", encoding="utf-8")
            (runtime / "position_baseline.meta.json").write_text(
                json.dumps({"trading_date": "2026-10-01"}),
                encoding="utf-8",
            )
            now = datetime(2026, 10, 2, 13, 20, tzinfo=TAIPEI)
            self.assertFalse(_baseline_ready(runtime, now))


class MarketFallbackTests(unittest.TestCase):
    def intent(self, quantity=1000):
        return ExecutionIntent(
            "force-flat-test",
            "3094",
            Side.SELL,
            quantity,
            Decimal("42.5"),
            purpose=IntentPurpose.EXIT,
        )

    def test_market_fallback_is_ioc_and_has_no_limit_price(self):
        result = _as_market_fallback(self.intent())
        self.assertEqual(result.price_type, PriceType.MARKET)
        self.assertEqual(result.time_in_force, TimeInForce.IOC)
        self.assertEqual(result.ap_code, APCode.REGULAR)
        self.assertIsNone(result.price)

    def test_small_partial_fill_never_uses_market_ioc_odd_lot(self):
        with self.assertRaisesRegex(RuntimeError, "FORCE_FLAT_ODD_LOT_UNSUPPORTED"):
            _as_market_fallback(self.intent(500))

    def test_mixed_board_and_odd_lot_fails_closed(self):
        with self.assertRaises(RuntimeError):
            _as_market_fallback(self.intent(1500))

    def test_authoritative_delta_never_includes_baseline_inventory(self):
        adapter = Mock()
        adapter.reconcile.return_value = Mock(
            broker_positions={"3094|0": 3000},
        )
        store = Mock()
        store.position_buckets.return_value = {"3094|0": 2000}
        result = _authoritative_cash_long_delta(
            adapter,
            store,
            baseline={"3094|0": 1000},
            symbol="3094",
            timeout=2,
        )
        self.assertEqual(result, 2000)
        adapter.reconcile.assert_called_once_with(timeout=2, strict_positions=True)

    def test_force_flat_excludes_verified_manual_inventory_adjustment(self):
        adapter = Mock()
        adapter.position_baseline = {"3094|0": 2000}
        adapter.reconcile.return_value = Mock(
            broker_positions={"3094|0": 3000},
        )
        store = Mock()
        store.position_buckets.return_value = {"3094|0": 1000}

        result = _authoritative_cash_long_delta(
            adapter,
            store,
            baseline={"3094|0": 1000},
            symbol="3094",
            timeout=2,
        )

        self.assertEqual(result, 1000)

    def test_manual_sale_below_baseline_never_buys_back(self):
        adapter = Mock()
        adapter.reconcile.return_value = Mock(
            broker_positions={"3094|0": 500},
        )
        store = Mock()
        store.position_buckets.return_value = {"3094|0": -500}
        with self.assertRaises(RuntimeError):
            _authoritative_cash_long_delta(
                adapter,
                store,
                baseline={"3094|0": 1000},
                symbol="3094",
                timeout=2,
            )

    def test_broker_local_mismatch_fails_closed(self):
        adapter = Mock()
        adapter.reconcile.return_value = Mock(
            broker_positions={"3094|0": 2000},
        )
        store = Mock()
        store.position_buckets.return_value = {"3094|0": 2000}
        with self.assertRaises(RuntimeError):
            _authoritative_cash_long_delta(
                adapter,
                store,
                baseline={"3094|0": 1000},
                symbol="3094",
                timeout=2,
            )


class SupervisorTests(unittest.TestCase):
    def make_runtime(self, root: Path):
        (root / "position_baseline.json").write_text("{}\n", encoding="utf-8")
        (root / "position_baseline.meta.json").write_text(
            json.dumps({"trading_date": "2026-10-02"}),
            encoding="utf-8",
        )

    def test_inactive_runtime_launches_exit_only_and_waits_for_broker_proof(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            self.make_runtime(runtime)
            now = datetime(2026, 10, 2, 13, 20, tzinfo=TAIPEI)
            launched = []

            def launcher(path):
                launched.append(path)
                return Mock(poll=Mock(return_value=None))

            def sleeper(_seconds):
                (runtime / "FORCE_FLAT_REQUEST").unlink(missing_ok=True)
                (runtime / "heartbeat.json").write_text(
                    json.dumps({"state": "STOPPED_CLEAN", "broker_flat_confirmed": True,
                                "broker_flat_confirmed_at": now.isoformat()}),
                    encoding="utf-8",
                )

            self.assertTrue(trigger_once(
                runtime,
                now=now,
                clock=lambda: now,
                wait_seconds=1,
                poll_seconds=.05,
                launcher=launcher,
                sleeper=sleeper,
            ))
            self.assertEqual(launched, [runtime.resolve()])
            ledger = (runtime / "force_flat_supervisor.jsonl").read_text(encoding="utf-8")
            self.assertIn("FORCE_FLAT_CONFIRMED_BASELINE_ONLY", ledger)

    def test_missing_daily_baseline_never_launches_recovery(self):
        with TemporaryDirectory() as tmp:
            launcher = Mock()
            with patch(
                "yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier.critical"
            ):
                self.assertFalse(trigger_once(
                    Path(tmp),
                    now=datetime(2026, 10, 2, 13, 20, tzinfo=TAIPEI),
                    wait_seconds=0,
                    launcher=launcher,
                ))
            launcher.assert_not_called()

    def test_scheduler_requires_explicit_enable_gate(self):
        with patch.dict(os.environ, {SCHEDULE_GATE: "NO"}, clear=False):
            with self.assertRaises(RuntimeError):
                scheduler_loop(Path("/tmp/not-used"), interval_seconds=.25)

    def test_scheduler_writes_durable_start_evidence(self):
        with TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {SCHEDULE_GATE: "YES"},
            clear=False,
        ), patch(
            "yuanta_live_runtime_v01.force_flat_supervisor.time.sleep",
            side_effect=KeyboardInterrupt,
        ):
            runtime = Path(tmp)
            with self.assertRaises(KeyboardInterrupt):
                scheduler_loop(runtime, interval_seconds=.25)
            ledger = (runtime / "force_flat_supervisor.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertIn("SUPERVISOR_STARTED", ledger)
            self.assertIn('"trigger_start": "13:20:00"', ledger)
            self.assertIn('"trigger_end": "13:29:30"', ledger)

    def test_trigger_window_never_retroactively_runs_after_market(self):
        self.assertFalse(_within_trigger_window(
            datetime(2026, 10, 2, 13, 19, 59, tzinfo=TAIPEI)
        ))
        self.assertTrue(_within_trigger_window(
            datetime(2026, 10, 2, 13, 20, 0, tzinfo=TAIPEI)
        ))
        self.assertTrue(_within_trigger_window(
            datetime(2026, 10, 2, 13, 29, 29, tzinfo=TAIPEI)
        ))
        self.assertFalse(_within_trigger_window(
            datetime(2026, 10, 2, 13, 29, 30, tzinfo=TAIPEI)
        ))
        self.assertFalse(_within_trigger_window(
            datetime(2026, 10, 2, 16, 0, 0, tzinfo=TAIPEI)
        ))

    def test_supervisor_never_waits_past_market_cutoff(self):
        self.assertEqual(
            _seconds_until_market_cutoff(
                datetime(2026, 10, 2, 13, 29, 30, tzinfo=TAIPEI)
            ),
            20.0,
        )
        self.assertEqual(
            _seconds_until_market_cutoff(
                datetime(2026, 10, 2, 13, 30, 0, tzinfo=TAIPEI)
            ),
            0.0,
        )

    def test_exit_only_child_has_three_live_gates_and_recovery_flag(self):
        with TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {SCHEDULE_GATE: "YES"},
            clear=False,
        ), patch(
            "yuanta_live_runtime_v01.force_flat_supervisor.subprocess.Popen",
            return_value=Mock(),
        ) as popen:
            runtime = Path(tmp)
            _launch_exit_only(runtime)
        command = popen.call_args.args[0]
        child_env = popen.call_args.kwargs["env"]
        self.assertIn("start-prod", command)
        self.assertIn("--live", command)
        self.assertIn("--recover-force-flat", command)
        self.assertEqual(child_env["EXECUTION_MODE"], "LIVE")
        self.assertEqual(child_env["ENABLE_LIVE_TRADING"], "YES")


if __name__ == "__main__":
    unittest.main()
