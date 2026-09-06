from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json


FEATURE_FAMILIES: tuple[tuple[str, str], ...] = (
    ("pivot_drawdown_20", "selloff"),
    ("pivot_return_5", "selloff"),
    ("close_vs_sma20", "selloff"),
    ("pivot_atr5_vs_atr20", "volatility"),
    ("signal_tr_vs_prior5", "volatility"),
    ("post_vs_pre_pivot_range", "volatility"),
    ("pivot_volume_ratio_20", "volume"),
    ("signal_volume_ratio_20", "volume"),
    ("post_pivot_volume_vs_pivot", "volume"),
    ("signal_return_1", "demand"),
    ("signal_lower_wick_fraction", "demand"),
    ("rs_5_vs_0050", "demand"),
)


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable, pre-registered constants for REVERSAL_EVENT_STUDY_V0_1."""

    strategy_id: str = "REVERSAL_EVENT_STUDY_V0_1"
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
    discontinuity_lower_ratio: float = 0.89
    discontinuity_upper_ratio: float = 1.11

    # Causal V reversal geometry.
    recent_pivot_window: int = 3
    pivot_drawdown_threshold: float = -0.10
    pivot_return_5_threshold: float = -0.06
    confirmation_min_rebound: float = 0.025
    confirmation_max_rebound: float = 0.08
    confirmation_min_close_location: float = 0.65

    # Causal early N/double-bottom retest geometry.
    n_search_sessions: int = 30
    n_minimum_pivot_separation: int = 6
    n_bottom_tolerance: float = 0.03
    n_minimum_intervening_bounce: float = 0.06

    primary_target: float = 0.08
    primary_stop: float = -0.05
    primary_horizon: int = 10
    causal_cooldown_sessions: int = 10

    feature_families: tuple[tuple[str, str], ...] = FEATURE_FAMILIES
    quintiles: int = 5
    fdr_alpha: float = 0.05
    discovery_minimum_pooled_lift: float = 1.25
    discovery_minimum_yearly_lift: float = 1.05
    discovery_minimum_dates_per_year: int = 100
    discovery_minimum_evaluable_per_year: int = 1_000
    discovery_minimum_positive_per_tail_year: int = 30
    maximum_selected_features_per_pattern: int = 3
    selection_score_threshold: float = 0.75
    daily_selection_count_per_pattern: int = 5

    bootstrap_iterations: int = 5_000
    bootstrap_seed: int = 20_260_906
    validation_ci_alpha: float = 0.025
    validation_minimum_evaluable: int = 500
    validation_minimum_evaluable_per_year: int = 150
    validation_minimum_dates_per_year: int = 100
    validation_minimum_event_rate: float = 0.45
    validation_break_even_event_rate: float = 5.0 / 13.0
    validation_minimum_day10_return: float = 0.003
    validation_maximum_attrition: float = 0.03
    validation_minimum_profit_factor: float = 1.10
    validation_maximum_drawdown: float = 0.25

    research_cash: float = 30_000.0
    maximum_positions: int = 3
    maximum_odd_lot_shares: int = 999
    entry_limit_above_signal: float = 0.03
    maximum_holding_sessions: int = 10
    commission_rate: float = 0.001425 * 0.28
    minimum_commission: int = 1
    sell_tax_rate: float = 0.003
    baseline_slippage_one_way: float = 0.001
    stress_slippage_one_way: float = 0.002
    stress_commission_rate: float = 0.001425

    result_status: str = "PROVISIONAL_CORPORATE_ACTION_UNRESOLVED"
    oos_disclosure: str = (
        "2025 is feature-OOS but aggregate market behavior was inspected in earlier research; "
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
        return result

    @lru_cache(maxsize=None)
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


CFG = Config()
