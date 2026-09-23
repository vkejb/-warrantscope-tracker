"""Independent fail-closed risk approval for live strategy intents."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_daily_loss: Decimal = Decimal("5000")
    max_order_value: Decimal = Decimal("190000")
    max_position_per_stock: int = 1000
    max_concurrent_positions: int = 1
    max_trades_per_day: int = 1
    stale_quote_seconds: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        numeric = (
            self.max_daily_loss,
            self.max_order_value,
            self.max_position_per_stock,
            self.max_concurrent_positions,
            self.max_trades_per_day,
            self.stale_quote_seconds,
        )
        if any(Decimal(str(value)) <= 0 for value in numeric):
            raise ValueError("all risk limits must be positive")


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
        return realized + unrealized <= -self.limits.max_daily_loss

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
        requested_quantity = int(signal.quantity)
        quantity = min(requested_quantity, self.limits.max_position_per_stock)
        price = Decimal(str(signal.entry_price))
        stock_id = str(signal.stock_id).strip().upper()
        active_symbols = {
            str(key).split("|", 1)[0]
            for key, value in broker_positions.items()
            if int(value) != 0
        }
        stock_quantity = sum(
            abs(int(value))
            for key, value in broker_positions.items()
            if str(key).split("|", 1)[0] == stock_id
        )
        if halted:
            reasons.append("BROKER_HALTED")
        if quote_age_seconds < 0 or Decimal(str(quote_age_seconds)) > self.limits.stale_quote_seconds:
            reasons.append("STALE_QUOTE")
        if quantity <= 0 or quantity + stock_quantity > self.limits.max_position_per_stock:
            reasons.append("MAX_POSITION_PER_STOCK")
        if stock_id not in active_symbols and len(active_symbols) >= self.limits.max_concurrent_positions:
            reasons.append("MAX_CONCURRENT_POSITIONS")
        if trades_today >= self.limits.max_trades_per_day:
            reasons.append("MAX_TRADES_PER_DAY")
        if price * quantity > self.limits.max_order_value:
            reasons.append("MAX_ORDER_VALUE")
        if any(getattr(order, "remaining_quantity", 0) > 0 for order in open_orders):
            reasons.append("OPEN_ORDER_EXISTS")
        if self.loss_kill_required(realized=realized, unrealized=unrealized):
            reasons.append("MAX_DAILY_LOSS")
        if reasons:
            return RiskDecision(False, tuple(reasons))
        side = "BUY" if signal.side == "LONG" else "SELL"
        stamp = signal.decision_time.strftime("%Y%m%d-%H%M%S")
        intent = {
            "status": "APPROVED",
            "intent_id": f"realtime-{stamp}-{stock_id}-{signal.side.lower()}-entry",
            "stock_id": stock_id,
            "side": side,
            "intent_type": "ENTRY",
            "quantity_lots": str(Decimal(quantity) / Decimal(1000)),
            "suggested_limit_price": str(price),
            "risk_limits": {
                "MAX_DAILY_LOSS": str(self.limits.max_daily_loss),
                "MAX_ORDER_VALUE": str(self.limits.max_order_value),
                "MAX_POSITION_PER_STOCK": self.limits.max_position_per_stock,
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
        if quantity <= 0 or price <= 0:
            raise ValueError("exit quantity and price must be positive")
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
