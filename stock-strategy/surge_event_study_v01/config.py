from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json


FEATURE_FAMILIES: tuple[tuple[str, str], ...] = (
    ("return_5", "momentum"),
    ("return_20", "momentum"),
    ("rs_5_vs_0050", "momentum"),
    ("rs_20_vs_0050", "momentum"),
    ("close_vs_sma20", "trend"),
    ("sma20_slope_5", "trend"),
    ("breakout_vs_prior20", "trend"),
    ("close_to_60d_high", "trend"),
    ("volume_ratio_1_20", "volume"),
    ("volume_ratio_5_20", "volume"),
    ("prior_volume_contraction_5_20", "volume"),
    ("turnover_proxy_ratio_1_20", "volume"),
    ("volatility_20", "volatility_candle"),
    ("range_compression_10", "volatility_candle"),
    ("atr_ratio_14", "volatility_candle"),
    ("close_location", "volatility_candle"),
)


@dataclass(frozen=True, slots=True)
class Config:
    """Pre-registered constants for SURGE_EVENT_STUDY_V0_1.

    These values are intentionally not CLI-tunable. Results must never be used
    to change this version in place.
    """

    strategy_id: str = "SURGE_EVENT_STUDY_V0_1"
    execution_mode: str = "RESEARCH_ONLY_NO_BROKER_NO_ORDER"
    benchmark_symbol: str = "0050"
    timezone: str = "Asia/Taipei"

    warmup_start: str = "20190101"
    discovery_start: str = "20200101"
    discovery_end: str = "20221231"
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
    maximum_forward_sessions: int = 20
    discontinuity_lower_ratio: float = 0.89
    discontinuity_upper_ratio: float = 1.11

    primary_threshold: float = 0.15
    primary_horizon: int = 10
    clean_close_drawdown: float = 0.05
    sensitivity_thresholds: tuple[float, ...] = (0.10, 0.15, 0.20)
    sensitivity_horizons: tuple[int, ...] = (5, 10, 20)

    feature_families: tuple[tuple[str, str], ...] = FEATURE_FAMILIES
    quintiles: int = 5
    fdr_alpha: float = 0.05
    discovery_minimum_pooled_lift: float = 1.25
    discovery_minimum_yearly_lift: float = 1.05
    discovery_minimum_dates_per_year: int = 100
    discovery_minimum_tail_evaluable_per_year: int = 1_000
    maximum_selected_features: int = 4

    daily_selection_count: int = 30
    causal_cooldown_sessions: int = 10
    validation_minimum_hit_lift: float = 1.30
    validation_minimum_yearly_hit_lift: float = 1.15
    validation_minimum_return_increment: float = 0.005
    validation_minimum_coverage: float = 0.80
    validation_minimum_usable_dates_per_year: int = 100
    validation_maximum_attrition_gap: float = 0.02
    bootstrap_iterations: int = 10_000
    bootstrap_seed: int = 20_260_906

    research_cash: float = 30_000.0
    maximum_positions: int = 3
    maximum_odd_lot_shares: int = 999
    entry_limit_above_signal: float = 0.03
    target_close_return: float = 0.15
    stop_close_return: float = -0.05
    maximum_holding_sessions: int = 10
    commission_rate: float = 0.001425 * 0.28
    minimum_commission: int = 1
    sell_tax_rate: float = 0.003
    baseline_slippage_one_way: float = 0.001
    stress_slippage_one_way: float = 0.002
    stress_commission_rate: float = 0.001425

    result_status: str = "PROVISIONAL_CORPORATE_ACTION_UNRESOLVED"
    oos_disclosure: str = (
        "2025 is feature-OOS but aggregate label prevalence was inspected before this run; "
        "it is not a fully blind holdout."
    )

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.feature_families)

    @property
    def family_by_feature(self) -> dict[str, str]:
        return dict(self.feature_families)

    def snapshot(self) -> dict:
        result = asdict(self)
        result["feature_families"] = [
            {"feature": feature, "family": family}
            for feature, family in self.feature_families
        ]
        result["feature_names"] = list(self.feature_names)
        result["sensitivity_thresholds"] = list(self.sensitivity_thresholds)
        result["sensitivity_horizons"] = list(self.sensitivity_horizons)
        return result

    @lru_cache(maxsize=None)
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


CFG = Config()
