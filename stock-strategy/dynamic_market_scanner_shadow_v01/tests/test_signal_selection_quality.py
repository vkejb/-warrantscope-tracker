from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.signal_selection_quality import (
    _path, _rs_bucket, quality_label,
)


class SignalSelectionQualityTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 11, 13, 9, 10, tzinfo=ZoneInfo("Asia/Taipei"))

    def test_labels_are_fixed_on_after_cost_mfe(self):
        self.assertEqual(quality_label(3500), "STRONG_RISE_1R")
        self.assertEqual(quality_label(1), "COST_COVERING_RISE_BELOW_1R")
        self.assertEqual(quality_label(0), "NO_COST_COVERING_RISE")
        self.assertEqual(quality_label(None), "DATA_INSUFFICIENT")

    def test_rs_buckets_keep_existing_one_percent_boundary(self):
        self.assertEqual(_rs_bucket({"relative_strength_5m": 0.0049}), "LT_0_5PCT")
        self.assertEqual(
            _rs_bucket({"relative_strength_5m": 0.0099}), "GE_0_5_LT_1PCT",
        )
        self.assertEqual(_rs_bucket({"relative_strength_5m": 0.01}), "GE_1_LT_2PCT")

    def test_path_uses_only_first_thirty_minutes_and_records_timing(self):
        books = [
            {"time": self.at, "best_bid": 100.0},
            {"time": self.at + timedelta(minutes=5), "best_bid": 104.0},
            {"time": self.at + timedelta(minutes=31), "best_bid": 200.0},
        ]
        result = _path(books, self.at, 100.0, 1000)
        self.assertEqual(result["mfe_30m_bid"], 104.0)
        self.assertEqual(result["time_to_1r_seconds"], 300.0)


if __name__ == "__main__":
    unittest.main()
