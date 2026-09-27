"""Broker-neutral schemas for official Yuanta read-only context data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SecurityContext:
    symbol: str
    market: str
    day_trade_code: str
    credit_percent: int | None = None
    lend_percent: int | None = None
    credit_remnants: int | None = None
    lend_remnants: int | None = None
    lend_sell_mark: str = ""
    lend_qty: int | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    update_date: str = ""


@dataclass(frozen=True, slots=True)
class QuoteContext:
    symbol: str
    market: str
    stock_name: str
    quote_time: datetime
    previous_close: float
    open_reference: float
    limit_up: float
    limit_down: float
    open_price: float
    high_price: float
    low_price: float
    bid: float
    ask: float
    last: float
    total_volume: int
    total_amount: int
    total_out_volume: int
    total_in_volume: int
    order_buy_count: int | None = None
    order_buy_qty: int | None = None
    order_sell_count: int | None = None
    order_sell_qty: int | None = None

    @property
    def spread_bps(self) -> float | None:
        midpoint = (self.bid + self.ask) / 2
        if self.bid <= 0 or self.ask < self.bid or midpoint <= 0:
            return None
        return (self.ask - self.bid) / midpoint * 10_000

    @property
    def cumulative_trade_imbalance(self) -> float | None:
        total = self.total_out_volume + self.total_in_volume
        if total <= 0:
            return None
        return (self.total_out_volume - self.total_in_volume) / total
