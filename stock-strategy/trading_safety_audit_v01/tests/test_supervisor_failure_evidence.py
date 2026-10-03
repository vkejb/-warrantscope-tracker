"""Repaired supervisor invariants, using no trading account or real network.

The original defect proofs have been converted where repairs were authorized.
Passing these isolated mocks is still NOT production/deployment certification.

All persisted state is temporary; broker, Keychain and network boundaries are
mocked. These tests do not demonstrate what code an installed Bot process has
already loaded, nor whether its scheduler thread is alive.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    _launch_exit_only,
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
    """Only explicitly tested invariants are certified, never deployment."""

    def test_scheduled_recovery_passes_flags_and_preserves_existing_markers(self):
        for marker in ("EMERGENCY_STOP", "STOP_REQUEST"):
            with self.subTest(marker=marker), TemporaryDirectory() as tmp:
                runtime = Path(tmp)
                (runtime / marker).write_text("synthetic audit marker\n", encoding="utf-8")
                baseline = runtime / "position_baseline.json"
                baseline.write_text("{}\n", encoding="utf-8")
                with (
                    patch.dict(os.environ, {SCHEDULE_GATE: "YES"}),
                    patch("yuanta_live_runtime_v01.force_flat_supervisor.subprocess.Popen", return_value=Mock()) as popen,
                ):
                    _launch_exit_only(runtime)
                    self.assertIn("--recover-emergency", popen.call_args.args[0])
                    self.assertIn("--recover-force-flat", popen.call_args.args[0])
                self.assertTrue((runtime / marker).exists())

    def test_three_launch_failures_do_not_prevent_a_fourth_recovery(self):
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
                    json.dumps({"state": "STOPPED_CLEAN", "broker_flat_confirmed": True,
                                "broker_flat_confirmed_at": datetime(2026, 10, 2, 13, 20, tzinfo=TAIPEI).isoformat()}), encoding="utf-8"
                )
                return Mock(poll=Mock(return_value=0))

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
                    clock=lambda: datetime(2026, 10, 2, 13, 20, tzinfo=TAIPEI) + timedelta(seconds=elapsed[0]),
                    wait_seconds=300,
                    launcher=launcher,
                    sleeper=sleeper,
                )
            self.assertTrue(result)
            self.assertEqual(launch_times, [0.0, 30.0, 60.0, 90.0])
            self.assertFalse((runtime / "FORCE_FLAT_REQUEST").exists())
            self.assertIn(
                "FORCE_FLAT_CONFIRMED_BASELINE_ONLY",
                (runtime / "force_flat_supervisor.jsonl").read_text(encoding="utf-8"),
            )

    def test_dead_scheduler_thread_stops_bot_polling_with_nonzero_exit(self):
        scheduler_failed = threading.Event()
        scheduler_errors = []
        polls = []

        def exception_hook(info):
            scheduler_errors.append(str(info.exc_value))
            scheduler_failed.set()

        def scheduler(_runtime, **_kwargs):
            scheduler_errors.append("synthetic scheduler ledger failure")
            scheduler_failed.set()
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
            patch("yuanta_live_runtime_v01.trading_bot_service.AsyncTradingNotifier"),
            patch("threading.excepthook", side_effect=exception_hook),
        ):
            result = serve(Path(tmp))
        self.assertEqual(result, 2)
        self.assertEqual(scheduler_errors, ["synthetic scheduler ledger failure"])
        self.assertLessEqual(len(polls), 1)

    def test_fresh_heartbeat_with_nonexistent_pid_is_not_reported_healthy(self):
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
                self.assertFalse(check_once(runtime))
            self.assertFalse(build_status(runtime)["monitoring_market"])

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

    def test_telegram_network_failure_is_distinguishable_from_api_confirmation(self):
        with patch(
            "yuanta_live_runtime_v01.trading_bot_notifier.urllib.request.urlopen",
            side_effect=OSError("synthetic network outage"),
        ) as open_url:
            self.assertFalse(_telegram_send("synthetic-token", "synthetic-chat", "alert").delivered)
            self.assertEqual(open_url.call_count, 1)
        with patch(
            "yuanta_live_runtime_v01.trading_bot_notifier.urllib.request.urlopen"
        ) as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = b'{"ok": true}'
            self.assertTrue(_telegram_send("synthetic-token", "synthetic-chat", "alert").delivered)
            self.assertEqual(open_url.call_count, 1)


if __name__ == "__main__":
    unittest.main()
