from __future__ import annotations

import math

from multi_setup_study_v01.config import CFG as MULTI_CFG
from multi_setup_study_v01.outcomes import evaluate_unified_outcome

from .config import CFG, Config
from .market_data_provider import MarketDataSnapshot, normalize_date


def _same_float(left, right, tolerance: float = 1e-12) -> bool:
    return math.isclose(float(left), float(right), rel_tol=tolerance, abs_tol=tolerance)


def _base_candidate(signal: dict[str, str], as_of: str, cfg: Config) -> dict:
    return {
        "schema_version": cfg.schema_version,
        "signal_key": signal["signal_key"],
        "signal_payload_hash": signal["signal_payload_hash"],
        "stock_id": signal["stock_id"],
        "signal_date": signal["signal_date"],
        "setup": signal["setup"],
        "observed_through_date": as_of,
        "available_forward_sessions": 0,
        "entry_date": None,
        "entry_open_proxy": None,
        "entry_gap": None,
        "day1_close_return": None,
        "day3_close_return": None,
        "day5_close_return": None,
        "day10_close_return": None,
        "mfe_close_5": None,
        "mfe_close_10": None,
        "mae_close_5": None,
        "mae_close_10": None,
        "first_target_day": None,
        "first_stop_day": None,
        "plus8_before_minus5": None,
        "primary_success": None,
        "path_result": None,
        "barrier_status": "UNRESOLVED",
        "outcome_status": "PENDING",
        "outcome_reason": "NO_FORWARD_SESSION_YET",
        "forward_window_complete": False,
        "is_final": False,
        "execution_mode": cfg.execution_mode,
        "is_actual_order": False,
        "is_actual_fill": False,
    }


def _censor(candidate: dict, reason: str) -> dict:
    if candidate["barrier_status"] == "UNRESOLVED":
        candidate["barrier_status"] = "CENSORED"
    candidate.update(
        {
            "outcome_status": "CENSORED",
            "outcome_reason": reason,
            "forward_window_complete": False,
            "is_final": True,
        }
    )
    return candidate


def _first_day(values: list[float], threshold: float, above: bool) -> int | None:
    for day, value in enumerate(values, 1):
        if above and value >= threshold - 1e-12:
            return day
        if not above and value <= threshold + 1e-12:
            return day
    return None


def _assert_complete_parity(stock, index: int, candidate: dict) -> None:
    shared = evaluate_unified_outcome(stock, index, MULTI_CFG)
    if shared["outcome_status"] != "EVALUABLE":
        raise RuntimeError(
            f"shared outcome unexpectedly censored: {shared['outcome_reason']}"
        )
    exact = {
        "entry_date": "entry_date",
        "primary_success": "primary_success",
        "path_result": "path_result",
        "first_target_day": "first_target_day",
        "first_stop_day": "first_stop_day",
    }
    for ours, theirs in exact.items():
        if candidate[ours] != shared[theirs]:
            raise RuntimeError(f"outcome parity failed for {ours}")
    numeric = {
        "entry_open_proxy": "entry_open_proxy",
        "entry_gap": "entry_gap",
        "day1_close_return": "day1_close_return",
        "day3_close_return": "day3_close_return",
        "day5_close_return": "day5_close_return",
        "day10_close_return": "day10_close_return",
        "mfe_close_5": "mfe_5d",
        "mfe_close_10": "mfe_10d",
        "mae_close_5": "mae_5d",
        "mae_close_10": "mae_10d",
    }
    for ours, theirs in numeric.items():
        if not _same_float(candidate[ours], shared[theirs]):
            raise RuntimeError(f"outcome parity failed for {ours}")


def evaluate_signal_progress(
    snapshot: MarketDataSnapshot,
    signal: dict[str, str],
    as_of_date: str,
    cfg: Config = CFG,
) -> dict:
    """Create one monotonic outcome state without touching signal storage."""

    as_of = normalize_date(as_of_date)
    candidate = _base_candidate(signal, as_of, cfg)
    if signal["setup"] != cfg.setup:
        raise RuntimeError(f"unexpected setup in signal ledger: {signal['setup']}")
    if signal["signal_date"] > as_of:
        raise RuntimeError("cannot update an outcome before its signal date")
    stocks = {stock.code: stock for stock in snapshot.prepared_stocks}
    stock = stocks.get(signal["stock_id"])
    if stock is None:
        raise RuntimeError(f"signal stock missing from outcome data: {signal['stock_id']}")
    local_by_date = {bar.date: index for index, bar in enumerate(stock.bars)}
    signal_index = local_by_date.get(signal["signal_date"])
    if signal_index is None:
        raise RuntimeError("signal date disappeared from the source data")
    signal_bar = stock.bars[signal_index]
    if not _same_float(signal_bar.close, signal["signal_close"]):
        raise RuntimeError("historical signal Close was revised")

    calendar = snapshot.benchmark.calendar
    try:
        calendar_index = calendar.index(signal["signal_date"])
    except ValueError as exc:
        raise RuntimeError("signal date disappeared from the market calendar") from exc
    known_sessions = [
        day
        for day in calendar[calendar_index + 1 : calendar_index + 1 + cfg.primary_horizon]
        if day <= as_of
    ]
    if not known_sessions:
        return candidate

    window = []
    censor_reason: str | None = None
    for forward_day, day in enumerate(known_sessions, 1):
        local_index = local_by_date.get(day)
        if local_index is None:
            censor_reason = f"MISSING_FORWARD_SESSION_DAY_{forward_day}"
            break
        if forward_day == 1:
            first_bar = stock.bars[local_index]
            candidate.update(
                {
                    "entry_date": first_bar.date,
                    "entry_open_proxy": first_bar.open,
                    "entry_gap": first_bar.open / signal_bar.close - 1.0,
                }
            )
        if (
            stock.calendar_indices[local_index]
            != stock.calendar_indices[signal_index] + forward_day
        ):
            censor_reason = f"NONCONTIGUOUS_FORWARD_SESSION_DAY_{forward_day}"
            break
        if stock.segment_ids[local_index] != stock.segment_ids[signal_index]:
            censor_reason = f"FORWARD_DISCONTINUITY_DAY_{forward_day}"
            break
        if forward_day == 1 and stock.bars[local_index].volume <= 0:
            censor_reason = "T_PLUS_1_ZERO_VOLUME"
            break
        window.append(stock.bars[local_index])

    if not window:
        return _censor(candidate, censor_reason or "NO_FORWARD_SESSION_YET")

    entry = window[0]
    returns = [bar.close / entry.open - 1.0 for bar in window]
    count = len(returns)
    candidate.update(
        {
            "available_forward_sessions": count,
            "entry_date": entry.date,
            "entry_open_proxy": entry.open,
            "entry_gap": entry.open / signal_bar.close - 1.0,
            "day1_close_return": returns[0],
            "outcome_status": "PARTIAL",
            "outcome_reason": "AWAITING_FORWARD_SESSIONS",
        }
    )
    if count >= 3:
        candidate["day3_close_return"] = returns[2]
    if count >= 5:
        candidate.update(
            {
                "day5_close_return": returns[4],
                "mfe_close_5": max(returns[:5]),
                "mae_close_5": min(returns[:5]),
            }
        )

    target_day = _first_day(returns, cfg.primary_target, True)
    stop_day = _first_day(returns, cfg.primary_stop, False)
    candidate["first_target_day"] = target_day
    candidate["first_stop_day"] = stop_day
    if target_day is not None and (stop_day is None or target_day < stop_day):
        candidate.update(
            {
                "primary_success": True,
                "plus8_before_minus5": True,
                "path_result": "TARGET_BEFORE_STOP",
                "barrier_status": "TARGET_BEFORE_STOP",
            }
        )
    elif stop_day is not None and (target_day is None or stop_day < target_day):
        candidate.update(
            {
                "primary_success": False,
                "plus8_before_minus5": False,
                "path_result": "STOP_BEFORE_TARGET",
                "barrier_status": "STOP_BEFORE_TARGET",
            }
        )

    if censor_reason is not None:
        return _censor(candidate, censor_reason)

    if count == cfg.primary_horizon:
        candidate.update(
            {
                "day10_close_return": returns[9],
                "mfe_close_10": max(returns),
                "mae_close_10": min(returns),
                "outcome_status": "COMPLETE",
                "outcome_reason": "COMPLETE_FORWARD_WINDOW",
                "forward_window_complete": True,
                "is_final": True,
            }
        )
        if candidate["primary_success"] is None:
            candidate.update(
                {
                    "primary_success": False,
                    "plus8_before_minus5": False,
                    "path_result": "DAY10_TIMEOUT",
                    "barrier_status": "DAY10_TIMEOUT",
                }
            )
        _assert_complete_parity(stock, signal_index, candidate)
    return candidate


def build_outcome_candidates(
    snapshot: MarketDataSnapshot,
    signals: list[dict[str, str]],
    as_of_date: str,
    cfg: Config = CFG,
) -> list[dict]:
    as_of = normalize_date(as_of_date)
    if snapshot.data_through_date != as_of:
        raise RuntimeError("outcome snapshot must be loaded exactly through as-of")
    if as_of not in snapshot.benchmark.calendar:
        raise RuntimeError("outcome as-of is absent from the market calendar")
    if snapshot.benchmark.calendar and snapshot.benchmark.calendar[-1] > as_of:
        raise RuntimeError("outcome provider leaked future market sessions")
    if any(
        bar.date > as_of
        for stock in snapshot.prepared_stocks
        for bar in stock.bars
    ):
        raise RuntimeError("outcome provider leaked future stock bars")
    return [evaluate_signal_progress(snapshot, row, as_of, cfg) for row in signals]
