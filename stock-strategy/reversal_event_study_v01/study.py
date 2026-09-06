from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
import math

from surge_event_study_v01.models import PreparedBenchmark, PreparedStock

from .config import CFG, Config
from .models import PatternObservation, ReversalOutcome


PATTERNS = ("V_REVERSAL", "N_RETEST")


def _mean(prefix: list[float], start: int, end: int) -> float:
    if end <= start:
        raise ValueError("mean window must be non-empty")
    return (prefix[end] - prefix[start]) / (end - start)


def _latest_min_close(stock: PreparedStock, start: int, end: int) -> int:
    """Return the latest index holding the minimum Close in [start, end)."""

    minimum = min(stock.bars[index].close for index in range(start, end))
    return max(
        index for index in range(start, end) if stock.bars[index].close == minimum
    )


def _close_location(stock: PreparedStock, index: int) -> float:
    item = stock.bars[index]
    if item.high <= item.low:
        return 0.5
    return (item.close - item.low) / (item.high - item.low)


def _lower_wick_fraction(stock: PreparedStock, index: int) -> float:
    item = stock.bars[index]
    if item.high <= item.low:
        return 0.0
    return (min(item.open, item.close) - item.low) / (item.high - item.low)


def _history_eligible(stock: PreparedStock, index: int, cfg: Config) -> bool:
    if index < cfg.feature_lookback_sessions:
        return False
    calendar_index = stock.calendar_indices[index]
    start = index - cfg.feature_lookback_sessions
    if stock.calendar_indices[start] != calendar_index - cfg.feature_lookback_sessions:
        return False
    if stock.segment_ids[start] != stock.segment_ids[index]:
        return False
    current = stock.bars[index]
    if current.volume <= 0 or not (cfg.minimum_price <= current.close <= cfg.maximum_price):
        return False
    average_volume = _mean(stock.prefix_volume, index - 20, index)
    average_turnover = _mean(stock.prefix_turnover_proxy, index - 20, index)
    return bool(
        average_volume >= cfg.minimum_average_volume_20
        and average_turnover >= cfg.minimum_average_turnover_proxy_20
    )


def recent_pivot_index(stock: PreparedStock, index: int, cfg: Config = CFG) -> int:
    return _latest_min_close(stock, index - cfg.recent_pivot_window, index)


def _confirmation_geometry(
    stock: PreparedStock, index: int, pivot: int, cfg: Config
) -> dict[str, float] | None:
    current = stock.bars[index]
    rebound = current.close / stock.bars[pivot].close - 1.0
    location = _close_location(stock, index)
    if not (
        current.close > stock.bars[index - 1].high
        and cfg.confirmation_min_rebound <= rebound <= cfg.confirmation_max_rebound
        and location >= cfg.confirmation_min_close_location
    ):
        return None
    return {
        "confirmation_rebound": rebound,
        "confirmation_close_location": location,
        "confirmation_break_vs_prior_high": current.close
        / stock.bars[index - 1].high
        - 1.0,
    }


def detect_v_pattern(
    stock: PreparedStock, index: int, cfg: Config = CFG
) -> dict[str, float | int | str] | None:
    """Past-only capitulation/reversal confirmation; no future low is consulted."""

    if index < cfg.feature_lookback_sessions:
        return None
    pivot = recent_pivot_index(stock, index, cfg)
    pivot_close = stock.bars[pivot].close
    prior_high = max(item.close for item in stock.bars[pivot - 20 : pivot])
    drawdown = pivot_close / prior_high - 1.0
    return_5 = pivot_close / stock.bars[pivot - 5].close - 1.0
    confirmation = _confirmation_geometry(stock, index, pivot, cfg)
    if (
        drawdown > cfg.pivot_drawdown_threshold
        or return_5 > cfg.pivot_return_5_threshold
        or confirmation is None
    ):
        return None
    return {
        "pivot_index": pivot,
        "pivot_date": stock.bars[pivot].date,
        "pivot_drawdown_20": drawdown,
        "pivot_return_5": return_5,
        **confirmation,
    }


def _is_five_bar_local_minimum(stock: PreparedStock, index: int) -> bool:
    value = stock.bars[index].close
    return value <= min(item.close for item in stock.bars[index - 2 : index + 3])


def detect_n_pattern(
    stock: PreparedStock, index: int, cfg: Config = CFG
) -> dict[str, float | int | str] | None:
    """Past-only early N/double-bottom retest followed by one-day confirmation."""

    if index < cfg.feature_lookback_sessions:
        return None
    second = recent_pivot_index(stock, index, cfg)
    search_start = second - cfg.n_search_sessions
    search_end = second - cfg.n_minimum_pivot_separation
    candidates = [
        candidate
        for candidate in range(search_start, search_end + 1)
        if _is_five_bar_local_minimum(stock, candidate)
    ]
    if not candidates:
        return None
    # Deliberately take the latest local low; do not pick the one that looks
    # most similar to the second bottom.
    first = candidates[-1]
    first_close = stock.bars[first].close
    second_close = stock.bars[second].close
    first_prior_high = max(item.close for item in stock.bars[first - 20 : first])
    first_drawdown = first_close / first_prior_high - 1.0
    bottom_difference = second_close / first_close - 1.0
    intervening = stock.bars[first + 2 : second - 1]
    if not intervening:
        return None
    intervening_peak = max(item.close for item in intervening)
    bounce = intervening_peak / min(first_close, second_close) - 1.0
    confirmation = _confirmation_geometry(stock, index, second, cfg)
    if (
        first_drawdown > cfg.pivot_drawdown_threshold
        or not (-cfg.n_bottom_tolerance <= bottom_difference <= cfg.n_bottom_tolerance)
        or bounce < cfg.n_minimum_intervening_bounce
        or confirmation is None
    ):
        return None
    return {
        "pivot_index": second,
        "pivot_date": stock.bars[second].date,
        "first_pivot_index": first,
        "first_pivot_date": stock.bars[first].date,
        "first_pivot_drawdown_20": first_drawdown,
        "bottom_difference": bottom_difference,
        "pivot_separation_sessions": second - first,
        "intervening_bounce": bounce,
        **confirmation,
    }


def _benchmark_return(
    benchmark: PreparedBenchmark, end_index: int, sessions: int
) -> float | None:
    start_index = end_index - sessions
    if start_index < 0:
        return None
    start = benchmark.normalized_closes[start_index]
    end = benchmark.normalized_closes[end_index]
    start_segment = benchmark.segment_ids[start_index]
    end_segment = benchmark.segment_ids[end_index]
    if (
        start is None
        or end is None
        or start_segment is None
        or end_segment is None
        or start_segment != end_segment
    ):
        return None
    return end / start - 1.0


def _feature_values(
    stock: PreparedStock,
    index: int,
    pivot: int,
    benchmark: PreparedBenchmark,
) -> dict[str, float] | None:
    current = stock.bars[index]
    pivot_bar = stock.bars[pivot]
    pivot_atr5 = _mean(stock.prefix_true_range_ratio, pivot - 4, pivot + 1)
    pivot_atr20 = _mean(stock.prefix_true_range_ratio, pivot - 19, pivot + 1)
    prior5_range = _mean(stock.prefix_true_range_ratio, index - 5, index)
    pre_pivot_range = _mean(stock.prefix_true_range_ratio, pivot - 5, pivot)
    post_pivot_range = _mean(stock.prefix_true_range_ratio, pivot + 1, index + 1)
    pivot_prior_volume = _mean(stock.prefix_volume, pivot - 20, pivot)
    signal_prior_volume = _mean(stock.prefix_volume, index - 20, index)
    post_pivot_volume = _mean(stock.prefix_volume, pivot + 1, index + 1)
    sma20 = _mean(stock.prefix_close, index - 19, index + 1)
    stock_return_5 = current.close / stock.bars[index - 5].close - 1.0
    benchmark_return_5 = _benchmark_return(benchmark, stock.calendar_indices[index], 5)
    if (
        pivot_atr20 <= 0
        or prior5_range <= 0
        or pre_pivot_range <= 0
        or pivot_prior_volume <= 0
        or signal_prior_volume <= 0
        or pivot_bar.volume <= 0
        or benchmark_return_5 is None
    ):
        return None
    values = {
        "pivot_drawdown_20": pivot_bar.close
        / max(item.close for item in stock.bars[pivot - 20 : pivot])
        - 1.0,
        "pivot_return_5": pivot_bar.close / stock.bars[pivot - 5].close - 1.0,
        "close_vs_sma20": current.close / sma20 - 1.0,
        "pivot_atr5_vs_atr20": pivot_atr5 / pivot_atr20,
        "signal_tr_vs_prior5": stock.true_range_ratios[index] / prior5_range,
        "post_vs_pre_pivot_range": post_pivot_range / pre_pivot_range,
        "pivot_volume_ratio_20": pivot_bar.volume / pivot_prior_volume,
        "signal_volume_ratio_20": current.volume / signal_prior_volume,
        "post_pivot_volume_vs_pivot": post_pivot_volume / pivot_bar.volume,
        "signal_return_1": current.close / stock.bars[index - 1].close - 1.0,
        "signal_lower_wick_fraction": _lower_wick_fraction(stock, index),
        "rs_5_vs_0050": (1.0 + stock_return_5) / (1.0 + benchmark_return_5) - 1.0,
    }
    if any(not math.isfinite(value) for value in values.values()):
        return None
    return values


def build_pattern_observation(
    stock: PreparedStock,
    index: int,
    benchmark: PreparedBenchmark,
    cfg: Config = CFG,
) -> PatternObservation | None:
    """Return at most one observation. N takes priority over a same-day V overlap."""

    if not _history_eligible(stock, index, cfg):
        return None
    v = detect_v_pattern(stock, index, cfg)
    n = detect_n_pattern(stock, index, cfg)
    geometry = n if n is not None else v
    if geometry is None:
        return None
    pattern = "N_RETEST" if n is not None else "V_REVERSAL"
    pivot = int(geometry["pivot_index"])
    features = _feature_values(stock, index, pivot, benchmark)
    if features is None:
        return None
    current = stock.bars[index]
    return PatternObservation(
        signal_date=current.date,
        calendar_index=stock.calendar_indices[index],
        code=stock.code,
        name=current.name,
        pattern=pattern,
        signal_close=current.close,
        average_volume_20=_mean(stock.prefix_volume, index - 20, index),
        average_turnover_proxy_20=_mean(
            stock.prefix_turnover_proxy, index - 20, index
        ),
        pivot_date=str(geometry["pivot_date"]),
        second_pivot_date=str(geometry["pivot_date"]) if n is not None else None,
        raw_overlap=v is not None and n is not None,
        geometry=geometry,
        features=features,
    )


def evaluate_outcome(
    stock: PreparedStock, index: int, cfg: Config = CFG
) -> ReversalOutcome:
    """Attach the fixed forward label after signal formation and ranking."""

    if index + 1 >= len(stock.bars):
        return ReversalOutcome("CENSORED", "MISSING_T_PLUS_1_BAR")
    entry = stock.bars[index + 1]
    if stock.calendar_indices[index + 1] != stock.calendar_indices[index] + 1:
        return ReversalOutcome("CENSORED", "MISSING_T_PLUS_1_BAR")
    if entry.volume <= 0:
        return ReversalOutcome("CENSORED", "T_PLUS_1_ZERO_VOLUME")
    if stock.segment_ids[index + 1] != stock.segment_ids[index]:
        return ReversalOutcome("CENSORED", "T_PLUS_1_DISCONTINUITY")
    end = index + cfg.primary_horizon
    if end >= len(stock.bars):
        return ReversalOutcome(
            "CENSORED",
            "INCOMPLETE_FORWARD_WINDOW",
            entry_date=entry.date,
            entry_open=entry.open,
            entry_gap=entry.open / stock.bars[index].close - 1.0,
        )
    if stock.calendar_indices[end] != stock.calendar_indices[index] + cfg.primary_horizon:
        return ReversalOutcome(
            "CENSORED",
            "MISSING_FORWARD_SESSION",
            entry_date=entry.date,
            entry_open=entry.open,
            entry_gap=entry.open / stock.bars[index].close - 1.0,
        )
    if stock.segment_ids[end] != stock.segment_ids[index]:
        return ReversalOutcome(
            "CENSORED",
            "FORWARD_DISCONTINUITY",
            entry_date=entry.date,
            entry_open=entry.open,
            entry_gap=entry.open / stock.bars[index].close - 1.0,
        )
    window = stock.bars[index + 1 : end + 1]
    returns = [item.close / entry.open - 1.0 for item in window]
    first_target = next(
        (day for day, value in enumerate(returns, 1) if value >= cfg.primary_target),
        None,
    )
    first_stop = next(
        (day for day, value in enumerate(returns, 1) if value <= cfg.primary_stop),
        None,
    )
    success = first_target is not None and (
        first_stop is None or first_target < first_stop
    )
    if success:
        result = "TARGET_BEFORE_STOP"
    elif first_stop is not None and (first_target is None or first_stop < first_target):
        result = "STOP_BEFORE_TARGET"
    else:
        result = "DAY10_TIMEOUT"
    return ReversalOutcome(
        status="EVALUABLE",
        reason="COMPLETE_FORWARD_WINDOW",
        entry_date=entry.date,
        entry_open=entry.open,
        entry_gap=entry.open / stock.bars[index].close - 1.0,
        primary_success=success,
        path_result=result,
        first_target_day=first_target,
        first_stop_day=first_stop,
        day10_close_return=returns[-1],
        mfe_close_10=max(returns),
        mae_close_10=min(returns),
    )


def scan_period(
    stocks: list[PreparedStock],
    benchmark: PreparedBenchmark,
    start_date: str,
    end_date: str,
    period: str,
    cfg: Config = CFG,
    allowed_patterns: set[str] | None = None,
) -> dict:
    """Scan every eligible stock-day; forward availability never affects signals."""

    signal_rows: list[dict] = []
    outcome_reasons: dict[str, int] = defaultdict(int)
    raw_pattern_counts: dict[str, int] = defaultdict(int)
    maximum_signal_date_read: str | None = None
    seeded_keys: set[tuple[str, str]] = set()
    counts_by_date: dict[tuple[str, str], int] = defaultdict(int)
    for stock in stocks:
        dates = [item.date for item in stock.bars]
        start = max(cfg.feature_lookback_sessions, bisect_left(dates, start_date))
        stop = bisect_right(dates, end_date)
        # Replay the complete causal signal history for this stock so a raw
        # signal just before a period boundary cannot incorrectly replace an
        # earlier accepted signal in the cooldown state.
        last_signal_by_pattern: dict[str, int] = {}
        for index in range(cfg.feature_lookback_sessions, stop):
            day = stock.bars[index].date
            observation = build_pattern_observation(stock, index, benchmark, cfg)
            if observation is None:
                continue
            if allowed_patterns is not None and observation.pattern not in allowed_patterns:
                continue
            previous = last_signal_by_pattern.get(observation.pattern)
            included = previous is None or (
                observation.calendar_index - previous >= cfg.causal_cooldown_sessions
            )
            if included:
                last_signal_by_pattern[observation.pattern] = observation.calendar_index
            if index < start:
                if included:
                    seeded_keys.add((observation.code, observation.pattern))
                continue
            maximum_signal_date_read = max(maximum_signal_date_read or day, day)
            outcome = evaluate_outcome(stock, index, cfg)
            outcome_reasons[outcome.reason] += 1
            raw_pattern_counts[observation.pattern] += 1
            raw_pattern_counts["RAW_OVERLAP"] += int(observation.raw_overlap)
            gross_rule_return = None
            if outcome.status == "EVALUABLE":
                if outcome.path_result == "TARGET_BEFORE_STOP":
                    trigger_day = int(outcome.first_target_day or 0)
                    gross_rule_return = (
                        stock.bars[index + trigger_day].close
                        / float(outcome.entry_open)
                        - 1.0
                    )
                elif outcome.path_result == "STOP_BEFORE_TARGET":
                    trigger_day = int(outcome.first_stop_day or 0)
                    gross_rule_return = (
                        stock.bars[index + trigger_day].close
                        / float(outcome.entry_open)
                        - 1.0
                    )
                else:
                    gross_rule_return = outcome.day10_close_return
            row = {
                "period": period,
                "signal_date": observation.signal_date,
                "calendar_index": observation.calendar_index,
                "signal_segment_id": stock.segment_ids[index],
                "code": observation.code,
                "name": observation.name,
                "pattern": observation.pattern,
                "signal_close": observation.signal_close,
                "average_volume_20": observation.average_volume_20,
                "average_turnover_proxy_20": observation.average_turnover_proxy_20,
                "pivot_date": observation.pivot_date,
                "second_pivot_date": observation.second_pivot_date,
                "raw_overlap": observation.raw_overlap,
                "cooldown_included": included,
                "outcome_status": outcome.status,
                "outcome_reason": outcome.reason,
                "entry_date": outcome.entry_date,
                "entry_open_proxy": outcome.entry_open,
                "entry_gap": outcome.entry_gap,
                "primary_success": outcome.primary_success,
                "path_result": outcome.path_result,
                "first_target_day": outcome.first_target_day,
                "first_stop_day": outcome.first_stop_day,
                "day10_close_return": outcome.day10_close_return,
                "mfe_close_10": outcome.mfe_close_10,
                "mae_close_10": outcome.mae_close_10,
                "gross_close_rule_return": gross_rule_return,
                "is_actual_order": False,
                "is_actual_fill": False,
                **{f"geometry_{key}": value for key, value in observation.geometry.items()},
                **observation.features,
            }
            signal_rows.append(row)
            counts_by_date[(observation.signal_date, observation.pattern)] += 1

    signal_rows.sort(
        key=lambda row: (row["signal_date"], row["code"], row["pattern"])
    )
    daily_rows = [
        {"period": period, "signal_date": day, "pattern": pattern, "signal_count": count}
        for (day, pattern), count in sorted(counts_by_date.items())
    ]
    return {
        "period": period,
        "signal_rows": signal_rows,
        "daily_rows": daily_rows,
        "outcome_reasons": dict(sorted(outcome_reasons.items())),
        "raw_pattern_counts": dict(sorted(raw_pattern_counts.items())),
        "maximum_signal_date_read": maximum_signal_date_read,
        "cooldown_seed_count": len(seeded_keys),
    }


def merge_unique_signals(rows: list[dict]) -> list[dict]:
    """N-priority scan already removes overlaps; keep one stock-date defensively."""

    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in sorted(rows, key=lambda item: (item["signal_date"], item["code"], item["pattern"])):
        key = (row["signal_date"], row["code"])
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(row))
    return result


def rank_portfolio_signals(rows: list[dict], pattern: str) -> list[dict]:
    """Causal fixed priority: stronger T-day demand, then liquidity, then code."""

    eligible = [
        dict(row)
        for row in rows
        if row["pattern"] == pattern and row.get("cooldown_included")
    ]
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in eligible:
        by_date[row["signal_date"]].append(row)
    result: list[dict] = []
    for day in sorted(by_date):
        daily = sorted(
            by_date[day],
            key=lambda row: (
                -float(row["signal_return_1"]),
                -float(row["average_turnover_proxy_20"]),
                row["code"],
            ),
        )
        for rank, row in enumerate(daily, 1):
            row["daily_rank"] = rank
            row["portfolio_priority_rule"] = (
                "signal_return_1 desc, average_turnover_proxy_20 desc, code asc"
            )
            result.append(row)
    return result
