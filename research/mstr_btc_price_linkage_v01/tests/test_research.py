import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_research as rr


class ResearchInvariantTests(unittest.TestCase):
    def test_last_known_never_uses_future_candle(self):
        candles = pd.DataFrame({
            "candle_end_utc": pd.to_datetime(["2024-01-02T14:15:00Z", "2024-01-02T14:30:00Z", "2024-01-02T14:45:00Z"]),
            "close": [100.0, 101.0, 999.0],
        })
        target = pd.Series(pd.to_datetime(["2024-01-02T14:30:00Z"]))
        got = rr.last_known(candles, target).iloc[0]
        self.assertEqual(got["close"], 101.0)
        self.assertLessEqual(got["candle_end_utc"], got["target_utc"])

    def test_regression_recovers_beta(self):
        x = pd.Series(np.linspace(-0.05, 0.05, 101), name="btc")
        y = 0.001 + 2.5 * x
        got = rr.regression(y, x)
        self.assertAlmostEqual(got["beta_btc"], 2.5, places=10)
        self.assertAlmostEqual(got["r2"], 1.0, places=10)

    def test_fixed_boundaries_are_not_result_selected(self):
        self.assertEqual(rr.HOLDOUT_START, pd.Timestamp("2024-01-01"))
        self.assertEqual(rr.SPLIT_DATE, pd.Timestamp("2024-08-08"))
        self.assertEqual(rr.WINDOWS, (20, 60, 120))

    def test_intraday_status_is_explicit(self):
        self.assertEqual(rr.INTRADAY_STATUS, "INSUFFICIENT_INTRADAY_DATA")


if __name__ == "__main__":
    unittest.main()
