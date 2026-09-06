from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json


SETUPS: tuple[str, ...] = (
    "V_REVERSAL",
    "N_RETEST",
    "N_COMPACT_RETEST_HYPOTHESIS",
    "MOMENTUM_DIRECTIONAL",
    "TREND_PULLBACK",
    "CONSOLIDATION_BREAKOUT_V2",
)

PERIODS: tuple[tuple[str, str, str], ...] = (
    ("HISTORICAL_DISCOVERY", "20200101", "20221231"),
    ("RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", "20230101", "20241231"),
    ("STRESS_PREVALENCE_SEEN_NOT_BLIND", "20250101", "20251231"),
)

MOMENTUM_FEATURES: tuple[str, ...] = (
    "atr_ratio_14",
    "sma20_slope_5",
    "return_20",
    "return_5",
    "breakout_vs_prior20",
    "close_to_60d_high",
    "volume_ratio_1_20",
    "volume_ratio_5_20",
    "prior_volume_contraction_5_20",
    "range_compression_10",
    "close_location",
    "rs_5_vs_0050",
    "rs_20_vs_0050",
)

MOMENTUM_SCORE_FEATURES: tuple[str, ...] = (
    "atr_ratio_14",
    "sma20_slope_5",
    "return_20",
    "prior_volume_contraction_5_20",
)


def period_label(day: str) -> str:
    normalized = str(day).replace("-", "")
    for label, start, end in PERIODS:
        if start <= normalized <= end:
            return label
    if normalized > "20260906":
        return "PROSPECTIVE_SHADOW_AFTER_2026_09_06"
    return "OUTSIDE_FIXED_HISTORICAL_STUDY"


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable engineering baseline; none of these constants are searched."""

    strategy_id: str = "MULTI_SETUP_STUDY_V0_1"
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
    # Compatibility aliases used by the shared loader; the labels above are
    # the only terms used in this study's reports.
    validation_start: str = "20230101"
    validation_end: str = "20241231"
    feature_oos_start: str = "20250101"
    feature_oos_end: str = "20251231"
    maximum_input_date: str = "20260228"
    prospective_shadow_after: str = "20260906"

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

    # Exact legacy reversal baseline constants.
    recent_pivot_window: int = 3
    pivot_drawdown_threshold: float = -0.10
    pivot_return_5_threshold: float = -0.06
    confirmation_min_rebound: float = 0.025
    confirmation_max_rebound: float = 0.08
    confirmation_min_close_location: float = 0.65
    n_search_sessions: int = 30
    n_minimum_pivot_separation: int = 6
    n_bottom_tolerance: float = 0.03
    n_minimum_intervening_bounce: float = 0.06

    compact_max_separation_sessions: int = 7

    # Frozen Trend Pullback V0.1 baseline.
    pullback_minimum_depth: float = 0.03
    pullback_maximum_depth: float = 0.10
    pullback_recent_high_window: int = 20

    # Frozen Consolidation Breakout V2 engineering baseline.
    consolidation_range_maximum: float = 0.12
    consolidation_ma_spread_maximum: float = 0.04
    consolidation_atr_ratio_maximum: float = 0.80

    # Exact frozen Surge V0.1 score carried forward without rediscovery.
    momentum_features: tuple[str, ...] = MOMENTUM_FEATURES
    momentum_score_features: tuple[str, ...] = MOMENTUM_SCORE_FEATURES
    momentum_daily_selection_count: int = 30
    source_surge_rule_hash: str = (
        "03bf257904806c572f13498b16044eb21f6133e0b5579fb939492c692136aad6"
    )

    commission_rate: float = 0.001425 * 0.28
    minimum_commission: int = 1
    sell_tax_rate: float = 0.003
    slippage_one_way: float = 0.001
    per_trade_notional: float = 30_000.0

    bootstrap_iterations: int = 5_000
    bootstrap_seed: int = 20_260_906
    edge_minimum_evaluable: int = 500
    edge_minimum_signal_date_clusters: int = 100
    edge_minimum_month_clusters: int = 12

    ownership_status: str = "NOT_TESTED_DATA_UNAVAILABLE"
    margin_short_status: str = "AVAILABLE_OFFICIAL_NOT_INGESTED"
    result_status: str = "PROVISIONAL_CORPORATE_ACTION_UNRESOLVED"

    def snapshot(self) -> dict:
        result = asdict(self)
        result["setups"] = list(SETUPS)
        result["periods"] = [
            {"label": label, "start": start, "end": end}
            for label, start, end in PERIODS
        ]
        result["descriptive_targets"] = list(self.descriptive_targets)
        result["momentum_features"] = list(self.momentum_features)
        result["momentum_score_features"] = list(self.momentum_score_features)
        return result

    @lru_cache(maxsize=None)
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


CFG = Config()
