from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time, timedelta
import statistics
from typing import Callable

from .config import CFG, Config
from .models import (
    IndexMinuteBar,
    MinuteBar,
    Signal,
    SignalEvaluation,
    TradingSession,
    UniverseMember,
)


EPSILON = 1e-10


def tick_size(price: float) -> float:
    """Taiwan ordinary-share tick size; ETFs and special products are excluded."""

    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.10
    if price < 500:
        return 0.50
    if price < 1000:
        return 1.00
    return 5.00


def one_tick_above(price: float) -> float:
    return round(price + tick_size(price), 8)


def ticks_above(price: float, count: int) -> float:
    """Return the price after ``count`` legal upward ticks.

    Re-evaluating the tick size after each step matters at Taiwan price-band
    boundaries such as 50, 100, 500, and 1,000.
    """

    result = price
    for _ in range(count):
        result = one_tick_above(result)
    return result


def _clock(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _expected_bar_ends(day: date, last: time, first: time = time(9, 1)) -> list[datetime]:
    cursor = datetime.combine(day, first)
    end = datetime.combine(day, last)
    result = []
    while cursor <= end:
        result.append(cursor)
        cursor += timedelta(minutes=1)
    return result


def _data_issue(member: UniverseMember, reason: str, cfg: Config) -> SignalEvaluation:
    return SignalEvaluation(
        strategy_id=cfg.strategy_id,
        config_hash=cfg.fingerprint(),
        trade_date=member.trade_date,
        symbol=member.symbol,
        market=member.market,
        bar_end=None,
        or_high=None,
        or_low=None,
        signal_price=None,
        vwap=None,
        vwap_extension=None,
        breakout_extension=None,
        rvol=None,
        stock_return=None,
        index_return=None,
        intraday_relative_strength=None,
        two_closes_above_or=False,
        at_least_one_tick_above_or=False,
        above_vwap=False,
        vwap_extension_ok=False,
        breakout_extension_ok=False,
        rvol_ok=False,
        stock_return_ok=False,
        relative_strength_ok=False,
        current_bar_has_volume=False,
        limit_up_buffer_ok=False,
        passed=False,
        rejection_reason=reason,
    )


def generate_signals(
    universe: list[UniverseMember],
    minutes: list[MinuteBar],
    index_minutes: list[IndexMinuteBar],
    trading_calendar: list[TradingSession],
    *,
    cfg: Config = CFG,
    evaluation_sink: Callable[[SignalEvaluation], None] | None = None,
    retain_failed_evaluations: bool = True,
) -> tuple[list[SignalEvaluation], list[Signal], list[Signal], dict]:
    """Replay the fixed entry rules using completed regular-market minute bars.

    Returns every evaluation, every stock's first raw signal, and the one selected
    signal per day. No odd-lot fill is asserted here.
    """

    expected_indexes = dict(cfg.benchmark_index_ids)
    if any(
        row.index_id != expected_indexes.get(row.market) for row in index_minutes
    ):
        raise ValueError("index_minutes contains a non-canonical benchmark index_id")

    by_symbol_day: dict[tuple[str, str, date], list[MinuteBar]] = defaultdict(list)
    for bar in minutes:
        by_symbol_day[(bar.market, bar.symbol, bar.date)].append(bar)
    for rows in by_symbol_day.values():
        rows.sort(key=lambda x: x.bar_end)

    index_by_market_time = {(bar.market, bar.bar_end): bar for bar in index_minutes}
    market_dates: dict[str, list[date]] = defaultdict(list)
    for session in trading_calendar:
        market_dates[session.market].append(session.date)
    market_dates = {
        market: sorted(set(dates)) for market, dates in market_dates.items()
    }

    evaluations: list[SignalEvaluation] = []
    evaluation_count = 0

    def record(evaluation: SignalEvaluation) -> None:
        nonlocal evaluation_count
        evaluation_count += 1
        if evaluation_sink is not None:
            evaluation_sink(evaluation)
        if retain_failed_evaluations or evaluation.passed:
            evaluations.append(evaluation)

    raw_signals: list[Signal] = []
    data_issue_counts: dict[str, int] = defaultdict(int)
    censored_dates: set[date] = set()
    config_hash = cfg.fingerprint()
    first_bar_time = _clock(cfg.opening_first_bar_end)
    first_signal_time = _clock(cfg.first_signal_bar_end)
    last_signal_time = _clock(cfg.last_signal_bar_end)
    opening_last_time = _clock(cfg.opening_last_bar_end)

    for member in sorted(universe, key=lambda x: (x.trade_date, x.market, x.symbol)):
        current = by_symbol_day.get((member.market, member.symbol, member.trade_date), [])
        current_by_end = {bar.bar_end: bar for bar in current}
        market_session_dates = market_dates.get(member.market, [])
        try:
            target_position = market_session_dates.index(member.trade_date)
        except ValueError:
            target_position = -1

        if target_position < cfg.rvol_history_sessions:
            reason = "INSUFFICIENT_RVOL_HISTORY"
            record(_data_issue(member, reason, cfg))
            data_issue_counts[reason] += 1
            censored_dates.add(member.trade_date)
            continue
        history_dates = market_session_dates[
            target_position - cfg.rvol_history_sessions : target_position
        ]
        historical_rows = [
            by_symbol_day.get((member.market, member.symbol, day), [])
            for day in history_dates
        ]
        if any(not rows for rows in historical_rows):
            reason = "MISSING_RVOL_SESSION"
            record(_data_issue(member, reason, cfg))
            data_issue_counts[reason] += 1
            censored_dates.add(member.trade_date)
            continue

        history_cumulative: dict[date, dict[time, int]] = {}
        history_valid = True
        for day, rows in zip(history_dates, historical_rows):
            row_by_end = {row.bar_end: row for row in rows}
            expected = _expected_bar_ends(day, last_signal_time, first_bar_time)
            if any(timestamp not in row_by_end for timestamp in expected):
                history_valid = False
                break
            cumulative = 0
            by_time: dict[time, int] = {}
            for timestamp in expected:
                cumulative += row_by_end[timestamp].volume
                by_time[timestamp.time()] = cumulative
            history_cumulative[day] = by_time
        if not history_valid:
            reason = "INCOMPLETE_RVOL_MINUTE_GRID"
            record(_data_issue(member, reason, cfg))
            data_issue_counts[reason] += 1
            censored_dates.add(member.trade_date)
            continue

        opening_expected = _expected_bar_ends(
            member.trade_date, opening_last_time, first_bar_time
        )
        if any(timestamp not in current_by_end for timestamp in opening_expected):
            reason = "INCOMPLETE_OR15"
            record(_data_issue(member, reason, cfg))
            data_issue_counts[reason] += 1
            censored_dates.add(member.trade_date)
            continue
        opening = [current_by_end[timestamp] for timestamp in opening_expected]
        or_high = max(bar.high for bar in opening)
        or_low = min(bar.low for bar in opening)

        cumulative_volume = 0
        cumulative_turnover = 0.0
        grid_broken = False
        for timestamp in _expected_bar_ends(
            member.trade_date, last_signal_time, first_bar_time
        ):
            bar = current_by_end.get(timestamp)
            if bar is None:
                grid_broken = True
                reason = "INCOMPLETE_CURRENT_MINUTE_GRID"
                issue = _data_issue(member, reason, cfg)
                record(replace(issue, bar_end=timestamp))
                data_issue_counts[reason] += 1
                censored_dates.add(member.trade_date)
                break
            prior_close = (
                member.previous_close
                if timestamp.time() == first_bar_time
                else current_by_end[timestamp - timedelta(minutes=1)].close
            )
            if bar.volume == 0 and (
                abs(bar.turnover) > EPSILON
                or max(bar.open, bar.high, bar.low, bar.close)
                - min(bar.open, bar.high, bar.low, bar.close)
                > EPSILON
                or abs(bar.close - prior_close) > EPSILON
            ):
                grid_broken = True
                reason = "ZERO_VOLUME_MINUTE_CHANGED_PRICE"
                issue = _data_issue(member, reason, cfg)
                record(replace(issue, bar_end=timestamp))
                data_issue_counts[reason] += 1
                censored_dates.add(member.trade_date)
                break
            cumulative_volume += bar.volume
            cumulative_turnover += bar.turnover
            if timestamp.time() < first_signal_time:
                continue

            previous = current_by_end.get(timestamp - timedelta(minutes=1))
            index_bar = index_by_market_time.get((member.market, timestamp))
            if previous is None or index_bar is None:
                reason = "MISSING_PREVIOUS_OR_INDEX_MINUTE"
                evaluation = _data_issue(
                    member,
                    reason,
                    cfg,
                )
                record(replace(evaluation, bar_end=timestamp))
                data_issue_counts[reason] += 1
                censored_dates.add(member.trade_date)
                grid_broken = True
                break

            vwap = (
                cumulative_turnover / cumulative_volume
                if cumulative_volume > 0
                else None
            )
            historical_at_time = [
                history_cumulative[day][timestamp.time()] for day in history_dates
            ]
            historical_median = statistics.median(historical_at_time)
            rvol = (
                cumulative_volume / historical_median
                if historical_median > 0
                else None
            )
            stock_return = bar.close / member.previous_close - 1.0
            index_return = index_bar.close / member.previous_index_close - 1.0
            relative_strength = (
                stock_return - index_return if index_return is not None else None
            )
            vwap_extension = bar.close / vwap - 1.0 if vwap else None
            breakout_extension = bar.close / or_high - 1.0

            two_closes = previous.close > or_high and bar.close > or_high
            one_tick = bar.close + EPSILON >= one_tick_above(or_high)
            above_vwap = vwap is not None and bar.close > vwap
            vwap_ok = (
                vwap_extension is not None
                and vwap_extension <= cfg.maximum_vwap_extension + EPSILON
            )
            breakout_ok = (
                breakout_extension <= cfg.maximum_breakout_extension + EPSILON
            )
            rvol_ok = rvol is not None and rvol + EPSILON >= cfg.minimum_rvol
            stock_return_ok = (
                cfg.minimum_intraday_return - EPSILON
                <= stock_return
                <= cfg.maximum_intraday_return + EPSILON
            )
            relative_strength_ok = (
                relative_strength is not None
                and relative_strength + EPSILON
                >= cfg.minimum_intraday_relative_strength
            )
            has_volume = bar.volume > 0
            limit_buffer = (
                ticks_above(bar.close, cfg.limit_up_buffer_ticks)
                <= bar.limit_up + EPSILON
            )
            conditions = (
                (two_closes, "NEEDS_TWO_CLOSES_ABOVE_OR"),
                (one_tick, "NOT_ONE_TICK_ABOVE_OR"),
                (above_vwap, "NOT_ABOVE_VWAP"),
                (vwap_ok, "TOO_FAR_ABOVE_VWAP"),
                (breakout_ok, "TOO_FAR_ABOVE_OR"),
                (rvol_ok, "RVOL_BELOW_1_8"),
                (stock_return_ok, "INTRADAY_RETURN_OUTSIDE_1_TO_6_PCT"),
                (relative_strength_ok, "RELATIVE_STRENGTH_BELOW_1_PCT"),
                (has_volume, "NO_VOLUME_IN_CONFIRMATION_BAR"),
                (limit_buffer, "TOO_CLOSE_TO_LIMIT_UP"),
            )
            rejection = next((reason for passed, reason in conditions if not passed), "")
            passed = not rejection
            evaluation = SignalEvaluation(
                strategy_id=cfg.strategy_id,
                config_hash=config_hash,
                trade_date=member.trade_date,
                symbol=member.symbol,
                market=member.market,
                bar_end=timestamp,
                or_high=or_high,
                or_low=or_low,
                signal_price=bar.close,
                vwap=vwap,
                vwap_extension=vwap_extension,
                breakout_extension=breakout_extension,
                rvol=rvol,
                stock_return=stock_return,
                index_return=index_return,
                intraday_relative_strength=relative_strength,
                two_closes_above_or=two_closes,
                at_least_one_tick_above_or=one_tick,
                above_vwap=above_vwap,
                vwap_extension_ok=vwap_ok,
                breakout_extension_ok=breakout_ok,
                rvol_ok=rvol_ok,
                stock_return_ok=stock_return_ok,
                relative_strength_ok=relative_strength_ok,
                current_bar_has_volume=has_volume,
                limit_up_buffer_ok=limit_buffer,
                passed=passed,
                rejection_reason=rejection or "PASSED",
            )
            record(evaluation)
            if passed:
                raw_signals.append(
                    Signal(
                        strategy_id=cfg.strategy_id,
                        config_hash=config_hash,
                        trade_date=member.trade_date,
                        symbol=member.symbol,
                        name=member.name,
                        market=member.market,
                        signal_time=timestamp,
                        signal_price=bar.close,
                        or_high=or_high,
                        or_low=or_low,
                        vwap=vwap,
                        rvol=rvol,
                        stock_return=stock_return,
                        index_return=index_return,
                        intraday_relative_strength=relative_strength,
                        median_turnover_20d=member.median_turnover_20d,
                    )
                )
                break
        if grid_broken:
            continue

    selectable_raw = [
        signal for signal in raw_signals if signal.trade_date not in censored_dates
    ]
    updated_raw, selected = select_daily_signals(selectable_raw)
    updated_raw.extend(
        replace(
            signal,
            selected=False,
            selection_reason="DAY_CENSORED_INCOMPLETE_UNIVERSE_DATA",
        )
        for signal in raw_signals
        if signal.trade_date in censored_dates
    )
    updated_raw.sort(key=lambda x: (x.trade_date, x.signal_time, x.symbol))
    censored_members = sum(data_issue_counts.values())
    audit = {
        "strategy_id": cfg.strategy_id,
        "config_hash": config_hash,
        "universe_rows": len(universe),
        "evaluation_rows": evaluation_count,
        "retained_evaluation_rows": len(evaluations),
        "raw_signal_rows": len(updated_raw),
        "selected_signal_rows": len(selected),
        "data_issue_counts": dict(sorted(data_issue_counts.items())),
        "censored_universe_rows": censored_members,
        "censored_universe_rate": (
            censored_members / len(universe) if universe else None
        ),
        "censored_trade_date_count": len(censored_dates),
        "censored_trade_dates": [day.isoformat() for day in sorted(censored_dates)],
        "minute_timestamp_semantics": "bar_end; completed regular-market minute",
        "selection_policy": "earliest signal minute, then RS desc, RVOL desc, T-1 turnover desc, symbol asc",
        "market_calendar_source": "explicit pre-known trading calendar, not daily prices or minute-file presence",
    }
    return evaluations, updated_raw, selected, audit


def select_daily_signals(raw_signals: list[Signal]) -> tuple[list[Signal], list[Signal]]:
    by_date: dict[date, list[Signal]] = defaultdict(list)
    for signal in raw_signals:
        by_date[signal.trade_date].append(signal)

    updated: list[Signal] = []
    selected: list[Signal] = []
    for _, rows in sorted(by_date.items()):
        earliest = min(row.signal_time for row in rows)
        simultaneous = [row for row in rows if row.signal_time == earliest]
        winner = sorted(
            simultaneous,
            key=lambda x: (
                -x.intraday_relative_strength,
                -x.rvol,
                -x.median_turnover_20d,
                x.symbol,
            ),
        )[0]
        for row in rows:
            if row is winner:
                chosen = row.selected_for_day()
                updated.append(chosen)
                selected.append(chosen)
            elif row.signal_time == earliest:
                updated.append(replace(row, selection_reason="NOT_TOP_RANKED_SAME_MINUTE"))
            else:
                updated.append(replace(row, selection_reason="LATER_THAN_DAILY_SELECTION"))
    updated.sort(key=lambda x: (x.trade_date, x.signal_time, x.symbol))
    selected.sort(key=lambda x: (x.trade_date, x.signal_time, x.symbol))
    return updated, selected
