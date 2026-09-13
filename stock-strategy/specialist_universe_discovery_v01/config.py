from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


PERIODS = (
    ("HISTORICAL_DISCOVERY", "20200101", "20221231"),
    ("RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", "20230101", "20241231"),
    ("STRESS_PREVALENCE_SEEN_NOT_BLIND", "20250101", "20251231"),
)

CLUSTER_FEATURES = (
    "beta_0050",
    "correlation_0050",
    "upside_capture",
    "downside_capture",
    "median_atr14_pct",
    "median_realized_vol20_annualized_pct",
    "median_efficiency20",
    "lag1_daily_return_autocorrelation",
    "idiosyncratic_volatility_annualized_pct",
    "p95_abs_overnight_gap_pct",
)

SCORE_WEIGHTS = {
    "liquidity_component": 0.25,
    "continuity_component": 0.20,
    "behavior_stability_component": 0.20,
    "tradable_volatility_component": 0.15,
    "gap_safety_component": 0.10,
    "trend_structure_quality_component": 0.10,
}


@dataclass(frozen=True)
class Config:
    study_id: str = "SPECIALIST_UNIVERSE_DISCOVERY_V0_1"
    execution_mode: str = "RESEARCH_ONLY_NO_EXECUTION"
    benchmark_symbol: str = "0050"
    maximum_input_date: str = "20251231"
    discovery_start: str = "20200101"
    discovery_end: str = "20221231"
    annual_minimum_coverage: float = 0.90
    annual_minimum_sessions: int = 180
    minimum_median_turnover_proxy: float = 200_000_000.0
    minimum_median_atr14_pct: float = 1.0
    minimum_median_close: float = 10.0
    maximum_p95_gap_pct: float = 8.0
    maximum_discovery_missing_run_sessions: int = 20
    atr_window: int = 14
    realized_vol_window: int = 20
    efficiency_window: int = 20
    forward_sessions: int = 10
    annualization_sessions: int = 252
    cluster_count: int = 8
    kmeans_seed: int = 20_260_914
    kmeans_max_iterations: int = 300
    kmeans_tolerance: float = 1e-10
    winsor_lower_quantile: float = 0.025
    winsor_upper_quantile: float = 0.975
    tradable_volatility_full_credit_percentile: float = 0.60
    trend_efficiency_full_credit_percentile: float = 0.60
    maximum_representatives_per_cluster: int = 2
    rank2_maximum_primary_correlation: float = 0.75
    final_pool_maximum: int = 15
    pairwise_high_redundancy_threshold: float = 0.80
    defensive_beta_maximum: float = 0.80
    defensive_downside_capture_maximum: float = 0.90
    core_retention_minimum: float = 0.60
    core_retention_maximum: float = 1.80
    core_maximum_gap_multiple: float = 2.00
    core_maximum_beta_absolute_change: float = 0.50
    later_normal_minimum_coverage: float = 0.90
    later_normal_minimum_sessions: int = 180
    later_normal_minimum_turnover_proxy: float = 200_000_000.0
    later_normal_minimum_turnover_retention: float = 0.40
    severe_minimum_coverage: float = 0.80
    severe_minimum_sessions: int = 160
    severe_minimum_turnover_proxy: float = 100_000_000.0
    severe_minimum_turnover_retention: float = 0.25
    severe_maximum_p95_gap_pct: float = 12.0
    severe_maximum_gap_multiple: float = 3.0
    severe_maximum_missing_run_sessions: int = 40
    actual_orders: int = 0
    actual_fills: int = 0
    broker_connections: int = 0
    stage_a_refit_count: int = 0

    def snapshot(self) -> dict:
        payload = asdict(self)
        payload["periods"] = [
            {"label": label, "start": start, "end": end}
            for label, start, end in PERIODS
        ]
        payload["cluster_features"] = list(CLUSTER_FEATURES)
        payload["score_weights"] = SCORE_WEIGHTS
        payload["future_outcomes_used_for_universe_selection"] = False
        payload["industry_used_for_score_cluster_or_selection"] = False
        return payload

    def fingerprint(self) -> str:
        raw = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


CFG = Config()
