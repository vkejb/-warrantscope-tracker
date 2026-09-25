from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from yuanta_live_runtime_v01.trading_bot_service import build_status, render_status


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

    def test_missing_runtime_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = build_status(Path(tmp))
            self.assertEqual(status["runtime_state"], "NOT_STARTED")
            self.assertIn("NOT_STARTED", render_status(status))


if __name__ == "__main__":
    unittest.main()
