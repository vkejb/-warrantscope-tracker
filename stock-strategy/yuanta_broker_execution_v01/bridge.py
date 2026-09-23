"""Explicit conversion from strategy intents to broker execution intents."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .models import (
    ExecutionIntent,
    IntentPurpose,
    PriceType,
    Side,
    StockOrderType,
    TimeInForce,
)


class IntentBridgeError(ValueError):
    pass


def _read(raw: Any, name: str, default: Any = None) -> Any:
    if isinstance(raw, Mapping):
        return raw.get(name, default)
    return getattr(raw, name, default)


def _text(value: Any) -> str:
    return str(getattr(value, "value", value)).strip().upper()


def _lots_to_shares(value: Any) -> int:
    try:
        lots = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise IntentBridgeError("quantity_lots must be numeric") from exc
    shares = lots * Decimal("1000")
    if not lots.is_finite() or lots <= 0 or shares != shares.to_integral_value():
        raise IntentBridgeError("quantity_lots must convert to a positive whole share quantity")
    return int(shares)


def bridge_strategy_intent(
    raw: Any,
    *,
    approved_statuses: tuple[str, ...] = ("APPROVED",),
    short_entry_order_type: StockOrderType | None = None,
    short_cover_order_type: StockOrderType | None = None,
) -> ExecutionIntent:
    """Convert an already risk-approved strategy intent without guessing shorts.

    Long entry/exit cash orders are deterministic.  Sell-first and buy-to-cover
    semantics vary with the account and eligibility, so the caller must provide
    the reviewed Yuanta order type explicitly.
    """

    status = _text(_read(raw, "status", ""))
    allowed = {_text(value) for value in approved_statuses}
    if status not in allowed:
        raise IntentBridgeError(f"strategy intent is not approved: {status or 'MISSING'}")

    intent_id = str(_read(raw, "intent_id", "")).strip()
    symbol = str(_read(raw, "stock_id", "")).strip().upper()
    side_text = _text(_read(raw, "side", ""))
    intent_type = _text(_read(raw, "intent_type", ""))
    if not intent_id or not symbol:
        raise IntentBridgeError("intent_id and stock_id are required")
    try:
        side = Side(side_text)
        purpose = IntentPurpose(intent_type)
    except ValueError as exc:
        raise IntentBridgeError("side and intent_type must be explicit BUY/SELL and ENTRY/EXIT") from exc

    quantity = _lots_to_shares(_read(raw, "quantity_lots"))
    price_value = _read(raw, "suggested_limit_price")
    try:
        price = Decimal(str(price_value))
    except (InvalidOperation, ValueError) as exc:
        raise IntentBridgeError("suggested_limit_price must be numeric") from exc
    if not price.is_finite() or price <= 0:
        raise IntentBridgeError("suggested_limit_price must be positive")

    if purpose == IntentPurpose.ENTRY and side == Side.BUY:
        order_type = StockOrderType.CASH
    elif purpose == IntentPurpose.EXIT and side == Side.SELL:
        order_type = StockOrderType.CASH
    elif purpose == IntentPurpose.ENTRY and side == Side.SELL:
        if short_entry_order_type is None:
            raise IntentBridgeError("sell-first entry requires an explicit reviewed order type")
        order_type = short_entry_order_type
    else:
        if short_cover_order_type is None:
            raise IntentBridgeError("buy-to-cover exit requires an explicit reviewed order type")
        order_type = short_cover_order_type

    return ExecutionIntent(
        intent_id=intent_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        price_type=PriceType.LIMIT,
        time_in_force=TimeInForce.ROD,
        order_type=order_type,
        purpose=purpose,
    )
