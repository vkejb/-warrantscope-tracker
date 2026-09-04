from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import math

from .config import CFG, Config
from .models import OddLotQuote, ShadowDecision, Signal
from .signals import EPSILON, ticks_above


SHADOW_STATUS = "NOT_SUBMITTED_SHADOW"


def target_quantity(ask_price: float, *, cfg: Config = CFG) -> int:
    if not math.isfinite(ask_price) or ask_price <= 0:
        return 0
    affordable = math.floor(cfg.research_cash * cfg.maximum_cash_fraction / ask_price)
    return max(0, min(affordable, cfg.maximum_odd_lot_shares))


def evaluate_quote(
    signal: Signal,
    quote: OddLotQuote,
    *,
    cfg: Config = CFG,
) -> ShadowDecision:
    """Evaluate an odd-lot quote without creating an order or claiming a fill."""

    quote_age = (quote.received_time - quote.exchange_time).total_seconds()
    midpoint = (quote.ask1 + quote.bid1) / 2.0 if quote.ask1 > 0 and quote.bid1 > 0 else 0
    spread = (quote.ask1 - quote.bid1) / midpoint if midpoint > 0 else None
    premium = (
        quote.ask1 / quote.regular_last - 1.0 if quote.regular_last > 0 else None
    )
    quantity = target_quantity(quote.ask1, cfg=cfg)
    depth = quote.ask1_quantity + quote.ask2_quantity
    limit_price = quote.ask1 if quote.ask1 > 0 else None

    checks = (
        (quote.symbol == signal.symbol and quote.market == signal.market, "QUOTE_ID_MISMATCH"),
        (quote.exchange_time.date() == signal.trade_date, "QUOTE_DATE_MISMATCH"),
        (quote.exchange_time > signal.signal_time, "QUOTE_NOT_AFTER_SIGNAL"),
        (
            quote.received_time <= signal.signal_time
            + timedelta(seconds=cfg.shadow_intent_lifetime_seconds),
            "QUOTE_AFTER_60_SECOND_WINDOW",
        ),
        (quote.feed_state == "HEALTHY", "FEED_NOT_HEALTHY"),
        (quote.market_status == "NORMAL", "MARKET_NOT_NORMAL"),
        (quote_age >= 0, "RECEIVE_TIME_BEFORE_EXCHANGE_TIME"),
        (quote_age <= cfg.quote_freshness_seconds, "QUOTE_STALE"),
        (quote.bid1 > 0 and quote.ask1 > 0, "EMPTY_BID_OR_ASK"),
        (quote.bid1 <= quote.ask1, "CROSSED_ODD_LOT_BOOK"),
        (
            spread is not None and spread <= cfg.maximum_odd_lot_spread,
            "ODD_LOT_SPREAD_TOO_WIDE",
        ),
        (
            premium is not None and premium <= cfg.maximum_odd_lot_ask_premium,
            "ODD_LOT_ASK_PREMIUM_TOO_HIGH",
        ),
        (quantity > 0, "ZERO_TARGET_QUANTITY"),
        (
            depth >= math.ceil(quantity * cfg.minimum_ask_depth_multiple),
            "INSUFFICIENT_ASK_DEPTH",
        ),
        (
            quote.ask1
            <= signal.signal_price * (1.0 + cfg.maximum_limit_above_signal),
            "ABOVE_SIGNAL_PRICE_CAP",
        ),
        (
            ticks_above(quote.ask1, cfg.limit_up_buffer_ticks)
            <= quote.limit_up + EPSILON,
            "TOO_CLOSE_TO_LIMIT_UP",
        ),
    )
    reason = next((reason for passed, reason in checks if not passed), "")
    return ShadowDecision(
        strategy_id=cfg.strategy_id,
        config_hash=cfg.fingerprint(),
        trade_date=signal.trade_date,
        symbol=signal.symbol,
        signal_time=signal.signal_time,
        quote_time=quote.exchange_time,
        target_quantity=quantity,
        reference_limit_price=limit_price,
        spread_pct=spread,
        ask_premium_pct=premium,
        ask_depth_2=depth,
        quote_age_seconds=quote_age,
        status=SHADOW_STATUS if not reason else "REJECTED_SHADOW_QUOTE",
        is_actual_order=False,
        is_actual_fill=False,
        rejection_reason=reason or "ELIGIBLE_QUOTE_FILL_NOT_ASSUMED",
    )


def replay_shadow_quotes(
    selected_signals: list[Signal],
    quotes: list[OddLotQuote],
    *,
    cfg: Config = CFG,
) -> tuple[list[ShadowDecision], list[ShadowDecision]]:
    """Return all quote checks and one final shadow outcome per selected signal."""

    quotes_by_key: dict[tuple[str, str], list[OddLotQuote]] = defaultdict(list)
    for quote in quotes:
        quotes_by_key[(quote.market, quote.symbol)].append(quote)
    for rows in quotes_by_key.values():
        rows.sort(key=lambda x: (x.received_time, x.exchange_time))

    checks: list[ShadowDecision] = []
    outcomes: list[ShadowDecision] = []
    consumed_keys: set[str] = set()
    for signal in sorted(selected_signals, key=lambda x: (x.signal_time, x.symbol)):
        key = f"{cfg.strategy_id}|{signal.trade_date.isoformat()}"
        if key in consumed_keys:
            outcomes.append(_expired(signal, "DUPLICATE_DAILY_INTENT_BLOCKED", cfg))
            continue
        consumed_keys.add(key)
        candidates = [
            quote
            for quote in quotes_by_key.get((signal.market, signal.symbol), [])
            if quote.received_time > signal.signal_time
            and quote.received_time
            <= signal.signal_time + timedelta(seconds=cfg.shadow_intent_lifetime_seconds)
        ]
        chosen = None
        for quote in candidates:
            decision = evaluate_quote(signal, quote, cfg=cfg)
            checks.append(decision)
            if decision.status == SHADOW_STATUS:
                chosen = decision
                break
        outcome = chosen or _expired(signal, "NO_ELIGIBLE_QUOTE_WITHIN_60_SECONDS", cfg)
        outcomes.append(outcome)
    return checks, outcomes


def _expired(signal: Signal, reason: str, cfg: Config) -> ShadowDecision:
    return ShadowDecision(
        strategy_id=cfg.strategy_id,
        config_hash=cfg.fingerprint(),
        trade_date=signal.trade_date,
        symbol=signal.symbol,
        signal_time=signal.signal_time,
        quote_time=None,
        target_quantity=0,
        reference_limit_price=None,
        spread_pct=None,
        ask_premium_pct=None,
        ask_depth_2=None,
        quote_age_seconds=None,
        status="EXPIRED_SHADOW",
        is_actual_order=False,
        is_actual_fill=False,
        rejection_reason=reason,
    )
