from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.strict_replay import evaluate_observation


TZ = ZoneInfo("Asia/Taipei")


class StrictObservationTest(unittest.TestCase):
    def _data(self):
        start = datetime(2026, 11, 10, 9, 10, tzinfo=TZ)
        ticks = []
        for index in range(7):
            at = start + timedelta(seconds=index * 10)
            price = 100.0 + index * 0.1
            ticks.append({"time": at, "received_at": at, "price": price,
                          "volume": 1000.0, "bid": price - .05,
                          "ask": price + .05, "flag": "1"})
        books = [{"time": start + timedelta(seconds=60), "best_bid": 100.55,
                  "best_ask": 100.60, "buy_volume": 5000, "sell_volume": 4000}]
        return start, {"ticks": ticks, "books": books,
                       "tick_times": [row["time"] for row in ticks],
                       "book_times": [row["time"] for row in books]}

    def test_positive_after_cost_follow_through_passes(self):
        start, data = self._data()
        result = evaluate_observation(
            {"decision_time": start, "entry_price": 100.05,
             "breakout_boundary_price": 100.1}, data,
            start + timedelta(seconds=60), confirmation_market_regime="NEUTRAL")
        self.assertTrue(result["strict_passed"], result)
        self.assertGreater(result["hypothetical_net_progress_twd"], 0)

    def test_bearish_context_fails_closed(self):
        start, data = self._data()
        result = evaluate_observation(
            {"decision_time": start, "entry_price": 100.05,
             "breakout_boundary_price": 100.1}, data,
            start + timedelta(seconds=60), confirmation_market_regime="BEARISH")
        self.assertFalse(result["strict_passed"])
        self.assertIn("MARKET_BEARISH", result["strict_reason"])


if __name__ == "__main__":
    unittest.main()
