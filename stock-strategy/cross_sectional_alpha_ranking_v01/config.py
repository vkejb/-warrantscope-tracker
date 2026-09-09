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
    "return_3",
    "return_5",
    "return_10",
    "return_20",
    "return_60",
    "close_vs_ma5",
    "close_vs_ma10",
    "close_vs_ma20",
    "close_vs_ma60",
    "distance_to_prior20_close_high",
    "distance_to_prior60_close_high",
    "drawdown_from_20d_high",
    "drawdown_from_60d_high",
    "days_since_20d_high",
    "days_since_60d_high",
    "ma5_slope",
    "ma10_slope",
    "ma20_slope",
    "ma60_slope",
    "volume_ratio5",
    "volume_ratio20",
    "prior_volume_contraction_5_20",
    "atr5",
    "atr20",
    "atr60",
    "atr5_atr20",
    "range_compression10",
    "range_compression20",
    "volatility20",
    "rs5_vs_0050",
    "rs20_vs_0050",
    "rs60_vs_0050",
    "recent_breakout_flag",
    "recent_retest_flag",
    "recent_local_low_distance",
    "recent_local_high_distance",
    "bias5",
    "bias10",
    "bias20",
)

TOP_K: tuple[int, ...] = (5, 10, 20, 30)
ALPHA_CANDIDATES: tuple[float, ...] = (0.1, 1.0, 10.0, 100.0)


def period_label(day: int | str) -> str:
    value = str(day).replace("-", "")
    for label, start, end in PERIODS:
        if start <= value <= end:
            return label
    if value >= "20260907":
        return "PROSPECTIVE_EXCLUDED"
    return "OUTSIDE_STUDY"


@dataclass(frozen=True, slots=True)
class Config:
    study_id: str = "CROSS_SECTIONAL_ALPHA_RANKING_V0_1"
    execution_mode: str = "RESEARCH_ONLY_NO_EXECUTION"
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
    warmup_start: str = "20190101"
    maximum_input_date: str = "20260130"
    benchmark_symbol: str = "0050"
    timezone: str = "Asia/Taipei"

    primary_model: str = "LINEAR_RIDGE_NET_RETURN"
    benchmark_model: str = "IC_WEIGHTED_LINEAR_RANK"
    primary_top_k: int = 10
    alpha_candidates: tuple[float, ...] = ALPHA_CANDIDATES
    top_k: tuple[int, ...] = TOP_K
    label_purge_sessions: int = 10
    cooldown_sessions: int = 10
    cross_sectional_missing_value: float = 0.5
    score_deciles: int = 10
    diagnostic_quintiles: int = 5
    bootstrap_iterations: int = 5_000
    bootstrap_seed: int = 20_260_909

    primary_target: float = 0.08
    primary_stop: float = -0.05
    primary_horizon: int = 10
    commission_rate: float = 0.001425 * 0.28
    minimum_commission: int = 1
    sell_tax_rate: float = 0.003
    slippage_one_way: float = 0.001
    per_trade_notional: float = 30_000.0

    minimum_price: float = 15.0
    maximum_price: float = 500.0
    minimum_average_volume_20: float = 500_000.0
    minimum_average_turnover_proxy_20: float = 50_000_000.0
    feature_lookback_sessions: int = 60
    discontinuity_lower_ratio: float = 0.89
    discontinuity_upper_ratio: float = 1.11

    expected_winner_store_sha256: str = (
        "157339658c60fdb3604dc845a3234107f08f27dab1f39204766852ceaa17f5f8"
    )
    expected_extension_store_sha256: str = (
        "d1b16923961701c2ec049892d4d59bd779cfd7a67ecd7d3b6baa57f7bfea83e4"
    )
    expected_mother_rows: int = 704_327
    expected_year_counts: tuple[tuple[str, int], ...] = (
        ("2020", 96_599),
        ("2021", 128_800),
        ("2022", 104_351),
        ("2023", 119_115),
        ("2024", 139_580),
        ("2025", 115_882),
    )
    expected_compact_detector_sha256: str = (
        "a025efcd65422e1651eb468b00cc8ebcb4a753b7bf5250df6a2ae5b31b389ec3"
    )
    expected_multi_setup_config_hash: str = (
        "9f15e6bdaa2186ac3a84a61064b3b71ce1b3532e9085135feb4aef59d3172b5f"
    )

    def snapshot(self) -> dict:
        payload = asdict(self)
        payload["periods"] = [
            {"label": label, "start": start, "end": end}
            for label, start, end in PERIODS
        ]
        payload["feature_names"] = list(FEATURE_NAMES)
        return payload

    @lru_cache(maxsize=None)
    def fingerprint(self) -> str:
        raw = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


CFG = Config()
