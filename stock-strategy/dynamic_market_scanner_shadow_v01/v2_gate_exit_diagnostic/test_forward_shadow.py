import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from dynamic_market_scanner_shadow_v01.v2_gate_exit_diagnostic import (
    forward_shadow,
)


class ForwardShadowTests(unittest.TestCase):
    def test_quote_checkpoint_never_reads_future_quote(self):
        now = datetime.fromisoformat("2026-11-16T09:12:30+08:00")
        before = now - timedelta(seconds=2)
        after = now + timedelta(seconds=1)
        stock = {
            "ticks": [(before, {"buy_price": "25.1", "sell_price": "25.2",
                                 "quote_time": "09:12:28"}),
                      (after, {"buy_price": "26", "sell_price": "26.1"})],
            "tick_times": [before, after],
            "books": [(before, {"buy_prices": ["25.1"],
                                  "buy_volumes": ["3"],
                                  "sell_prices": ["25.2"],
                                  "sell_volumes": ["4"]}),
                      (after, {"buy_prices": ["26"],
                               "sell_prices": ["26.1"]})],
            "book_times": [before, after],
        }
        result = forward_shadow.quote_checkpoint(stock, now)
        self.assertEqual(result["tick_buy_price"], "25.1")
        self.assertEqual(result["buy_volumes_raw"], ["3"])
        self.assertEqual(result["tick_age_seconds"], 2)
        self.assertIsNone(result["book_exchange_time"])
        self.assertIsNone(result["capacity_shares"])

    def test_stale_and_missing_are_explicit(self):
        now = datetime.fromisoformat("2026-11-16T09:12:30+08:00")
        old = now - timedelta(seconds=11)
        stock = {"ticks": [], "tick_times": [],
                 "books": [(old, {"buy_volumes": ["1"]})],
                 "book_times": [old]}
        result = forward_shadow.quote_checkpoint(stock, now)
        self.assertFalse(result["tick_fresh_5s"])
        self.assertFalse(result["book_fresh_10s"])
        self.assertIsNone(result["tick_received_at"])

    def test_activation_fails_closed_on_code_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "activation.json"
            path.write_text(json.dumps({"start_session_date": "20261116",
                "config_hash": forward_shadow.CONFIG_HASH,
                "config": forward_shadow.CONFIG, "head": "frozen-head",
                "file_hashes": {"x": "frozen"},
                "actual_orders": 0, "broker_order_calls": 0}))
            with patch.object(forward_shadow, "ACTIVATION", path), \
                 patch.object(forward_shadow, "_head", return_value="changed"):
                with self.assertRaisesRegex(RuntimeError, "FORWARD_CODE_DRIFT"):
                    forward_shadow._require_activation("20261116")

    def test_research_has_no_order_capability(self):
        self.assertEqual(forward_shadow.CONFIG["actual_orders"], 0)
        self.assertEqual(forward_shadow.CONFIG["broker_order_calls"], 0)
        self.assertEqual(forward_shadow.START_DATE, "20261116")

    def test_missing_calendar_day_is_not_zero_pnl(self):
        self.assertEqual(forward_shadow._missing_calendar_days(
            "20261117", {"20261117"}), ["20261116"])


if __name__ == "__main__":
    unittest.main()
