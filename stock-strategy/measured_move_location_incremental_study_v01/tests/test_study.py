from __future__ import annotations

import json
from pathlib import Path
import unittest

from surge_event_study_v01.models import Bar, PreparedStock

from measured_move_location_incremental_study_v01.analysis import classify
from measured_move_location_incremental_study_v01.config import CFG, RATIO_BUCKETS
from measured_move_location_incremental_study_v01.pivots import (
    build_structure, compress_pivot, primary_group, ratio_bucket, raw_confirmed_pivots,
)


def stock_from(highs, lows, closes=None):
    closes = closes or [(high + low) / 2 for high, low in zip(highs, lows)]
    bars = [Bar(f"202001{index + 1:02d}", "9999", "TEST", 1000, close, high, low, close) for index, (high, low, close) in enumerate(zip(highs, lows, closes))]
    n = len(bars)
    return PreparedStock("9999", "TEST", bars, list(range(n)), [0] * n, [], [], [], [0.0] * n, [], [], [], [])


def pivot(kind, index, price, confirmation=None):
    confirmation = index + 2 if confirmation is None else confirmation
    return {
        "pivot_type": kind, "pivot_index": index, "pivot_date": f"202001{index+1:02d}",
        "pivot_price": price, "confirmation_index": confirmation,
        "confirmation_date": f"202001{confirmation+1:02d}",
    }


class PivotTests(unittest.TestCase):
    def test_2x2_waits_for_two_right_bars(self):
        stock = stock_from([1, 2, 5, 4, 3], [0.5, 1, 2, 2, 1])
        high = next(row for row in raw_confirmed_pivots(stock, 2, 2) if row["pivot_type"] == "HIGH")
        self.assertEqual(high["pivot_index"], 2)
        self.assertEqual(high["confirmation_index"], 4)

    def test_signal_t_cannot_use_t_plus_1_confirmation(self):
        result = build_structure((pivot("LOW", 0, 10, 2), pivot("HIGH", 3, 20, 5), pivot("LOW", 6, 15, 9)), 17, 8)
        self.assertEqual(result["structure_status"], "STRUCTURE_INVALID")

    def test_compression_keeps_more_extreme_high(self):
        seq = [pivot("HIGH", 1, 10)]
        compress_pivot(seq, pivot("HIGH", 2, 12))
        self.assertEqual(seq[0]["pivot_price"], 12)

    def test_compression_equal_keeps_earlier(self):
        seq = [pivot("LOW", 1, 10)]
        compress_pivot(seq, pivot("LOW", 2, 10))
        self.assertEqual(seq[0]["pivot_index"], 1)

    def test_abc_must_be_low_high_low(self):
        result = build_structure((pivot("HIGH", 0, 10), pivot("LOW", 3, 20), pivot("HIGH", 6, 15)), 17, 10)
        self.assertEqual(result["structure_status"], "STRUCTURE_INVALID")

    def test_c_must_exceed_a(self):
        result = build_structure((pivot("LOW", 0, 10), pivot("HIGH", 3, 20), pivot("LOW", 6, 9)), 17, 10)
        self.assertEqual(result["structure_status"], "STRUCTURE_INVALID")

    def test_target_d(self):
        result = build_structure((pivot("LOW", 0, 10), pivot("HIGH", 3, 20), pivot("LOW", 6, 15)), 20, 10)
        self.assertEqual(result["projected_target_d"], 25)

    def test_completion_ratio(self):
        result = build_structure((pivot("LOW", 0, 10), pivot("HIGH", 3, 20), pivot("LOW", 6, 15)), 20, 10)
        self.assertEqual(result["completion_ratio"], 0.5)

    def test_unavailable_is_not_filled(self):
        result = build_structure(None, 20, 10)
        self.assertIsNone(result["completion_ratio"])
        self.assertEqual(result["unavailable_reason"], "NO_ABC")


class ContractTests(unittest.TestCase):
    def test_bucket_boundaries(self):
        expected = ["B0_R_LT_0", "B1_R_0_TO_0_5", "B2_R_0_5_TO_0_8", "B3_R_0_8_TO_1_0", "B4_R_1_0_TO_1_2", "B5_R_1_2_TO_1_5", "B6_R_GE_1_5"]
        actual = [ratio_bucket(value) for value in (-0.1, 0, 0.5, 0.8, 1.0, 1.2, 1.5)]
        self.assertEqual(actual, expected)

    def test_primary_group_boundaries(self):
        self.assertEqual(primary_group(0.799), "A_R_LT_0_8")
        self.assertEqual(primary_group(0.8), "B_R_0_8_TO_1_2")
        self.assertEqual(primary_group(1.2), "C_R_GE_1_2")

    def test_later_period_cannot_change_buckets(self):
        self.assertEqual(len(RATIO_BUCKETS), 7)
        self.assertEqual(CFG.primary_left, 2)
        self.assertEqual(CFG.sensitivity_left, 3)

    def test_sensitivity_does_not_replace_primary(self):
        self.assertEqual((CFG.primary_left, CFG.primary_right), (2, 2))
        self.assertEqual((CFG.sensitivity_left, CFG.sensitivity_right), (3, 3))

    def test_safety(self):
        self.assertEqual((CFG.actual_orders, CFG.actual_fills, CFG.broker_connections), (0, 0, 0))
        self.assertEqual((CFG.stage_a_refit_count, CFG.model_fit_count), (0, 0))

    def test_spec_excludes_future_outcomes_from_structure(self):
        path = Path(__file__).resolve().parents[1] / "analysis_spec.json"
        if path.exists():
            spec = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(spec["future_outcomes_used_for_structure_construction"])

    def test_classification_requires_later_support(self):
        periods = {}
        for period in ("D", "C", "S"):
            periods[period] = {
                "A_MINUS_B": {"mean_mfe10_delta": .01, "day5_net_mean_delta": .01, "downside_first_rate_delta": -.01},
                "C_MINUS_B": {"mean_mfe10_delta": -.01, "day5_net_mean_delta": -.01, "downside_first_rate_delta": .01},
            }
        periods["S"]["A_MINUS_B"] = {"mean_mfe10_delta": -.01, "day5_net_mean_delta": -.01, "downside_first_rate_delta": .01}
        label, _ = classify(periods, [], 0.8, {p: {"A_R_LT_0_8": 200, "B_R_0_8_TO_1_2": 200} for p in periods})
        self.assertNotEqual(label, "MEASURED_MOVE_LOCATION_EDGE_FOUND")

    def test_source_is_exactly_16_setups(self):
        from stock_specific_setup_discovery_v01.config import SETUP_DEFINITIONS
        self.assertEqual(len(SETUP_DEFINITIONS), 16)

    def test_published_hashes_verify_deterministically(self):
        manifest_path = Path(__file__).resolve().parents[1] / "run_manifest.json"
        if manifest_path.exists():
            from measured_move_location_incremental_study_v01.main import verify
            self.assertEqual(verify()["status"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
