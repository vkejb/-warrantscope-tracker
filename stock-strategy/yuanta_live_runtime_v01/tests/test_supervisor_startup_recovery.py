"""Offline regressions for startup deadlines and independent supervision."""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from yuanta_live_runtime_v01.account_lock import acquire_account_lock, release_account_lock
from yuanta_live_runtime_v01.force_flat_supervisor import (
    SCHEDULE_GATE, TAIPEI, _supervisor_beat, scheduler_loop,
)
from yuanta_live_runtime_v01.trading_bot_service import _supervisor_thread_healthy, serve
from yuanta_live_runtime_v01.watchdog import runtime_health


class SupervisorStartupRecoveryTests(unittest.TestCase):
    def live_evidence(self, runtime, now):
        (runtime / "heartbeat.json").write_text(json.dumps({
            "at": now.isoformat(), "environment": "PROD", "submit_live": True,
            "state": "STOPPED_UNSAFE",
        }), encoding="utf-8")
        (runtime / "position_baseline.json").write_text("{}", encoding="utf-8")
        (runtime / "position_baseline.meta.json").write_text(
            json.dumps({"trading_date": now.date().isoformat()}), encoding="utf-8")

    def cycle(self, runtime, now, *, health=None, inhibitor_error=None):
        stop = threading.Event()
        health_patch = (patch("yuanta_live_runtime_v01.force_flat_supervisor.runtime_health",
                              return_value=health) if health is not None else nullcontext())
        with (
            patch.dict(os.environ, {SCHEDULE_GATE: "YES"}),
            patch("yuanta_live_runtime_v01.force_flat_supervisor._SleepInhibitor") as inhibitor,
            patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier") as notifier,
            patch("yuanta_live_runtime_v01.force_flat_supervisor.trigger_once", return_value=True) as trigger,
            health_patch,
        ):
            inhibitor.return_value.ensure.side_effect = inhibitor_error
            result = scheduler_loop(runtime, stop_event=stop, clock=lambda: now,
                                    sleeper=lambda _seconds: stop.set())
            self.assertEqual(result, 0)
            return trigger, notifier

    def test_optional_sleep_inhibitor_failure_cannot_block_dead_child_recovery(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime(2026, 10, 2, 10, 0, tzinfo=TAIPEI)
            self.live_evidence(runtime, now)
            trigger, notifier = self.cycle(runtime, now, inhibitor_error=OSError("synthetic failure"))
            trigger.assert_called_once()
            self.assertEqual(trigger.call_args.kwargs["reason"], "RUNTIME_CHILD_EXIT_ONLY_RECOVERY")
            events = [call.args[0] for call in notifier.return_value.critical.call_args_list]
            self.assertIn("TRADING_SLEEP_INHIBITOR_UNAVAILABLE", events)
            heartbeat = json.loads((runtime / "supervisor_heartbeat.json").read_text())
            self.assertEqual(heartbeat["state"], "SUPERVISING_WITH_WARNING")
            self.assertEqual(heartbeat["warnings"], ["TRADING_SLEEP_INHIBITOR_UNAVAILABLE"])

    def test_optional_sleep_inhibitor_failure_cannot_block_scheduled_force_flat(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime(2026, 10, 2, 13, 20, tzinfo=TAIPEI)
            self.live_evidence(runtime, now)
            trigger, _ = self.cycle(runtime, now, health=Mock(healthy=True),
                                    inhibitor_error=OSError("synthetic failure"))
            trigger.assert_called_once()
            self.assertEqual(trigger.call_args.kwargs["reason"], "SCHEDULED_FORCE_FLAT_1320")

    def test_optional_warning_cannot_suppress_exit_request_for_existing_unhealthy_owner(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime(2026, 10, 2, 10, 0, tzinfo=TAIPEI)
            self.live_evidence(runtime, now)
            trigger, _ = self.cycle(runtime, now,
                                    health=Mock(healthy=False, controller_present=True, reason="HEARTBEAT_STALE"),
                                    inhibitor_error=OSError("synthetic failure"))
            trigger.assert_not_called()
            self.assertIn("UNHEALTHY_CONTROLLER_EXIT_ONLY", (runtime / "FORCE_FLAT_REQUEST").read_text())

    def test_optional_warning_survives_recovery_heartbeat_and_clears_after_guard_resumes(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime(2026, 10, 2, 10, 0, tzinfo=TAIPEI)
            self.live_evidence(runtime, now)
            self.cycle(runtime, now, health=Mock(healthy=True), inhibitor_error=OSError("synthetic failure"))
            _supervisor_beat(runtime, now, "EXIT_RECOVERY_SUPERVISING")
            heartbeat = json.loads((runtime / "supervisor_heartbeat.json").read_text())
            self.assertEqual(heartbeat["warnings"], ["TRADING_SLEEP_INHIBITOR_UNAVAILABLE"])
            self.cycle(runtime, now, health=Mock(healthy=True))
            heartbeat = json.loads((runtime / "supervisor_heartbeat.json").read_text())
            self.assertEqual((heartbeat["state"], heartbeat["warnings"]), ("SUPERVISING", []))

    def test_optional_inhibitor_cleanup_failure_cannot_skip_notifier_cleanup(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime(2026, 10, 2, 10, 0, tzinfo=TAIPEI)
            self.live_evidence(runtime, now)
            stop = threading.Event()
            with (
                patch.dict(os.environ, {SCHEDULE_GATE: "YES"}),
                patch("yuanta_live_runtime_v01.force_flat_supervisor._SleepInhibitor") as inhibitor,
                patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier") as notifier,
                patch("yuanta_live_runtime_v01.force_flat_supervisor.runtime_health", return_value=Mock(healthy=True)),
            ):
                inhibitor.return_value.close.side_effect = ProcessLookupError("synthetic terminate race")
                self.assertEqual(scheduler_loop(runtime, stop_event=stop, clock=lambda: now,
                                                sleeper=lambda _seconds: stop.set()), 0)
                notifier.return_value.close.assert_called_once_with(timeout=0.25)
                heartbeat = json.loads((runtime / "supervisor_heartbeat.json").read_text())
                self.assertEqual(heartbeat["warnings"], ["TRADING_SLEEP_INHIBITOR_CLEANUP_FAILED"])

    def test_optional_inhibitor_cleanup_failure_does_not_terminate_post_cutoff_cycles(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime(2026, 10, 2, 16, 0, tzinfo=TAIPEI)
            self.live_evidence(runtime, now)
            stop = threading.Event()
            cycles = []

            def sleep(_seconds):
                cycles.append(1)
                if len(cycles) == 2:
                    stop.set()

            with (
                patch.dict(os.environ, {SCHEDULE_GATE: "YES"}),
                patch("yuanta_live_runtime_v01.force_flat_supervisor._SleepInhibitor") as inhibitor,
                patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier") as notifier,
                patch("yuanta_live_runtime_v01.force_flat_supervisor.trigger_once") as trigger,
            ):
                inhibitor.return_value.close.side_effect = OSError("synthetic wait failure")
                self.assertEqual(scheduler_loop(runtime, stop_event=stop, clock=lambda: now, sleeper=sleep), 0)
                self.assertEqual(len(cycles), 2)
                trigger.assert_not_called()
                notifier.return_value.close.assert_called_once_with(timeout=0.25)

    def test_old_pid_supervisor_heartbeat_gets_only_bounded_restart_grace(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "supervisor_heartbeat.json").write_text(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid() + 1,
            }), encoding="utf-8")
            thread = Mock(is_alive=Mock(return_value=True))
            with patch("yuanta_live_runtime_v01.trading_bot_service.time.monotonic", return_value=100):
                self.assertTrue(_supervisor_thread_healthy(thread, runtime, 100))
            with patch("yuanta_live_runtime_v01.trading_bot_service.time.monotonic", return_value=116):
                self.assertFalse(_supervisor_thread_healthy(thread, runtime, 100))

    def test_dead_thread_and_stale_current_pid_do_not_get_restart_grace(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "supervisor_heartbeat.json").write_text(json.dumps({
                "at": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(), "pid": os.getpid(),
            }), encoding="utf-8")
            with patch("yuanta_live_runtime_v01.trading_bot_service.time.monotonic", return_value=100):
                self.assertFalse(_supervisor_thread_healthy(Mock(is_alive=Mock(return_value=True)), runtime, 100))
                self.assertFalse(_supervisor_thread_healthy(Mock(is_alive=Mock(return_value=False)), runtime, 100))

    def test_bot_restart_polls_while_new_scheduler_replaces_previous_pid_heartbeat(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "supervisor_heartbeat.json").write_text(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid() + 1,
            }), encoding="utf-8")
            stop = threading.Event()

            def poll(*_args, **_kwargs):
                stop.set()
                return {"result": []}

            with (
                patch.dict(os.environ, {SCHEDULE_GATE: "YES"}),
                patch("yuanta_live_runtime_v01.trading_bot_service._supervisor_thread",
                      return_value=Mock(is_alive=Mock(return_value=True))),
                patch("yuanta_live_runtime_v01.trading_bot_service.AsyncTradingNotifier"),
                patch("yuanta_live_runtime_v01.trading_bot_service.load_trading_bot_credentials",
                      return_value=("synthetic-token", "synthetic-chat")),
                patch("yuanta_live_runtime_v01.trading_bot_service._telegram_json", side_effect=poll) as telegram,
                patch("yuanta_live_runtime_v01.trading_bot_service.time.sleep"),
            ):
                self.assertEqual(serve(runtime, stop_event=stop), 0)
                telegram.assert_called_once()

    @contextmanager
    def owned_runtime(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            runtime.mkdir()
            account = acquire_account_lock("S00000000000", "PROD", lock_root=root / "mock_locks", runtime_dir=runtime)
            local = (runtime / "runtime.lock").open("w+", encoding="utf-8")
            try:
                json.dump({"pid": os.getpid()}, local)
                local.flush()
                fcntl.flock(local.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                started = datetime(2026, 10, 2, 9, 30, tzinfo=TAIPEI)
                row = {"at": started.isoformat(), "pid": os.getpid(), "state": "STARTING",
                       "submit_live": True, "environment": "PROD",
                       "account_lock_path": account.name, "account_lock_instance": account.instance_id,
                       "startup_stage": "BROKER_CONNECT",
                       "startup_deadline_at": (started + timedelta(seconds=35)).isoformat()}
                yield runtime, started, row
            finally:
                local.close()
                release_account_lock(account)

    def save_heartbeat(self, runtime, row):
        (runtime / "heartbeat.json").write_text(json.dumps(row), encoding="utf-8")

    def test_owned_startup_phase_deadline_prevents_spurious_force_flat_at_sixteen_seconds(self):
        with self.owned_runtime() as (runtime, started, row):
            self.save_heartbeat(runtime, row)
            now = started + timedelta(seconds=16)
            self.assertTrue(runtime_health(runtime, now=now).healthy)
            trigger, _ = self.cycle(runtime, now)
            trigger.assert_not_called()
            self.assertFalse((runtime / "FORCE_FLAT_REQUEST").exists())

    def test_expired_startup_deadline_requests_exit_only_without_second_controller(self):
        with self.owned_runtime() as (runtime, started, row):
            self.save_heartbeat(runtime, row)
            now = started + timedelta(seconds=35)
            health = runtime_health(runtime, now=now)
            self.assertEqual(health.reason, "STARTUP_DEADLINE_EXPIRED")
            self.assertTrue(health.controller_present)
            trigger, _ = self.cycle(runtime, now)
            trigger.assert_not_called()
            self.assertIn("UNHEALTHY_CONTROLLER_EXIT_ONLY", (runtime / "FORCE_FLAT_REQUEST").read_text())

    def test_malformed_or_oversized_startup_deadlines_fail_closed_even_with_fresh_heartbeat(self):
        with self.owned_runtime() as (runtime, started, row):
            invalid_values = [None, "not-a-time", started.replace(tzinfo=None).isoformat(),
                              started.isoformat(), (started - timedelta(seconds=1)).isoformat(),
                              (started + timedelta(seconds=91)).isoformat()]
            for value in invalid_values:
                with self.subTest(deadline=value):
                    row["startup_deadline_at"] = value
                    self.save_heartbeat(runtime, row)
                    health = runtime_health(runtime, now=started)
                    self.assertFalse(health.healthy)
                    self.assertEqual(health.reason, "STARTUP_DEADLINE_INVALID")
                    self.assertTrue(health.controller_present)

    def test_startup_deadline_cannot_extend_stale_running_or_legacy_starting(self):
        with self.owned_runtime() as (runtime, started, row):
            now = started + timedelta(seconds=16)
            row["state"] = "RUNNING"
            self.save_heartbeat(runtime, row)
            self.assertEqual(runtime_health(runtime, now=now).reason, "HEARTBEAT_STALE")
            row["state"] = "STARTING"
            del row["startup_deadline_at"]
            self.save_heartbeat(runtime, row)
            self.assertEqual(runtime_health(runtime, now=now).reason, "HEARTBEAT_STALE")

    def test_startup_deadline_cannot_replace_account_ownership_or_accept_future_heartbeat(self):
        with self.owned_runtime() as (runtime, started, row):
            row["account_lock_instance"] = "wrong-instance"
            self.save_heartbeat(runtime, row)
            self.assertFalse(runtime_health(runtime, now=started + timedelta(seconds=16)).healthy)
            self.assertFalse(runtime_health(runtime, now=started + timedelta(seconds=16), require_lock=False).healthy)
            self.assertEqual(runtime_health(runtime, now=started - timedelta(seconds=1)).reason, "HEARTBEAT_FUTURE")

    def test_new_bounded_phase_progress_resets_deadline_without_changing_running_timeout(self):
        with self.owned_runtime() as (runtime, started, row):
            progressed = started + timedelta(seconds=30)
            row.update({"at": progressed.isoformat(), "startup_stage": "BROKER_RECONCILIATION",
                        "startup_deadline_at": (progressed + timedelta(seconds=25)).isoformat()})
            self.save_heartbeat(runtime, row)
            self.assertTrue(runtime_health(runtime, now=started + timedelta(seconds=50)).healthy)
            self.assertEqual(runtime_health(runtime, now=started + timedelta(seconds=55)).reason, "STARTUP_DEADLINE_EXPIRED")


if __name__ == "__main__":
    unittest.main()
