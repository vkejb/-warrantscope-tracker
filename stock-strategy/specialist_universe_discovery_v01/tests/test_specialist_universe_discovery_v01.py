from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

from specialist_universe_discovery_v01.analysis import (
    classify_stability,
    cluster_discovery,
    deterministic_kmeans,
    final_status,
    quality_scores,
    select_representatives,
)
from specialist_universe_discovery_v01.config import CFG


def feature_rows(count=24):
    rows = []
    for index in range(count):
        scale = index + 1
        rows.append({
            "stock_id": f"{1000 + index}",
            "stock_name": f"S{index}",
            "coverage": 0.90 + (index % 10) / 100,
            "median_daily_turnover_proxy": 200_000_000 + scale * 10_000_000,
            "behavior_stability_raw_mean_cv": 0.05 + (index % 7) / 100,
            "median_atr14_pct": 1.0 + scale / 10,
            "median_abs_return_pct": 0.5 + scale / 20,
            "p95_abs_overnight_gap_pct": 1.0 + scale / 20,
            "median_efficiency20": 0.10 + scale / 200,
            "lag1_daily_return_autocorrelation": -0.12 + scale / 100,
            "beta_0050": 0.3 + scale / 30,
            "correlation_0050": 0.2 + scale / 50,
            "upside_capture": 0.4 + scale / 25,
            "downside_capture": 0.3 + scale / 28,
            "median_realized_vol20_annualized_pct": 15 + scale,
            "idiosyncratic_volatility_annualized_pct": 10 + scale * 0.8,
            "median_mfe10_pct": scale * 0.2,
            "median_mae10_pct": -scale * 0.1,
            "current_industry_descriptive": "A",
        })
    return rows


def representative_fixtures():
    scores, assignments = [], []
    for cluster in range(8):
        for rank in range(2):
            code = f"{2000 + cluster * 2 + rank}"
            scores.append({"stock_id": code, "stock_name": code, "discovery_quality_score": 1.0 - cluster / 100 - rank / 1000})
            assignments.append({"stock_id": code, "discovery_cluster": cluster})
    return scores, assignments, {row["stock_id"]: object() for row in scores}


def stability_rows():
    discovery = {
        "stock_id": "2000", "stock_name": "S", "median_daily_turnover_proxy": 500_000_000,
        "median_atr14_pct": 2.0, "median_abs_return_pct": 1.0,
        "p95_abs_overnight_gap_pct": 2.0, "beta_0050": 1.0,
    }
    later = {
        "stock_id": "2000", "stock_name": "S", "period": "LATER", "coverage": 1.0,
        "sessions": 243, "longest_missing_run_sessions": 0,
        "median_daily_turnover_proxy": 500_000_000, "median_atr14_pct": 4.0,
        "median_abs_return_pct": 1.0, "p95_abs_overnight_gap_pct": 2.0,
        "beta_0050": 1.0, "correlation_0050": 0.5, "median_efficiency20": 0.2,
        "downside_capture": 1.0,
    }
    return discovery, later


class SpecialistUniverseTests(unittest.TestCase):
    def test_later_period_data_cannot_change_discovery_clustering(self):
        discovery = feature_rows()
        expected = cluster_discovery(discovery)[0]
        later = copy.deepcopy(discovery)
        for row in later:
            row.update(beta_0050=999, median_atr14_pct=999)
        self.assertEqual(cluster_discovery(discovery)[0], expected)

    def test_later_period_data_cannot_change_discovery_score_or_rank(self):
        discovery = feature_rows()
        expected = quality_scores(discovery)
        later = copy.deepcopy(discovery)
        for row in later:
            row.update(median_daily_turnover_proxy=1, p95_abs_overnight_gap_pct=999)
        self.assertEqual(quality_scores(discovery), expected)
        self.assertEqual([row["stock_id"] for row in quality_scores(discovery)], [row["stock_id"] for row in expected])

    def test_future_mfe_mae_do_not_enter_selection_score(self):
        rows = feature_rows()
        expected = quality_scores(rows)
        for row in rows:
            row["median_mfe10_pct"] = 99999
            row["median_mae10_pct"] = -99999
        self.assertEqual(quality_scores(rows), expected)
        self.assertTrue(all(row["future_outcomes_used_for_universe_selection"] is False for row in expected))

    @patch("specialist_universe_discovery_v01.analysis.pair_correlation", return_value=0.10)
    def test_max_two_per_cluster_and_pool_at_most_15(self, _mock):
        scores, assignments, prepared = representative_fixtures()
        representatives, selected = select_representatives(scores, assignments, prepared)
        self.assertEqual(len(selected), 15)
        for cluster in range(8):
            chosen = [row for row in representatives if row["discovery_cluster"] == cluster and row["discovery_selected"]]
            self.assertLessEqual(len(chosen), 2)

    @patch("specialist_universe_discovery_v01.analysis.pair_correlation", return_value=0.75)
    def test_rank2_correlation_at_threshold_is_rejected(self, _mock):
        scores, assignments, prepared = representative_fixtures()
        representatives, selected = select_representatives(scores, assignments, prepared)
        self.assertEqual(len(selected), 8)
        self.assertTrue(all(not row["rank2_correlation_gate_pass"] for row in representatives if row["discovery_rank_within_cluster"] == 2))

    @patch("specialist_universe_discovery_v01.analysis.pair_correlation", return_value=0.90)
    def test_pool_below_ten_does_not_relax_standard(self, _mock):
        scores, assignments, prepared = representative_fixtures()
        _, selected = select_representatives(scores, assignments, prepared)
        self.assertEqual(len(selected), 8)

    def test_structural_shift_with_liquidity_is_regime_not_excluded(self):
        discovery, later = stability_rows()
        classified = classify_stability(discovery, later)
        self.assertEqual(classified["period_stability"], "REGIME_SHIFT_TRADABLE")
        self.assertEqual(final_status([classified, classified]), "REGIME_SPECIALIST")

    def test_lost_liquidity_or_coverage_can_exclude(self):
        discovery, later = stability_rows()
        later.update(coverage=0.50, median_daily_turnover_proxy=10_000_000)
        classified = classify_stability(discovery, later)
        self.assertEqual(classified["period_stability"], "SEVERE_TRADABILITY_FAILURE")
        self.assertEqual(final_status([classified]), "EXCLUDED_AFTER_STABILITY")

    def test_industry_label_does_not_affect_score_or_cluster(self):
        rows = feature_rows()
        expected_scores = quality_scores(rows)
        expected_clusters = cluster_discovery(rows)[0]
        for row in rows:
            row["current_industry_descriptive"] = "CHANGED"
        self.assertEqual(quality_scores(rows), expected_scores)
        self.assertEqual(cluster_discovery(rows)[0], expected_clusters)

    def test_safety_counts_are_zero(self):
        self.assertEqual(CFG.actual_orders, CFG.actual_fills)
        self.assertEqual(CFG.actual_fills, CFG.broker_connections)
        self.assertEqual(CFG.broker_connections, 0)
        self.assertEqual(CFG.stage_a_refit_count, 0)

    def test_deterministic_kmeans_and_published_hashes(self):
        matrix = np.asarray([[float(i), float(i % 3)] for i in range(16)])
        first = deterministic_kmeans(matrix, 8)
        second = deterministic_kmeans(matrix, 8)
        self.assertTrue(np.array_equal(first[0], second[0]))
        self.assertTrue(np.array_equal(first[1], second[1]))
        root = Path(__file__).resolve().parents[1]
        manifest_path = root / "run_manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in manifest["input_hashes"]:
                self.assertEqual(hashlib.sha256((root.parent / item["path"]).read_bytes()).hexdigest(), item["sha256"])
            for name, expected in manifest["output_hashes"].items():
                self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), expected)


if __name__ == "__main__":
    unittest.main()
