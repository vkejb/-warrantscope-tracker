from __future__ import annotations

from types import SimpleNamespace
import unittest

from prospective_notifications_v01.notifier import top30_market_summary_messages
from shadow_daily_runner.top30_market_summary import calculate_row


class Top30MarketSummaryTests(unittest.TestCase):
    def _bars(self):
        bars = []
        for day in range(1, 21):
            bars.append(
                SimpleNamespace(
                    date=f"202609{day:02d}",
                    name="測試股",
                    volume=day * 1_000,
                    open=99.0 if day == 20 else 98.0,
                    high=105.0 if day == 20 else 101.0,
                    low=95.0 if day == 20 else 97.0,
                    close=102.0 if day == 20 else 100.0,
                )
            )
        return bars

    def test_calculates_requested_fields_with_first_extreme_times(self):
        ticks = [
            {"quote_time": "09:00:01.000", "price": 99.0, "volume": 2.0, "selected_role": "STAGE_A_TOP30"},
            {"quote_time": "09:10:00.000", "price": 105.0, "volume": 1.0},
            {"quote_time": "09:20:00.000", "price": 105.0, "volume": 3.0},
            {"quote_time": "10:00:00.000", "price": 95.0, "volume": 4.0},
        ]
        row = calculate_row(
            {"rank": 1, "stock_id": "1234", "stock_name": "測試股", "score": 0.1},
            self._bars(),
            "20260920",
            ticks,
        )
        self.assertEqual(row["total_volume"], 20_000)
        self.assertEqual(row["average_volume_20"], 10_500.0)
        self.assertEqual(row["high_first_time"], "09:10:00.000")
        self.assertEqual(row["low_first_time"], "10:00:00.000")
        self.assertEqual(row["intraday_range_pct"], 10.0)
        self.assertAlmostEqual(row["vwap"], 99.8)
        self.assertAlmostEqual(row["close_vs_vwap_pct"], (102 / 99.8 - 1) * 100, places=5)
        self.assertEqual(row["intraday_data_status"], "COMPLETE_CAPTURED_TICK_METRICS")

    def test_missing_intraday_data_is_null_not_imputed(self):
        row = calculate_row(
            {"rank": 1, "stock_id": "1234", "stock_name": "測試股", "score": 0.1},
            self._bars(),
            "20260920",
            [],
        )
        self.assertIsNone(row["vwap"])
        self.assertIsNone(row["close_vs_vwap_pct"])
        self.assertIsNone(row["high_first_time"])
        self.assertIsNone(row["low_first_time"])
        self.assertEqual(row["intraday_data_status"], "MISSING_TICK_DATA")

    def test_notification_contains_all_rows_and_stays_below_limit(self):
        rows = []
        for rank in range(1, 31):
            rows.append(
                {
                    "rank": rank,
                    "stock_id": str(2000 + rank),
                    "stock_name": "測試股",
                    "open": 100,
                    "high": 105,
                    "low": 95,
                    "close": 102,
                    "total_volume": 1_234_567,
                    "average_volume_20": 1_000_000,
                    "close_vs_vwap_pct": 1.25,
                    "intraday_range_pct": 10.0,
                    "high_first_time": "09:10:00.000",
                    "low_first_time": "10:00:00.000",
                }
            )
        messages = top30_market_summary_messages(
            "20260920", {"signal_date": "20260920", "rows": rows}
        )
        combined = "\n".join(messages)
        self.assertIn("1. 2001 測試股", combined)
        self.assertIn("30. 2030 測試股", combined)
        self.assertIn("C-VWAP+1.25%", combined)
        self.assertIn("時間取首次", combined)
        self.assertTrue(all(len(message) <= 3800 for message in messages))


if __name__ == "__main__":
    unittest.main()
