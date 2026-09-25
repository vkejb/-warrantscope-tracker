from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from yuanta_live_runtime_v01.trading_bot_service import (
    _RemoteControl,
    _invoke_runtime_control,
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

    def test_missing_runtime_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = build_status(Path(tmp))
            self.assertEqual(status["runtime_state"], "NOT_STARTED")
            self.assertIn("NOT_STARTED", render_status(status))



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


if __name__ == "__main__":
    unittest.main()
