from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import statistics

from .config import CFG, Config
from .models import DailyBar, IndexDailyBar, TradingSession, UniverseMember


def build_universes(
    daily: list[DailyBar],
    index_daily: list[IndexDailyBar],
    trading_calendar: list[TradingSession],
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    cfg: Config = CFG,
) -> tuple[list[UniverseMember], dict]:
    """Build each trade day's Top-30 using information available at T-1.

    Every stock and benchmark must have a complete bar for each of the previous
    60 known calendar sessions. The target date itself never needs a daily bar.
    This intentionally fails closed on suspensions or data gaps.
    """

    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must not be after end_date")

    expected_indexes = dict(cfg.benchmark_index_ids)
    if any(row.index_id != expected_indexes.get(row.market) for row in index_daily):
        raise ValueError("index_daily contains a non-canonical benchmark index_id")
    required_markets = set(expected_indexes)
    coverage = {
        "daily": {row.market for row in daily},
        "index_daily": {row.market for row in index_daily},
        "trading_calendar": {row.market for row in trading_calendar},
    }
    missing_coverage = {
        source: sorted(required_markets - markets)
        for source, markets in coverage.items()
        if required_markets - markets
    }
    if missing_coverage:
        raise ValueError(
            f"fixed strategy requires TWSE and TPEX coverage: {missing_coverage}"
        )

    sessions: dict[str, list[date]] = defaultdict(list)
    for row in trading_calendar:
        sessions[row.market].append(row.date)
    sessions = {
        market: sorted(set(dates)) for market, dates in sessions.items()
    }
    reference_market = sorted(required_markets)[0]
    reference_sessions = sessions[reference_market]
    if any(sessions[market] != reference_sessions for market in required_markets):
        raise ValueError(
            "TWSE and TPEX trading calendars must contain the same ordered sessions"
        )

    indexes: dict[tuple[str, date], IndexDailyBar] = {}
    for row in index_daily:
        indexes[(row.market, row.date)] = row

    stocks: dict[tuple[str, str], dict[date, DailyBar]] = defaultdict(dict)
    for row in daily:
        stocks[(row.market, row.symbol)][row.date] = row
    daily_market_dates = {(row.market, row.date) for row in daily}

    candidate_sessions: dict[str, list[date]] = {}
    for market in {row.market for row in daily}:
        eligible_dates = [
            day
            for position, day in enumerate(sessions.get(market, []))
            if position >= cfg.daily_history_sessions
            and (start_date is None or day >= start_date)
            and (end_date is None or day <= end_date)
        ]
        if not eligible_dates:
            raise ValueError(
                f"trading calendar has no {market} target session with "
                f"{cfg.daily_history_sessions} prior sessions in the requested range"
            )
        candidate_sessions[market] = eligible_dates

    target_dates = candidate_sessions[reference_market]
    if any(candidate_sessions[market] != target_dates for market in required_markets):
        raise ValueError("TWSE and TPEX target sessions are not aligned")
    session_position = {day: position for position, day in enumerate(reference_sessions)}
    for trade_date in target_dates:
        position = session_position[trade_date]
        history_dates = reference_sessions[
            position - cfg.daily_history_sessions : position
        ]
        for market in sorted(required_markets):
            missing = [day for day in history_dates if (market, day) not in indexes]
            if missing:
                raise ValueError(
                    f"{market} benchmark history is incomplete before {trade_date}: "
                    f"first missing session {missing[0]}"
                )
            missing_daily_market = [
                day for day in history_dates if (market, day) not in daily_market_dates
            ]
            if missing_daily_market:
                raise ValueError(
                    f"{market} stock daily feed is absent before {trade_date}: "
                    f"first missing session {missing_daily_market[0]}"
                )

    eligible_by_date: dict[date, list[UniverseMember]] = defaultdict(list)
    rejection_counts: Counter[str] = Counter()

    for (market, symbol), rows_by_date in stocks.items():
        market_sessions = sessions.get(market, [])
        if not market_sessions:
            rejection_counts["missing_trading_calendar"] += 1
            continue

        for target_position in range(cfg.daily_history_sessions, len(market_sessions)):
            trade_date = market_sessions[target_position]
            if start_date and trade_date < start_date:
                continue
            if end_date and trade_date > end_date:
                continue

            history_dates = market_sessions[
                target_position - cfg.daily_history_sessions : target_position
            ]
            history = [rows_by_date.get(day) for day in history_dates]
            if any(row is None for row in history):
                rejection_counts["incomplete_60_session_history"] += 1
                continue
            benchmark_history = [indexes.get((market, day)) for day in history_dates]
            if any(row is None for row in benchmark_history):
                rejection_counts["incomplete_index_60_session_history"] += 1
                continue

            complete_history = [row for row in history if row is not None]
            complete_benchmark = [row for row in benchmark_history if row is not None]
            current = complete_history[-1]
            if current.security_type != "COMMON_STOCK":
                rejection_counts["not_common_stock"] += 1
                continue
            if current.trading_status != "NORMAL":
                rejection_counts["not_normal_t_minus_1"] += 1
                continue

            closes = [item.close for item in complete_history]
            recent20 = complete_history[-20:]
            sma20 = statistics.fmean(item.close for item in recent20)
            sma60 = statistics.fmean(closes)
            median_volume = statistics.median(item.volume for item in recent20)
            median_turnover = statistics.median(item.turnover for item in recent20)
            close_to_high = current.close / max(closes)

            index_close = complete_benchmark[-1].close
            stock_return5 = current.close / complete_history[-6].close - 1.0
            stock_return20 = current.close / complete_history[-21].close - 1.0
            index_return5 = index_close / complete_benchmark[-6].close - 1.0
            index_return20 = index_close / complete_benchmark[-21].close - 1.0
            rs5 = stock_return5 - index_return5
            rs20 = stock_return20 - index_return20

            checks = (
                (current.close >= cfg.minimum_price, "price_below_minimum"),
                (
                    median_turnover >= cfg.minimum_median_turnover_20d,
                    "turnover_below_minimum",
                ),
                (
                    median_volume >= cfg.minimum_median_volume_20d,
                    "volume_below_minimum",
                ),
                (current.close > sma20 > sma60, "trend_not_aligned"),
                (rs5 > 0.0, "rs5_not_positive"),
                (rs20 > 0.0, "rs20_not_positive"),
                (
                    close_to_high >= cfg.near_60d_high_ratio,
                    "not_near_60d_high",
                ),
            )
            failed = next((reason for passed, reason in checks if not passed), None)
            if failed:
                rejection_counts[failed] += 1
                continue

            eligible_by_date[trade_date].append(
                UniverseMember(
                    trade_date=trade_date,
                    symbol=symbol,
                    name=current.name,
                    market=market,
                    previous_close=current.close,
                    previous_index_close=index_close,
                    sma20=sma20,
                    sma60=sma60,
                    median_volume_20d=median_volume,
                    median_turnover_20d=median_turnover,
                    rs5=rs5,
                    rs20=rs20,
                    close_to_60d_high=close_to_high,
                )
            )

    selected: list[UniverseMember] = []
    eligible_before_rank = 0
    for trade_date, rows in sorted(eligible_by_date.items()):
        eligible_before_rank += len(rows)
        ranked = sorted(
            rows,
            key=lambda x: (
                -x.rs20,
                -x.rs5,
                -x.median_turnover_20d,
                x.symbol,
            ),
        )
        selected.extend(
            row.with_rank(rank)
            for rank, row in enumerate(ranked[: cfg.universe_size], start=1)
        )

    audit = {
        "strategy_id": cfg.strategy_id,
        "config_hash": cfg.fingerprint(),
        "daily_rows": len(daily),
        "index_daily_rows": len(index_daily),
        "trading_calendar_rows": len(trading_calendar),
        "required_markets": sorted(required_markets),
        "source_market_coverage": {
            source: sorted(markets) for source, markets in coverage.items()
        },
        "candidate_trade_dates": len(target_dates),
        "candidate_market_date_rows": sum(
            len(rows) for rows in candidate_sessions.values()
        ),
        "candidate_first_date": min(target_dates).isoformat(),
        "candidate_last_date": max(target_dates).isoformat(),
        "trade_dates_with_universe": len({row.trade_date for row in selected}),
        "eligible_before_top30": eligible_before_rank,
        "selected_rows": len(selected),
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "lookback_policy": "target from known trading calendar; exactly 60 prior sessions; daily/index data through T-1 only",
        "ranking": "RS20 desc, RS5 desc, median turnover 20d desc, symbol asc",
    }
    return selected, audit
