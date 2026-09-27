"""Execution-based FIFO accounting. Fees are estimates, not broker settlement.

Fee minimums/rounding apply once per executed order; fees are allocated to
matched and remaining shares. Partial fills count regardless of order status.
SPARK's normalized reports currently lack a trusted trade date. ROD executions
are attributed to the persisted order's Taiwan session date, never replay time.
Overnight lots require a separately configured tax model and are refused here.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from typing import Mapping
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
ZERO = Decimal("0")
BUCKET = {"0": "0", "9": "0", "3": "3", "4": "4", "5": "6", "6": "6"}


class AccountingError(RuntimeError):
    pass


@dataclass
class Lot:
    symbol: str
    side: str
    quantity: int
    price: Decimal
    fee_per_share: Decimal
    day: object


@dataclass
class Pnl:
    realized: Decimal
    open_lots: list[Lot]

    def unrealized(self, prices: Mapping[str, Decimal], spec: Mapping) -> Decimal:
        """Mark only remaining shares, including allocated entry and exit costs."""
        result = ZERO
        groups = defaultdict(list)
        for lot in self.open_lots:
            groups[(lot.symbol, lot.side)].append(lot)
        for (symbol, side), lots in groups.items():
            if symbol not in prices:
                raise AccountingError("fresh mark missing for open position")
            price = Decimal(str(prices[symbol]))
            if not price.is_finite() or price <= 0:
                raise AccountingError("invalid position mark")
            quantity = sum(lot.quantity for lot in lots)
            for lot in lots:
                gross = (price - lot.price) * lot.quantity
                result += (gross if side == "BUY" else -gross) - lot.fee_per_share * lot.quantity
            result -= _fees(price * quantity, "SELL" if side == "BUY" else "BUY", spec)
        return result


def _fees(notional: Decimal, side: str, spec: Mapping) -> Decimal:
    commission = max(
        Decimal(str(spec["minimum_commission_twd"])),
        (notional * Decimal(str(spec["commission_rate_each_side"]))).to_integral_value(rounding=ROUND_CEILING),
    )
    tax = (
        (notional * Decimal(str(spec["day_trade_sell_tax_rate"]))).to_integral_value(rounding=ROUND_CEILING)
        if side == "SELL" else ZERO
    )
    return commission + tax


def execution_pnl(store, now: datetime, spec: Mapping) -> Pnl:
    fills = store.fills()
    notionals = defaultdict(lambda: ZERO)
    quantities = defaultdict(int)
    for fill in fills:
        key = fill["client_order_id"]
        notionals[key] += Decimal(fill["price"]) * fill["quantity"]
        quantities[key] += fill["quantity"]
    queues = defaultdict(deque)
    realized = ZERO
    today = now.astimezone(TAIPEI).date()
    for fill in fills:
        key = fill["client_order_id"]
        day = datetime.fromisoformat(fill["order_created_at"].replace("Z", "+00:00")).astimezone(TAIPEI).date()
        side = fill["side"]
        bucket = (fill["symbol"], BUCKET[fill["order_type"]])
        fee = _fees(notionals[key], side, spec) / quantities[key]
        price = Decimal(fill["price"])
        remaining = int(fill["quantity"])
        if fill["purpose"] == "ENTRY":
            if queues[bucket] and queues[bucket][0].side != side:
                raise AccountingError("opposing entries cannot be netted silently")
            queues[bucket].append(Lot(fill["symbol"], side, remaining, price, fee, day))
            continue
        while remaining:
            if not queues[bucket] or queues[bucket][0].side == side:
                raise AccountingError("exit execution has no matching entry")
            lot = queues[bucket][0]
            if lot.day != day:
                raise AccountingError("overnight execution requires reviewed tax attribution")
            matched = min(remaining, lot.quantity)
            gross = (price - lot.price) * matched
            if day == today:
                realized += (gross if lot.side == "BUY" else -gross) - (lot.fee_per_share + fee) * matched
            lot.quantity -= matched
            remaining -= matched
            if not lot.quantity:
                queues[bucket].popleft()
    return Pnl(realized, [lot for queue in queues.values() for lot in queue])
