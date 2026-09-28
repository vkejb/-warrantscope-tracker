from datetime import datetime, timedelta
import unittest

from pullback_entry_study_v01.analysis import EntryVariant, find_pullback_entry
from yuanta_intraday_shadow_v01.direction_follow_backtest import TAIPEI


class PullbackEntryTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 24, 9, 30, tzinfo=TAIPEI)

    def row(self, seconds, price):
        return {
            "time": self.start + timedelta(seconds=seconds),
            "price": price,
            "bid": price - 0.01,
            "ask": price + 0.01,
        }

    def find(self, rows, side="LONG", variant=None):
        return find_pullback_entry(
            rows,
            side=side,
            original_entry_time=self.start,
            reference_entry_price=100.0,
            reference_r_per_share=2.0,
            variant=variant or EntryVariant("TEST", 0.5, 60),
            latest_entry_time=self.start + timedelta(minutes=10),
        )

    def test_no_entry_without_required_pullback(self):
        self.assertIsNone(self.find([self.row(30, 99.5), self.row(100, 100.2)]))

    def test_new_low_resets_stability_clock(self):
        rows = [
            self.row(10, 99.0), self.row(65, 99.2), self.row(70, 98.8),
            self.row(125, 99.4), self.row(131, 99.5),
        ]
        found = self.find(rows)
        self.assertIsNotNone(found)
        self.assertEqual(found["time"], self.start + timedelta(seconds=131))

    def test_current_tick_is_excluded_from_breakout_window(self):
        rows = [
            self.row(10, 99.0), self.row(50, 99.1), self.row(70, 99.4),
            self.row(71, 99.5),
        ]
        found = self.find(rows)
        self.assertEqual(found["time"], self.start + timedelta(seconds=71))

    def test_short_is_side_symmetric(self):
        rows = [
            self.row(10, 101.0), self.row(50, 100.9), self.row(70, 100.6),
            self.row(71, 100.5),
        ]
        found = self.find(rows, side="SHORT")
        self.assertEqual(found["time"], self.start + timedelta(seconds=71))

    def test_wait_window_is_bounded(self):
        rows = [self.row(610, 99.0), self.row(680, 99.6)]
        self.assertIsNone(self.find(rows))


if __name__ == "__main__":
    unittest.main()
