import json
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_study as rs


class FixedRuleTests(unittest.TestCase):
    def setUp(self):
        self.params = json.loads((Path(__file__).resolve().parents[1] / "params.json").read_text())

    def test_adverse_fill_is_symmetric(self):
        self.assertEqual(rs.adverse_price(100.0, "BUY", 0.01), 101.0)
        self.assertEqual(rs.adverse_price(100.0, "SELL", 0.01), 99.0)

    def test_btc_asof_never_uses_future(self):
        candles = pd.DataFrame({
            "candle_end_utc": pd.to_datetime(["2024-01-01T10:00:00Z", "2024-01-01T10:15:00Z"]),
            "close": [100.0, 999.0],
        })
        target = pd.Series(pd.to_datetime(["2024-01-01T10:07:00Z"]))
        got = rs.last_known_price(candles, target).iloc[0]
        self.assertEqual(got["close"], 100.0)
        self.assertLessEqual(got["candle_end_utc"], got["target"])

    def test_m4_only_adds_btc_direction_filter(self):
        row = pd.Series({"long_signal": True, "short_signal": False, "m4_long_signal": False, "m4_short_signal": False})
        self.assertEqual(rs.entry_side(row, "M3"), "LONG")
        self.assertIsNone(rs.entry_side(row, "M4"))
        row["m4_long_signal"] = True
        self.assertEqual(rs.entry_side(row, "M4"), "LONG")

    def test_trend_invalidation_is_direction_symmetric(self):
        long_bad = pd.Series({"close": 99.0, "ma20": 100.0, "ma60": 90.0})
        short_bad = pd.Series({"close": 101.0, "ma20": 100.0, "ma60": 110.0})
        self.assertTrue(rs.trend_invalid(long_bad, "LONG"))
        self.assertTrue(rs.trend_invalid(short_bad, "SHORT"))

    def test_fixed_parameters_match_mandate(self):
        self.assertEqual(self.params["ma_fast_days"], 20)
        self.assertEqual(self.params["ma_slow_days"], 60)
        self.assertEqual(self.params["breakout_days"], 20)
        self.assertEqual(self.params["atr_days"], 14)
        self.assertEqual(self.params["risk_per_trade_fraction"], 0.005)
        self.assertEqual(self.params["max_notional_fraction"], 0.25)
        self.assertEqual(self.params["btc_confirmation_lookback_calendar_days"], 30)


if __name__ == "__main__":
    unittest.main()
