from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class DailyBar:
    date: date
    symbol: str
    name: str
    market: str
    security_type: str
    trading_status: str
    close: float
    volume: int
    turnover: float


@dataclass(frozen=True, slots=True)
class TradingSession:
    date: date
    market: str


@dataclass(frozen=True, slots=True)
class IndexDailyBar:
    date: date
    market: str
    index_id: str
    close: float


@dataclass(frozen=True, slots=True)
class MinuteBar:
    bar_end: datetime
    symbol: str
    market: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover: float
    limit_up: float

    @property
    def date(self) -> date:
        return self.bar_end.date()


@dataclass(frozen=True, slots=True)
class IndexMinuteBar:
    bar_end: datetime
    market: str
    index_id: str
    close: float

    @property
    def date(self) -> date:
        return self.bar_end.date()


@dataclass(frozen=True, slots=True)
class UniverseMember:
    trade_date: date
    symbol: str
    name: str
    market: str
    previous_close: float
    previous_index_close: float
    sma20: float
    sma60: float
    median_volume_20d: float
    median_turnover_20d: float
    rs5: float
    rs20: float
    close_to_60d_high: float
    universe_rank: int = 0

    def with_rank(self, rank: int) -> "UniverseMember":
        return replace(self, universe_rank=rank)


@dataclass(frozen=True, slots=True)
class SignalEvaluation:
    strategy_id: str
    config_hash: str
    trade_date: date
    symbol: str
    market: str
    bar_end: datetime | None
    or_high: float | None
    or_low: float | None
    signal_price: float | None
    vwap: float | None
    vwap_extension: float | None
    breakout_extension: float | None
    rvol: float | None
    stock_return: float | None
    index_return: float | None
    intraday_relative_strength: float | None
    two_closes_above_or: bool
    at_least_one_tick_above_or: bool
    above_vwap: bool
    vwap_extension_ok: bool
    breakout_extension_ok: bool
    rvol_ok: bool
    stock_return_ok: bool
    relative_strength_ok: bool
    current_bar_has_volume: bool
    limit_up_buffer_ok: bool
    passed: bool
    rejection_reason: str

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["trade_date"] = self.trade_date.isoformat()
        row["bar_end"] = self.bar_end.isoformat(sep=" ") if self.bar_end else ""
        return row


@dataclass(frozen=True, slots=True)
class Signal:
    strategy_id: str
    config_hash: str
    trade_date: date
    symbol: str
    name: str
    market: str
    signal_time: datetime
    signal_price: float
    or_high: float
    or_low: float
    vwap: float
    rvol: float
    stock_return: float
    index_return: float
    intraday_relative_strength: float
    median_turnover_20d: float
    selected: bool = False
    selection_reason: str = "RAW_SIGNAL"

    def selected_for_day(self) -> "Signal":
        return replace(self, selected=True, selection_reason="EARLIEST_MINUTE_TOP_RANKED")

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["trade_date"] = self.trade_date.isoformat()
        row["signal_time"] = self.signal_time.isoformat(sep=" ")
        return row


@dataclass(frozen=True, slots=True)
class OddLotQuote:
    exchange_time: datetime
    received_time: datetime
    symbol: str
    market: str
    bid1: float
    ask1: float
    ask1_quantity: int
    ask2_quantity: int
    regular_last: float
    limit_up: float
    feed_state: str = "HEALTHY"
    market_status: str = "NORMAL"


@dataclass(frozen=True, slots=True)
class ShadowDecision:
    strategy_id: str
    config_hash: str
    trade_date: date
    symbol: str
    signal_time: datetime
    quote_time: datetime | None
    target_quantity: int
    reference_limit_price: float | None
    spread_pct: float | None
    ask_premium_pct: float | None
    ask_depth_2: int | None
    quote_age_seconds: float | None
    status: str
    is_actual_order: bool
    is_actual_fill: bool
    rejection_reason: str

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["trade_date"] = self.trade_date.isoformat()
        row["signal_time"] = self.signal_time.isoformat(sep=" ")
        row["quote_time"] = self.quote_time.isoformat(sep=" ") if self.quote_time else ""
        return row
