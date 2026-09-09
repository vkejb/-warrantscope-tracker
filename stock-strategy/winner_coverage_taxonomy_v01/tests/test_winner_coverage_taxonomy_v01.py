from __future__ import annotations

import hashlib
from pathlib import Path
import unittest

import numpy as np

from extension_entry_study_v01.pipeline import OUTCOME_FIELDS
from multi_setup_study_v01.config import CFG as MULTI_CFG
from multi_setup_study_v01.outcomes import evaluate_unified_outcome
from multi_setup_study_v01.setup_detectors import is_compact_retest
from prospective_shadow_v01.detector import assert_frozen_contract
from surge_event_study_v01.models import Bar, PreparedBenchmark, PreparedStock

from winner_coverage_taxonomy_v01.analysis import (
    choose_discovery_candidate,
    overlap_rows,
    setup_coverage_rows,
    unique_marginal_rows,
)
from winner_coverage_taxonomy_v01.config import (
    CFG,
    FAMILY_BIT,
    FEATURE_NAMES,
)
from winner_coverage_taxonomy_v01.features import build_taxonomy_features
from winner_coverage_taxonomy_v01.taxonomy import (
    assign_frozen_taxonomy,
    fit_discovery_taxonomy,
)


def prepared_stock(closes: list[float], *, code: str = "2330") -> PreparedStock:
    bars = [
        Bar(
            f"2020{1 + index // 28:02d}{1 + index % 28:02d}",
            code,
            code,
            1_000_000,
            close,
            close * 1.01,
            close * 0.99,
            close,
        )
        for index, close in enumerate(closes)
    ]
    prefix_close = [0.0]
    prefix_volume = [0.0]
    prefix_turnover = [0.0]
    returns = [0.0]
    tr = [0.0]
    for index, bar in enumerate(bars):
        prefix_close.append(prefix_close[-1] + bar.close)
        prefix_volume.append(prefix_volume[-1] + bar.volume)
        prefix_turnover.append(prefix_turnover[-1] + bar.close * bar.volume)
        if index:
            returns.append(bar.close / bars[index - 1].close - 1.0)
            tr.append(0.02)
    prefix_return = [0.0]
    prefix_square = [0.0]
    prefix_tr = [0.0]
    for value, tr_value in zip(returns, tr):
        prefix_return.append(prefix_return[-1] + value)
        prefix_square.append(prefix_square[-1] + value * value)
        prefix_tr.append(prefix_tr[-1] + tr_value)
    return PreparedStock(
        code=code,
        name=code,
        bars=bars,
        calendar_indices=list(range(len(bars))),
        segment_ids=[0] * len(bars),
        prefix_close=prefix_close,
        prefix_volume=prefix_volume,
        prefix_turnover_proxy=prefix_turnover,
        daily_returns=returns,
        prefix_return=prefix_return,
        prefix_return_square=prefix_square,
        true_range_ratios=tr,
        prefix_true_range_ratio=prefix_tr,
    )


def synthetic_arrays() -> dict[str, np.ndarray]:
    meta = np.zeros(
        4,
        dtype=[
            ("signal_date", "i4"),
            ("stock_code", "i4"),
            ("cohort_mask", "u1"),
            ("momentum_strength_quintile", "u1"),
            ("entry_gap_bucket", "u1"),
            ("outcome_evaluable", "?"),
        ],
    )
    meta["signal_date"] = [20200102, 20200102, 20200103, 20200103]
    meta["stock_code"] = [1101, 1102, 1103, 1104]
    meta["outcome_evaluable"] = [True, True, True, False]
    outcomes = np.full((4, len(OUTCOME_FIELDS)), np.nan)
    outcomes[:3, OUTCOME_FIELDS.index("primary_success")] = [1, 1, 0]
    for field in OUTCOME_FIELDS[1:]:
        outcomes[:3, OUTCOME_FIELDS.index(field)] = [0.10, 0.09, -0.04]
    outcomes[:3, OUTCOME_FIELDS.index("gross_return")] = [0.08, 0.09, -0.04]
    outcomes[:3, OUTCOME_FIELDS.index("net_return")] = [0.074, 0.084, -0.046]
    masks = np.zeros(4, dtype=np.uint16)
    masks[0] = FAMILY_BIT["N_COMPACT_RETEST_HYPOTHESIS"] | FAMILY_BIT["N_RETEST"]
    masks[1] = FAMILY_BIT["MOMENTUM_DIRECTIONAL"]
    masks[2] = FAMILY_BIT["MOMENTUM_DIRECTIONAL"] | FAMILY_BIT["TREND_PULLBACK"]
    return {
        "meta": meta,
        "outcomes": outcomes,
        "descriptive_outcomes": np.asarray([[1, 0], [0, 0], [0, 0], [np.nan, np.nan]]),
        "family_masks": masks,
        "taxonomy_features": np.zeros((4, len(FEATURE_NAMES))),
    }


class CausalFeatureTests(unittest.TestCase):
    def test_taxonomy_features_do_not_read_t_plus_1_or_later(self):
        stock = prepared_stock([100 + index * 0.1 for index in range(85)])
        benchmark = PreparedBenchmark(
            stock.bars[0].date and [bar.date for bar in stock.bars],
            [bar.close for bar in stock.bars],
            [0] * len(stock.bars),
        )
        before = build_taxonomy_features(stock, 70, benchmark)
        for index in range(71, len(stock.bars)):
            old = stock.bars[index]
            stock.bars[index] = Bar(
                old.date, old.code, old.name, old.volume, 9999, 9999, 9999, 9999
            )
        after = build_taxonomy_features(stock, 70, benchmark)
        np.testing.assert_equal(np.asarray(before), np.asarray(after))

    def test_shared_winner_label_target_must_precede_stop(self):
        closes = [100.0] * 80
        stock = prepared_stock(closes)
        entry_index = 61
        path = [103, 108, 94, 100, 100, 100, 100, 100, 100, 100]
        stock.bars[entry_index] = Bar(
            stock.bars[entry_index].date,
            "2330",
            "2330",
            1_000_000,
            100,
            103,
            99,
            path[0],
        )
        for offset, close in enumerate(path[1:], 1):
            index = entry_index + offset
            old = stock.bars[index]
            stock.bars[index] = Bar(old.date, old.code, old.name, old.volume, close, close, close, close)
        result = evaluate_unified_outcome(stock, 60, MULTI_CFG)
        self.assertEqual("EVALUABLE", result["outcome_status"])
        self.assertTrue(result["primary_success"])
        self.assertEqual(2, result["first_target_day"])
        self.assertEqual(3, result["first_stop_day"])


class CoverageMathTests(unittest.TestCase):
    def test_coverage_denominator_is_all_evaluable_winners(self):
        rows = setup_coverage_rows(synthetic_arrays())
        compact = next(
            row for row in rows
            if row["time_slice_type"] == "OVERALL"
            and row["family"] == "N_COMPACT_RETEST_HYPOTHESIS"
        )
        self.assertEqual(2, compact["all_winners_denominator"])
        self.assertEqual(1, compact["winner_signals"])
        self.assertEqual(0.5, compact["winner_coverage_recall"])

    def test_overlap_and_unique_coverage(self):
        arrays = synthetic_arrays()
        overlap = overlap_rows(arrays)
        row = next(
            item for item in overlap
            if item["time_slice_type"] == "OVERALL"
            and item["left_setup"] == "N_RETEST"
            and item["right_setup"] == "N_COMPACT_RETEST_HYPOTHESIS"
        )
        self.assertEqual(1, row["intersection_winners"])
        self.assertEqual(1.0, row["winner_jaccard"])
        marginal = unique_marginal_rows(arrays)
        momentum = next(
            item for item in marginal
            if item["time_slice_type"] == "OVERALL"
            and item["setup"] == "MOMENTUM_DIRECTIONAL"
        )
        self.assertEqual(1, momentum["incremental_winners_vs_n_compact"])
        self.assertEqual(0.5, momentum["incremental_coverage_vs_n_compact"])

    def test_candidate_selection_ignores_later_period_results(self):
        base = {
            "time_slice_type": "PERIOD",
            "precision_evaluable_signals": 0.1,
            "gross_mean": 0.01,
            "profit_factor": 1.2,
            "top1_removed_pf": 1.1,
            "tail_dependent": False,
        }
        rows = [
            {**base, "time_slice": "HISTORICAL_DISCOVERY", "candidate_family": "A", "unique_winner_coverage": 0.2},
            {**base, "time_slice": "HISTORICAL_DISCOVERY", "candidate_family": "B", "unique_winner_coverage": 0.1},
            {**base, "time_slice": "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", "candidate_family": "A", "unique_winner_coverage": 0.0},
            {**base, "time_slice": "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", "candidate_family": "B", "unique_winner_coverage": 0.9},
        ]
        self.assertEqual("A", choose_discovery_candidate(rows))

    def test_candidate_without_directional_edge_is_not_recommended(self):
        rows = [
            {
                "time_slice_type": "PERIOD",
                "time_slice": "HISTORICAL_DISCOVERY",
                "candidate_family": "BROAD_WINNER_STATE",
                "unique_winner_coverage": 0.8,
                "precision_evaluable_signals": 0.2,
                "gross_mean": -0.001,
                "profit_factor": 0.95,
                "top1_removed_pf": 0.92,
                "tail_dependent": False,
            }
        ]
        self.assertIsNone(choose_discovery_candidate(rows))


class FrozenTaxonomyTests(unittest.TestCase):
    def feature_matrix(self, count: int = 240) -> np.ndarray:
        rng = np.random.default_rng(7)
        matrix = rng.normal(size=(count, len(FEATURE_NAMES)))
        for name in ("recent_breakout_flag", "recent_retest_flag"):
            matrix[:, FEATURE_NAMES.index(name)] = rng.integers(0, 2, count)
        return matrix

    def test_fit_rejects_later_period_and_assignment_does_not_refit(self):
        matrix = self.feature_matrix()
        dates = np.full(len(matrix), 20211231)
        selected = np.ones(len(matrix), dtype=bool)
        model = fit_discovery_taxonomy(matrix, dates, selected, CFG)
        fingerprint = model.fingerprint()
        assigned = assign_frozen_taxonomy(matrix, model)
        self.assertEqual(len(matrix), len(assigned))
        self.assertEqual(fingerprint, model.fingerprint())
        dates[-1] = 20230103
        with self.assertRaisesRegex(RuntimeError, "post-discovery"):
            fit_discovery_taxonomy(matrix, dates, selected, CFG)


class FrozenContractAndSafetyTests(unittest.TestCase):
    def test_n_compact_source_definition_is_unchanged(self):
        assert_frozen_contract()
        self.assertTrue(
            is_compact_retest(
                {"pivot_separation_sessions": 7, "bottom_difference": 1e-12},
                MULTI_CFG,
            )
        )
        self.assertFalse(
            is_compact_retest(
                {"pivot_separation_sessions": 8, "bottom_difference": 1e-12},
                MULTI_CFG,
            )
        )
        path = Path(__file__).resolve().parents[2] / "multi_setup_study_v01" / "setup_detectors.py"
        self.assertEqual(
            CFG.expected_compact_detector_sha256,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def test_module_has_no_order_fill_or_broker_path(self):
        package = Path(__file__).resolve().parents[1]
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in package.glob("*.py")
        )
        for forbidden in ("Yuanta", "submit_order", "place_order", "broker_api"):
            self.assertNotIn(forbidden, source)
        self.assertIn('"actual_orders": 0', source)
        self.assertIn('"actual_fills": 0', source)
        self.assertIn('"broker_connections": 0', source)


if __name__ == "__main__":
    unittest.main()
