from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Bar:
    date: str
    code: str
    name: str
    volume: int
    open: float
    high: float
    low: float
    close: float


@dataclass(slots=True)
class PreparedStock:
    code: str
    name: str
    bars: list[Bar]
    calendar_indices: list[int]
    segment_ids: list[int]
    prefix_close: list[float]
    prefix_volume: list[float]
    prefix_turnover_proxy: list[float]
    daily_returns: list[float]
    prefix_return: list[float]
    prefix_return_square: list[float]
    true_range_ratios: list[float]
    prefix_true_range_ratio: list[float]


@dataclass(slots=True)
class PreparedBenchmark:
    calendar: list[str]
    normalized_closes: list[float | None]
    segment_ids: list[int | None]


@dataclass(frozen=True, slots=True)
class SignalObservation:
    signal_date: str
    calendar_index: int
    code: str
    name: str
    signal_close: float
    average_volume_20: float
    average_turnover_proxy_20: float
    features: dict[str, float]


@dataclass(frozen=True, slots=True)
class Outcome:
    status: str
    reason: str
    entry_date: str | None = None
    entry_open: float | None = None
    entry_gap: float | None = None
    primary_event: bool | None = None
    clean_primary_event: bool | None = None
    first_primary_hit_day: int | None = None
    close_return_10: float | None = None
    mfe_close_10: float | None = None
    mae_close_10: float | None = None
    high_touch_event_10: bool | None = None
    max_close_returns: tuple[tuple[int, float], ...] = ()
    max_high_returns: tuple[tuple[int, float], ...] = ()

    def max_close_return(self, horizon: int) -> float | None:
        return dict(self.max_close_returns).get(horizon)

    def max_high_return(self, horizon: int) -> float | None:
        return dict(self.max_high_returns).get(horizon)


@dataclass(frozen=True, slots=True)
class RankedSignal:
    observation: SignalObservation
    outcome: Outcome
    score: float
    rank: int
    feature_percentiles: dict[str, float]
    cooldown_included: bool
