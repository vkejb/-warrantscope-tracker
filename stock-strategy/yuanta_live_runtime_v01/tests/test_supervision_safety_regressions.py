"""Safety supervision/outbox regressions: all broker/network/process I/O mocked."""
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
    SCHEDULE_GATE, TAIPEI, _launch_exit_only, _SleepInhibitor,
    _prior_session_remediated, scheduler_loop, trigger_once,
)
from yuanta_live_runtime_v01.notifications import RuntimeNotifier
from yuanta_live_runtime_v01.trading_bot_notifier import DeliveryResult, NotificationOutbox, _telegram_send
from yuanta_live_runtime_v01.trading_bot_service import build_status, serve, _supervisor_thread_healthy
from yuanta_live_runtime_v01.watchdog import broker_flat_proof, runtime_health, runtime_lock_owned
from yuanta_broker_execution_v01 import LiveOrderStore


class HealthRegressions(unittest.TestCase):
    def test_live_health_requires_pid_kernel_lock_and_account_instance(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            runtime.mkdir()
            local = (runtime / "runtime.lock").open("w+")
            account = acquire_account_lock("S00000000000", "PROD", lock_root=root / "locks", runtime_dir=runtime)
            try:
                json.dump({"pid": os.getpid()}, local)
                local.flush()
                fcntl.flock(local.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                now = datetime.now(timezone.utc)
                row = {"at": now.isoformat(), "pid": os.getpid(), "state": "RUNNING", "submit_live": True,
                       "account_lock_path": account.name, "account_lock_instance": account.instance_id}
                path = runtime / "heartbeat.json"
                path.write_text(json.dumps(row))
                self.assertTrue(runtime_health(runtime, now=now).healthy)
                # A living, lock-owning controller alone is not market monitoring.
                self.assertFalse(build_status(runtime)["monitoring_market"])
                row["state"] = "PREOPEN_WAITING"
                path.write_text(json.dumps(row))
                self.assertTrue(runtime_health(runtime, now=now).healthy)
                self.assertEqual(
                    build_status(runtime)["runtime_state"],
                    "LIVE_PREOPEN_WAITING",
                )
                self.assertFalse(build_status(runtime)["monitoring_market"])
                row["state"] = "RUNNING"
                row["last_quote_at"] = now.isoformat()
                path.write_text(json.dumps(row))
                self.assertTrue(build_status(runtime)["monitoring_market"])
                row["account_lock_instance"] = "wrong-instance"
                path.write_text(json.dumps(row))
                health = runtime_health(runtime, now=now)
                self.assertFalse(health.healthy)
                self.assertTrue(health.controller_present)  # Never take over a living owner.
                self.assertFalse(build_status(runtime)["monitoring_market"])
                del row["account_lock_instance"]
                path.write_text(json.dumps(row))
                health = runtime_health(runtime, now=now)
                self.assertFalse(health.healthy)
                self.assertTrue(health.controller_present)
            finally:
                local.close()
                release_account_lock(account)

    def test_future_heartbeat_and_dead_pid_are_not_healthy(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime.now(timezone.utc)
            path = runtime / "heartbeat.json"
            path.write_text(json.dumps({"at": (now + timedelta(seconds=10)).isoformat(),
                                        "pid": os.getpid(), "state": "RUNNING"}))
            self.assertFalse(runtime_health(runtime, now=now).healthy)
            path.write_text(json.dumps({"at": now.isoformat(), "pid": 2147483647,
                                        "state": "RUNNING", "submit_live": True}))
            self.assertFalse(runtime_health(runtime, now=now).healthy)
            self.assertFalse(build_status(runtime)["monitoring_market"])

    def test_marker_only_clean_or_old_flat_proof_is_not_certified(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime.now(timezone.utc)
            path = runtime / "heartbeat.json"
            path.write_text(json.dumps({"state": "STOPPED_CLEAN"}))
            self.assertFalse(broker_flat_proof(runtime, since=now))
            row = {"state": "STOPPED_CLEAN", "broker_flat_confirmed": True,
                   "broker_flat_confirmed_at": (now - timedelta(minutes=1)).isoformat()}
            path.write_text(json.dumps(row))
            self.assertFalse(broker_flat_proof(runtime, since=now))
            row["broker_flat_confirmed_at"] = now.isoformat()
            path.write_text(json.dumps(row))
            self.assertTrue(broker_flat_proof(runtime, since=now))

    def test_malformed_or_overflowing_pid_is_fail_closed(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            for pid in (float("inf"), 10 ** 100, "not-a-pid"):
                with self.subTest(pid=str(pid)):
                    (runtime / "heartbeat.json").write_text(json.dumps({
                        "at": datetime.now(timezone.utc).isoformat(), "pid": pid, "state": "RUNNING"}))
                    self.assertFalse(runtime_health(runtime).healthy)
                    self.assertFalse(build_status(runtime)["monitoring_market"])

    def test_invalid_lock_owner_or_supervisor_pid_does_not_crash_health_probe(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            for owner in ([], {"pid": float("inf")}, {"pid": 10 ** 100}):
                (runtime / "runtime.lock").write_text(json.dumps(owner))
                self.assertFalse(runtime_lock_owned(runtime, os.getpid()))
            thread = Mock(is_alive=Mock(return_value=True))
            for pid in (float("inf"), 10 ** 100):
                (runtime / "supervisor_heartbeat.json").write_text(json.dumps({
                    "at": datetime.now(timezone.utc).isoformat(), "pid": pid}))
                self.assertFalse(_supervisor_thread_healthy(thread, runtime, 0))


class SupervisorRegressions(unittest.TestCase):
    @staticmethod
    def fixture(runtime, now):
        (runtime / "position_baseline.json").write_text("{}\n")
        (runtime / "position_baseline.meta.json").write_text(json.dumps({"trading_date": now.date().isoformat()}))

    def test_fourth_recovery_attempt_succeeds_after_three_failures(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime(2026, 10, 2, 13, 20, tzinfo=TAIPEI)
            self.fixture(runtime, now)
            elapsed = [0.0]
            attempts = []
            def clock():
                return now + timedelta(seconds=elapsed[0])
            def launcher(path):
                attempts.append(elapsed[0])
                if len(attempts) <= 3:
                    raise OSError("synthetic transient launch failure")
                (path / "FORCE_FLAT_REQUEST").unlink()
                (path / "heartbeat.json").write_text(json.dumps({
                    "state": "STOPPED_CLEAN", "broker_flat_confirmed": True,
                    "broker_flat_confirmed_at": clock().isoformat()}))
                return Mock(poll=Mock(return_value=0))
            def sleep(_seconds):
                elapsed[0] += 30
            with patch("yuanta_live_runtime_v01.force_flat_supervisor.time.monotonic", side_effect=lambda: elapsed[0]), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier"):
                self.assertTrue(trigger_once(runtime, now=now, clock=clock, wait_seconds=300,
                                             max_launch_attempts=3, launcher=launcher, sleeper=sleep))
            self.assertEqual(attempts, [0, 30, 60, 90])

    def test_alive_unhealthy_child_does_not_spawn_duplicate_and_alerts(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime(2026, 10, 2, 13, 20, tzinfo=TAIPEI)
            self.fixture(runtime, now)
            elapsed = [0.0]
            launcher = Mock(return_value=Mock(poll=Mock(return_value=None)))
            def sleep(_seconds):
                elapsed[0] += 30
            with patch("yuanta_live_runtime_v01.force_flat_supervisor.time.monotonic", side_effect=lambda: elapsed[0]), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier") as notifier:
                self.assertFalse(trigger_once(runtime, now=now, clock=lambda: now + timedelta(seconds=elapsed[0]),
                                              wait_seconds=120, launcher=launcher, sleeper=sleep))
            self.assertEqual(launcher.call_count, 1)
            self.assertIn("FORCE_FLAT_CHILD_UNHEALTHY", [call.args[0] for call in notifier.return_value.critical.call_args_list])

    def test_missing_proof_and_market_cutoff_never_launch(self):
        with TemporaryDirectory() as tmp, patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier") as notifier:
            launcher = Mock()
            self.assertFalse(trigger_once(Path(tmp), now=datetime(2026, 10, 2, 13, 30, tzinfo=TAIPEI), launcher=launcher))
            launcher.assert_not_called()
            notifier.return_value.critical.assert_called_once()

    def test_missing_baseline_still_requests_exit_only_but_never_launches(self):
        with TemporaryDirectory() as tmp, patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier"):
            runtime = Path(tmp)
            launcher = Mock()
            self.assertFalse(trigger_once(runtime, now=datetime(2026, 10, 2, 13, 20, tzinfo=TAIPEI), launcher=launcher))
            launcher.assert_not_called()
            self.assertTrue((runtime / "FORCE_FLAT_REQUEST").exists())

    def test_scheduler_survives_ledger_cycle_exception(self):
        with TemporaryDirectory() as tmp:
            stop = threading.Event()
            cycles = []
            def sleep(_seconds):
                cycles.append(1)
                if len(cycles) == 2:
                    stop.set()
            with patch.dict(os.environ, {SCHEDULE_GATE: "YES"}), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor._supervisor_beat", side_effect=[OSError("disk failure"), None]) as beat, \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor._is_trading_day", return_value=False), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier"):
                self.assertEqual(scheduler_loop(Path(tmp), stop_event=stop, sleeper=sleep,
                                               clock=lambda: datetime(2026, 10, 2, 9, 30, tzinfo=TAIPEI)), 0)
            self.assertEqual(beat.call_count, 2)

    def test_dead_live_child_before_1320_requests_only_exit_recovery(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime(2026, 10, 2, 9, 30, tzinfo=TAIPEI)
            self.fixture(runtime, now)
            (runtime / "heartbeat.json").write_text(json.dumps({"at": now.isoformat(), "environment": "PROD",
                                                               "submit_live": True, "state": "STOPPED_UNSAFE"}))
            stop = threading.Event()
            with patch.dict(os.environ, {SCHEDULE_GATE: "YES"}), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor._is_trading_day", return_value=True), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor._SleepInhibitor"), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.trigger_once", return_value=True) as trigger, \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier"):
                scheduler_loop(runtime, stop_event=stop, clock=lambda: now, sleeper=lambda _s: stop.set())
            self.assertEqual(trigger.call_args.kwargs["reason"], "RUNTIME_CHILD_EXIT_ONLY_RECOVERY")

    def test_calendar_failure_cannot_block_previously_authorized_child_recovery(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime(2026, 10, 2, 9, 30, tzinfo=TAIPEI)
            self.fixture(runtime, now)
            (runtime / "heartbeat.json").write_text(json.dumps({"at": now.isoformat(), "environment": "PROD",
                                                               "submit_live": True, "state": "STOPPED_UNSAFE"}))
            stop = threading.Event()
            with patch.dict(os.environ, {SCHEDULE_GATE: "YES"}), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor._is_trading_day", side_effect=OSError("missing calendar")) as calendar, \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor._SleepInhibitor"), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.trigger_once", return_value=True) as trigger, \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier"):
                scheduler_loop(runtime, stop_event=stop, clock=lambda: now, sleeper=lambda _s: stop.set())
            calendar.assert_not_called()
            trigger.assert_called_once()

    def test_prior_unsafe_session_warns_before_current_market_window(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            prior = datetime(2026, 10, 2, 9, 30, tzinfo=TAIPEI)
            now = datetime(2026, 10, 3, 8, 0, tzinfo=TAIPEI)
            (runtime / "heartbeat.json").write_text(json.dumps({"at": prior.isoformat(), "environment": "PROD",
                                                               "submit_live": True, "state": "STOPPED_UNSAFE"}))
            stop = threading.Event()
            with patch.dict(os.environ, {SCHEDULE_GATE: "YES"}), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.trigger_once") as trigger, \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier") as notifier:
                scheduler_loop(runtime, stop_event=stop, clock=lambda: now, sleeper=lambda _s: stop.set())
            trigger.assert_not_called()
            self.assertIn("SUPERVISOR_PRIOR_SESSION_UNRESOLVED", [call.args[0] for call in notifier.return_value.critical.call_args_list])

    def test_newer_baseline_and_halt_clear_suppress_stale_prior_session_warning(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            prior = datetime.now(TAIPEI) - timedelta(days=1)
            now = datetime.now(TAIPEI) + timedelta(minutes=1)
            (runtime / "heartbeat.json").write_text(json.dumps({"at": prior.isoformat(), "environment": "PROD",
                                                               "submit_live": True, "state": "STOPPED_UNSAFE"}))
            (runtime / "position_baseline.json").write_text("{}\n")
            (runtime / "position_baseline.meta.json").write_text(json.dumps({
                "trading_date": now.date().isoformat(),
                "captured_at": (now - timedelta(minutes=2)).isoformat(),
            }))
            with LiveOrderStore(runtime / "live-orders.sqlite") as store:
                store.halt("test prior unsafe session")
                store.clear_halt("broker reconciliation succeeded")
            prior_row = json.loads((runtime / "heartbeat.json").read_text())
            self.assertTrue(_prior_session_remediated(runtime, prior_row, now))

            stop = threading.Event()
            with patch.dict(os.environ, {SCHEDULE_GATE: "YES"}), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.trigger_once") as trigger, \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier") as notifier:
                scheduler_loop(runtime, stop_event=stop, clock=lambda: now, sleeper=lambda _s: stop.set())
            trigger.assert_not_called()
            self.assertNotIn("SUPERVISOR_PRIOR_SESSION_UNRESOLVED",
                             [call.args[0] for call in notifier.return_value.critical.call_args_list])
            ledger = (runtime / "force_flat_supervisor.jsonl").read_text()
            self.assertIn("SUPERVISOR_PRIOR_SESSION_REMEDIATION_RECOGNIZED", ledger)

    def test_safety_marker_prevents_prior_session_warning_suppression(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            prior = datetime.now(TAIPEI) - timedelta(days=1)
            now = datetime.now(TAIPEI) + timedelta(minutes=1)
            (runtime / "position_baseline.json").write_text("{}\n")
            (runtime / "position_baseline.meta.json").write_text(json.dumps({
                "trading_date": now.date().isoformat(),
                "captured_at": (now - timedelta(minutes=2)).isoformat(),
            }))
            with LiveOrderStore(runtime / "live-orders.sqlite") as store:
                store.clear_halt("broker reconciliation succeeded")
            prior_row = {"at": prior.isoformat(), "environment": "PROD",
                         "submit_live": True, "state": "STOPPED_UNSAFE"}
            (runtime / "EMERGENCY_STOP").write_text("active\n")
            self.assertFalse(_prior_session_remediated(runtime, prior_row, now))

    def test_missed_window_warns_without_retroactive_order(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime(2026, 10, 2, 16, 0, tzinfo=TAIPEI)
            (runtime / "heartbeat.json").write_text(json.dumps({"at": now.isoformat(), "environment": "PROD",
                                                               "submit_live": True, "state": "STOPPED_UNSAFE"}))
            stop = threading.Event()
            with patch.dict(os.environ, {SCHEDULE_GATE: "YES"}), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.trigger_once") as trigger, \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier") as notifier:
                scheduler_loop(runtime, stop_event=stop, clock=lambda: now, sleeper=lambda _s: stop.set())
            trigger.assert_not_called()
            self.assertIn("SUPERVISOR_CUTOFF_EXPOSURE_UNCONFIRMED", [call.args[0] for call in notifier.return_value.critical.call_args_list])

    def test_new_live_session_after_earlier_flat_still_gets_1320_supervision(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            moments = [datetime(2026, 10, 2, 9, 30, tzinfo=TAIPEI)]
            self.fixture(runtime, moments[0])
            (runtime / "heartbeat.json").write_text(json.dumps({"state": "STOPPED_CLEAN", "broker_flat_confirmed": True,
                "broker_flat_confirmed_at": moments[0].isoformat(), "at": moments[0].isoformat(),
                "environment": "PROD", "submit_live": True}))
            stop = threading.Event()
            def sleep(_s):
                if moments[0].hour == 9:
                    moments[0] = datetime(2026, 10, 2, 13, 20, tzinfo=TAIPEI)
                    (runtime / "heartbeat.json").write_text(json.dumps({"state": "RUNNING", "at": moments[0].isoformat(),
                        "environment": "PROD", "submit_live": True}))
                else:
                    stop.set()
            with patch.dict(os.environ, {SCHEDULE_GATE: "YES"}), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor._is_trading_day", return_value=True), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.runtime_health", return_value=Mock(healthy=True)), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor._SleepInhibitor"), \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.trigger_once", return_value=True) as trigger, \
                 patch("yuanta_live_runtime_v01.force_flat_supervisor.RuntimeNotifier"):
                scheduler_loop(runtime, stop_event=stop, clock=lambda: moments[0], sleeper=sleep)
            self.assertEqual(trigger.call_count, 1)
            self.assertEqual(trigger.call_args.kwargs["reason"], "SCHEDULED_FORCE_FLAT_1320")

    def test_missing_telegram_credentials_does_not_block_safety_thread(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {SCHEDULE_GATE: "YES"}), \
             patch("yuanta_live_runtime_v01.trading_bot_service.load_trading_bot_credentials", side_effect=OSError("missing credentials")), \
             patch("yuanta_live_runtime_v01.trading_bot_service.AsyncTradingNotifier"), \
             patch("yuanta_live_runtime_v01.trading_bot_service._supervisor_thread", return_value=Mock(is_alive=Mock(return_value=True))) as thread, \
             patch("yuanta_live_runtime_v01.trading_bot_service._telegram_json") as poll:
            stop = threading.Event()
            with patch("yuanta_live_runtime_v01.trading_bot_service.time.sleep", side_effect=lambda _s: stop.set()):
                self.assertEqual(serve(Path(tmp), stop_event=stop), 0)
            thread.assert_called_once()
            poll.assert_not_called()

    def test_dead_supervisor_exits_bot_nonzero_before_more_polling(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {SCHEDULE_GATE: "YES"}), \
             patch("yuanta_live_runtime_v01.trading_bot_service.load_trading_bot_credentials", return_value=("mock", "mock")), \
             patch("yuanta_live_runtime_v01.trading_bot_service.AsyncTradingNotifier") as notifier, \
             patch("yuanta_live_runtime_v01.trading_bot_service._supervisor_thread", return_value=Mock(is_alive=Mock(return_value=False))), \
             patch("yuanta_live_runtime_v01.trading_bot_service._telegram_json") as poll:
            self.assertEqual(serve(Path(tmp)), 2)
            poll.assert_not_called()
            notifier.return_value.critical.assert_called_once()

    def test_child_logs_observed_and_emergency_recovery_flags_kept(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {SCHEDULE_GATE: "YES"}), \
             patch("yuanta_live_runtime_v01.force_flat_supervisor.subprocess.Popen", return_value=Mock()) as popen:
            _launch_exit_only(Path(tmp))
            command = popen.call_args.args[0]
            self.assertIn("--recover-emergency", command)
            self.assertIn("--recover-force-flat", command)
            self.assertEqual(popen.call_args.kwargs["stdout"].name, str(Path(tmp) / "exit_recovery.out.log"))
            self.assertEqual(popen.call_args.kwargs["stderr"].name, str(Path(tmp) / "exit_recovery.err.log"))

    def test_sleep_inhibitor_is_future_lifecycle_and_bounded(self):
        with TemporaryDirectory() as tmp, patch("yuanta_live_runtime_v01.force_flat_supervisor.sys.platform", "darwin"), \
             patch("yuanta_live_runtime_v01.force_flat_supervisor.subprocess.Popen", return_value=Mock(poll=Mock(return_value=None))) as popen:
            keeper = _SleepInhibitor()
            now = datetime(2026, 10, 2, 9, 30, tzinfo=TAIPEI)
            keeper.ensure(Path(tmp), now)
            keeper.ensure(Path(tmp), now)
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(popen.call_args.args[0][0], "/usr/bin/caffeinate")
            keeper.close()
            popen.return_value.terminate.assert_called_once()


class OutboxRegressions(unittest.TestCase):
    def test_credentials_reload_after_failure_and_restart(self):
        with TemporaryDirectory() as tmp, patch("yuanta_live_runtime_v01.trading_bot_notifier.time.time", return_value=100):
            outbox = NotificationOutbox(Path(tmp))
            identifier = outbox.enqueue("TEST", "alert")
            loader = Mock(side_effect=[OSError("temporary credential absence"), ("MUST_NOT_PERSIST", "chat")])
            sender = Mock(return_value=DeliveryResult(True, "API_CONFIRMED"))
            self.assertFalse(outbox.deliver_once(now=100, credential_loader=loader, sender=sender).delivered)
            sender.assert_not_called()
            self.assertEqual(outbox.snapshot()[0]["status"], "RETRY")
            restarted = NotificationOutbox(Path(tmp))
            self.assertIsNone(restarted.deliver_once(now=101, credential_loader=loader, sender=sender))
            self.assertTrue(restarted.deliver_once(now=102, credential_loader=loader, sender=sender).delivered)
            row = restarted.snapshot()[0]
            self.assertEqual((row["notification_id"], row["status"], row["attempts"]), (identifier, "SENT", 2))
            self.assertNotIn("MUST_NOT_PERSIST", restarted.ledger.read_text())

    def test_cross_process_lease_prevents_concurrent_send_and_recovers_crash(self):
        with TemporaryDirectory() as tmp, patch("yuanta_live_runtime_v01.trading_bot_notifier.time.time", return_value=100):
            one, two = NotificationOutbox(Path(tmp)), NotificationOutbox(Path(tmp))
            one.enqueue("TEST", "alert")
            self.assertIsNotNone(one._claim(100))
            self.assertIsNone(two._claim(100))
            sender = Mock(return_value=DeliveryResult(True, "API_CONFIRMED"))
            self.assertTrue(two.deliver_once(now=161, credential_loader=lambda: ("mock", "chat"), sender=sender).delivered)
            self.assertEqual(two.snapshot()[0]["attempts"], 2)
            sender.assert_called_once()

    def test_network_failure_is_explicit_and_pending_survives(self):
        with patch("yuanta_live_runtime_v01.trading_bot_notifier.urllib.request.urlopen", side_effect=OSError("secret-url")):
            failure = _telegram_send("mock", "chat", "alert")
        self.assertFalse(failure.delivered)
        self.assertNotIn("secret-url", failure.outcome)
        with patch("yuanta_live_runtime_v01.trading_bot_notifier.urllib.request.urlopen") as url:
            url.return_value.__enter__.return_value.read.return_value = b'{"ok": true}'
            self.assertTrue(_telegram_send("mock", "chat", "alert").delivered)

    def test_duplicate_key_and_retry_after_are_persisted(self):
        with TemporaryDirectory() as tmp, patch("yuanta_live_runtime_v01.trading_bot_notifier.time.time", return_value=100):
            outbox = NotificationOutbox(Path(tmp))
            first = outbox.enqueue("TEST", "alert", key="dedupe")
            self.assertEqual(first, outbox.enqueue("TEST", "alert", key="dedupe"))
            result = outbox.deliver_once(now=100, credential_loader=lambda: ("mock", "chat"),
                                        sender=lambda *_a: DeliveryResult(False, "API_REJECTED", 60))
            self.assertFalse(result.delivered)
            self.assertEqual(len(outbox.snapshot()), 1)
            self.assertEqual(outbox.snapshot()[0]["next_attempt_at"], 160)

    def test_critical_receipt_does_not_claim_delivery_and_disk_failure_is_visible(self):
        with TemporaryDirectory() as tmp, patch("yuanta_live_runtime_v01.notifications.AsyncTradingNotifier") as asynchronous:
            asynchronous.return_value.critical.return_value = "mock-id"
            notifier = RuntimeNotifier(Path(tmp))
            receipt = notifier.critical("TEST", "alert")
            self.assertEqual(receipt["TRADING_BOT"], "DURABLY_QUEUED_NOT_DELIVERED")
            asynchronous.return_value.critical.side_effect = OSError("disk-full")
            receipt = notifier.critical("TEST", "alert")
            self.assertEqual(receipt["TRADING_BOT"], "PERSISTENCE_FAILED_NOT_DELIVERED")
            notifier.close()


if __name__ == "__main__":
    unittest.main()
