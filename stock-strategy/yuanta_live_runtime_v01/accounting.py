"""Execution-based FIFO accounting. Fees are estimates, not broker settlement.

Fee minimums/rounding apply once per executed order; fees are allocated to
matched and remaining shares. Partial fills count regardless of order status.
SPARK's normalized reports currently lack a trusted trade date. ROD executions
are attributed to the persisted order's Taiwan session date, never replay time.
Same-session estimates are unchanged. Carryover rescue uses the full stock sell
tax conservatively, never the day-trade discount; this is not broker settlement.
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
# Ordinary stock sale rate, checked against Taiwan MOF on 2026-10-03:
# https://www.etax.nat.gov.tw/etwmain/tax-info/understanding/tax-q-and-a/national/securities-transaction-tax/filing/mM8n39b
FULL_STOCK_SELL_TAX_RATE = Decimal("0.003")
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
    as_of_day: object = None

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
            carried = self.as_of_day is not None and any(lot.day != self.as_of_day for lot in lots)
            result -= _fees(
                price * quantity, "SELL" if side == "BUY" else "BUY", spec,
                sell_tax_rate=_full_sell_tax(spec) if carried else None,
            )
        return result


def _fee_number(value, *, rate: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        raise AccountingError("invalid fee configuration") from None
    if not parsed.is_finite() or parsed < 0 or (rate and parsed >= 1):
        raise AccountingError("fee configuration must be finite and nonnegative with rates below one")
    return parsed


def _full_sell_tax(spec: Mapping) -> Decimal:
    rate = _fee_number(spec.get("ordinary_stock_sell_tax_rate", FULL_STOCK_SELL_TAX_RATE), rate=True)
    discounted = _fee_number(spec["day_trade_sell_tax_rate"], rate=True)
    if rate < FULL_STOCK_SELL_TAX_RATE:
        raise AccountingError("invalid conservative carryover sell tax rate")
    return max(rate, discounted)


def _fees(notional: Decimal, side: str, spec: Mapping, *, sell_tax_rate=None) -> Decimal:
    if not notional.is_finite() or notional < 0:
        raise AccountingError("fee notional must be finite and nonnegative")
    try:
        minimum = _fee_number(spec["minimum_commission_twd"])
        commission_rate = _fee_number(spec["commission_rate_each_side"], rate=True)
        tax_rate = _fee_number(spec["day_trade_sell_tax_rate"], rate=True)
    except KeyError:
        raise AccountingError("fee configuration is missing required fields") from None
    if sell_tax_rate is not None:
        tax_rate = _fee_number(sell_tax_rate, rate=True)
    commission = max(
        minimum,
        (notional * commission_rate).to_integral_value(rounding=ROUND_CEILING),
    )
    tax = (
        (notional * tax_rate).to_integral_value(rounding=ROUND_CEILING)
        if side == "SELL" else ZERO
    )
    return commission + tax


def execution_pnl(store, now: datetime, spec: Mapping) -> Pnl:
    _fees(ZERO, "BUY", spec)  # Validate even if today's fill ledger is empty.
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
    # Determine tax at order level first, so mixed/partial callbacks never
    # charge a second minimum or give earlier slices an optimistic tax rate.
    tax_queues = defaultdict(deque)
    full_tax_orders = set()
    for fill in fills:
        day = datetime.fromisoformat(fill["order_created_at"].replace("Z", "+00:00")).astimezone(TAIPEI).date()
        bucket = (fill["symbol"], BUCKET[fill["order_type"]])
        side, key, remaining = fill["side"], fill["client_order_id"], int(fill["quantity"])
        queue = tax_queues[bucket]
        if fill["purpose"] == "ENTRY":
            if queue and queue[0][0] != side:
                raise AccountingError("opposing entries cannot be netted silently")
            queue.append([side, remaining, day, key])
            if side == "SELL" and day != today:
                full_tax_orders.add(key)
            continue
        while remaining:
            if not queue or queue[0][0] == side:
                raise AccountingError("exit execution has no matching entry")
            lot = queue[0]
            if lot[2] != day:
                full_tax_orders.add(key if side == "SELL" else lot[3])
            matched = min(remaining, lot[1])
            lot[1] -= matched
            remaining -= matched
            if not lot[1]:
                queue.popleft()
    for fill in fills:
        key = fill["client_order_id"]
        day = datetime.fromisoformat(fill["order_created_at"].replace("Z", "+00:00")).astimezone(TAIPEI).date()
        side = fill["side"]
        bucket = (fill["symbol"], BUCKET[fill["order_type"]])
        fee = _fees(
            notionals[key], side, spec,
            sell_tax_rate=_full_sell_tax(spec) if key in full_tax_orders else None,
        ) / quantities[key]
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
            matched = min(remaining, lot.quantity)
            gross = (price - lot.price) * matched
            if day == today:
                realized += (gross if lot.side == "BUY" else -gross) - (lot.fee_per_share + fee) * matched
            lot.quantity -= matched
            remaining -= matched
            if not lot.quantity:
                queues[bucket].popleft()
    return Pnl(realized, [lot for queue in queues.values() for lot in queue], today)
