"""Reproduce current supervisor defects without touching a trading account.

These assertions deliberately describe existing failure behavior. Passing them
is diagnostic evidence, NOT a safety certification. They should change when a
separately reviewed production repair changes the reproduced behavior.

All persisted state is temporary; broker, Keychain and network boundaries are
mocked. These tests do not demonstrate what code an installed Bot process has
already loaded, nor whether its scheduler thread is alive.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from yuanta_live_runtime_v01.force_flat_supervisor import (
    SCHEDULE_GATE,
    TAIPEI,
    trigger_once,
)
from yuanta_live_runtime_v01.main import (
    _acquire_runtime_instance_lock,
    _release_runtime_instance_lock,
    _run_realtime,
)
from yuanta_live_runtime_v01.trading_bot_notifier import _telegram_send
from yuanta_live_runtime_v01.trading_bot_service import build_status, serve
from yuanta_live_runtime_v01.watchdog import check_once


class SupervisorFailureEvidenceTests(unittest.TestCase):
    """Passing tests confirm a defect was reproduced, not that it is resolved."""

    def test_scheduled_recovery_is_blocked_by_emergency_or_graceful_marker(self):
        for marker in ("EMERGENCY_STOP", "STOP_REQUEST"):
            with self.subTest(marker=marker), TemporaryDirectory() as tmp:
                runtime = Path(tmp)
                (runtime / marker).write_text("synthetic audit marker\n", encoding="utf-8")
                baseline = runtime / "position_baseline.json"
                baseline.write_text("{}\n", encoding="utf-8")
                args = SimpleNamespace(
                    runtime_dir=runtime,
                    baseline=baseline,
                    live=True,
                    recover_emergency=False,
                    recover_force_flat=True,
                )
                with (
                    patch("yuanta_live_runtime_v01.main.RuntimeNotifier"),
                    patch("yuanta_live_runtime_v01.main.AsyncTradingNotifier"),
                    patch(
                        "yuanta_live_runtime_v01.main.LiveTradingGate.from_environment",
                        return_value=SimpleNamespace(authorized=True),
                    ),
                    patch("yuanta_live_runtime_v01.main.load_credentials") as credentials,
                    patch("yuanta_live_runtime_v01.main.load_stage_a_watchlist") as watchlist,
                ):
                    with self.assertRaises(RuntimeError):
                        _run_realtime(args, environment="PROD", submit_live=True)
                    credentials.assert_not_called()
                    watchlist.assert_not_called()
                self.assertTrue((runtime / marker).exists())

    def test_three_launch_failures_exhaust_retry_before_a_fourth_can_recover(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "position_baseline.json").write_text("{}\n", encoding="utf-8")
            (runtime / "position_baseline.meta.json").write_text(
                json.dumps({"trading_date": "2026-10-02"}), encoding="utf-8"
            )
            elapsed = [0.0]
            launch_times = []

            def launcher(path):
                launch_times.append(elapsed[0])
                if len(launch_times) <= 3:
                    raise OSError("synthetic transient startup failure")
                # This recovery would succeed if a fourth attempt were made.
                (path / "FORCE_FLAT_REQUEST").unlink(missing_ok=True)
                (path / "heartbeat.json").write_text(
                    json.dumps({"state": "STOPPED_CLEAN"}), encoding="utf-8"
                )
                return Mock()

            def sleeper(_seconds):
                elapsed[0] += 30.0

            with (
                patch(
                    "yuanta_live_runtime_v01.force_flat_supervisor.time.monotonic",
                    side_effect=lambda: elapsed[0],
                ),
                patch(
                    "yuanta_live_runtime_v01.force_flat_supervisor._runtime_active",
                    return_value=False,
                ),
                patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier"),
            ):
                result = trigger_once(
                    runtime,
                    now=datetime(2026, 10, 2, 13, 20, tzinfo=TAIPEI),
                    wait_seconds=300,
                    launcher=launcher,
                    sleeper=sleeper,
                )
            self.assertFalse(result)
            self.assertEqual(launch_times, [0.0, 30.0, 60.0])
            self.assertGreaterEqual(elapsed[0], 300)
            self.assertTrue((runtime / "FORCE_FLAT_REQUEST").exists())
            self.assertIn(
                "FORCE_FLAT_UNCONFIRMED",
                (runtime / "force_flat_supervisor.jsonl").read_text(encoding="utf-8"),
            )

    def test_dead_scheduler_thread_does_not_stop_bot_polling(self):
        scheduler_failed = threading.Event()
        scheduler_errors = []
        polls = []

        def exception_hook(info):
            scheduler_errors.append(str(info.exc_value))
            scheduler_failed.set()

        def scheduler(_runtime):
            raise OSError("synthetic scheduler ledger failure")

        def telegram_poll(*_args, **_kwargs):
            self.assertTrue(scheduler_failed.wait(timeout=5.0))
            polls.append("polled_after_scheduler_failure")
            if len(polls) == 3:
                raise KeyboardInterrupt
            return {"result": []}

        with (
            TemporaryDirectory() as tmp,
            patch.dict(os.environ, {SCHEDULE_GATE: "YES"}),
            patch(
                "yuanta_live_runtime_v01.trading_bot_service.load_trading_bot_credentials",
                return_value=("synthetic-token", "synthetic-chat"),
            ),
            patch(
                "yuanta_live_runtime_v01.trading_bot_service.force_flat_scheduler_loop",
                side_effect=scheduler,
            ),
            patch(
                "yuanta_live_runtime_v01.trading_bot_service._telegram_json",
                side_effect=telegram_poll,
            ),
            patch("yuanta_live_runtime_v01.trading_bot_service.time.sleep"),
            patch("threading.excepthook", side_effect=exception_hook),
        ):
            result = serve(Path(tmp))
        self.assertEqual(result, 0)
        self.assertEqual(scheduler_errors, ["synthetic scheduler ledger failure"])
        self.assertEqual(len(polls), 3)

    def test_fresh_heartbeat_with_nonexistent_pid_is_reported_healthy(self):
        missing_pid = 2_147_483_647
        with self.assertRaises(ProcessLookupError):
            os.kill(missing_pid, 0)  # Existence check only; no process is signalled.
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime.now(timezone.utc).isoformat()
            (runtime / "heartbeat.json").write_text(
                json.dumps({
                    "at": now,
                    "state": "RUNNING",
                    "pid": missing_pid,
                    "submit_live": True,
                    "last_quote_at": now,
                }),
                encoding="utf-8",
            )
            with patch("yuanta_live_runtime_v01.watchdog.RuntimeNotifier"):
                self.assertTrue(check_once(runtime))
            self.assertTrue(build_status(runtime)["monitoring_market"])

    def test_distinct_runtime_directories_can_own_two_runtime_locks(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = second = None
            try:
                first = _acquire_runtime_instance_lock(root / "checkout_A")
                with self.assertRaises(RuntimeError):
                    _acquire_runtime_instance_lock(root / "checkout_A")
                second = _acquire_runtime_instance_lock(root / "checkout_B")
                self.assertIsNotNone(first)
                self.assertIsNotNone(second)
            finally:
                _release_runtime_instance_lock(second)
                _release_runtime_instance_lock(first)

    def test_telegram_network_failure_is_silently_indistinguishable_from_success(self):
        with patch(
            "yuanta_live_runtime_v01.trading_bot_notifier.urllib.request.urlopen",
            side_effect=OSError("synthetic network outage"),
        ) as open_url:
            self.assertIsNone(_telegram_send("synthetic-token", "synthetic-chat", "alert"))
            self.assertEqual(open_url.call_count, 1)
        with patch(
            "yuanta_live_runtime_v01.trading_bot_notifier.urllib.request.urlopen"
        ) as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = b'{"ok": true}'
            self.assertIsNone(_telegram_send("synthetic-token", "synthetic-chat", "alert"))
            self.assertEqual(open_url.call_count, 1)


if __name__ == "__main__":
    unittest.main()
