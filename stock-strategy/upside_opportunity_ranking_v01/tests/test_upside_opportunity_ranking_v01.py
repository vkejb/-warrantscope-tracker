from __future__ import annotations

import hashlib
from pathlib import Path
import unittest

import numpy as np

from cross_sectional_alpha_ranking_v01.data import ledger_hashes
from cross_sectional_alpha_ranking_v01.preprocessing import (
    purged_training_mask,
    same_day_cross_sectional_percentiles,
)
from cross_sectional_alpha_ranking_v01.ranking import build_daily_ranks
from multi_setup_study_v01.config import CFG as MULTI_CFG
from multi_setup_study_v01.outcomes import evaluate_unified_outcome
from multi_setup_study_v01.setup_detectors import is_compact_retest
from prospective_shadow_v01.detector import assert_frozen_contract
from surge_event_study_v01.models import Bar, PreparedStock

from upside_opportunity_ranking_v01.config import CFG
from upside_opportunity_ranking_v01.data import load_reused_inputs
from upside_opportunity_ranking_v01.models import fit_ridge_mfe
from upside_opportunity_ranking_v01.ranking import (
    cooldown_proxy,
    same_day_quadrants,
    two_stage_ranks,
)


def stock_fixture() -> PreparedStock:
    closes = [100.0] * 90
    bars = [Bar(f"2020{1 + i // 28:02d}{1 + i % 28:02d}", "2330", "2330", 1_000_000, c, c, c, c) for i, c in enumerate(closes)]
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
            returns.append(0.0)
            tr.append(0.0)
    prefix_return = [0.0]
    prefix_square = [0.0]
    prefix_tr = [0.0]
    for value, tr_value in zip(returns, tr):
        prefix_return.append(prefix_return[-1] + value)
        prefix_square.append(prefix_square[-1] + value * value)
        prefix_tr.append(prefix_tr[-1] + tr_value)
    return PreparedStock("2330", "2330", bars, list(range(90)), [0] * 90, prefix_close, prefix_volume, prefix_turnover, returns, prefix_return, prefix_square, tr, prefix_tr)


class TargetAndFitTests(unittest.TestCase):
    def test_no_future_leakage_in_reused_same_day_transform(self):
        raw = np.asarray([[1.0], [3.0], [2.0], [100.0], [200.0]])
        dates = np.asarray([20200102, 20200102, 20200102, 20200103, 20200103])
        before, _ = same_day_cross_sectional_percentiles(raw, dates)
        raw[3:] = [[-999.0], [9999.0]]
        after, _ = same_day_cross_sectional_percentiles(raw, dates)
        np.testing.assert_equal(before[:3], after[:3])

    def test_mfe10_target_uses_t_plus_1_open_and_day1_to_day10_closes(self):
        stock = stock_fixture()
        signal = 65
        entry = stock.bars[signal + 1]
        stock.bars[signal + 1] = Bar(entry.date, entry.code, entry.name, entry.volume, 102, 200, 1, 103)
        closes = [103, 108, 105, 104, 106, 111, 100, 99, 101, 102]
        for offset, close in enumerate(closes):
            old = stock.bars[signal + 1 + offset]
            stock.bars[signal + 1 + offset] = Bar(old.date, old.code, old.name, old.volume, old.open, max(old.high, close), min(old.low, close), close)
        outcome = evaluate_unified_outcome(stock, signal, MULTI_CFG)
        self.assertAlmostEqual(111 / 102 - 1, outcome["mfe_10d"])
        self.assertNotEqual(200 / 102 - 1, outcome["mfe_10d"])

    def test_discovery_mask_and_target_changes_later_do_not_refit(self):
        rng = np.random.default_rng(8)
        x = rng.uniform(size=(180, 39))
        y = rng.normal(size=180)
        mask = np.zeros(180, dtype=bool)
        mask[:140] = True
        features = tuple(f"f{i}" for i in range(39))
        first = fit_ridge_mfe(x, y, mask, 1.0, features, "DISCOVERY")
        changed = y.copy()
        changed[140:] = 99999
        second = fit_ridge_mfe(x, changed, mask, 1.0, features, "DISCOVERY")
        self.assertEqual(first.fingerprint(), second.fingerprint())

    def test_purged_training_mask_excludes_later_period(self):
        dates = np.asarray([20200101 + i for i in range(15)] + [20230103] * 5)
        mask, audit = purged_training_mask(dates, np.ones(20, dtype=bool), "20200101", "20201231", 10)
        self.assertFalse(np.any(mask[15:]))
        self.assertEqual(5, np.count_nonzero(mask))
        self.assertEqual(10, len(audit["purged_signal_dates"]))


class RankingTests(unittest.TestCase):
    def test_stage_a_ranking_resets_on_each_signal_date(self):
        scores = np.asarray([3.0, 1.0, 2.0, 1.0, 3.0, 2.0])
        dates = np.asarray([20200102] * 3 + [20200103] * 3)
        codes = np.asarray([1, 2, 3, 1, 2, 3])
        ranking, _ = build_daily_ranks(scores, dates, codes)
        np.testing.assert_equal(ranking["rank"], [1, 3, 2, 3, 1, 2])

    def test_stage_a_top30_and_stage_b_top5_within_pool(self):
        dates = np.repeat([20200102, 20200103], 40)
        codes = np.tile(np.arange(1000, 1040), 2)
        stage_a_rank = np.tile(np.arange(1, 41), 2)
        stage_b = np.tile(np.arange(40, 0, -1), 2).astype(float)
        final, audit = two_stage_ranks(stage_a_rank, stage_b, dates, codes, 30)
        self.assertEqual(60, np.count_nonzero(final))
        self.assertTrue(np.all(final[stage_a_rank > 30] == 0))
        self.assertEqual(10, np.count_nonzero((final > 0) & (final <= 5)))
        self.assertEqual(60, audit["pool_rows"])

    def test_stage_b_tie_break_is_stock_code(self):
        dates = np.asarray([20200102] * 4)
        codes = np.asarray([4000, 1000, 3000, 2000])
        final, _ = two_stage_ranks(np.asarray([1, 2, 3, 4]), np.ones(4), dates, codes, 3)
        self.assertEqual(1, final[1])
        self.assertEqual(2, final[2])
        self.assertEqual(3, final[0])

    def test_cooldown_distance_ten_is_allowed(self):
        dates = np.repeat(np.arange(20200101, 20200112), 1)
        selected = np.ones(11, dtype=bool)
        accepted, audit = cooldown_proxy(selected, np.ones(11), dates, np.asarray([2330] * 11), 10)
        self.assertEqual(2, np.count_nonzero(accepted))
        self.assertTrue(accepted[0] and accepted[10])
        self.assertEqual(10, audit["cooldown_sessions"])

    def test_quadrants_are_same_day_median_splits(self):
        dates = np.asarray([20200102] * 4)
        labels = same_day_quadrants(np.asarray([1, 2, 3, 4]), np.asarray([1, 4, 2, 3]), dates)
        np.testing.assert_equal(labels, [4, 3, 2, 1])


class FrozenAndSafetyTests(unittest.TestCase):
    def test_stage_b_published_model_is_exactly_reused(self):
        root = Path(__file__).resolve().parents[2]
        arrays, model, audit = load_reused_inputs(
            root / "winner_coverage_taxonomy_v01/runtime/observation_store.npz",
            root / "cross_sectional_alpha_ranking_v01/runtime/ranking_store.npz",
            root / "cross_sectional_alpha_ranking_v01/model_spec.json",
            root / "cross_sectional_alpha_ranking_v01/run_manifest.json",
        )
        self.assertEqual(CFG.expected_stage_b_fingerprint, model.fingerprint())
        self.assertEqual(0, audit["stage_b_refit_count"])
        self.assertEqual(CFG.expected_mother_rows, len(arrays["meta"]))

    def test_n_compact_definition_and_source_unchanged(self):
        assert_frozen_contract()
        self.assertTrue(is_compact_retest({"pivot_separation_sessions": 7, "bottom_difference": 1e-12}, MULTI_CFG))
        self.assertFalse(is_compact_retest({"pivot_separation_sessions": 8, "bottom_difference": 1e-12}, MULTI_CFG))
        detector = Path(__file__).resolve().parents[2] / "multi_setup_study_v01/setup_detectors.py"
        self.assertEqual(MULTI_CFG.fingerprint(), "9f15e6bdaa2186ac3a84a61064b3b71ce1b3532e9085135feb4aef59d3172b5f")
        self.assertEqual(hashlib.sha256(detector.read_bytes()).hexdigest(), "a025efcd65422e1651eb468b00cc8ebcb4a753b7bf5250df6a2ae5b31b389ec3")

    def test_research_functions_do_not_change_prospective_ledger(self):
        root = Path(__file__).resolve().parents[2]
        before = ledger_hashes(root)
        same_day_quadrants(np.asarray([1.0, 2.0]), np.asarray([2.0, 1.0]), np.asarray([20200102, 20200102]))
        self.assertEqual(before, ledger_hashes(root))

    def test_no_broker_or_order_path(self):
        package = Path(__file__).resolve().parents[1]
        text = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
        self.assertNotIn("submit_order", text)
        self.assertNotIn("place_order", text)
        self.assertNotIn("YuantaProvider", text)
        self.assertIn('"actual_orders": 0', text)
        self.assertIn('"actual_fills": 0', text)
        self.assertIn('"broker_connections": 0', text)


if __name__ == "__main__":
    unittest.main()
