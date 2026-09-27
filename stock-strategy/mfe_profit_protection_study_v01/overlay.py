"""Stateful, side-symmetric MFE exit overlay for research replays only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import os
from typing import Callable, Iterable


ENABLE_MFE_PROFIT_PROTECTION = (
    os.environ.get("ENABLE_MFE_PROFIT_PROTECTION", "false").strip().lower()
    in {"1", "true", "yes", "on"}
)


@dataclass(frozen=True, slots=True)
class EntryFill:
    at: datetime
    price: float
    quantity: int

    def __post_init__(self) -> None:
        if not math.isfinite(self.price) or self.price <= 0 or self.quantity <= 0:
            raise ValueError("entry fills require a positive finite price and quantity")


@dataclass(frozen=True, slots=True)
class PositionBasis:
    side: str
    entry_time: datetime
    entry_price: float
    quantity: int
    initial_stop_price: float
    initial_risk_per_share: float
    initial_risk_R: float = 1.0

    @classmethod
    def from_fills(
        cls,
        side: str,
        fills: Iterable[EntryFill],
        *,
        initial_stop_price: float,
    ) -> "PositionBasis":
        rows = tuple(fills)
        side = side.upper()
        if side not in {"LONG", "SHORT"} or not rows:
            raise ValueError("side must be LONG or SHORT and at least one fill is required")
        quantity = sum(row.quantity for row in rows)
        entry_price = sum(row.price * row.quantity for row in rows) / quantity
        risk = (
            entry_price - initial_stop_price
            if side == "LONG"
            else initial_stop_price - entry_price
        )
        if (
            not math.isfinite(initial_stop_price)
            or initial_stop_price <= 0
            or not math.isfinite(risk)
            or risk <= 0
        ):
            raise ValueError("initial stop must define a strictly positive R")
        return cls(
            side=side,
            entry_time=min(row.at for row in rows),
            entry_price=entry_price,
            quantity=quantity,
            initial_stop_price=float(initial_stop_price),
            initial_risk_per_share=float(risk),
        )

    @property
    def initial_risk_pnl(self) -> float:
        return self.initial_risk_per_share * self.quantity

    def favorable_move(self, price: float) -> float:
        return price - self.entry_price if self.side == "LONG" else self.entry_price - price

    def favorable_r(self, price: float) -> float:
        return self.favorable_move(price) / self.initial_risk_per_share

    def price_for_r(self, value: float) -> float:
        move = value * self.initial_risk_per_share
        return self.entry_price + move if self.side == "LONG" else self.entry_price - move

    def floor_breached(self, price: float, locked_profit_r: float) -> bool:
        return self.favorable_r(price) <= locked_profit_r


@dataclass(frozen=True, slots=True)
class OverlayVariant:
    name: str
    retain_1_5: float
    retain_2_0: float
    retain_3_0: float

    def __post_init__(self) -> None:
        rates = (self.retain_1_5, self.retain_2_0, self.retain_3_0)
        if any(not 0 <= rate <= 1 for rate in rates):
            raise ValueError("retention rates must be between zero and one")
        if not self.retain_1_5 <= self.retain_2_0 <= self.retain_3_0:
            raise ValueError("retention rates must be non-decreasing")

    def candidate_locked_r(self, mfe_r: float) -> float | None:
        if not math.isfinite(mfe_r) or mfe_r < 1.0:
            return None
        if mfe_r < 1.5:
            return 0.0
        if mfe_r < 2.0:
            return self.retain_1_5 * mfe_r
        if mfe_r < 3.0:
            return self.retain_2_0 * mfe_r
        return self.retain_3_0 * mfe_r


VARIANTS = {
    "MFE_LOOSE": OverlayVariant("MFE_LOOSE", 0.50, 0.60, 0.70),
    "MFE_V1": OverlayVariant("MFE_V1", 0.60, 0.70, 0.75),
    "MFE_AGGRESSIVE": OverlayVariant("MFE_AGGRESSIVE", 0.70, 0.75, 0.80),
}


@dataclass(slots=True)
class MFEProtectionState:
    basis: PositionBasis
    variant: OverlayVariant
    mfe_price: float | None = None
    mfe_pnl: float | None = None
    mfe_r: float = 0.0
    mfe_time: datetime | None = None
    locked_profit_r: float = 0.0
    locked_profit_pnl: float | None = None
    armed: bool = False
    activation_time: datetime | None = None
    intrabar_ambiguous: bool = False

    def observe(
        self,
        *,
        price: float,
        at: datetime,
        projected_net_pnl: float,
        pnl_at_price: Callable[[float], float],
    ) -> None:
        current_r = self.basis.favorable_r(price)
        if self.mfe_price is None or current_r > self.mfe_r:
            self.mfe_price = price
            self.mfe_pnl = projected_net_pnl
            self.mfe_r = max(0.0, current_r)
            self.mfe_time = at
        candidate = self.variant.candidate_locked_r(self.mfe_r)
        if candidate is None:
            return
        if not self.armed:
            self.armed = True
            self.activation_time = at
        # This max is an invariant: later observations can never loosen protection.
        self.locked_profit_r = max(self.locked_profit_r, candidate)
        self.locked_profit_pnl = pnl_at_price(
            self.basis.price_for_r(self.locked_profit_r)
        )

    def triggered(self, price: float) -> bool:
        return self.armed and self.basis.floor_breached(price, self.locked_profit_r)


@dataclass(frozen=True, slots=True)
class OhlcBar:
    at: datetime
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self) -> None:
        values = (self.open, self.high, self.low, self.close)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("OHLC values must be positive and finite")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("invalid OHLC ordering")


@dataclass(frozen=True, slots=True)
class BarExit:
    reason: str
    price: float
    intrabar_ambiguous: bool


def _crossed_long(level: float, bar: OhlcBar) -> bool:
    return bar.low <= level


def _crossed_short(level: float, bar: OhlcBar) -> bool:
    return bar.high >= level


def _conservative_level_fill(side: str, level: float, opening: float) -> float:
    if side == "LONG":
        return min(level, opening) if opening <= level else level
    return max(level, opening) if opening >= level else level


def evaluate_ohlc_bar(
    state: MFEProtectionState,
    bar: OhlcBar,
    *,
    pnl_at_price: Callable[[float], float],
    forced_exit: bool = False,
) -> BarExit | None:
    """Conservative helper for future bar replays; tick backtests do not call it.

    An existing initial stop or forced liquidation keeps priority.  When one bar
    both establishes a higher MFE floor and crosses it, ordering is unknowable;
    the function records ambiguity and assumes the protective exit occurred.
    """
    basis = state.basis
    crossed = _crossed_long if basis.side == "LONG" else _crossed_short
    adverse = bar.low if basis.side == "LONG" else bar.high
    favorable = bar.high if basis.side == "LONG" else bar.low

    if crossed(basis.initial_stop_price, bar):
        ambiguous = state.armed and crossed(basis.price_for_r(state.locked_profit_r), bar)
        return BarExit(
            "STOP_LOSS",
            _conservative_level_fill(basis.side, basis.initial_stop_price, bar.open),
            ambiguous,
        )
    if forced_exit:
        return BarExit("HARD_EXIT", bar.close, False)

    old_armed = state.armed
    old_floor = basis.price_for_r(state.locked_profit_r) if old_armed else None
    if old_floor is not None and crossed(old_floor, bar):
        return BarExit(
            "MFE_PROFIT_PROTECTION",
            _conservative_level_fill(basis.side, old_floor, bar.open),
            False,
        )

    state.observe(
        price=favorable,
        at=bar.at,
        projected_net_pnl=pnl_at_price(favorable),
        pnl_at_price=pnl_at_price,
    )
    if not state.armed:
        return None
    new_floor = basis.price_for_r(state.locked_profit_r)
    if crossed(new_floor, bar):
        state.intrabar_ambiguous = True
        return BarExit(
            "MFE_PROFIT_PROTECTION",
            _conservative_level_fill(basis.side, new_floor, bar.open),
            True,
        )
    # Keep the variable used so static review makes the conservative ordering clear.
    _ = adverse
    return None
