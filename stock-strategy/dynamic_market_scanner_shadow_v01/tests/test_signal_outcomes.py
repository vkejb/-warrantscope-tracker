from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.signal_outcomes import (
    _mark, _price_bucket, _time_bucket, _track,
)


class SignalOutcomeTest(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 11, 10, 9, 10, tzinfo=ZoneInfo("Asia/Taipei"))

    def test_mark_uses_quote_at_or_before_checkpoint(self):
        books = [{"time": self.at + timedelta(seconds=59), "best_bid": 101,
                  "best_ask": 101.1},
                 {"time": self.at + timedelta(seconds=61), "best_bid": 999,
                  "best_ask": 999.1}]
        mark = _mark(books, self.at + timedelta(seconds=60))
        self.assertEqual(mark["best_bid"], 101)

    def test_stale_mark_is_not_evaluable(self):
        books = [{"time": self.at, "best_bid": 101, "best_ask": 101.1}]
        self.assertIsNone(_mark(books, self.at + timedelta(minutes=1)))

    def test_unaffordable_track_stays_na(self):
        result = _track({}, [], "original", self.at, 200.0, 0)
        self.assertFalse(result["original_evaluable"])
        self.assertEqual(result["original_status"], "ENTRY_NOT_EVALUABLE")

    def test_frozen_bucket_boundaries(self):
        self.assertEqual(_price_bucket(50), "LE_50")
        self.assertEqual(_price_bucket(100), "GT_50_LE_100")
        self.assertEqual(_price_bucket(100.01), "GT_100_LE_190")
        self.assertEqual(_time_bucket(self.at.replace(hour=10)), "MID_1000_1129")
        self.assertEqual(_time_bucket(self.at.replace(hour=11, minute=30)),
                         "LATE_1130_OR_LATER")


if __name__ == "__main__":
    unittest.main()
