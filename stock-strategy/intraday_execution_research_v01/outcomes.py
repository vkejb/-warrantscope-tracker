from __future__ import annotations


OUTCOME_SCHEMA = {
    "entry_proxy": "first valid tradable quote/trade strictly after snapshot",
    "same_day_close_return": None,
    "same_day_mfe": None,
    "same_day_mae": None,
    "next_day_open_return": None,
    "day1_day5_mfe": None,
    "day1_day5_mae": None,
    "day1_day10_mfe": None,
    "day1_day10_mae": None,
    "plus8_before_minus5": None,
    "plus5_before_minus3": None,
    "downside_first": None,
}


def blank_outcome(stock_code: str, snapshot_time: str, entry_proxy: dict | None) -> dict:
    return {
        "stock_code": stock_code,
        "snapshot_time": snapshot_time,
        **OUTCOME_SCHEMA,
        "entry_proxy": entry_proxy,
        "status": "NOT_EVALUATED_NO_REAL_INTRADAY_HISTORY",
    }
