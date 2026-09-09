from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json


PERIODS: tuple[tuple[str, str, str], ...] = (
    ("HISTORICAL_DISCOVERY", "20200101", "20221231"),
    ("RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", "20230101", "20241231"),
    ("STRESS_PREVALENCE_SEEN_NOT_BLIND", "20250101", "20251231"),
)

MAJOR_SETUPS: tuple[str, ...] = (
    "N_COMPACT_RETEST_HYPOTHESIS",
    "N_RETEST",
    "V_REVERSAL",
    "MOMENTUM_DIRECTIONAL",
    "TREND_PULLBACK",
    "CONSOLIDATION_BREAKOUT_V2",
)

DESCRIPTIVE_COHORTS: tuple[str, ...] = (
    "NEAR_OR_ABOVE_PRIOR20_CLOSE_HIGH_WITHIN_2PCT",
    "RETURN20_TOP20PCT_SAME_DAY",
)

ALL_FAMILIES = MAJOR_SETUPS + DESCRIPTIVE_COHORTS
FAMILY_BIT = {name: 1 << index for index, name in enumerate(ALL_FAMILIES)}

FEATURE_NAMES: tuple[str, ...] = (
    "signal_close",
    "average_volume_20",
    "return_3",
    "return_5",
    "return_10",
    "return_20",
    "return_60",
    "distance_to_prior20_close_high",
    "distance_to_prior60_close_high",
    "drawdown_from_20d_high",
    "drawdown_from_60d_high",
    "close_vs_ma5",
    "close_vs_ma10",
    "close_vs_ma20",
    "close_vs_ma60",
    "ma5_slope",
    "ma10_slope",
    "ma20_slope",
    "ma60_slope",
    "atr5",
    "atr20",
    "atr60",
    "atr5_atr20",
    "range_compression10",
    "range_compression20",
    "volatility20",
    "volume_ratio5",
    "volume_ratio20",
    "prior_volume_contraction_5_20",
    "rs5_vs_0050",
    "rs20_vs_0050",
    "rs60_vs_0050",
    "recent_local_low_distance",
    "recent_local_high_distance",
    "days_since_20d_high",
    "days_since_60d_high",
    "recent_breakout_flag",
    "recent_retest_flag",
    "bias5",
    "bias10",
    "bias20",
)

# One representative per economic family limits duplicate weighting in clustering.
CLUSTER_FEATURES: tuple[str, ...] = (
    "return_5",
    "return_20",
    "return_60",
    "distance_to_prior20_close_high",
    "drawdown_from_60d_high",
    "close_vs_ma20",
    "ma20_slope",
    "atr5_atr20",
    "range_compression20",
    "volatility20",
    "volume_ratio20",
    "prior_volume_contraction_5_20",
    "rs20_vs_0050",
    "days_since_20d_high",
    "recent_breakout_flag",
    "recent_retest_flag",
)


def period_label(day: str) -> str:
    normalized = str(day).replace("-", "")
    for label, start, end in PERIODS:
        if start <= normalized <= end:
            return label
    if normalized >= "20260907":
        return "PROSPECTIVE_EXCLUDED"
    return "OUTSIDE_STUDY"


@dataclass(frozen=True, slots=True)
class Config:
    study_id: str = "WINNER_COVERAGE_TAXONOMY_V0_1"
    execution_mode: str = "RESEARCH_ONLY_NO_BROKER_NO_ORDER"
    benchmark_symbol: str = "0050"
    timezone: str = "Asia/Taipei"

    warmup_start: str = "20190101"
    discovery_start: str = "20200101"
    discovery_end: str = "20221231"
    retrospective_start: str = "20230101"
    retrospective_end: str = "20241231"
    stress_start: str = "20250101"
    stress_end: str = "20251231"
    validation_start: str = "20230101"
    validation_end: str = "20241231"
    feature_oos_start: str = "20250101"
    feature_oos_end: str = "20251231"
    maximum_input_date: str = "20260130"

    minimum_price: float = 15.0
    maximum_price: float = 500.0
    minimum_average_volume_20: float = 500_000.0
    minimum_average_turnover_proxy_20: float = 50_000_000.0
    feature_lookback_sessions: int = 60
    discontinuity_lower_ratio: float = 0.89
    discontinuity_upper_ratio: float = 1.11

    primary_target: float = 0.08
    primary_stop: float = -0.05
    primary_horizon: int = 10
    descriptive_targets: tuple[float, ...] = (0.10, 0.15)
    causal_cooldown_sessions: int = 10

    commission_rate: float = 0.001425 * 0.28
    minimum_commission: int = 1
    sell_tax_rate: float = 0.003
    slippage_one_way: float = 0.001
    per_trade_notional: float = 30_000.0

    breakout_near_floor: float = -0.02
    momentum_daily_selection_count: int = 30
    control_price_buckets: int = 5
    control_volume_buckets: int = 5
    cluster_k_candidates: tuple[int, ...] = (2, 3, 4, 5, 6)
    cluster_max_iterations: int = 80
    cluster_tolerance: float = 1e-7
    cluster_radius_quantile: float = 0.90
    winsor_lower_quantile: float = 0.01
    winsor_upper_quantile: float = 0.99
    minimum_cluster_share: float = 0.02
    minimum_centroid_separation: float = 0.75
    random_seed: int = 20_260_909

    expected_extension_observation_sha256: str = (
        "1fb1100ddf29dc8be7a64b1351e1f049ec6d0b6de4c029cee4c8943f7e7d0b08"
    )
    expected_extension_store_sha256: str = (
        "d1b16923961701c2ec049892d4d59bd779cfd7a67ecd7d3b6baa57f7bfea83e4"
    )
    expected_mother_year_counts: tuple[tuple[str, int], ...] = (
        ("2020", 96_599),
        ("2021", 128_800),
        ("2022", 104_351),
        ("2023", 119_115),
        ("2024", 139_580),
        ("2025", 115_882),
    )
    expected_multi_setup_config_hash: str = (
        "9f15e6bdaa2186ac3a84a61064b3b71ce1b3532e9085135feb4aef59d3172b5f"
    )
    expected_reversal_config_hash: str = (
        "d0c00ee5b733a37ed0093a764a5f679506b455ff7aaf77d14e952ec4280d7be1"
    )
    expected_compact_detector_sha256: str = (
        "a025efcd65422e1651eb468b00cc8ebcb4a753b7bf5250df6a2ae5b31b389ec3"
    )
    expected_reversal_study_sha256: str = (
        "6dc42e7bd4ae3ce44f87a809cf905df20c106eb8d1044180a142728bb0589691"
    )

    result_status: str = "DESCRIPTIVE_ONLY"

    def snapshot(self) -> dict:
        result = asdict(self)
        result["periods"] = [
            {"label": label, "start": start, "end": end}
            for label, start, end in PERIODS
        ]
        result["major_setups"] = list(MAJOR_SETUPS)
        result["descriptive_cohorts"] = list(DESCRIPTIVE_COHORTS)
        result["feature_names"] = list(FEATURE_NAMES)
        result["cluster_features"] = list(CLUSTER_FEATURES)
        return result

    @lru_cache(maxsize=None)
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


CFG = Config()
