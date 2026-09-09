from __future__ import annotations

import inspect
from pathlib import Path
import unittest

import numpy as np

from cross_sectional_alpha_ranking_v01.data import ledger_hashes
from cross_sectional_alpha_ranking_v01.preprocessing import purged_training_mask
from extension_entry_study_v01.pipeline import OUTCOME_FIELDS
from prospective_shadow_v01.detector import assert_frozen_contract

from conditional_path_quality_ranking_v01.analysis import probability_metrics, retention_rows
from conditional_path_quality_ranking_v01.config import CFG
from conditional_path_quality_ranking_v01.data import load_reused_inputs, path_classes
from conditional_path_quality_ranking_v01.models import build_conditional_features, fit_logistic_ridge
from conditional_path_quality_ranking_v01.ranking import conditional_ranks, cooldown_proxy


PACKAGE = Path(__file__).resolve().parents[1]
STOCK_STRATEGY = PACKAGE.parent


class FrozenReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.arrays, cls.stage_a, cls.audit = load_reused_inputs(
            STOCK_STRATEGY / "winner_coverage_taxonomy_v01/runtime/observation_store.npz",
            STOCK_STRATEGY / "cross_sectional_alpha_ranking_v01/runtime/ranking_store.npz",
            STOCK_STRATEGY / "upside_opportunity_ranking_v01/runtime/ranking_store.npz",
            STOCK_STRATEGY / "upside_opportunity_ranking_v01/stage_a_model_spec.json",
            STOCK_STRATEGY / "upside_opportunity_ranking_v01/run_manifest.json",
        )

    def test_frozen_stage_a_exact_reuse_and_no_refit(self):
        self.assertEqual(self.stage_a.fingerprint(), CFG.expected_stage_a_fingerprint)
        self.assertEqual(self.audit["stage_a_refit_count"], 0)
        self.assertEqual(self.audit["stage_a_pool_rows"], 30 * self.audit["stage_a_pool_dates"])

    def test_conditional_pool_is_exact_published_stage_a_top30(self):
        pool = self.arrays["stage_a_ranks"] <= 30
        ranks, audit = conditional_ranks(
            self.arrays["stage_a_scores"], pool,
            self.arrays["meta"]["signal_date"], self.arrays["meta"]["stock_code"],
        )
        self.assertTrue(np.array_equal(ranks["rank"] > 0, pool))
        self.assertEqual(audit["maximum_pool_rank"], 30)

    def test_n_compact_contract_unchanged(self):
        assert_frozen_contract()
        source = inspect.getsource(__import__("multi_setup_study_v01.setup_detectors", fromlist=["is_compact_retest"]).is_compact_retest)
        self.assertIn("pivot_separation_sessions", source)
        self.assertIn("bottom_difference", source)

    def test_prospective_ledger_unchanged_by_pure_research_functions(self):
        before = ledger_hashes(STOCK_STRATEGY)
        pool = self.arrays["stage_a_ranks"] <= 30
        conditional_ranks(
            self.arrays["stage_a_scores"], pool,
            self.arrays["meta"]["signal_date"], self.arrays["meta"]["stock_code"],
        )
        self.assertEqual(before, ledger_hashes(STOCK_STRATEGY))

    def test_no_broker_order_or_fill_execution_surface(self):
        text = "\n".join(
            path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py")
        ).lower()
        for forbidden in ("yuanta", "submit_order", "place_order", "send_order", "broker_sdk"):
            self.assertNotIn(forbidden, text)

    def test_reported_execution_counters_are_zero(self):
        source = (PACKAGE / "main.py").read_text(encoding="utf-8")
        self.assertIn('"actual_orders": 0', source)
        self.assertIn('"actual_fills": 0', source)
        self.assertIn('"broker_connections": 0', source)


class PathLabelTests(unittest.TestCase):
    def _outcomes(self, rows: int) -> np.ndarray:
        return np.full((rows, len(OUTCOME_FIELDS)), np.nan, dtype=np.float64)

    def test_path_label_correctness(self):
        values = self._outcomes(3)
        success = OUTCOME_FIELDS.index("primary_success")
        mfe = OUTCOME_FIELDS.index("mfe_10d")
        mae = OUTCOME_FIELDS.index("mae_10d")
        values[:, success] = [1, 0, 0]
        values[:, mfe] = [0.09, 0.10, 0.07]
        values[:, mae] = [-0.02, -0.06, -0.04]
        result = path_classes(values, OUTCOME_FIELDS, np.ones(3, dtype=bool))
        self.assertEqual(result.tolist(), [1, 2, 3])

    def test_timeout_is_non_success_and_target_reaching_timeout_fails(self):
        values = self._outcomes(1)
        values[0, OUTCOME_FIELDS.index("primary_success")] = 0
        values[0, OUTCOME_FIELDS.index("mfe_10d")] = 0.07
        values[0, OUTCOME_FIELDS.index("mae_10d")] = -0.04
        result = path_classes(values, OUTCOME_FIELDS, np.ones(1, dtype=bool))
        self.assertEqual(int(result[0]), 3)
        self.assertEqual(float(result[0] == 1), 0.0)
        values[0, OUTCOME_FIELDS.index("mfe_10d")] = 0.08
        with self.assertRaises(RuntimeError):
            path_classes(values, OUTCOME_FIELDS, np.ones(1, dtype=bool))


class CausalityAndFitTests(unittest.TestCase):
    def test_conditional_features_do_not_read_future_for_past_rows(self):
        base = np.arange(390, dtype=float).reshape(10, 39) / 390
        scores = np.linspace(0.01, 0.10, 10)
        ranks = np.tile(np.arange(1, 6), 2)
        dates = np.repeat([20200102, 20200103], 5)
        fit = np.array([True] * 5 + [False] * 5)
        first, spec = build_conditional_features(base, scores, ranks, dates, fit)
        changed_base = base.copy()
        changed_scores = scores.copy()
        changed_base[5:] = 999
        changed_scores[5:] = 999
        second, changed_spec = build_conditional_features(changed_base, changed_scores, ranks, dates, fit)
        np.testing.assert_allclose(first[:5], second[:5])
        self.assertEqual(spec, changed_spec)

    def test_discovery_only_fit_ignores_later_target_changes(self):
        rng = np.random.default_rng(7)
        features = rng.normal(size=(120, 5))
        target = (rng.random(120) > 0.6).astype(float)
        mask = np.zeros(120, dtype=bool)
        mask[:80] = True
        preprocessing = {"stage_a_score_mean": 0.1, "stage_a_score_scale": 0.02}
        first = fit_logistic_ridge(features, target, mask, 1.0, "DISCOVERY", preprocessing)
        changed = target.copy()
        changed[80:] = 1.0 - changed[80:]
        second = fit_logistic_ridge(features, changed, mask, 1.0, "DISCOVERY", preprocessing)
        self.assertEqual(first.fingerprint(), second.fingerprint())

    def test_purged_fit_has_no_later_period_rows(self):
        dates = np.repeat(np.arange(20200101, 20200131), 2)
        mask, audit = purged_training_mask(dates, np.ones(len(dates), dtype=bool), "20200101", "20200130", 10)
        self.assertLessEqual(int(np.max(dates[mask])), audit["last_included_signal_date"])
        self.assertEqual(len(audit["purged_signal_dates"]), 10)


class RankingAndMetricTests(unittest.TestCase):
    def test_top5_ranking_is_within_pool_and_tie_breaks_by_code(self):
        scores = np.r_[np.ones(6), np.zeros(2)]
        pool = np.array([True] * 6 + [False] * 2)
        dates = np.repeat(20200102, 8)
        codes = np.array([6, 5, 4, 3, 2, 1, 7, 8])
        result, _ = conditional_ranks(scores, pool, dates, codes)
        selected_codes = codes[(result["rank"] > 0) & (result["rank"] <= 5)]
        self.assertEqual(selected_codes.tolist(), [5, 4, 3, 2, 1])
        self.assertTrue(np.all(result["rank"][~pool] == 0))

    def test_probability_deciles_cover_one_through_ten_without_uint8_overflow(self):
        scores = np.arange(30, dtype=float)
        pool = np.ones(30, dtype=bool)
        dates = np.repeat(20200102, 30)
        codes = np.arange(1000, 1030)
        result, _ = conditional_ranks(scores, pool, dates, codes)
        self.assertEqual(sorted(np.unique(result["probability_decile"]).tolist()), list(range(1, 11)))
        self.assertEqual(int(np.count_nonzero(result["probability_decile"] == 1)), 3)
        self.assertEqual(int(np.count_nonzero(result["probability_decile"] == 10)), 3)

    def test_calibration_metrics_are_exact(self):
        result = probability_metrics(
            np.array([1.0, 0.0, 1.0, 0.0]),
            np.array([0.8, 0.2, 0.6, 0.4]),
        )
        self.assertAlmostEqual(result["event_rate"], 0.5)
        self.assertAlmostEqual(result["predicted_probability_mean"], 0.5)
        self.assertAlmostEqual(result["brier_score"], 0.1)
        self.assertAlmostEqual(result["auc"], 1.0)

    def test_mfe_retention_and_mae_improvement_are_exact(self):
        rows = []
        for kind, label, _start, _end in __import__("conditional_path_quality_ranking_v01.analysis", fromlist=["time_slices"]).time_slices():
            rows.extend([
                {"time_slice_type": kind, "time_slice": label, "cohort": "STAGE_A_TOP30", "mfe10_mean": 0.10, "mae10_mean": -0.08},
                {"time_slice_type": kind, "time_slice": label, "cohort": "CONDITIONAL_TOP5", "mfe10_mean": 0.08, "mae10_mean": -0.05},
            ])
        mfe, mae = retention_rows(rows)
        self.assertAlmostEqual(mfe[0]["mfe_retention_ratio"], 0.8)
        self.assertAlmostEqual(mae[0]["mae_absolute_improvement"], 0.03)
        self.assertAlmostEqual(mae[0]["mae_improvement_percentage"], 0.375)

    def test_cooldown_allows_same_stock_at_session_distance_ten(self):
        dates = np.arange(20)
        codes = np.repeat(2330, 20)
        selected = np.ones(20, dtype=bool)
        rank = np.ones(20, dtype=np.uint8)
        accepted, _ = cooldown_proxy(selected, rank, dates, codes, 10)
        self.assertEqual(np.flatnonzero(accepted).tolist(), [0, 10])

    def test_later_period_refit_is_fixed_zero(self):
        source = (PACKAGE / "main.py").read_text(encoding="utf-8")
        self.assertIn('"later_period_refit_count": 0', source)
        self.assertIn('"stage_a_refit_count": 0', source)


if __name__ == "__main__":
    unittest.main()
