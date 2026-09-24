"""Broker-neutral execution models for the Yuanta SPARK live adapter."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import Enum, IntEnum
from hashlib import sha256
from typing import Any, Mapping


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PriceType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    LIMIT_UP = "LIMIT_UP"
    LIMIT_DOWN = "LIMIT_DOWN"
    FLAT = "FLAT"


class TimeInForce(str, Enum):
    ROD = "ROD"
    IOC = "IOC"
    FOK = "FOK"


class APCode(IntEnum):
    REGULAR = 0
    ODD_LOT = 2
    INTRADAY_ODD_LOT = 4
    AFTER_HOURS = 7


class StockOrderType(str, Enum):
    CASH = "0"
    MARGIN_BUY = "3"
    SHORT_SELL = "4"
    BORROW_SELL_STRATEGY = "5"
    BORROW_SELL_HEDGE = "6"
    DAY_TRADE_CONTROL = "9"


class IntentPurpose(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class BrokerOrderStatus(str, Enum):
    RESERVED = "RESERVED"
    SEND_PENDING = "SEND_PENDING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


TERMINAL_STATUSES = {
    BrokerOrderStatus.FILLED,
    BrokerOrderStatus.CANCELED,
    BrokerOrderStatus.EXPIRED,
    BrokerOrderStatus.REJECTED,
}


def _enum(enum_type, value):
    if isinstance(value, enum_type):
        return value
    raw = str(value).strip()
    aliases = {
        Side: {"B": Side.BUY, "BUY": Side.BUY, "S": Side.SELL, "SELL": Side.SELL},
        PriceType: {
            "": PriceType.LIMIT, "LIMIT": PriceType.LIMIT, "LMT": PriceType.LIMIT,
            "M": PriceType.MARKET, "MARKET": PriceType.MARKET, "MKT": PriceType.MARKET,
            "H": PriceType.LIMIT_UP, "LIMIT_UP": PriceType.LIMIT_UP,
            "L": PriceType.LIMIT_DOWN, "LIMIT_DOWN": PriceType.LIMIT_DOWN,
            "-": PriceType.FLAT, "FLAT": PriceType.FLAT,
        },
        TimeInForce: {"0": TimeInForce.ROD, "ROD": TimeInForce.ROD,
                      "3": TimeInForce.IOC, "IOC": TimeInForce.IOC,
                      "4": TimeInForce.FOK, "FOK": TimeInForce.FOK},
    }
    mapped = aliases.get(enum_type, {}).get(raw.upper())
    if mapped is not None:
        return mapped
    try:
        return enum_type(raw)
    except ValueError:
        try:
            return enum_type[raw.upper()]
        except KeyError:
            return enum_type(value)


def _decimal(value: Any | None) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid price: {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("price must be finite and non-negative")
    return result


def _read(obj: Any, *names: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """Canonical execution contract accepted by the adapter.

    Quantity is always expressed internally in shares. Broker report quantities
    are also normalized as shares. Where Yuanta's outbound order contract uses a
    different unit, the adapter converts only at the broker boundary.
    """

    intent_id: str
    symbol: str
    side: Side
    quantity: int
    price: Decimal | None = None
    price_type: PriceType = PriceType.LIMIT
    time_in_force: TimeInForce = TimeInForce.ROD
    ap_code: APCode = APCode.REGULAR
    order_type: StockOrderType = StockOrderType.CASH
    purpose: IntentPurpose = IntentPurpose.ENTRY

    def __post_init__(self) -> None:
        if not self.intent_id.strip():
            raise ValueError("intent_id is required")
        if not self.symbol.strip():
            raise ValueError("symbol is required")
        if not isinstance(self.quantity, int) or isinstance(self.quantity, bool) or self.quantity <= 0:
            raise ValueError("quantity must be a positive integer number of shares")
        if self.price_type == PriceType.LIMIT and (self.price is None or self.price <= 0):
            raise ValueError("positive price is required for LIMIT orders")
        if self.price_type != PriceType.LIMIT and self.price not in {None, Decimal("0")}:
            raise ValueError("non-limit orders must not carry a positive limit price")

    @classmethod
    def coerce(cls, raw: Any) -> "ExecutionIntent":
        if isinstance(raw, cls):
            return raw

        intent_id = _read(raw, "intent_id", "idempotency_key", "client_order_id", "signal_id")
        symbol = _read(raw, "symbol", "stock_id", "code", "stk_code")
        side = _read(raw, "side", "buy_sell", "bs")
        quantity = _read(raw, "quantity", "qty", "shares", "order_qty")
        price = _read(raw, "price", "limit_price", default=None)
        price_type = _read(raw, "price_type", "order_price_type", default=PriceType.LIMIT)
        tif = _read(raw, "time_in_force", "tif", default=TimeInForce.ROD)
        ap_code = _read(raw, "ap_code", default=APCode.REGULAR)
        order_type = _read(raw, "order_type", default=StockOrderType.CASH)
        purpose = _read(raw, "purpose", "intent", default=IntentPurpose.ENTRY)

        if intent_id is None or symbol is None or side is None or quantity is None:
            raise ValueError("intent must provide intent_id, symbol, side and quantity")

        parsed_price_type = _enum(PriceType, price_type)
        parsed_price = _decimal(price)
        if parsed_price_type != PriceType.LIMIT and parsed_price is None:
            parsed_price = None

        return cls(
            intent_id=str(intent_id).strip(),
            symbol=str(symbol).strip().upper(),
            side=_enum(Side, side),
            quantity=int(quantity),
            price=parsed_price,
            price_type=parsed_price_type,
            time_in_force=_enum(TimeInForce, tif),
            ap_code=_enum(APCode, ap_code),
            order_type=_enum(StockOrderType, order_type),
            purpose=_enum(IntentPurpose, purpose),
        )

    def with_price(self, price: Any) -> "ExecutionIntent":
        parsed = _decimal(price)
        if parsed is None or parsed <= 0:
            raise ValueError("new limit price must be positive")
        return replace(self, price=parsed, price_type=PriceType.LIMIT)


@dataclass(frozen=True, slots=True)
class StoredOrder:
    client_order_id: str
    intent_id: str
    basket_no: str
    identify: int
    broker_order_no: str | None
    symbol: str
    side: Side
    quantity: int
    price: Decimal | None
    price_type: PriceType
    time_in_force: TimeInForce
    ap_code: APCode
    order_type: StockOrderType
    purpose: IntentPurpose
    status: BrokerOrderStatus
    filled_quantity: int
    average_fill_price: Decimal | None
    last_error: str | None
    created_at: str
    updated_at: str

    @property
    def remaining_quantity(self) -> int:
        return max(0, self.quantity - self.filled_quantity)


def basket_no_for(intent_id: str) -> str:
    # Yuanta documents BasketNo as at most 32 alphanumeric characters.
    return "WS" + sha256(intent_id.encode("utf-8")).hexdigest()[:30]


PRICE_FLAG = {
    PriceType.LIMIT: " ",
    PriceType.MARKET: "M",
    PriceType.LIMIT_UP: "H",
    PriceType.LIMIT_DOWN: "L",
    PriceType.FLAT: "-",
}

TIF_CODE = {
    TimeInForce.ROD: "0",
    TimeInForce.IOC: "3",
    TimeInForce.FOK: "4",
}
