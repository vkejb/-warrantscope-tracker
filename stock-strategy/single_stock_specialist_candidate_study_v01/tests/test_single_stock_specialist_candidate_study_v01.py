from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import unittest

from single_stock_specialist_candidate_study_v01.analysis import (
    build_discovery_ranking,
    select_candidate,
)
from single_stock_specialist_candidate_study_v01.config import CFG, CANDIDATES


def _fixtures():
    period_rows, annual_rows = [], []
    for index, (code, name) in enumerate(CANDIDATES, start=1):
        base = {
            "code": code,
            "name": name,
            "sessions": 700,
            "market_sessions": 700,
            "coverage": 0.95 + index * 0.005,
            "median_atr14_pct": float(index),
            "median_abs_return_pct": float(index),
            "median_daily_turnover_proxy": float(index) * 1_000_000,
            "p95_abs_overnight_gap_pct": float(6 - index),
            "plus_8pct_within_10d_close_hit_rate": index / 10,
            "median_mfe10_pct": float(index) * 10,
        }
        period_rows.append({**base, "period": "HISTORICAL_DISCOVERY"})
        period_rows.append({**base, "period": "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS"})
        period_rows.append({**base, "period": "STRESS_PREVALENCE_SEEN_NOT_BLIND"})
        for year, multiplier in zip((2020, 2021, 2022), (0.9, 1.0, 1.1)):
            annual_rows.append({
                **base,
                "period": str(year),
                "median_atr14_pct": base["median_atr14_pct"] * multiplier,
                "median_abs_return_pct": base["median_abs_return_pct"] * multiplier,
                "median_daily_turnover_proxy": base["median_daily_turnover_proxy"] * multiplier,
            })
    return period_rows, annual_rows


class SpecialistCandidateTests(unittest.TestCase):
    def test_later_period_data_cannot_change_discovery_ranking(self):
        periods, annual = _fixtures()
        expected = build_discovery_ranking(periods, annual)
        mutated = copy.deepcopy(periods)
        for row in mutated:
            if row["period"] != "HISTORICAL_DISCOVERY":
                row.update(median_atr14_pct=9999, median_abs_return_pct=9999, median_daily_turnover_proxy=1, p95_abs_overnight_gap_pct=9999)
        self.assertEqual(build_discovery_ranking(mutated, annual), expected)

    def test_rank_one_failure_selects_next_passing_candidate(self):
        ranking = [{"code": "2408", "discovery_rank": 1}, {"code": "2344", "discovery_rank": 2}]
        stability = [
            {"code": "2408", "cross_period_stability_pass": False},
            {"code": "2344", "cross_period_stability_pass": True},
        ]
        self.assertEqual(select_candidate(ranking, stability)["code"], "2344")

    def test_forward_outcomes_do_not_enter_candidate_score(self):
        periods, annual = _fixtures()
        expected = build_discovery_ranking(periods, annual)
        for row in periods:
            row["plus_8pct_within_10d_close_hit_rate"] = 1 - row["plus_8pct_within_10d_close_hit_rate"]
            row["median_mfe10_pct"] = -9999
        self.assertEqual(build_discovery_ranking(periods, annual), expected)
        self.assertTrue(all(row["future_outcomes_used_in_candidate_score"] is False for row in expected))

    def test_all_fail_returns_none(self):
        ranking = [{"code": code, "discovery_rank": rank} for rank, (code, _) in enumerate(CANDIDATES, 1)]
        stability = [{"code": code, "cross_period_stability_pass": False} for code, _ in CANDIDATES]
        self.assertIsNone(select_candidate(ranking, stability))

    def test_candidate_list_is_exact_and_fixed(self):
        self.assertEqual(CFG.candidates, ("2408", "2344", "3231", "3017", "2368"))

    def test_safety_counts_are_zero(self):
        self.assertEqual(CFG.actual_orders, CFG.actual_fills)
        self.assertEqual(CFG.actual_fills, CFG.broker_connections)
        self.assertEqual(CFG.broker_connections, 0)
        self.assertEqual(CFG.model_fit_count, CFG.stage_a_refit_count)
        self.assertEqual(CFG.stage_a_refit_count, 0)

    def test_published_input_and_output_hashes_if_present(self):
        root = Path(__file__).resolve().parents[1]
        manifest_path = root / "run_manifest.json"
        if not manifest_path.exists():
            return
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest["input_hashes"]:
            path = root.parent / item["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])
        for name, expected in manifest["output_hashes"].items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), expected)
        self.assertEqual(len(manifest["manifest_payload_sha256"]), 64)
        self.assertIs(manifest["future_outcomes_used_in_candidate_score"], False)
        self.assertTrue(all(value == 0 for value in manifest["safety"].values()))


if __name__ == "__main__":
    unittest.main()
