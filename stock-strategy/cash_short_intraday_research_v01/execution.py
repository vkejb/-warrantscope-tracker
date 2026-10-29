"""Pure offline regular-stock execution scenarios, never broker fill evidence.

All quantities exposed by this API are shares.  Raw book quantities require an
explicit 1 (assumed shares) or 1000 (assumed lots) multiplier.  Unknown units or
eligibility block the default strict mode.  Conditional scenarios are deliberately
labelled assumptions, not verified executable performance.  No SDK, I/O or network.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping, MutableMapping, Sequence
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
LOT_SIZE = 1000
DEPTH_FRACTION = Decimal("0.10")
_PRICE_DEPTH_NAMESPACE = "__cash_short_conservative_price_depth_v1__"


def _number(value: object) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("INVALID_NUMBER") from exc
    if not number.is_finite():
        raise ValueError("NONFINITE_NUMBER")
    return number


def _positive(value: object) -> Decimal:
    number = _number(value)
    if number <= 0:
        raise ValueError("NONPOSITIVE_PRICE")
    return number


def _step(price: Decimal) -> Decimal:
    if price < 10:
        return Decimal("0.01")
    if price < 50:
        return Decimal("0.05")
    if price < 100:
        return Decimal("0.1")
    if price < 500:
        return Decimal("0.5")
    if price < 1000:
        return Decimal("1")
    return Decimal("5")


def tick_size(price: object, asset: str = "stock") -> float:
    """Ordinary-stock ticks only; never silently apply these bands to ETFs."""
    if asset != "stock":
        raise ValueError("UNSUPPORTED_ASSET_TICK_GRID")
    return float(_step(_positive(price)))


def _legal_price(value: object) -> Decimal:
    price = _positive(value)
    # Reject SDK market/locked-book sentinels even without a known daily limit.
    if price >= 99999 or price % _step(price):
        raise ValueError("ILLEGAL_STOCK_PRICE_GRID_OR_SENTINEL")
    return price


def _shift(price: Decimal, ticks: int, direction: int) -> Decimal:
    current = price
    for _ in range(ticks):
        # At 50, the next lower legal price is 49.95, not 49.90.
        step = _step(current if direction > 0 else current - Decimal("0.00000001"))
        current += direction * step
    return _legal_price(current)


def shift_price(price: object, ticks: int, direction: int) -> float:
    """Move legal ordinary-stock ticks, re-evaluating bands at every step.

    ``direction`` is +1 upward or -1 downward; fractional/negative tick counts
    and non-grid input prices are rejected.
    """
    count = _number(ticks)
    if isinstance(ticks, bool) or count < 0 or count != count.to_integral_value():
        raise ValueError("NONNEGATIVE_INTEGER_TICKS_REQUIRED")
    if isinstance(direction, bool) or direction not in (-1, 1):
        raise ValueError("DIRECTION_MUST_BE_PLUS_OR_MINUS_ONE")
    return float(_shift(_legal_price(price), int(count), direction))


def _stamp(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return result.astimezone(TAIPEI)


def _quantity(value: object, *, allow_zero: bool = False) -> int:
    number = _number(value)
    if isinstance(value, bool) or number != number.to_integral_value():
        raise ValueError("INTEGER_QUANTITY_REQUIRED")
    quantity = int(number)
    if quantity < 0 or (not allow_zero and quantity == 0) or quantity % LOT_SIZE:
        raise ValueError("REGULAR_BOARD_LOT_QUANTITY_REQUIRED")
    return quantity


def _ceil(number: Decimal) -> int:
    return int(number.to_integral_value(rounding=ROUND_CEILING))


def cost(side: str, entry_price: object, exit_price: object, qty: int,
         config: Mapping[str, Any]) -> dict[str, Any]:
    """Estimate one completed round trip for the explicitly filled quantity.

    Fees are per entry/exit order VWAP, not per depth level.  SHORT/SELL tax is
    on entry sell value; LONG/BUY tax is on exit sell value.  Open-position PnL
    must be handled separately by the caller and is never invented here.
    """
    direction = str(side).upper()
    if direction not in {"SHORT", "SELL", "LONG", "BUY"}:
        raise ValueError("UNKNOWN_POSITION_SIDE")
    quantity = _quantity(qty, allow_zero=True)
    entry, exit_ = _positive(entry_price), _positive(exit_price)
    rate = _number(config.get("commission_rate", Decimal("0.000855")))
    minimum = _number(config.get("minimum_commission", 20))
    tax_rate = _number(config.get("sell_tax_rate", Decimal("0.0015")))
    if min(rate, minimum, tax_rate) < 0:
        raise ValueError("NEGATIVE_COST_SETTING")
    # A float VWAP may carry <1e-6 notional noise after averaging legal fills;
    # do not let that turn an exact integer fee/tax into an extra dollar.
    entry_value = (entry * quantity).quantize(Decimal("0.000001"))
    exit_value = (exit_ * quantity).quantize(Decimal("0.000001"))
    short = direction in {"SHORT", "SELL"}
    gross = (entry_value - exit_value) if short else (exit_value - entry_value)
    entry_fee = max(_ceil(minimum), _ceil(entry_value * rate)) if quantity else 0
    exit_fee = max(_ceil(minimum), _ceil(exit_value * rate)) if quantity else 0
    sell_value = entry_value if short else exit_value
    tax = _ceil(sell_value * tax_rate)
    commission = entry_fee + exit_fee
    return {"gross": float(gross), "commission": commission, "tax": tax,
            "net": float(gross - commission - tax), "entry_commission": entry_fee,
            "exit_commission": exit_fee, "commission_entry": entry_fee,
            "commission_exit": exit_fee, "sell_value": float(sell_value),
            "quantity": quantity, "cost_model": "ASSUMED_QUALIFIED_DAY_TRADE_ROUND_TRIP"}


def _levels(book: Mapping[str, Any], side: str) -> list[tuple[object, object]]:
    name = "bids" if side == "BUY" else "asks"
    levels = book.get(name)
    if levels is not None:
        result = []
        for level in list(levels)[:5]:
            if isinstance(level, Mapping):
                result.append((level.get("price"), level.get("quantity", level.get("volume", level.get("size")))))
            else:
                result.append((level[0], level[1] if len(level) > 1 else None))
        return result
    prefix = "buy" if side == "BUY" else "sell"
    alternate = "bid" if side == "BUY" else "ask"
    prices = book.get(prefix + "_prices", book.get(alternate + "_prices", []))
    volumes = book.get(prefix + "_volumes", book.get(alternate + "_volumes", book.get(alternate + "_sizes", [])))
    return list(zip(prices, volumes))[:5]


def execute_ioc(side_action: str, qty: int, signal_time: object,
                signal_quote: Mapping[str, Any], books: Sequence[Mapping[str, Any]],
                config: Mapping[str, Any], used_depth: MutableMapping | None = None
                ) -> dict[str, Any]:
    """One snapshot, 10% displayed-depth scenario with whole-lot partials.

    ``delay_seconds`` defaults to 1 (stress: 3), and
    ``extra_slippage_ticks`` defaults to 1.  ``unit_verified`` and
    ``eligibility_verified`` must both be true in default strict mode.
    ``used_depth`` retains the snapshot consumption keys and a separate private
    namespace of (symbol, opposing-side, price) capacity. New callbacks do not
    reset price capacity: only a positive observed depth increment restores
    10%, while a decline reduces unused capacity. A price leaving/re-entering
    the book or moving levels does not reset its state. This remains a
    conservative scenario, not queue evidence or order-idempotency.

    Books accept bids/asks [(price, raw_depth)] or buy_prices/buy_volumes and
    sell_prices/sell_volumes.  All times must be timezone-aware.  Missing book
    exchange time/daily limits remain explicit conditional evidence.
    """
    action = str(side_action).upper()
    if action not in {"BUY", "SELL"}:
        raise ValueError("UNKNOWN_SIDE_ACTION")
    quantity = _quantity(qty)
    signal_at = _stamp(signal_time)
    delay = _number(config.get("delay_seconds", config.get("latency_seconds", 1)))
    if delay < 0:
        raise ValueError("NEGATIVE_EXECUTION_DELAY")
    arrival = signal_at + timedelta(seconds=float(delay))
    wait = _number(config.get("quote_wait_seconds", 3))
    if wait < 0 or wait > 3:
        raise ValueError("QUOTE_WAIT_MUST_BE_BETWEEN_ZERO_AND_THREE_SECONDS")
    participation = _number(config.get("depth_participation", DEPTH_FRACTION))
    if participation != DEPTH_FRACTION:
        raise ValueError("FROZEN_DEPTH_PARTICIPATION_MUST_BE_0_1")
    cutoff = datetime.strptime(str(config.get("execution_cutoff", "13:25")), "%H:%M").time()
    if cutoff > datetime.min.replace(hour=13, minute=25).time():
        raise ValueError("EXECUTION_CUTOFF_CANNOT_ENTER_CLOSING_AUCTION")
    end = arrival + timedelta(seconds=float(wait))
    multiplier = _number(config.get("depth_unit_multiplier", 1000))
    if multiplier not in {Decimal(1), Decimal(1000)}:
        raise ValueError("SUPPORTED_DEPTH_UNIT_MULTIPLIERS_ARE_1_AND_1000")
    penalty_raw = _number(config.get("extra_slippage_ticks", 1))
    if penalty_raw < 0 or penalty_raw != penalty_raw.to_integral_value():
        raise ValueError("NONNEGATIVE_INTEGER_SLIPPAGE_REQUIRED")
    penalty = int(penalty_raw)
    symbol = str(config.get("symbol", signal_quote.get("symbol", signal_quote.get("stock_id", ""))))
    issues: list[str] = []

    def issue(code: str) -> None:
        if code not in issues:
            issues.append(code)

    result: dict[str, Any] = {
        "fills": [], "filled_qty": 0, "remaining_qty": quantity,
        "completion_time": None, "issues": issues, "arrival_time": arrival.isoformat(),
        "limit_price": None, "symbol": symbol, "depth_unit_multiplier": int(multiplier),
        "depth_fraction": float(DEPTH_FRACTION), "scenario_status": "CONDITIONAL_SCENARIO",
        "execution_model": "SINGLE_SNAPSHOT_10PCT_DEPTH_WHOLE_LOT_NO_QUEUE",
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
        "ioc_remainder_canceled_in_model": True,
    }
    strict = bool(config.get("strict_evidence", True))
    units_verified = config.get("unit_verified") is True
    eligibility_verified = config.get("eligibility_verified") is True
    if not units_verified:
        issue("UNKNOWN_UNITS" if strict else
              ("ASSUMED_LOTS_DEPTH_MULTIPLIER_1000" if multiplier == 1000 else "ASSUMED_SHARES_DEPTH_MULTIPLIER_1"))
    if not eligibility_verified:
        issue("UNKNOWN_ELIGIBILITY")
    if config.get("eligible") is False or signal_quote.get("eligible") is False:
        issue("EXPLICITLY_INELIGIBLE")
    if (strict and not (units_verified and eligibility_verified)) or "EXPLICITLY_INELIGIBLE" in issues:
        result["scenario_status"] = "BLOCKED_STRICT_EVIDENCE" if strict else "BLOCKED_INELIGIBLE"
        return result
    cover = bool(config.get("is_cover", False))
    reducing_exit = cover or bool(config.get("is_exit", False))
    if cover and action != "BUY":
        issue("INVALID_COVER_SIDE")
        return result
    if signal_quote.get("received_at") is not None:
        try:
            if _stamp(signal_quote["received_at"]) > signal_at:
                issue("SIGNAL_QUOTE_FUTURE_RECEIPT")
                return result
        except (ValueError, TypeError):
            issue("SIGNAL_QUOTE_INVALID_RECEIPT")
            return result
    limit: Decimal | None = None
    if not reducing_exit:
        signal_levels = _levels(signal_quote, "BUY" if action == "SELL" else "SELL")
        try:
            best = _legal_price(signal_levels[0][0])
            limit = _shift(best, 2, -1 if action == "SELL" else 1)
        except (ValueError, IndexError, TypeError):
            issue("INVALID_SIGNAL_QUOTE")
            return result
        result["limit_price"] = float(limit)
    candidates: list[tuple[datetime, int, Mapping[str, Any]]] = []
    for order, book in enumerate(books):
        if book.get("kind", "book") != "book":
            issue("NON_BOOK_EVENT_EXCLUDED")
            continue
        if book.get("out_of_order"):
            issue("OUT_OF_ORDER_BOOK_EXCLUDED")
            continue
        try:
            received = _stamp(book["received_at"])
        except (ValueError, TypeError, KeyError):
            issue("BOOK_RECEIPT_TIME_INVALID")
            continue
        # Even a zero-delay scenario must not reuse the decision's old snapshot.
        if received <= signal_at or received < arrival or received > end:
            continue
        if received.date() != signal_at.date():
            issue("BOOK_DATE_MISMATCH")
            continue
        if received.hour < 9:
            issue("PREOPEN_BOOK_EXCLUDED")
            continue
        if received.time().replace(tzinfo=None) >= cutoff:
            issue("CLOSING_AUCTION_EXCLUDED")
            continue
        book_symbol = str(book.get("symbol", book.get("stock_id", symbol)))
        if symbol and book_symbol != symbol:
            issue("BOOK_SYMBOL_MISMATCH")
            continue
        try:
            bids, asks = _levels(book, "BUY"), _levels(book, "SELL")
            if bids and asks and _positive(bids[0][0]) > _positive(asks[0][0]):
                issue("CROSSED_BOOK_EXCLUDED")
                continue
        except (ValueError, TypeError, IndexError):
            # Single-sided limit books can still reduce existing exposure;
            # unusable execution-side levels will be rejected below.
            pass
        exchange = book.get("exchange_time", book.get("event_timestamp_exchange"))
        if exchange is not None:
            try:
                age = (received - _stamp(exchange)).total_seconds()
            except (ValueError, TypeError):
                issue("BOOK_EXCHANGE_TIME_INVALID")
                continue
            if age > 10:
                issue("BOOK_EXCHANGE_TIME_STALE")
                continue
            if age < -0.5:
                issue("BOOK_EXCHANGE_TIME_FUTURE")
                continue
        candidates.append((received, order, book))
    if not candidates:
        issue("NO_VALID_BOOK_AFTER_ARRIVAL")
        return result
    received, _, chosen = min(candidates, key=lambda item: (item[0], item[1]))
    if not symbol:
        symbol = str(chosen.get("symbol", chosen.get("stock_id", "")))
        result["symbol"] = symbol
    result["book_time"] = received.isoformat()
    if chosen.get("exchange_time", chosen.get("event_timestamp_exchange")) is None:
        issue("BOOK_EXCHANGE_TIME_UNKNOWN")
    if chosen.get("receipt_order_ambiguous"):
        issue("BOOK_RECEIPT_ORDER_AMBIGUOUS")
    lower_raw = chosen.get("lower_limit", chosen.get("limit_down"))
    upper_raw = chosen.get("upper_limit", chosen.get("limit_up"))
    try:
        lower = None if lower_raw is None else _legal_price(lower_raw)
        upper = None if upper_raw is None else _legal_price(upper_raw)
        if lower is not None and upper is not None and lower > upper:
            raise ValueError("REVERSED_DAILY_LIMITS")
    except ValueError:
        issue("INVALID_DAILY_PRICE_LIMITS")
        return result
    if lower is None or upper is None:
        issue("DAILY_PRICE_LIMITS_UNKNOWN")
    opposite = "BUY" if action == "SELL" else "SELL"
    levels = _levels(chosen, opposite)
    consumed = used_depth if used_depth is not None else {}
    price_states = consumed.setdefault(_PRICE_DEPTH_NAMESPACE, {})
    if not isinstance(price_states, dict):
        raise ValueError("INVALID_PRIVATE_PRICE_DEPTH_STATE")
    sequence = chosen.get("book_sequence", chosen.get("sequence", chosen.get("sequence_id", chosen.get("serial_no"))))
    if chosen.get("sequence_known") is False:
        sequence = None
        issue("BOOK_SEQUENCE_SYNTHETIC_NOT_VENDOR_ID")
    identity = str(sequence) if sequence is not None else received.isoformat()
    remaining = quantity
    seen_prices: set[Decimal] = set()
    for level_index, (raw_price, raw_depth) in enumerate(levels):
        try:
            price = _legal_price(raw_price)
            depth = _number(raw_depth)
            if depth < 0 or depth != depth.to_integral_value():
                raise ValueError("INVALID_RAW_DEPTH")
            adjusted = _shift(price, penalty, -1 if action == "SELL" else 1)
        except (ValueError, TypeError):
            issue("INVALID_BOOK_PRICE_OR_DEPTH")
            continue
        if price in seen_prices:
            issue("DUPLICATE_PRICE_LEVEL_EXCLUDED")
            continue
        seen_prices.add(price)
        if ((lower is not None and min(price, adjusted) < lower) or
                (upper is not None and max(price, adjusted) > upper)):
            issue("BOOK_PRICE_OUTSIDE_DAILY_LIMITS")
            continue
        if limit is not None and ((action == "SELL" and adjusted < limit) or
                                  (action == "BUY" and adjusted > limit)):
            issue("ADVERSE_PRICE_OUTSIDE_IOC_LIMIT")
            continue
        capacity = int((depth * multiplier * DEPTH_FRACTION / LOT_SIZE)
                       .to_integral_value(rounding=ROUND_FLOOR)) * LOT_SIZE
        # Price identity is independent of callback sequence and level index.
        # Canonicalize Decimal exponents so 100 and 100.0 share one state.
        price_key = (symbol, opposite, format(price.normalize(), "f"))
        displayed = int(depth * multiplier)
        state = price_states.get(price_key)
        if state is None:
            state = {"last_displayed_shares": displayed,
                     "available_shares": Decimal(displayed) * DEPTH_FRACTION,
                     "depth_unit_multiplier": int(multiplier),
                     "last_snapshot_identity": identity,
                     "last_received_at": received}
            price_states[price_key] = state
        else:
            if state["depth_unit_multiplier"] != int(multiplier):
                issue("PRICE_DEPTH_UNIT_ASSUMPTION_CHANGED")
                continue
            if received < state["last_received_at"]:
                issue("PRICE_DEPTH_RECEIPT_REGRESSION")
                continue
            delta = displayed - state["last_displayed_shares"]
            if identity == state["last_snapshot_identity"] and delta:
                issue("DUPLICATE_SNAPSHOT_DEPTH_CONFLICT")
                continue
            state["available_shares"] = max(Decimal(0), min(
                Decimal(displayed) * DEPTH_FRACTION,
                state["available_shares"] + Decimal(delta) * DEPTH_FRACTION))
            state["last_displayed_shares"] = displayed
            state["last_snapshot_identity"] = identity
            state["last_received_at"] = received
        key = (symbol, identity, opposite, level_index)
        already = _quantity(consumed.get(key, 0), allow_zero=True)
        price_available = int((state["available_shares"] / LOT_SIZE)
                              .to_integral_value(rounding=ROUND_FLOOR)) * LOT_SIZE
        available = min(max(0, capacity - already), price_available)
        take = min(remaining, available)
        if already:
            issue("BOOK_DEPTH_ALREADY_CONSUMED")
        if take <= 0:
            if price_available < LOT_SIZE:
                issue("PRICE_DEPTH_NO_NEW_WHOLE_LOT_CAPACITY")
            continue
        result["fills"].append({"time": received.isoformat(), "price": float(adjusted),
                                "quantity": take, "book_sequence": identity,
                                "level": level_index + 1})
        consumed[key] = already + take
        state["available_shares"] -= take
        remaining -= take
        if remaining == 0:
            break
    result["filled_qty"] = quantity - remaining
    result["remaining_qty"] = remaining
    if remaining == 0:
        result["completion_time"] = received.isoformat()
    elif result["filled_qty"]:
        issue("PARTIAL_IOC_REMAINDER_CANCELED_IN_MODEL")
    else:
        issue("NO_EXECUTABLE_WHOLE_LOT_DEPTH")
    return result
