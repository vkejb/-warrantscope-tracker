from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


PERIODS = (
    ("HISTORICAL_DISCOVERY", "20200101", "20221231", 3),
    ("RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", "20230101", "20241231", 2),
    ("STRESS_PREVALENCE_SEEN_NOT_BLIND", "20250101", "20251231", 1),
)

THRESHOLDS = (
    ("UP5_5D", 0.05, 5),
    ("UP8_10D", 0.08, 10),
    ("UP10_10D", 0.10, 10),
    ("UP15_10D", 0.15, 10),
)

SCORE_WEIGHTS = {
    "up8_episode_component": 0.20,
    "up10_episode_component": 0.20,
    "up15_episode_component": 0.10,
    "p75_mfe10_component": 0.15,
    "median_mfe10_component": 0.10,
    "up8_before_down5_component": 0.10,
    "swing_persistence_component": 0.10,
    "daily_movement_component": 0.05,
}


@dataclass(frozen=True)
class Config:
    study_id: str = "HIGH_UPSIDE_SWING_SPECIALIST_UNIVERSE_V0_1"
    execution_mode: str = "RESEARCH_ONLY_NO_EXECUTION"
    maximum_input_date: str = "20251231"
    annual_minimum_coverage: float = 0.90
    annual_minimum_sessions: int = 180
    minimum_median_turnover_proxy: float = 200_000_000.0
    minimum_median_close: float = 10.0
    maximum_p95_gap_pct: float = 8.0
    maximum_discovery_missing_run_sessions: int = 20
    minimum_median_atr14_pct: float = 2.0
    minimum_median_daily_range_pct: float = 2.5
    atr_window: int = 14
    efficiency_short_window: int = 10
    efficiency_long_window: int = 20
    forward_sessions: int = 10
    annualization_sessions: int = 252
    episode_cooldown_sessions: int = 10
    repeatability_minimum_years: int = 2
    repeatability_minimum_up8_episodes: int = 3
    repeatability_minimum_up10_episodes: int = 2
    tail_p75_mfe10_minimum_retention: float = 0.80
    tail_up10_rate_minimum_retention: float = 0.75
    maximum_primary_pool_size: int = 15
    maximum_reserve_size: int = 15
    maximum_pairwise_correlation: float = 0.80
    persistent_up8_minimum_retention: float = 0.60
    persistent_up10_minimum_retention: float = 0.50
    persistent_mfe10_minimum_retention: float = 0.60
    regime_expansion_retention: float = 1.20
    later_normal_minimum_coverage: float = 0.90
    later_normal_minimum_sessions: int = 180
    later_normal_minimum_turnover_proxy: float = 200_000_000.0
    severe_minimum_coverage: float = 0.80
    severe_minimum_sessions: int = 160
    severe_minimum_turnover_proxy: float = 100_000_000.0
    severe_maximum_p95_gap_pct: float = 12.0
    severe_maximum_gap_multiple: float = 3.0
    severe_maximum_missing_run_sessions: int = 40
    model_fit_count: int = 0
    actual_orders: int = 0
    actual_fills: int = 0
    broker_connections: int = 0
    stage_a_refit_count: int = 0

    def snapshot(self) -> dict:
        payload = asdict(self)
        payload["periods"] = [
            {"label": label, "start": start, "end": end, "calendar_years": years}
            for label, start, end, years in PERIODS
        ]
        payload["thresholds"] = [
            {"threshold_id": key, "return_threshold": threshold, "horizon_sessions": horizon}
            for key, threshold, horizon in THRESHOLDS
        ]
        payload["score_weights"] = SCORE_WEIGHTS
        return payload

    def fingerprint(self) -> str:
        raw = json.dumps(self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()


CFG = Config()
