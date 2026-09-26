from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from yuanta_live_runtime_v01.trading_bot_service import (
    _RemoteControl,
    _invoke_runtime_control,
    _launch_runtime_start,
    _run_start_preflight,
    build_status,
    render_status,
    serve,
)


class TradingBotStatusTests(unittest.TestCase):
    def test_live_status_is_compact_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime.now(timezone.utc).isoformat()

            heartbeat = {
                "at": now,
                "pid": 123,
                "state": "RUNNING",
                "environment": "PROD",
                "submit_live": True,
                "signal_date": "20260923",
                "watchlist_count": 30,
                "entry_start": "09:05",
                "trade_attempted": True,
                "last_quote_at": now,
                "gate": {
                    "execution_mode": "LIVE",
                    "enable_live_trading": "YES",
                    "cli_live": True,
                    "authorized": True,
                },
                "position": {
                    "stock_id": "9876",
                    "side": "LONG",
                    "quantity": 3000,
                    "entry_price": 77.7,
                },
                "account": "MUST_NOT_RENDER",
                "position_baseline": {"9999|0": 1234},
            }

            (runtime / "heartbeat.json").write_text(
                json.dumps(heartbeat), encoding="utf-8"
            )

            rows = [
                {
                    "at": now,
                    "event": "RISK_APPROVED_CANDIDATE",
                    "candidate": {
                        "stock_id": "1234",
                        "stock_name": "測試",
                        "side": "LONG",
                        "entry_price": 50.0,
                        "quantity": 1000,
                        "score": 0.55,
                    },
                },
                {
                    "at": now,
                    "event": "ENTRY_NOT_FILLED",
                    "stock_id": "1234",
                    "status": "REJECTED",
                    "last_error": "數量錯誤",
                },
            ]

            (runtime / "session.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )

            status = build_status(runtime)
            self.assertEqual(status["runtime_state"], "LIVE_RUNNING")
            self.assertEqual(status["quote_health"], "FRESH")
            self.assertTrue(status["gate_authorized"])
            self.assertNotIn("position", status)

            text = render_status(status)
            self.assertIn("1234", text)
            self.assertIn("數量錯誤", text)
            self.assertNotIn("MUST_NOT_RENDER", text)
            self.assertNotIn("position_baseline", text)
            self.assertNotIn("9876", text)
            self.assertNotIn("3000", text)
            self.assertNotIn("77.7", text)
            self.assertNotIn("目前部位", text)

    def test_status_uses_latest_signal_ledger_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime.now(timezone.utc).isoformat()

            heartbeat = {
                "at": now,
                "state": "RUNNING",
                "environment": "PROD",
                "submit_live": True,
                "last_quote_at": now,
                "watchlist_count": 30,
                "entry_start": "09:05",
                "trade_attempted": True,
            }

            (runtime / "heartbeat.json").write_text(
                json.dumps(heartbeat),
                encoding="utf-8",
            )

            rows = [
                {
                    "at": now,
                    "event": "RISK_APPROVED_CANDIDATE",
                    "candidate": {
                        "stock_id": "1111",
                        "stock_name": "舊訊號",
                        "side": "LONG",
                        "entry_price": 10.0,
                        "quantity": 1000,
                        "score": 0.50,
                    },
                },
                {
                    "at": now,
                    "event": "SIGNAL_DETECTED",
                    "signal_id": "20260929:test",
                    "candidate": {
                        "stock_id": "2222",
                        "stock_name": "新訊號",
                        "side": "LONG",
                        "entry_price": 20.0,
                        "quantity": 1000,
                        "score": 0.75,
                    },
                },
                {
                    "at": now,
                    "event": "SIGNAL_SKIPPED",
                    "signal_id": "20260929:test",
                    "reason": "LIVE_TRADE_LIMIT_CONSUMED",
                    "candidate": {
                        "stock_id": "2222",
                        "stock_name": "新訊號",
                        "side": "LONG",
                        "entry_price": 20.0,
                        "quantity": 1000,
                        "score": 0.75,
                    },
                },
            ]

            (runtime / "session.jsonl").write_text(
                "\n".join(
                    json.dumps(
                        row,
                        ensure_ascii=False,
                    )
                    for row in rows
                )
                + "\n",
                encoding="utf-8",
            )

            status = build_status(runtime)

            self.assertIsInstance(
                status["last_signal"],
                dict,
            )
            self.assertEqual(
                status["last_signal"]["stock_id"],
                "2222",
            )
            self.assertEqual(
                status["last_signal"]["score"],
                0.75,
            )

            rendered = render_status(status)

            self.assertIn("2222", rendered)
            self.assertIn("新訊號", rendered)
            self.assertNotIn("1111", rendered)

    def test_stopping_runtime_is_reported_as_graceful_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime.now(timezone.utc).isoformat()
            heartbeat = {
                "at": now,
                "state": "STOPPING",
                "environment": "PROD",
                "submit_live": True,
                "last_quote_at": now,
                "watchlist_count": 30,
                "entry_start": "09:05",
                "trade_attempted": True,
            }
            (runtime / "heartbeat.json").write_text(
                json.dumps(heartbeat),
                encoding="utf-8",
            )

            status = build_status(runtime)
            self.assertEqual(status["runtime_state"], "LIVE_STOPPING")
            self.assertEqual(status["quote_health"], "FRESH")

            rendered = render_status(status)
            self.assertIn("LIVE_STOPPING", rendered)
            self.assertIn("正常停止中", rendered)

    def test_emergency_stop_marker_is_reported_without_overwriting_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            now = datetime.now(timezone.utc).isoformat()

            (runtime / "heartbeat.json").write_text(
                json.dumps({
                    "at": now,
                    "state": "STOPPED_CLEAN",
                    "environment": "PROD",
                    "submit_live": True,
                }),
                encoding="utf-8",
            )
            (runtime / "EMERGENCY_STOP").write_text(
                "test emergency stop\n",
                encoding="utf-8",
            )

            status = build_status(runtime)

            self.assertEqual(status["runtime_state"], "STOPPED_CLEAN")
            self.assertTrue(status["emergency_stop_active"])

            rendered = render_status(status)
            self.assertIn("Runtime：STOPPED_CLEAN", rendered)
            self.assertIn(
                "交易HALT：ACTIVE（EMERGENCY_STOP）",
                rendered,
            )

    def test_missing_runtime_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = build_status(Path(tmp))
            self.assertEqual(status["runtime_state"], "NOT_STARTED")
            self.assertFalse(status["emergency_stop_active"])

            rendered = render_status(status)
            self.assertIn("NOT_STARTED", rendered)
            self.assertIn("交易HALT：CLEAR", rendered)



class TradingBotRemoteControlTests(unittest.TestCase):
    def test_remote_stop_requires_confirmation_and_executes_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            controls = _RemoteControl(runtime, confirm_ttl_seconds=120)

            with patch(
                "yuanta_live_runtime_v01.trading_bot_service.secrets.randbelow",
                return_value=1234,
            ), patch(
                "yuanta_live_runtime_v01.trading_bot_service._invoke_runtime_control",
                return_value=(True, "GRACEFUL_STOP_REQUESTED"),
            ) as invoke:
                request = controls.handle({"text": "/stop"}, 100)
                self.assertIn("/confirm 1234", request)
                invoke.assert_not_called()

                confirmed = controls.handle({"text": "/confirm 1234"}, 101)
                self.assertIn("正常停止", confirmed)
                invoke.assert_called_once_with("stop", runtime)

                # Replaying the same confirmation cannot execute again because
                # the pending action was consumed before execution.
                replay = controls.handle({"text": "/confirm 1234"}, 101)
                self.assertIn("沒有待確認", replay)
                invoke.assert_called_once()

            audit = (runtime / "trading_bot_audit.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("1234", audit)
            self.assertNotIn("chat_id", audit)
            self.assertNotIn("token", audit.lower())
            self.assertNotIn("account", audit.lower())
            self.assertNotIn("position", audit.lower())
            self.assertNotIn("balance", audit.lower())
            self.assertNotIn("password", audit.lower())
            self.assertNotIn("certificate", audit.lower())

    def test_remote_confirmation_expires_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            controls = _RemoteControl(runtime, confirm_ttl_seconds=120)

            with patch(
                "yuanta_live_runtime_v01.trading_bot_service.secrets.randbelow",
                return_value=7,
            ), patch(
                "yuanta_live_runtime_v01.trading_bot_service.time.monotonic",
                side_effect=[100.0, 221.0],
            ), patch(
                "yuanta_live_runtime_v01.trading_bot_service._invoke_runtime_control",
            ) as invoke:
                request = controls.handle({"text": "/kill"}, 200)
                self.assertIn("/confirm 0007", request)

                result = controls.handle({"text": "/confirm 0007"}, 201)
                self.assertIn("逾時", result)
                invoke.assert_not_called()

    def test_remote_kill_cli_never_adds_live_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)

            completed = SimpleNamespace(
                returncode=0,
                stdout='{"status":"EMERGENCY_STOP_REQUESTED"}',
                stderr="",
            )

            with patch(
                "yuanta_live_runtime_v01.trading_bot_service.subprocess.run",
                return_value=completed,
            ) as run:
                ok, status = _invoke_runtime_control("kill", runtime)

            self.assertTrue(ok)
            self.assertEqual(status, "EMERGENCY_STOP_REQUESTED")

            command = run.call_args.args[0]
            self.assertIn("kill", command)
            self.assertIn("--runtime-dir", command)
            self.assertIn("--reason", command)
            self.assertNotIn("--live", command)
            self.assertNotIn("start-prod", command)


    def test_serve_duplicate_update_id_does_not_execute_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)

            duplicate_update = {
                "update_id": 500,
                "message": {
                    "text": "/confirm 1234",
                    "chat": {"id": 999, "type": "private"},
                },
            }

            with patch(
                "yuanta_live_runtime_v01.trading_bot_service.load_trading_bot_credentials",
                return_value=("TEST_TOKEN", "999"),
            ), patch(
                "yuanta_live_runtime_v01.trading_bot_service._telegram_json",
                side_effect=[
                    {"ok": True, "result": [duplicate_update, duplicate_update]},
                    KeyboardInterrupt(),
                ],
            ), patch(
                "yuanta_live_runtime_v01.trading_bot_service._RemoteControl.handle",
                return_value="ok",
            ) as handle, patch(
                "yuanta_live_runtime_v01.trading_bot_service._send_message",
            ):
                result = serve(runtime, poll_timeout=1)

            self.assertEqual(result, 0)
            handle.assert_called_once()

    def test_serve_rejects_wrong_chat_and_non_private_chat_before_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)

            updates = [
                {
                    "update_id": 600,
                    "message": {
                        "text": "/kill",
                        "chat": {"id": 123, "type": "private"},
                    },
                },
                {
                    "update_id": 601,
                    "message": {
                        "text": "/kill",
                        "chat": {"id": 999, "type": "group"},
                    },
                },
            ]

            with patch(
                "yuanta_live_runtime_v01.trading_bot_service.load_trading_bot_credentials",
                return_value=("TEST_TOKEN", "999"),
            ), patch(
                "yuanta_live_runtime_v01.trading_bot_service._telegram_json",
                side_effect=[
                    {"ok": True, "result": updates},
                    KeyboardInterrupt(),
                ],
            ), patch(
                "yuanta_live_runtime_v01.trading_bot_service._RemoteControl.handle",
            ) as handle, patch(
                "yuanta_live_runtime_v01.trading_bot_service._send_message",
            ):
                result = serve(runtime, poll_timeout=1)

            self.assertEqual(result, 0)
            handle.assert_not_called()


    def test_remote_start_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            controls = _RemoteControl(runtime, confirm_ttl_seconds=120)

            with patch(
                "yuanta_live_runtime_v01.trading_bot_service.secrets.randbelow",
                return_value=4321,
            ), patch(
                "yuanta_live_runtime_v01.trading_bot_service._run_start_preflight",
                return_value=(True, "START_PREFLIGHT_READY"),
            ) as preflight, patch(
                "yuanta_live_runtime_v01.trading_bot_service._launch_runtime_start",
                return_value=(True, "START_DISPATCHED"),
            ) as launch:
                request = controls.handle({"text": "/start"}, 700)
                preflight.assert_called_once_with(runtime)
                self.assertIn("/confirm 4321", request)
                launch.assert_not_called()

                confirmed = controls.handle({"text": "/confirm 4321"}, 701)
                self.assertIn("啟動要求已送出", confirmed)
                launch.assert_called_once_with(runtime)

    def test_remote_start_preflight_failure_never_issues_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            controls = _RemoteControl(runtime, confirm_ttl_seconds=120)

            with patch(
                "yuanta_live_runtime_v01.trading_bot_service._run_start_preflight",
                return_value=(False, "START_PREFLIGHT_FAILED"),
            ) as preflight, patch(
                "yuanta_live_runtime_v01.trading_bot_service._launch_runtime_start",
            ) as launch:
                result = controls.handle({"text": "/start"}, 702)

            preflight.assert_called_once_with(runtime)
            launch.assert_not_called()
            self.assertIsNone(controls.pending)
            self.assertIn("前置檢查未通過", result)
            self.assertIn("未產生確認碼", result)


    def test_start_preflight_forces_dry_run_child_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)

            (runtime / "position_baseline.json").write_text(
                "{}\n",
                encoding="utf-8",
            )

            completed = SimpleNamespace(
                returncode=0,
                stdout='{"status":"READY"}',
                stderr="",
            )

            with patch.dict(
                os.environ,
                {
                    "EXECUTION_MODE": "LIVE",
                    "ENABLE_LIVE_TRADING": "YES",
                },
                clear=False,
            ), patch(
                "yuanta_live_runtime_v01.trading_bot_service.subprocess.run",
                return_value=completed,
            ) as run:
                ok, status = _run_start_preflight(runtime)

                self.assertEqual(os.environ["EXECUTION_MODE"], "LIVE")
                self.assertEqual(os.environ["ENABLE_LIVE_TRADING"], "YES")

            self.assertTrue(ok)
            self.assertEqual(status, "START_PREFLIGHT_READY")

            command = run.call_args.args[0]
            kwargs = run.call_args.kwargs

            self.assertIn("preflight-prod", command)
            self.assertIn("--runtime-dir", command)
            self.assertIn("--baseline", command)
            self.assertNotIn("--live", command)

            self.assertEqual(kwargs["env"]["EXECUTION_MODE"], "DRY_RUN")
            self.assertEqual(kwargs["env"]["ENABLE_LIVE_TRADING"], "NO")
            self.assertTrue(kwargs["capture_output"])
            self.assertEqual(kwargs["timeout"], 90)


    def test_start_launcher_sets_live_gates_only_in_child_environment(self):
        class FakeProcess:
            def wait(self, timeout):
                raise subprocess.TimeoutExpired(cmd="start-prod", timeout=timeout)

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)

            with patch(
                "yuanta_live_runtime_v01.trading_bot_service.subprocess.Popen",
                return_value=FakeProcess(),
            ) as popen:
                ok, status = _launch_runtime_start(runtime)

            self.assertTrue(ok)
            self.assertEqual(status, "START_DISPATCHED")

            command = popen.call_args.args[0]
            kwargs = popen.call_args.kwargs

            self.assertIn("start-prod", command)
            self.assertIn("--live", command)
            self.assertIn("--runtime-dir", command)
            self.assertIn("--baseline", command)

            self.assertNotIn("EXECUTION_MODE", command)
            self.assertNotIn("ENABLE_LIVE_TRADING", command)

            self.assertEqual(kwargs["env"]["EXECUTION_MODE"], "LIVE")
            self.assertEqual(kwargs["env"]["ENABLE_LIVE_TRADING"], "YES")

            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            self.assertTrue(kwargs["start_new_session"])

    def test_start_launcher_refuses_fresh_existing_runtime_before_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            heartbeat = {
                "at": datetime.now(timezone.utc).isoformat(),
                "pid": os.getpid(),
                "state": "RUNNING",
                "submit_live": True,
            }
            (runtime / "heartbeat.json").write_text(
                json.dumps(heartbeat),
                encoding="utf-8",
            )

            with patch(
                "yuanta_live_runtime_v01.trading_bot_service.subprocess.Popen",
            ) as popen:
                ok, status = _launch_runtime_start(runtime)

            self.assertFalse(ok)
            self.assertEqual(status, "RUNTIME_ALREADY_RUNNING")
            popen.assert_not_called()

    def test_start_launcher_reports_immediate_gate_rejection(self):
        class FakeProcess:
            def wait(self, timeout):
                return 1

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)

            with patch(
                "yuanta_live_runtime_v01.trading_bot_service.subprocess.Popen",
                return_value=FakeProcess(),
            ):
                ok, status = _launch_runtime_start(runtime)

            self.assertFalse(ok)
            self.assertEqual(status, "START_REJECTED")


    def test_start_launcher_refuses_persistent_stop_request_before_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "STOP_REQUEST").write_text(
                "test\n",
                encoding="utf-8",
            )

            with patch(
                "yuanta_live_runtime_v01.trading_bot_service.subprocess.Popen",
            ) as popen:
                ok, status = _launch_runtime_start(runtime)

            self.assertFalse(ok)
            self.assertEqual(status, "STOP_REQUEST_ACTIVE")
            popen.assert_not_called()


    def test_remote_clear_halt_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            controls = _RemoteControl(runtime, confirm_ttl_seconds=120)

            with patch(
                "yuanta_live_runtime_v01.trading_bot_service.secrets.randbelow",
                return_value=2468,
            ), patch(
                "yuanta_live_runtime_v01.trading_bot_service._invoke_runtime_control",
                return_value=(True, "HALT_CLEARED"),
            ) as invoke:
                request = controls.handle({"text": "/clear-halt"}, 800)
                self.assertIn("/confirm 2468", request)
                self.assertIn("解除交易 HALT", request)
                invoke.assert_not_called()

                confirmed = controls.handle({"text": "/confirm 2468"}, 801)
                self.assertIn("HALT 已解除", confirmed)
                invoke.assert_called_once_with("clear-halt", runtime)

    def test_clear_halt_cli_uses_prod_baseline_and_never_live_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)

            completed = SimpleNamespace(
                returncode=0,
                stdout='{"status":"HALT_CLEARED","reason":"telegram_clear-halt"}',
                stderr="",
            )

            with patch(
                "yuanta_live_runtime_v01.trading_bot_service.subprocess.run",
                return_value=completed,
            ) as run:
                ok, status = _invoke_runtime_control("clear-halt", runtime)

            self.assertTrue(ok)
            self.assertEqual(status, "HALT_CLEARED")

            command = run.call_args.args[0]
            self.assertIn("clear-halt", command)
            self.assertIn("--runtime-dir", command)
            self.assertIn("--baseline", command)
            self.assertIn(
                str(runtime.resolve() / "position_baseline.json"),
                command,
            )
            self.assertIn("--environment", command)
            self.assertIn("PROD", command)

            self.assertNotIn("--live", command)
            self.assertNotIn("start-prod", command)
            self.assertNotIn("EXECUTION_MODE", command)
            self.assertNotIn("ENABLE_LIVE_TRADING", command)
            self.assertEqual(run.call_args.kwargs["timeout"], 60)


    def test_clear_halt_status_is_found_after_diagnostic_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)

            completed = SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"event":"CONNECTED","status":"INFO"}\n'
                    '{\n'
                    '  "status": "HALT_CLEARED",\n'
                    '  "reason": "telegram_clear-halt"\n'
                    '}\n'
                ),
                stderr="",
            )

            with patch(
                "yuanta_live_runtime_v01.trading_bot_service.subprocess.run",
                return_value=completed,
            ):
                ok, status = _invoke_runtime_control("clear-halt", runtime)

            self.assertTrue(ok)
            self.assertEqual(status, "HALT_CLEARED")


if __name__ == "__main__":
    unittest.main()
