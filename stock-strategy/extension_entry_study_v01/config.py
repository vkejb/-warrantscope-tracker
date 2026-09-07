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

FEATURE_NAMES: tuple[str, ...] = (
    "bias_5",
    "bias_10",
    "bias_20",
    "bias_60",
    "atr_adjusted_bias20",
    "atr_adjusted_bias10",
    "return_3",
    "return_5",
    "return_10",
    "return_20",
    "bias20_change_3d",
    "bias10_change_3d",
    "distance_to_20d_high",
    "distance_to_60d_high",
    "entry_gap",
)

CORE_EXTENSION_FEATURES: tuple[str, ...] = (
    "bias_5",
    "bias_10",
    "bias_20",
    "bias_60",
    "atr_adjusted_bias20",
    "atr_adjusted_bias10",
)

COHORTS: tuple[str, ...] = (
    "ALL_ELIGIBLE",
    "RETURN20_TOP20PCT_SAME_DAY",
    "NEAR_OR_ABOVE_PRIOR20_CLOSE_HIGH_WITHIN_2PCT",
    "MOMENTUM_DIRECTIONAL_FROZEN_CANDIDATE",
    "N_RETEST",
    "N_COMPACT_RETEST_HYPOTHESIS",
)

GAP_BUCKETS: tuple[str, ...] = (
    "< -1%",
    "-1% to 0%",
    "0% to 1%",
    "1% to 2%",
    "2% to 3%",
    ">= 3%",
)


def period_label(day: str) -> str:
    normalized = str(day).replace("-", "")
    for label, start, end in PERIODS:
        if start <= normalized <= end:
            return label
    if normalized >= "20260907":
        return "PROSPECTIVE_AFTER_2026_09_06"
    return "OUTSIDE_FIXED_STUDY"


@dataclass(frozen=True, slots=True)
class Config:
    """Frozen V0.1 research contract; none of these values are searched."""

    study_id: str = "EXTENSION_ENTRY_STUDY_V0_1"
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
    # Shared-loader compatibility aliases.
    validation_start: str = "20230101"
    validation_end: str = "20241231"
    feature_oos_start: str = "20250101"
    feature_oos_end: str = "20251231"
    maximum_input_date: str = "20260228"

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
    commission_rate: float = 0.001425 * 0.28
    minimum_commission: int = 1
    sell_tax_rate: float = 0.003
    slippage_one_way: float = 0.001
    per_trade_notional: float = 30_000.0

    deciles: int = 10
    quintiles: int = 5
    breakout_near_floor: float = -0.02
    momentum_daily_selection_count: int = 30
    causal_cooldown_sessions: int = 10
    bootstrap_iterations: int = 5_000
    bootstrap_seed: int = 20_260_907

    # Frozen shape classifier. Later periods may confirm but never redefine it.
    shape_low_deciles: tuple[int, ...] = (1, 2, 3)
    shape_mid_deciles: tuple[int, ...] = (4, 5, 6, 7)
    shape_high_deciles: tuple[int, ...] = (8, 9, 10)
    monotonic_min_abs_spearman: float = 0.80
    monotonic_min_adjacent_agreement: int = 7
    stable_profile_min_correlation: float = 0.50

    source_surge_rule_hash: str = (
        "03bf257904806c572f13498b16044eb21f6133e0b5579fb939492c692136aad6"
    )
    source_surge_config_hash: str = (
        "1e3661497bcf52e9c5c17fd3a6c3bb0d06e729451000a65d2c3f7e472d75b4fe"
    )
    source_multi_setup_config_hash: str = (
        "9f15e6bdaa2186ac3a84a61064b3b71ce1b3532e9085135feb4aef59d3172b5f"
    )
    source_reversal_config_hash: str = (
        "d0c00ee5b733a37ed0093a764a5f679506b455ff7aaf77d14e952ec4280d7be1"
    )

    result_status: str = "PROVISIONAL_CORPORATE_ACTION_UNRESOLVED"

    def snapshot(self) -> dict:
        result = asdict(self)
        result["periods"] = [
            {"label": label, "start": start, "end": end}
            for label, start, end in PERIODS
        ]
        result["feature_names"] = list(FEATURE_NAMES)
        result["core_extension_features"] = list(CORE_EXTENSION_FEATURES)
        result["cohorts"] = list(COHORTS)
        result["gap_buckets"] = list(GAP_BUCKETS)
        return result

    @lru_cache(maxsize=None)
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


CFG = Config()
