"""Research-only next-regular-session observations and official limit flags."""

from __future__ import annotations

from math import isclose


def next_session_bar(stock, signal_date: str, calendar: list[str]):
    position = calendar.index(signal_date)
    if position + 1 >= len(calendar):
        return None
    target = calendar[position + 1]
    return next((bar for bar in stock.bars if bar.date == target), None)


def evaluate(signal_close: float, bar, upper_limit: float) -> dict:
    if upper_limit <= 0 or signal_close <= 0 or bar.open <= 0:
        raise ValueError("invalid outcome input")
    if bar.high > upper_limit + 0.00001 or bar.close > upper_limit + 0.00001:
        raise ValueError(f"OHLC exceeds official upper limit: {bar.code} {bar.date}")
    opening = bar.open / signal_close - 1
    close = bar.close / signal_close - 1
    from_open = bar.close / bar.open - 1
    max_up = bar.high / bar.open - 1
    max_down = bar.low / bar.open - 1
    hit_close = isclose(bar.close, upper_limit, abs_tol=0.00001)
    hit_high = isclose(bar.high, upper_limit, abs_tol=0.00001)
    if hit_close:
        if isclose(bar.open, upper_limit, abs_tol=0.00001):
            opening_bucket = "OPEN_AT_LIMIT"
        elif opening >= .08:
            opening_bucket = "OPEN_GE_8"
        elif opening >= .05:
            opening_bucket = "OPEN_5_TO_8"
        elif opening >= .03:
            opening_bucket = "OPEN_3_TO_5"
        else:
            opening_bucket = "OPEN_LT_3"
    else:
        opening_bucket = "NOT_CLOSE_LIMIT"
    return {
        "outcome_date": bar.date,
        "t1_open": bar.open, "t1_high": bar.high, "t1_low": bar.low, "t1_close": bar.close,
        "official_upper_limit": upper_limit,
        "t1_open_return": opening, "t1_close_return": close,
        "open_to_close_return": from_open,
        "max_upside_from_open": max_up, "max_drawdown_from_open": max_down,
        "t1_close_at_upper_limit": hit_close,
        "t1_high_touched_upper_limit": hit_high,
        "t1_return_ge_3": close >= .03, "t1_return_ge_5": close >= .05,
        "t1_return_ge_7": close >= .07, "t1_return_ge_8": close >= .08,
        "t1_positive": close > 0,
        "capturable_3": opening < .05 and max_up >= .03,
        "capturable_5": opening < .05 and max_up >= .05,
        "capturable_close_5": opening < .05 and from_open >= .05,
        "close_limit_open_bucket": opening_bucket,
    }


def rank_bucket(rank: int) -> str:
    if 1 <= rank <= 5:
        return "RANK_1_5"
    if rank <= 10:
        return "RANK_6_10"
    if rank <= 20:
        return "RANK_11_20"
    if rank <= 30:
        return "RANK_21_30"
    raise ValueError("rank outside frozen Top30")


def episode(previous_streak: int) -> tuple[str, int]:
    length = previous_streak + 1
    if length <= 0:
        raise ValueError("previous streak cannot be negative")
    if length == 1:
        return "NEW_ENTRY", length
    if length == 2:
        return "CONTINUING_DAY_2", length
    return "CONTINUING_DAY_3_PLUS", length
