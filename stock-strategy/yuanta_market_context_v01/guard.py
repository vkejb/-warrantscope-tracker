"""Fail-closed shadow gate for day-trade eligibility and quote quality."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import QuoteContext, SecurityContext


@dataclass(frozen=True, slots=True)
class GatePolicy:
    maximum_quote_age_seconds: float = 5.0
    maximum_spread_bps: float = 40.0
    minimum_total_volume: int = 1
    reject_any_warning: bool = True
    reject_limit_locked: bool = True


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reasons: tuple[str, ...]
    evidence: dict[str, object]


class PreTradeEvidenceGate:
    """Validate broker evidence before a signal may become a shadow intent.

    This class is deliberately not imported by the live runtime. It creates no
    order and contains no broker submission method.
    """

    def __init__(self, policy: GatePolicy | None = None):
        self.policy = policy or GatePolicy()

    def evaluate(
        self,
        security: SecurityContext | None,
        quote: QuoteContext | None,
        *,
        side: str,
        now: datetime,
    ) -> GateDecision:
        side = side.upper()
        if side not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        reasons: list[str] = []
        evidence: dict[str, object] = {"side": side}
        if security is None:
            reasons.append("MISSING_STOCK_INFORMATION")
        if quote is None:
            reasons.append("MISSING_QUOTE_CONTEXT")
        if security is None or quote is None:
            return GateDecision(False, tuple(reasons), evidence)

        evidence.update({
            "symbol": security.symbol,
            "day_trade_code": security.day_trade_code,
            "warnings": list(security.warnings),
            "lend_remnants": security.lend_remnants,
            "lend_qty": security.lend_qty,
            "spread_bps": quote.spread_bps,
            "quote_time": quote.quote_time.isoformat(),
        })
        if security.symbol != quote.symbol:
            reasons.append("SYMBOL_MISMATCH")
        if security.day_trade_code not in {"X", "Y"}:
            reasons.append("NOT_DAY_TRADE_ELIGIBLE")
        if side == "SHORT" and security.day_trade_code != "X":
            reasons.append("SELL_FIRST_NOT_ALLOWED")
        if self.policy.reject_any_warning and security.warnings:
            reasons.append("STOCK_WARNING")
        if side == "SHORT":
            lend_available = any(
                value is not None and value > 0
                for value in (security.lend_remnants, security.lend_qty)
            )
            if not lend_available:
                reasons.append("NO_CONFIRMED_SHORT_INVENTORY")
            if (
                quote.open_reference > 0
                and quote.last < quote.open_reference
                and security.lend_sell_mark != "Y"
            ):
                reasons.append("BELOW_REFERENCE_SHORT_NOT_ALLOWED")
        age = (now - quote.quote_time).total_seconds()
        evidence["quote_age_seconds"] = age
        if age < 0 or age > self.policy.maximum_quote_age_seconds:
            reasons.append("STALE_QUOTE_CONTEXT")
        if quote.spread_bps is None:
            reasons.append("INVALID_TOP_OF_BOOK")
        elif quote.spread_bps > self.policy.maximum_spread_bps:
            reasons.append("SPREAD_TOO_WIDE")
        if quote.total_volume < self.policy.minimum_total_volume:
            reasons.append("INSUFFICIENT_TRADED_VOLUME")
        if self.policy.reject_limit_locked:
            limit_up_locked = quote.limit_up > 0 and quote.bid >= quote.limit_up and quote.ask <= 0
            limit_down_locked = quote.limit_down > 0 and quote.ask <= quote.limit_down and quote.bid <= 0
            if limit_up_locked or limit_down_locked:
                reasons.append("LIMIT_LOCKED")
        return GateDecision(not reasons, tuple(reasons), evidence)
