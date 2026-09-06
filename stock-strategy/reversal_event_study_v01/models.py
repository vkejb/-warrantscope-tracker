from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PatternObservation:
    signal_date: str
    calendar_index: int
    code: str
    name: str
    pattern: str
    signal_close: float
    average_volume_20: float
    average_turnover_proxy_20: float
    pivot_date: str
    second_pivot_date: str | None
    raw_overlap: bool
    geometry: dict[str, float | int | str]
    features: dict[str, float]


@dataclass(frozen=True, slots=True)
class ReversalOutcome:
    status: str
    reason: str
    entry_date: str | None = None
    entry_open: float | None = None
    entry_gap: float | None = None
    primary_success: bool | None = None
    path_result: str | None = None
    first_target_day: int | None = None
    first_stop_day: int | None = None
    day10_close_return: float | None = None
    mfe_close_10: float | None = None
    mae_close_10: float | None = None
