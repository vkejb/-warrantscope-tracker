from __future__ import annotations

import copy
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch

from surge_event_study_v01.models import Bar, PreparedStock

from high_upside_swing_specialist_universe_v01.analysis import (
    deoverlap_episodes,
    eligibility,
    final_classification,
    greedy_select,
    repeatability,
    score_discovery,
)
from high_upside_swing_specialist_universe_v01.config import CFG


def business_dates(start: date, end: date) -> list[str]:
    dates = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            dates.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return dates


def synthetic_stock() -> tuple[dict[str, list[Bar]], PreparedStock, list[str]]:
    dates = business_dates(date(2019, 1, 1), date(2025, 12, 31))
    bars = []
    for index, day in enumerate(dates):
        close = 50.0 + index * 0.03 + 2.0 * math.sin(index / 5)
        bars.append(Bar(day, "9999", "TEST", 10_000_000, close - 0.2, close + 1.0, close - 1.0, close))
    returns = [0.0] + [bars[index].close / bars[index - 1].close - 1.0 for index in range(1, len(bars))]
    tr = [0.0] + [max(
        bars[index].high - bars[index].low,
        abs(bars[index].high - bars[index - 1].close),
        abs(bars[index].low - bars[index - 1].close),
    ) / bars[index - 1].close for index in range(1, len(bars))]
    stock = PreparedStock(
        code="9999", name="TEST", bars=bars, calendar_indices=list(range(len(bars))),
        segment_ids=[0] * len(bars), prefix_close=[], prefix_volume=[], prefix_turnover_proxy=[],
        daily_returns=returns, prefix_return=[], prefix_return_square=[], true_range_ratios=tr,
        prefix_true_range_ratio=[],
    )
    return {"9999": bars}, stock, dates


def score_row(code: str, opportunity: float, movement: float) -> dict:
    return {
        "stock_id": code, "stock_name": code,
        "up8_episodes_per_252_sessions": opportunity,
        "up10_episodes_per_252_sessions": opportunity,
        "up15_episodes_per_252_sessions": opportunity,
        "p75_mfe10": opportunity,
        "median_mfe10": opportunity,
        "up8_before_down5_rate": opportunity,
        "swing_persistence": opportunity,
        "median_atr14_pct": movement,
        "median_daily_range_pct": movement,
        "swing_repeatability_pass": True,
        "repeatability_status": "PASS",
    }


def outcome(index: int, hit: bool = True) -> dict:
    return {
        "calendar_index": index, "position": index, "signal_date": f"202001{index + 1:02d}",
        "entry_date": f"202001{index + 2:02d}", "mfe10": 0.12, "mae10": -0.01,
        "up5_5d": hit, "up8_10d": hit, "up10_10d": hit, "up15_10d": False,
        "first_up5_day": 2 if hit else None, "first_up8_day": 3 if hit else None,
        "first_up10_day": 4 if hit else None, "first_up15_day": None,
    }


class HighUpsideSwingTests(unittest.TestCase):
    def test_eligibility_reads_discovery_only(self):
        raw, stock, calendar = synthetic_stock()
        expected = eligibility(raw, [stock], calendar)[0]
        changed = copy.deepcopy(stock)
        for index, bar in enumerate(changed.bars):
            if bar.date.startswith("2025"):
                changed.bars[index] = Bar(bar.date, bar.code, bar.name, 1, 1, 1000, 0.1, 500)
        self.assertEqual(eligibility(raw, [changed], calendar)[0], expected)

    def test_swing_score_ignores_later_fields(self):
        rows = [score_row("1001", 1, 100), score_row("1002", 2, 1)]
        expected = score_discovery(copy.deepcopy(rows))
        for row in rows:
            row.update(confirmation_up8=999, stress_mfe=-999)
        actual = score_discovery(rows)
        fields = ("stock_id", "swing_score", *CFG.snapshot()["score_weights"].keys())
        self.assertEqual(
            [{field: row[field] for field in fields} for row in actual],
            [{field: row[field] for field in fields} for row in expected],
        )

    def test_later_period_cannot_change_discovery_rank(self):
        rows = [score_row("1001", 1, 1), score_row("1002", 2, 2)]
        first = [row["stock_id"] for row in score_discovery(rows)]
        rows[0]["later_result"] = 1_000_000
        rows[1]["later_result"] = -1_000_000
        self.assertEqual([row["stock_id"] for row in score_discovery(rows)], first)

    @patch("high_upside_swing_specialist_universe_v01.analysis.pair_correlation", return_value=0.5)
    def test_later_period_cannot_change_selected_pool(self, _mock):
        prepared = {str(i): object() for i in range(20)}
        rankings = [{**score_row(str(i), 20 - i, i), "swing_score": 20 - i, "discovery_rank": i + 1} for i in range(20)]
        first = [row["stock_id"] for row in greedy_select(rankings, prepared)[0]]
        for row in rankings:
            row["stress_score"] = 999 if row["stock_id"] not in first else -999
        self.assertEqual([row["stock_id"] for row in greedy_select(rankings, prepared)[0]], first)

    def test_daily_volatility_is_not_only_objective(self):
        rows = [score_row("NOISE", 1, 100), score_row("SWING", 100, 1)]
        scored = score_discovery(rows)
        self.assertEqual(scored[0]["stock_id"], "SWING")

    def test_mfe_and_episode_frequency_are_primary_components(self):
        low, high = score_row("LOW", 1, 1), score_row("HIGH", 2, 1)
        result = {row["stock_id"]: row for row in score_discovery([low, high])}
        self.assertGreater(result["HIGH"]["up8_episode_component"], result["LOW"]["up8_episode_component"])
        self.assertGreater(result["HIGH"]["p75_mfe10_component"], result["LOW"]["p75_mfe10_component"])

    def test_one_wave_is_not_ten_episodes(self):
        outcomes = [outcome(index) for index in range(10)]
        _, metrics = deoverlap_episodes(outcomes, "1", "S", "D", 100, 1)
        self.assertEqual(metrics["up10_deoverlapped_episode_count"], 1)

    def test_cooldown_allows_t_plus_11(self):
        outcomes = [outcome(0), outcome(10), outcome(11)]
        _, metrics = deoverlap_episodes(outcomes, "1", "S", "D", 100, 1)
        self.assertEqual(metrics["up10_deoverlapped_episode_count"], 2)

    def test_one_off_swing_stock_gate(self):
        rows = []
        for year, up8, up10 in ((2020, 4, 3), (2021, 1, 1), (2022, 1, 1)):
            rows.append({"stock_id": "1", "period": str(year), "up8_deoverlapped_episode_count": up8, "up10_deoverlapped_episode_count": up10})
        self.assertEqual(repeatability(rows)["1"]["repeatability_status"], "ONE_OFF_SWING_STOCK")

    @patch("high_upside_swing_specialist_universe_v01.analysis.pair_correlation", return_value=0.81)
    def test_high_correlation_enters_reserve(self, _mock):
        rankings = [{**score_row("1", 2, 2), "swing_score": 2, "discovery_rank": 1}, {**score_row("2", 1, 1), "swing_score": 1, "discovery_rank": 2}]
        selected, high_corr, _ = greedy_select(rankings, {"1": object(), "2": object()})
        self.assertEqual([row["stock_id"] for row in selected], ["1"])
        self.assertEqual([row["stock_id"] for row in high_corr], ["2"])

    def test_cluster_does_not_force_low_opportunity_selection(self):
        rows = [score_row("HIGH", 10, 1), score_row("LOW", 1, 10)]
        rows[1]["cluster"] = "LOW_VOL_CLUSTER"
        self.assertEqual(score_discovery(rows)[0]["stock_id"], "HIGH")

    @patch("high_upside_swing_specialist_universe_v01.analysis.pair_correlation", return_value=0.0)
    def test_final_pool_at_most_fifteen(self, _mock):
        rankings = [{**score_row(str(i), i, i), "swing_score": i, "discovery_rank": i} for i in range(30)]
        selected, _, _ = greedy_select(rankings, {str(i): object() for i in range(30)})
        self.assertEqual(len(selected), 15)

    @patch("high_upside_swing_specialist_universe_v01.analysis.pair_correlation", return_value=0.0)
    def test_pool_below_fifteen_does_not_relax(self, _mock):
        rankings = [{**score_row(str(i), i, i), "swing_score": i, "discovery_rank": i} for i in range(4)]
        selected, _, _ = greedy_select(rankings, {str(i): object() for i in range(4)})
        self.assertEqual(len(selected), 4)

    def test_forward_outcomes_only_discovery_can_rank(self):
        rows = [score_row("1", 1, 1), score_row("2", 2, 2)]
        expected = score_discovery(rows)[0]["stock_id"]
        rows[0]["later_forward_mfe"] = 999
        self.assertEqual(score_discovery(rows)[0]["stock_id"], expected)

    def test_later_period_is_classification_only(self):
        maintained = {"severe_tradability_gate_pass": True, "persistent_character_gate_pass": True, "up8_episode_retention": 1.0, "up10_episode_retention": 1.0, "median_mfe10_retention": 1.0}
        shifted = {"severe_tradability_gate_pass": True, "persistent_character_gate_pass": False, "up8_episode_retention": 0.3, "up10_episode_retention": 0.3, "median_mfe10_retention": 0.7}
        self.assertEqual(final_classification([maintained, shifted]), "REGIME_HIGH_UPSIDE_SPECIALIST")

    def test_lost_tradability_is_separate(self):
        lost = {"severe_tradability_gate_pass": False, "persistent_character_gate_pass": False, "up8_episode_retention": 1.0, "up10_episode_retention": 1.0, "median_mfe10_retention": 1.0}
        self.assertEqual(final_classification([lost, lost]), "LOST_TRADABILITY")

    def test_safety_counts_are_zero(self):
        self.assertEqual(CFG.actual_orders, 0)
        self.assertEqual(CFG.actual_fills, 0)
        self.assertEqual(CFG.broker_connections, 0)
        self.assertEqual(CFG.model_fit_count, 0)
        self.assertEqual(CFG.stage_a_refit_count, 0)

    def test_outputs_are_deterministic_when_published(self):
        root = Path(__file__).resolve().parents[1]
        manifest_path = root / "run_manifest.json"
        if not manifest_path.exists():
            return
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name, expected in manifest["output_hashes"].items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), expected)
        for item in manifest["input_hashes"]:
            self.assertEqual(hashlib.sha256((root.parent / item["path"]).read_bytes()).hexdigest(), item["sha256"])


if __name__ == "__main__":
    unittest.main()
