from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json


@dataclass(frozen=True, slots=True)
class Config:
    """Frozen, pre-registered constants for INTRADAY_ORB_V0_1.

    These values are research starting points. They have not been optimized and
    must not be changed in-place after results are observed. A change requires a
    new strategy version.
    """

    strategy_id: str = "INTRADAY_ORB_V0_1"
    timezone: str = "Asia/Taipei"
    benchmark_index_ids: tuple[tuple[str, str], ...] = (
        ("TWSE", "TAIEX"),
        ("TPEX", "TPEX"),
    )

    # T-1 universe.
    minimum_price: float = 15.0
    minimum_median_turnover_20d: float = 100_000_000.0
    minimum_median_volume_20d: int = 1_000_000
    near_60d_high_ratio: float = 0.95
    universe_size: int = 30
    daily_history_sessions: int = 60
    rvol_history_sessions: int = 20

    # Completed one-minute bars. bar_end=09:01 represents [09:00, 09:01).
    opening_first_bar_end: str = "09:01"
    opening_last_bar_end: str = "09:15"
    first_signal_bar_end: str = "09:17"
    last_signal_bar_end: str = "11:00"
    confirmation_bars: int = 2

    minimum_rvol: float = 1.8
    minimum_intraday_relative_strength: float = 0.01
    minimum_intraday_return: float = 0.01
    maximum_intraday_return: float = 0.06
    maximum_vwap_extension: float = 0.02
    maximum_breakout_extension: float = 0.01
    limit_up_buffer_ticks: int = 2

    # Odd-lot executable-quote gates. No actual fill is inferred from a quote.
    maximum_odd_lot_spread: float = 0.005
    maximum_odd_lot_ask_premium: float = 0.003
    minimum_ask_depth_multiple: float = 2.0
    maximum_limit_above_signal: float = 0.003
    quote_freshness_seconds: float = 3.0
    shadow_intent_lifetime_seconds: int = 60

    research_cash: float = 30_000.0
    maximum_cash_fraction: float = 0.95
    maximum_odd_lot_shares: int = 999
    forward_minutes: tuple[int, ...] = (5, 15, 30, 60)
    total_friction_scenarios: tuple[float, ...] = (
        0.0,
        0.002,
        0.003,
        0.004,
        0.005,
        0.006,
        0.008,
        0.010,
    )

    # This package intentionally has no live-order mode.
    execution_mode: str = "SHADOW_ONLY_NOT_SUBMITTED"

    def snapshot(self) -> dict:
        result = asdict(self)
        result["benchmark_index_ids"] = [
            {"market": market, "index_id": index_id}
            for market, index_id in self.benchmark_index_ids
        ]
        result["forward_minutes"] = list(self.forward_minutes)
        result["total_friction_scenarios"] = list(self.total_friction_scenarios)
        return result

    @lru_cache(maxsize=None)
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


CFG = Config()
