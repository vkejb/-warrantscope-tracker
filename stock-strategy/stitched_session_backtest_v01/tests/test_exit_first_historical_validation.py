from __future__ import annotations

import unittest

from stitched_session_backtest_v01.exit_first_historical_validation import (
    CHECKPOINT_SECONDS,
    _cohort_summary,
    _variants,
)


class ExitFirstHistoricalValidationTests(unittest.TestCase):
    def test_grid_is_fixed_and_baseline_is_first(self):
        variants = _variants()
        self.assertEqual(variants[0].name, "CURRENT_BASELINE")
        self.assertEqual(
            tuple(item.early_failure_seconds for item in variants[1:]),
            CHECKPOINT_SECONDS,
        )
        self.assertTrue(all(item.maximum_progress_r == 0.10 for item in variants))

    def test_leave_one_out_requires_every_deletion_to_remain_positive(self):
        baseline = {
            "a": {"net_pnl_twd": -100.0},
            "b": {"net_pnl_twd": -100.0},
            "c": {"net_pnl_twd": -100.0},
        }
        distributed = [
            {"validation_trade_id": key, "net_pnl_twd": -50.0,
             "entry_time": f"2026-01-0{index}T09:00:00+08:00",
             "exit_reason": "EARLY_FAILURE_NO_PROGRESS"}
            for index, key in enumerate(("a", "b", "c"), start=1)
        ]
        summary = _cohort_summary("TEST", distributed, baseline)
        self.assertEqual(summary["net_difference_vs_baseline_twd"], 150.0)
        self.assertTrue(summary["improvement_survives_every_leave_one_out"])

        outlier = [dict(row) for row in distributed]
        outlier[1]["net_pnl_twd"] = -100.0
        outlier[2]["net_pnl_twd"] = -100.0
        summary = _cohort_summary("TEST", outlier, baseline)
        self.assertFalse(summary["improvement_survives_every_leave_one_out"])


if __name__ == "__main__":
    unittest.main()
