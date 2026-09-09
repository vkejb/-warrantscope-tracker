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
from winner_coverage_taxonomy_v01.features import build_taxonomy_features

from cross_sectional_alpha_ranking_v01.analysis import metric_summary
from cross_sectional_alpha_ranking_v01.config import CFG, FEATURE_NAMES
from cross_sectional_alpha_ranking_v01.data import ledger_hashes
from cross_sectional_alpha_ranking_v01.models import fit_ridge
from cross_sectional_alpha_ranking_v01.preprocessing import (
    purged_training_mask,
    same_day_cross_sectional_percentiles,
    topk_indices,
)
from cross_sectional_alpha_ranking_v01.ranking import cooldown_trade_proxy


def prepared_stock(closes: list[float]) -> PreparedStock:
    bars = [
        Bar(
            f"2020{1 + index // 28:02d}{1 + index % 28:02d}",
            "2330", "2330", 1_000_000,
            close, close * 1.01, close * 0.99, close,
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
        code="2330", name="2330", bars=bars,
        calendar_indices=list(range(len(bars))), segment_ids=[0] * len(bars),
        prefix_close=prefix_close, prefix_volume=prefix_volume,
        prefix_turnover_proxy=prefix_turnover, daily_returns=returns,
        prefix_return=prefix_return, prefix_return_square=prefix_square,
        true_range_ratios=tr, prefix_true_range_ratio=prefix_tr,
    )


class CausalityTests(unittest.TestCase):
    def test_feature_builder_does_not_read_future(self):
        stock = prepared_stock([100 + index * 0.1 for index in range(90)])
        benchmark = PreparedBenchmark(
            [bar.date for bar in stock.bars],
            [bar.close for bar in stock.bars],
            [0] * len(stock.bars),
        )
        before = build_taxonomy_features(stock, 70, benchmark)
        for index in range(71, len(stock.bars)):
            old = stock.bars[index]
            stock.bars[index] = Bar(old.date, old.code, old.name, old.volume, 9999, 9999, 9999, 9999)
        after = build_taxonomy_features(stock, 70, benchmark)
        np.testing.assert_equal(np.asarray(before), np.asarray(after))

    def test_t_plus_1_open_and_close_barrier_outcome(self):
        stock = prepared_stock([100.0] * 90)
        signal_index = 65
        entry = stock.bars[signal_index + 1]
        stock.bars[signal_index + 1] = Bar(entry.date, entry.code, entry.name, entry.volume, 102, 104, 99, 103)
        target = stock.bars[signal_index + 2]
        stock.bars[signal_index + 2] = Bar(target.date, target.code, target.name, target.volume, 103, 112, 103, 111)
        result = evaluate_unified_outcome(stock, signal_index, MULTI_CFG)
        self.assertEqual(102, result["entry_open_proxy"])
        self.assertAlmostEqual(0.02, result["entry_gap"])
        self.assertTrue(result["primary_success"])
        self.assertEqual(2, result["first_target_day"])

    def test_same_day_transform_is_isolated_and_tie_aware(self):
        raw = np.asarray([[1, 10], [3, np.nan], [3, 30], [999, -5]], dtype=float)
        dates = np.asarray([20200102, 20200102, 20200102, 20200103])
        transformed, audit = same_day_cross_sectional_percentiles(raw, dates)
        np.testing.assert_allclose(transformed[:3, 0], [0.0, 0.75, 0.75])
        np.testing.assert_allclose(transformed[:3, 1], [0.0, 0.5, 1.0])
        self.assertEqual(0.5, transformed[3, 0])
        changed = raw.copy()
        changed[3] = [-999, 999]
        transformed_changed, _ = same_day_cross_sectional_percentiles(changed, dates)
        np.testing.assert_equal(transformed[:3], transformed_changed[:3])
        self.assertEqual(0, audit["transformed_missing_cells"])


class FitAndRankingTests(unittest.TestCase):
    def test_fit_target_changes_outside_discovery_do_not_change_model(self):
        rng = np.random.default_rng(4)
        x = rng.uniform(size=(160, len(FEATURE_NAMES)))
        y = rng.normal(size=160)
        mask = np.zeros(160, dtype=bool)
        mask[:120] = True
        first = fit_ridge(x, y, mask, 1.0, "DISCOVERY")
        changed = y.copy()
        changed[120:] = 1_000_000
        second = fit_ridge(x, changed, mask, 1.0, "DISCOVERY")
        self.assertEqual(first.fingerprint(), second.fingerprint())

    def test_training_mask_purges_endpoint_and_excludes_later_period(self):
        dates = np.asarray([20200101 + index for index in range(15)] + [20230101] * 5)
        mask, audit = purged_training_mask(dates, np.ones(20, dtype=bool), "20200101", "20201231", 10)
        self.assertEqual(5, np.count_nonzero(mask))
        self.assertFalse(np.any(mask[15:]))
        self.assertEqual(10, len(audit["purged_signal_dates"]))

    def test_topk_tie_breaks_by_stock_code(self):
        indices = topk_indices(
            np.asarray([1.0, 1.0, 0.5]), np.asarray([2330, 1101, 2603]), 2, largest=True
        )
        np.testing.assert_equal(indices, [1, 0])

    def test_cooldown_uses_market_sessions_and_allows_distance_ten(self):
        dates = np.repeat(np.arange(20200101, 20200112), 2)
        codes = np.tile([1101, 1102], 11)
        ranks = np.tile([1, 2], 11)
        accepted, audit = cooldown_trade_proxy(ranks, dates, codes, 10)
        self.assertEqual(4, np.count_nonzero(accepted))
        self.assertTrue(accepted[0] and accepted[1] and accepted[20] and accepted[21])
        self.assertEqual(10, audit["cooldown_sessions"])

    def test_same_day_universe_base_rate_is_not_global(self):
        meta = np.zeros(4, dtype=[("signal_date", "i4"), ("stock_code", "i4"), ("outcome_evaluable", "?")])
        meta["signal_date"] = [20200102, 20200102, 20200103, 20200103]
        meta["stock_code"] = [1, 2, 1, 2]
        meta["outcome_evaluable"] = True
        outcomes = np.zeros((4, len(OUTCOME_FIELDS)))
        outcomes[:, OUTCOME_FIELDS.index("primary_success")] = [1, 0, 0, 0]
        outcomes[:, OUTCOME_FIELDS.index("gross_return")] = [0.1, -0.1, -0.1, -0.1]
        outcomes[:, OUTCOME_FIELDS.index("net_return")] = [0.09, -0.11, -0.11, -0.11]
        arrays = {"meta": meta, "outcomes": outcomes, "entry_gap": np.zeros(4)}
        selected = np.asarray([True, False, False, False])
        summary = metric_summary(arrays, selected)
        self.assertEqual(0.5, summary["same_day_universe_winner_rate"])
        self.assertEqual(2.0, summary["winner_rate_lift"])


class FrozenContractsAndSafetyTests(unittest.TestCase):
    def test_cost_contract_is_exactly_reused(self):
        self.assertEqual(MULTI_CFG.commission_rate, CFG.commission_rate)
        self.assertEqual(MULTI_CFG.minimum_commission, CFG.minimum_commission)
        self.assertEqual(MULTI_CFG.sell_tax_rate, CFG.sell_tax_rate)
        self.assertEqual(MULTI_CFG.slippage_one_way, CFG.slippage_one_way)
        self.assertEqual(MULTI_CFG.per_trade_notional, CFG.per_trade_notional)

    def test_n_compact_contract_and_source_hash_unchanged(self):
        assert_frozen_contract()
        self.assertTrue(is_compact_retest({"pivot_separation_sessions": 7, "bottom_difference": 1e-12}, MULTI_CFG))
        self.assertFalse(is_compact_retest({"pivot_separation_sessions": 8, "bottom_difference": 1e-12}, MULTI_CFG))
        detector = Path(__file__).resolve().parents[2] / "multi_setup_study_v01" / "setup_detectors.py"
        digest = hashlib.sha256(detector.read_bytes()).hexdigest()
        self.assertEqual(CFG.expected_compact_detector_sha256, digest)

    def test_module_has_no_broker_or_order_execution_path(self):
        package = Path(__file__).resolve().parents[1]
        text = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
        self.assertNotIn("YuantaProvider", text)
        self.assertNotIn("submit_order", text)
        self.assertNotIn("place_order", text)
        self.assertIn('"actual_orders": 0', text)
        self.assertIn('"actual_fills": 0', text)
        self.assertIn('"broker_connections": 0', text)

    def test_pure_research_functions_do_not_change_prospective_ledgers(self):
        stock_strategy = Path(__file__).resolve().parents[2]
        before = ledger_hashes(stock_strategy)
        same_day_cross_sectional_percentiles(
            np.asarray([[1.0], [2.0]]), np.asarray([20200102, 20200102])
        )
        after = ledger_hashes(stock_strategy)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
