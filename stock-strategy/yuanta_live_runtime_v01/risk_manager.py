"""Independent fail-closed risk approval for live strategy intents."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


def _finite_decimal(value: Any) -> Decimal | None:
    """Untrusted numeric inputs must fail closed, not raise in a live loop."""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_daily_loss: Decimal = Decimal("5000")
    max_order_value: Decimal = Decimal("190000")
    max_position_per_stock: int | None = None
    max_concurrent_positions: int = 1
    max_trades_per_day: int = 1
    stale_quote_seconds: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        numeric = (
            self.max_daily_loss,
            self.max_order_value,
            self.max_concurrent_positions,
            self.max_trades_per_day,
            self.stale_quote_seconds,
        )
        parsed = [_finite_decimal(value) for value in numeric]
        if any(value is None or value <= 0 for value in parsed):
            raise ValueError("all risk limits must be finite and positive")
        for count in (self.max_concurrent_positions, self.max_trades_per_day):
            value = _finite_decimal(count)
            if value != value.to_integral_value():
                raise ValueError("position and trade counts must be integers")
        if self.max_position_per_stock is not None:
            value = _finite_decimal(self.max_position_per_stock)
            if value is None or value <= 0 or value != value.to_integral_value():
                raise ValueError("max_position_per_stock must be a positive integer or disabled")
            object.__setattr__(self, "max_position_per_stock", int(value))
        for name in ("max_daily_loss", "max_order_value", "stale_quote_seconds"):
            object.__setattr__(self, name, _finite_decimal(getattr(self, name)))
        for name in ("max_concurrent_positions", "max_trades_per_day"):
            object.__setattr__(self, name, int(_finite_decimal(getattr(self, name))))


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reasons: tuple[str, ...]
    intent: dict[str, Any] | None = None


class RiskManager:
    """Owns approval; strategy code can only produce an unapproved candidate."""

    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def loss_kill_required(self, *, realized: Decimal, unrealized: Decimal) -> bool:
        realized_value = _finite_decimal(realized)
        unrealized_value = _finite_decimal(unrealized)
        if realized_value is None or unrealized_value is None:
            # Unknown PnL is never permission to add exposure. This does not
            # fabricate a realized loss or mark an unknown position as flat.
            return True
        return realized_value + unrealized_value <= -self.limits.max_daily_loss

    def evaluate_entry(
        self,
        *,
        signal: Any,
        quote_age_seconds: float,
        broker_positions: Mapping[str, int],
        open_orders: Sequence[Any],
        trades_today: int,
        realized: Decimal = Decimal("0"),
        unrealized: Decimal = Decimal("0"),
        halted: bool = False,
    ) -> RiskDecision:
        reasons: list[str] = []
        requested = _finite_decimal(getattr(signal, "quantity", None))
        price = _finite_decimal(getattr(signal, "entry_price", None))
        stock_id = str(getattr(signal, "stock_id", "")).strip().upper()
        side_name = str(getattr(signal, "side", "")).upper()
        if (requested is None or requested <= 0 or requested != requested.to_integral_value()
                or price is None or price <= 0):
            return RiskDecision(False, ("INVALID_ORDER_SIZE",))
        if not stock_id or side_name not in {"LONG", "SHORT"}:
            return RiskDecision(False, ("INVALID_ORDER_IDENTITY",))
        parsed_positions = {}
        try:
            for key, value in broker_positions.items():
                count = _finite_decimal(value)
                if count is None or count != count.to_integral_value():
                    return RiskDecision(False, ("BROKER_POSITION_UNAVAILABLE",))
                parsed_positions[str(key)] = int(count)
        except (AttributeError, TypeError):
            return RiskDecision(False, ("BROKER_POSITION_UNAVAILABLE",))
        trade_count = _finite_decimal(trades_today)
        if trade_count is None or trade_count < 0 or trade_count != trade_count.to_integral_value():
            return RiskDecision(False, ("TRADE_COUNT_UNAVAILABLE",))
        decision_time = getattr(signal, "decision_time", None)
        if not isinstance(decision_time, datetime) or decision_time.utcoffset() is None:
            return RiskDecision(False, ("INVALID_SIGNAL_TIME",))
        requested_quantity = int(requested)
        active_symbols = {
            str(key).split("|", 1)[0]
            for key, value in parsed_positions.items()
            if value != 0
        }
        stock_quantity = sum(
            abs(value)
            for key, value in parsed_positions.items()
            if str(key).split("|", 1)[0] == stock_id
        )
        try:
            max_value_quantity = int(
                self.limits.max_order_value // (price * Decimal("1000"))
            ) * 1000
        except (ArithmeticError, ValueError, OverflowError):
            return RiskDecision(False, ("INVALID_ORDER_SIZE",))
        quantity = min(requested_quantity, max_value_quantity)
        if self.limits.max_position_per_stock is not None:
            remaining_stock_limit = max(
                0, self.limits.max_position_per_stock - stock_quantity
            )
            quantity = min(quantity, remaining_stock_limit)
        # Regular NEW uses board lots. A share cap or pre-existing odd-lot
        # residue must not produce a risk-approved order that the adapter then
        # rejects mid-session. This does not change valid strategy sizing.
        quantity = (quantity // 1000) * 1000
        if halted:
            reasons.append("BROKER_HALTED")
        quote_age = _finite_decimal(quote_age_seconds)
        if quote_age is None or quote_age < 0 or quote_age > self.limits.stale_quote_seconds:
            reasons.append("STALE_QUOTE")
        if quantity <= 0:
            reasons.append("MAX_ORDER_VALUE")
        if (
            self.limits.max_position_per_stock is not None
            and quantity + stock_quantity > self.limits.max_position_per_stock
        ):
            reasons.append("MAX_POSITION_PER_STOCK")
        if stock_id not in active_symbols and len(active_symbols) >= self.limits.max_concurrent_positions:
            reasons.append("MAX_CONCURRENT_POSITIONS")
        if trade_count >= self.limits.max_trades_per_day:
            reasons.append("MAX_TRADES_PER_DAY")
        if price * quantity > self.limits.max_order_value:
            reasons.append("MAX_ORDER_VALUE")
        try:
            for order in open_orders:
                remaining = _finite_decimal(getattr(order, "remaining_quantity", None))
                if remaining is None or remaining < 0 or remaining != remaining.to_integral_value():
                    reasons.append("OPEN_ORDER_STATE_UNAVAILABLE")
                    break
                if remaining > 0:
                    reasons.append("OPEN_ORDER_EXISTS")
                    break
        except TypeError:
            reasons.append("OPEN_ORDER_STATE_UNAVAILABLE")
        if _finite_decimal(realized) is None or _finite_decimal(unrealized) is None:
            reasons.append("PNL_UNAVAILABLE")
        elif self.loss_kill_required(realized=realized, unrealized=unrealized):
            reasons.append("MAX_DAILY_LOSS")
        if reasons:
            return RiskDecision(False, tuple(reasons))
        side = "BUY" if side_name == "LONG" else "SELL"
        stamp = decision_time.strftime("%Y%m%d-%H%M%S")
        intent = {
            "status": "APPROVED",
            "intent_id": f"realtime-{stamp}-{stock_id}-{side_name.lower()}-entry",
            "stock_id": stock_id,
            "side": side,
            "intent_type": "ENTRY",
            "quantity_lots": str(Decimal(quantity) / Decimal(1000)),
            "suggested_limit_price": str(price),
            "risk_limits": {
                "MAX_DAILY_LOSS": str(self.limits.max_daily_loss),
                "MAX_ORDER_VALUE": str(self.limits.max_order_value),
                "MAX_POSITION_PER_STOCK": (
                    "DISABLED"
                    if self.limits.max_position_per_stock is None
                    else self.limits.max_position_per_stock
                ),
                "MAX_CONCURRENT_POSITIONS": self.limits.max_concurrent_positions,
                "MAX_TRADES_PER_DAY": self.limits.max_trades_per_day,
                "STALE_QUOTE_SECONDS": str(self.limits.stale_quote_seconds),
            },
        }
        return RiskDecision(True, (), intent)

    def approve_exit(
        self, *, position: Any, quantity: int, price: float, reason: str, attempt: int
    ) -> dict[str, Any]:
        """Exits are exposure-reducing and are never blocked by entry limits."""
        parsed_quantity, parsed_price = _finite_decimal(quantity), _finite_decimal(price)
        if (parsed_quantity is None or parsed_quantity <= 0
                or parsed_quantity != parsed_quantity.to_integral_value()
                or parsed_price is None or parsed_price <= 0):
            raise ValueError("exit quantity and price must be finite and positive; quantity must be integral")
        if position.side not in {"LONG", "SHORT"}:
            raise ValueError("exit position side must be LONG or SHORT")
        side = "SELL" if position.side == "LONG" else "BUY"
        stamp = datetime.now(position.entry_time.tzinfo).strftime("%Y%m%d-%H%M%S-%f")
        return {
            "status": "APPROVED",
            "intent_id": (
                f"realtime-{stamp}-{position.stock_id}-{position.side.lower()}-"
                f"exit-{reason.lower()}-r{attempt}"
            ),
            "stock_id": position.stock_id,
            "side": side,
            "intent_type": "EXIT",
            "quantity_lots": str(Decimal(quantity) / Decimal(1000)),
            "suggested_limit_price": str(Decimal(str(price))),
        }
