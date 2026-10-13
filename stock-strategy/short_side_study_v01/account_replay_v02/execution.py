"""Same displayed-capacity bookkeeping, with 1000-share ordinary-stock fills.

Track raw shares before quantizing each executable fill. Rounding snapshots
before bookkeeping would invent replenishment when raw size crosses a lot edge.
This is a conditional research model, not proof of SDK units or broker fills.
"""
import math
from ..execution import DepthExecution


class LotDepthExecution(DepthExecution):
    lot_size = 1000

    def __init__(self):
        super().__init__()
        self.requests = []
        self.fills = []

    def fill(self, book, *, at, side, quantity, limit_price, max_age=3.):
        if side not in ('BUY', 'SELL'):
            raise ValueError('side must be BUY or SELL')
        if not isinstance(quantity, int) or quantity <= 0 or quantity % self.lot_size:
            raise ValueError('ORDINARY_STOCK_QUANTITY_MUST_BE_WHOLE_1000_SHARE_LOTS')
        if limit_price <= 0 or not math.isfinite(limit_price):
            raise ValueError('positive finite limit required')
        self.requests.append({'at': at.isoformat(), 'side': side, 'quantity': quantity, 'limit_price': limit_price})
        age = (at - book.received_at).total_seconds()
        if not 0 <= age <= max_age:
            return 0, None
        levels = book.asks if side == 'BUY' else book.bids
        if any(not math.isfinite(p) or p < 0 or not isinstance(q, int) or q < 0 for p, q in levels):
            return 0, None
        if self.last_snapshot.get(side) != book.snapshot_id:
            current = {}
            for p, q in levels:
                if 0 < p < 99999:
                    current[p] = current.get(p, 0) + q
            for key in [k for k in self.displayed if k[0] == side and k[1] not in current]:
                self.displayed.pop(key, None)
                self.available.pop(key, None)
            for p, q in current.items():
                key = (side, p)
                if key not in self.displayed:
                    self.available[key] = q
                else:
                    self.available[key] = min(q, max(0, self.available.get(key, 0) + q - self.displayed[key]))
                self.displayed[key] = q
            self.last_snapshot[side] = book.snapshot_id
        quantity_filled, value = 0, 0.
        for index, (price, _) in enumerate(levels):
            if not 0 < price < 99999:
                continue
            if (side == 'BUY' and price > limit_price) or (side == 'SELL' and price < limit_price):
                continue
            available = self.available.get((side, price), 0)
            taken = min(quantity - quantity_filled, available) // self.lot_size * self.lot_size
            quantity_filled += taken
            value += taken * price
            key = (book.snapshot_id, side, index)
            self.consumed[key] = self.consumed.get(key, 0) + taken
            self.available[(side, price)] = available - taken
            if taken:
                self.fills.append({'at': at.isoformat(), 'side': side, 'quantity': taken, 'depth_price': price})
            if quantity_filled == quantity:
                break
        return quantity_filled, value / quantity_filled if quantity_filled else None
