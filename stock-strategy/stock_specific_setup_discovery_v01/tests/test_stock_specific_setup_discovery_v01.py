from __future__ import annotations

import copy
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import unittest

from surge_event_study_v01.models import Bar, PreparedStock

from stock_specific_setup_discovery_v01.analysis import (
    classify_final,
    deoverlap_positions,
    evaluate_trade,
    month_cluster_bootstrap,
    select_primary_setup,
    tail_robustness,
)
from stock_specific_setup_discovery_v01.config import CFG
from stock_specific_setup_discovery_v01.main import FROZEN_CODES, _frozen_universe
from stock_specific_setup_discovery_v01.setups import setup_flags


def synthetic_stock(count: int = 100) -> PreparedStock:
    bars = []
    start = date(2020, 1, 1)
    for index in range(count):
        close = 100.0 + index * 0.2
        bars.append(Bar(
            date=(start + timedelta(days=index)).strftime("%Y%m%d"),
            code="9999",
            name="TEST",
            volume=1_000_000 + index,
            open=close - 0.1,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
        ))
    return PreparedStock(
        code="9999",
        name="TEST",
        bars=bars,
        calendar_indices=list(range(count)),
        segment_ids=[0] * count,
        prefix_close=[],
        prefix_volume=[],
        prefix_turnover_proxy=[],
        daily_returns=[],
        prefix_return=[],
        prefix_return_square=[],
        true_range_ratios=[],
        prefix_true_range_ratio=[],
    )


def candidate(setup_id: str, score: float = 0.01) -> dict:
    return {
        "setup_id": setup_id,
        "setup_family": "BREAKOUT",
        "discovery_candidate_pass": True,
        "minimum_annual_day5_net_mean": score,
        "day5_net_pf": 1.20,
        "day5_net_mean": 0.01,
        "deoverlapped_trade_count": 40,
    }


def trade(signal_date: str, value: float) -> dict:
    return {
        "signal_date": signal_date,
        "calendar_month": signal_date[:6],
        "day5_net_return": value,
    }


class StockSpecificSetupDiscoveryTests(unittest.TestCase):
    def test_t_signal_does_not_use_t_plus_1_data(self):
        stock = synthetic_stock()
        expected = setup_flags(stock, 70)
        changed = copy.deepcopy(stock)
        future = changed.bars[71]
        changed.bars[71] = Bar(
            future.date, future.code, future.name, 999_999_999,
            1.0, 10_000.0, 0.1, 9_999.0,
        )
        self.assertEqual(setup_flags(changed, 70), expected)

    def test_entry_is_t_plus_1_open(self):
        stock = synthetic_stock()
        result = evaluate_trade(stock, 70, "20201231", {})
        self.assertIsNotNone(result)
        self.assertEqual(result["entry_date"], stock.bars[71].date)
        self.assertEqual(result["entry_open"], stock.bars[71].open)

    def test_active_trade_deoverlap(self):
        self.assertEqual(deoverlap_positions([10, 11, 14, 15, 16, 20], 5), [10, 15, 20])

    def test_later_data_cannot_change_discovery_selection(self):
        rows = [candidate("A1", 0.02), candidate("A2", 0.01)]
        expected = select_primary_setup(rows)["setup_id"]
        for row in rows:
            row.update(confirmation_net_pf=999, stress_net_mean=-999)
        self.assertEqual(select_primary_setup(rows)["setup_id"], expected)

    def test_frozen_primary_is_not_reselected_from_later_results(self):
        rows = [candidate("A1", 0.02), candidate("A2", 0.01)]
        frozen = select_primary_setup(rows)["setup_id"]
        later_results = {"A1": -1.0, "A2": 100.0}
        self.assertEqual(frozen, "A1")
        self.assertGreater(later_results["A2"], later_results[frozen])

    def test_no_discovery_gate_pass_means_no_winner(self):
        row = candidate("A1")
        row["discovery_candidate_pass"] = False
        self.assertIsNone(select_primary_setup([row]))

    def test_future_path_metrics_do_not_affect_selection(self):
        rows = [candidate("A1", 0.02), candidate("A2", 0.01)]
        expected = select_primary_setup(rows)["setup_id"]
        rows[0].update(mfe10=-999, mae10=999)
        rows[1].update(mfe10=999, mae10=-999)
        self.assertEqual(select_primary_setup(rows)["setup_id"], expected)

    def test_top_five_percent_removal(self):
        trades = [trade(f"202001{index + 1:02d}", float(index)) for index in range(20)]
        result = tail_robustness(trades, 0.05)
        self.assertEqual(result["removed_trade_count"], 1)
        self.assertEqual(result["remaining_trade_count"], 19)
        self.assertAlmostEqual(result["day5_net_mean"], 9.0)

    def test_core_regime_metadata_does_not_affect_selection(self):
        core = [candidate("A1", 0.02), candidate("A2", 0.01)]
        regime = copy.deepcopy(core)
        for row in core:
            row["specialist_type"] = "CORE_SPECIALIST"
        for row in regime:
            row["specialist_type"] = "REGIME_SPECIALIST"
        self.assertEqual(select_primary_setup(core)["setup_id"], select_primary_setup(regime)["setup_id"])

    def test_universe_is_exactly_frozen_fifteen(self):
        snapshot, audit = _frozen_universe()
        self.assertEqual(tuple(row["stock_id"] for row in snapshot), FROZEN_CODES)
        self.assertEqual(audit["stock_count"], 15)

    def test_safety_counts_are_zero(self):
        self.assertEqual(CFG.actual_orders, 0)
        self.assertEqual(CFG.actual_fills, 0)
        self.assertEqual(CFG.broker_connections, 0)
        self.assertEqual(CFG.stage_a_refit_count, 0)

    def test_classification_preserves_regime_dependent_edge(self):
        positive = {"day5_net_mean": 0.01, "day5_net_pf": 1.1}
        negative = {"day5_net_mean": -0.01, "day5_net_pf": 0.9}
        self.assertEqual(
            classify_final(True, False, positive, negative),
            "REGIME_DEPENDENT_STOCK_EDGE",
        )

    def test_bootstrap_and_published_hashes_are_deterministic(self):
        trades = [trade(f"2020{month:02d}{day:02d}", (day - 3) / 100) for month in range(1, 5) for day in range(1, 6)]
        first = month_cluster_bootstrap(trades, "9999", "A1", "LATER")
        second = month_cluster_bootstrap(trades, "9999", "A1", "LATER")
        self.assertEqual(first, second)
        root = Path(__file__).resolve().parents[1]
        manifest_path = root / "run_manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in manifest["input_hashes"]:
                path = root.parent / item["path"]
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])
            for name, expected in manifest["output_hashes"].items():
                self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), expected)


if __name__ == "__main__":
    unittest.main()
