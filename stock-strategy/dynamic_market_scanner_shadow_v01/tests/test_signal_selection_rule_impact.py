from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.signal_selection_rule_impact import (
    _fresh_entry, current_opportunity,
)


class SignalSelectionRuleImpactTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 11, 13, 9, 10, tzinfo=ZoneInfo("Asia/Taipei"))

    def data(self):
        tick = {"time": self.at, "price": 50, "bid": 50, "ask": 50.1, "volume": 1}
        book = {"time": self.at, "best_bid": 50, "best_ask": 50.1}
        return {"ticks": [tick], "tick_times": [self.at],
                "books": [book], "book_times": [self.at]}

    def test_fresh_entry_uses_ask_plus_one_tick_and_whole_lot(self):
        fill, reason = _fresh_entry(self.data(), self.at)
        self.assertEqual(reason, "PASSED")
        self.assertGreater(fill["entry_price"], 50.1)
        self.assertEqual(fill["quantity"] % 1000, 0)

    def test_current_rule_rejects_below_one_percent_before_confirmation(self):
        signal = {"signal_time": self.at - timedelta(seconds=180),
                  "relative_strength_5m": 0.0099, "entry_price": 50}
        result = current_opportunity(signal, self.data())
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "RS_BELOW_1PCT")


if __name__ == "__main__":
    unittest.main()
