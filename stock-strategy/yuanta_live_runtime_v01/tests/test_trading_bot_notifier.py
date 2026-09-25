from __future__ import annotations

import unittest

from yuanta_live_runtime_v01.trading_bot_notifier import (
    format_critical,
    format_runtime_event,
)


class TradingBotNotifierTests(unittest.TestCase):
    def test_entry_rejection_contains_reason(self):
        message = format_runtime_event(
            "ENTRY_NOT_FILLED",
            {
                "stock_id": "1234",
                "status": "REJECTED",
                "last_error": "數量錯誤",
                "account": "MUST_NOT_RENDER",
                "position_baseline": {"1234|0": 1000},
            },
        )
        self.assertIn("數量錯誤", message)
        self.assertNotIn("MUST_NOT_RENDER", message)
        self.assertNotIn("position_baseline", message)

    def test_signal_is_compact(self):
        message = format_runtime_event(
            "RISK_APPROVED_CANDIDATE",
            {
                "candidate": {
                    "stock_id": "1234",
                    "stock_name": "測試",
                    "side": "LONG",
                    "entry_price": 50.0,
                    "quantity": 1000,
                    "score": 0.55,
                },
                "broker_positions": {"9999|0": 9999},
            },
        )
        self.assertIn("交易訊號成立", message)
        self.assertIn("1234", message)
        self.assertNotIn("broker_positions", message)

    def test_critical_omits_details(self):
        message = format_critical(
            "MAX_DAILY_LOSS",
            "Daily loss boundary reached",
        )
        self.assertIn("MAX_DAILY_LOSS", message)
        self.assertNotIn("account", message)


if __name__ == "__main__":
    unittest.main()
