"""Causal displayed-depth fills. Snapshots are evidence, never broker fills.

The caller must supply a documented shares-per-volume-unit conversion. Without
that evidence the capacity model remains DEPTH_UNITS_UNPROVEN. Identical depth
cannot be spent twice; an exit never reports FLAT until its remaining shares=0.
"""
from dataclasses import dataclass, field
from datetime import datetime
import math


@dataclass(frozen=True)
class DepthBook:
    received_at: datetime
    bids: tuple[tuple[float, int], ...]
    asks: tuple[tuple[float, int], ...]
    snapshot_id: str


@dataclass
class DepthExecution:
    consumed: dict[tuple[str, str, int], int] = field(default_factory=dict)
    last_snapshot: dict[str, str] = field(default_factory=dict)
    displayed: dict[tuple[str, float], int] = field(default_factory=dict)
    available: dict[tuple[str, float], int] = field(default_factory=dict)

    def fill(self, book, *, at, side, quantity, limit_price, max_age=3.):
        if side not in ('BUY', 'SELL'):
            raise ValueError('side must be BUY or SELL')
        if quantity <= 0 or limit_price <= 0 or not math.isfinite(limit_price):
            raise ValueError('positive finite quantity/limit required')
        age = (at - book.received_at).total_seconds()
        if not 0 <= age <= max_age:
            return 0, None
        levels = book.asks if side == 'BUY' else book.bids
        if any(not math.isfinite(p) or p < 0 or q < 0 for p,q in levels):
            return 0,None
        # A new callback ID does not prove liquidity replenishment. Refill only
        # by a positive observed change in displayed size at the same price.
        if self.last_snapshot.get(side) != book.snapshot_id:
            current={}
            for p,q in levels:
                if 0<p<99999:
                    current[p]=current.get(p,0)+q
            for key in [k for k in self.displayed if k[0]==side and k[1] not in current]:
                self.displayed.pop(key,None)
                self.available.pop(key,None)
            for p,q in current.items():
                key=(side,p)
                if key not in self.displayed:
                    self.available[key]=q
                else:
                    self.available[key]=min(q,max(0,self.available.get(key,0)+q-self.displayed[key]))
                self.displayed[key]=q
            self.last_snapshot[side]=book.snapshot_id
        quantity_filled, value = 0, 0.
        for index, (price, shares) in enumerate(levels):
            if not 0 < price < 99999 or shares == 0:
                continue
            if (side == 'BUY' and price > limit_price) or (side == 'SELL' and price < limit_price):
                continue
            key = (book.snapshot_id, side, index)
            available = self.available.get((side,price),0)
            taken = min(quantity - quantity_filled, available)
            quantity_filled += taken
            value += taken * price
            self.consumed[key] = self.consumed.get(key, 0) + taken
            self.available[(side,price)] = available-taken
            if quantity_filled == quantity:
                break
        return quantity_filled, value / quantity_filled if quantity_filled else None


@dataclass
class Exposure:
    side: str
    entered_shares: int
    remaining_shares: int
    covered_shares: int = 0

    @property
    def status(self):
        return 'FLAT' if self.remaining_shares == 0 else 'OPEN_EXPOSURE'

    def apply_exit(self, shares):
        if not 0 <= shares <= self.remaining_shares:
            raise ValueError('exit cannot exceed real remaining shares')
        self.covered_shares += shares
        self.remaining_shares -= shares
