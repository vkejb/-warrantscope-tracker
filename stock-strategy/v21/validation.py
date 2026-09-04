from __future__ import annotations

import math
import statistics

from .backtest import net_return
from .config import CFG
from .data_loader import Bar, TaiexBar
from .signals import Signal, taiex_filter


def validate_results(
    signals: list[Signal],
    trades: list[dict],
    events: list[dict],
    stocks: dict[str, list[Bar]],
    taiex: list[TaiexBar],
    institutional: dict[tuple[str, str], tuple[int, int]],
    financial_codes: set[str] | None,
) -> dict:
    """Recompute core V2.1 invariants independently of the CSV writer."""
    failures: list[str] = []

    def check(condition: bool, message: str):
        if not condition and len(failures) < 100:
            failures.append(message)

    def close(left, right, tolerance: float = 1e-10) -> bool:
        if left is None or right is None:
            return left is right
        return math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance)

    market_dates = sorted({bar.date for bar in taiex})
    market_index = {date: i for i, date in enumerate(market_dates)}
    market_next = dict(zip(market_dates, market_dates[1:]))
    market_ok = taiex_filter(taiex)
    stock_index = {
        code: {bar.date: i for i, bar in enumerate(bars)}
        for code, bars in stocks.items()
    }
    stock_by_date = {
        code: {bar.date: bar for bar in bars} for code, bars in stocks.items()
    }
    signals_by_key = {(signal.stock_id, signal.signal_date): signal for signal in signals}

    check(len(signals_by_key) == len(signals), "duplicate signal key")
    check(len(events) == len(signals), "event count differs from final signal count")

    for signal in signals:
        key = (signal.stock_id, signal.signal_date)
        bars = stocks[signal.stock_id]
        i = stock_index[signal.stock_id][signal.signal_date]
        check(i >= 20, f"{key}: insufficient price history")
        if i < 20:
            continue
        bar = bars[i]
        closes = [point.close for point in bars]
        avg_volume_prev = statistics.fmean(point.volume for point in bars[i - 20 : i])
        ma10 = statistics.fmean(closes[i - 9 : i + 1])
        ma20 = statistics.fmean(closes[i - 19 : i + 1])
        ma20_prev = statistics.fmean(closes[i - 20 : i])
        daily_return = bar.close / bars[i - 1].close - 1
        volume_ratio = bar.volume / avg_volume_prev
        previous_high = max(point.high for point in bars[i - 20 : i])
        breakout_ratio = bar.close / previous_high
        breakout_type = (
            "Pre-breakout"
            if breakout_ratio < 1
            else ("Breakout" if breakout_ratio <= 1.03 else "Extended Breakout")
        )
        check(market_ok.get(bar.date, False), f"{key}: TAIEX filter failed")
        check(CFG.min_price <= bar.close <= CFG.max_price, f"{key}: price filter failed")
        check(
            avg_volume_prev / 1000 >= CFG.min_avg_volume_lots,
            f"{key}: prior-volume filter failed",
        )
        check(
            bar.close > ma20 and ma10 > ma20 and ma20 > ma20_prev,
            f"{key}: stock-trend filter failed",
        )
        check(
            CFG.min_daily_return <= daily_return <= CFG.max_daily_return,
            f"{key}: daily-return filter failed",
        )
        check(volume_ratio >= CFG.min_volume_ratio, f"{key}: volume-ratio filter failed")
        check(
            CFG.min_breakout_ratio <= breakout_ratio <= CFG.max_breakout_ratio,
            f"{key}: breakout filter failed",
        )
        check(close(signal.daily_return, daily_return), f"{key}: daily return mismatch")
        check(close(signal.volume_ratio, volume_ratio), f"{key}: volume ratio mismatch")
        check(close(signal.breakout_ratio, breakout_ratio), f"{key}: breakout ratio mismatch")
        check(signal.breakout_type == breakout_type, f"{key}: breakout type mismatch")
        if financial_codes is not None:
            check(signal.stock_id not in financial_codes, f"{key}: financial stock included")

        dates = [point.date for point in bars[i - 2 : i + 1]]
        points = [institutional.get((signal.stock_id, date)) for date in dates]
        check(len(dates) == 3 and all(point is not None for point in points), f"{key}: institutional data missing")
        if len(dates) == 3 and all(point is not None for point in points):
            foreign = sum(point[0] for point in points)
            trust = sum(point[1] for point in points)
            total_volume = sum(bars[stock_index[signal.stock_id][date]].volume for date in dates)
            foreign_ratio = foreign / total_volume
            trust_ratio = trust / total_volume
            combined_ratio = (foreign + trust) / total_volume
            check(close(signal.foreign_ratio, foreign_ratio), f"{key}: foreign ratio mismatch")
            check(close(signal.investment_trust_ratio, trust_ratio), f"{key}: trust ratio mismatch")
            check(close(signal.combined_ratio, combined_ratio), f"{key}: combined ratio mismatch")
            check(combined_ratio >= CFG.min_combined_ratio, f"{key}: institutional filter failed")

    trades_by_key = {(row["stock_id"], row["signal_date"]): row for row in trades}
    check(len(trades_by_key) == len(trades), "duplicate completed trade key")

    for row in trades:
        key = (row["stock_id"], row["signal_date"])
        signal = signals_by_key[key]
        by_date = stock_by_date[row["stock_id"]]
        expected_entry_date = market_next[signal.signal_date]
        entry_bar = by_date[expected_entry_date]
        check(row["entry_date"] == expected_entry_date, f"{key}: entry is not actual T+1")
        check(close(row["entry_price"], entry_bar.open), f"{key}: entry price mismatch")
        check(entry_bar.open >= signal.signal_low, f"{key}: entered below signal low")
        check(
            entry_bar.open <= signal.signal_close * (1 + CFG.max_entry_gap),
            f"{key}: entered above 5 percent gap cap",
        )
        expected_gap = entry_bar.open / signal.signal_close - 1
        expected_stop = max(entry_bar.open * CFG.hard_stop_pct, signal.signal_low)
        check(close(row["t1_gap_pct"], expected_gap), f"{key}: entry gap mismatch")
        check(close(row["initial_stop_price"], expected_stop), f"{key}: stop mismatch")

        entry_market_i = market_index[row["entry_date"]]
        trigger_market_i = market_index[row["exit_trigger_date"]]
        expected_holding_days = trigger_market_i - entry_market_i + 1
        check(row["holding_days"] == expected_holding_days, f"{key}: holding day mismatch")
        check(expected_holding_days <= CFG.max_holding_days, f"{key}: held longer than 8 days")

        highest = row["entry_price"]
        highs = []
        lows = []
        close_returns = {}
        replay_reason = None
        replay_trigger_date = None
        for holding_day in range(1, expected_holding_days + 1):
            date = market_dates[entry_market_i + holding_day - 1]
            bar = by_date.get(date)
            if bar is None:
                continue
            highest = max(highest, bar.high)
            highs.append(bar.high)
            lows.append(bar.low)
            if holding_day in (1, 3, 5, 8):
                close_returns[holding_day] = bar.close / row["entry_price"] - 1
            if bar.close < expected_stop:
                replay_reason = "Close Confirmed Stop"
            elif (
                highest >= row["entry_price"] * CFG.trailing_activation_pct
                and bar.close < highest * CFG.trailing_drawdown_pct
            ):
                replay_reason = "Trailing Stop"
            elif holding_day == 5 and bar.close <= row["entry_price"]:
                replay_reason = "Day 5 Weakness"
            elif holding_day >= CFG.max_holding_days:
                replay_reason = "Day 8 Time Stop"
            if replay_reason:
                replay_trigger_date = date
                break

        check(replay_reason == row["exit_reason"], f"{key}: exit reason mismatch")
        check(replay_trigger_date == row["exit_trigger_date"], f"{key}: exit trigger date mismatch")
        if row["exit_reason"] == "Day 8 Time Stop":
            exit_bar = by_date[row["exit_trigger_date"]]
            check(row["exit_date"] == row["exit_trigger_date"], f"{key}: day-8 exit date mismatch")
            check(close(row["exit_price"], exit_bar.close), f"{key}: day-8 close mismatch")
        else:
            expected_exit_date = market_next[row["exit_trigger_date"]]
            exit_bar = by_date[expected_exit_date]
            check(row["exit_date"] == expected_exit_date, f"{key}: exit is not D+1")
            check(close(row["exit_price"], exit_bar.open), f"{key}: D+1 open mismatch")

        gross = row["exit_price"] / row["entry_price"] - 1
        check(close(row["gross_return"], gross), f"{key}: gross return mismatch")
        check(close(row["net_return"], net_return(row["entry_price"], row["exit_price"], 0)), f"{key}: net return mismatch")
        check(
            close(
                row["net_return_slippage_0_1"],
                net_return(row["entry_price"], row["exit_price"], 0.001),
            ),
            f"{key}: slippage return mismatch",
        )
        check(close(row["mfe"], max(highs) / row["entry_price"] - 1), f"{key}: MFE mismatch")
        check(close(row["mae"], min(lows) / row["entry_price"] - 1), f"{key}: MAE mismatch")
        for day in (1, 3, 5, 8):
            check(
                close(row[f"day{day}_close_return"], close_returns.get(day)),
                f"{key}: day-{day} close return mismatch",
            )

        exit_market_i = market_index[row["exit_date"]]
        post = [
            by_date[date].high
            for date in market_dates[exit_market_i + 1 : exit_market_i + 6]
            if date in by_date
        ]
        expected_post = max(post) / row["exit_price"] - 1 if post else None
        check(close(row["post_exit_5d_max_return"], expected_post), f"{key}: post-exit return mismatch")

    entered_event_keys = {
        (event["stock_id"], event["signal_date"])
        for event in events
        if event["status"] == "entered"
    }
    check(entered_event_keys == set(trades_by_key), "entered events differ from completed trades")

    positions = {}
    for event in events:
        if event["status"] != "entered":
            continue
        positions.setdefault(event["stock_id"], []).append(event)
    overlap_count = 0
    for stock_events in positions.values():
        stock_events.sort(key=lambda event: event["entry_date"])
        for prior, current in zip(stock_events, stock_events[1:]):
            if current["signal_date"] < prior["exit_date"]:
                overlap_count += 1
    check(overlap_count == 0, f"same-stock position overlaps: {overlap_count}")

    status_counts = {}
    reason_counts = {}
    for event in events:
        status_counts[event["status"]] = status_counts.get(event["status"], 0) + 1
        reason = event.get("cancel_reason")
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    return {
        "passed": not failures,
        "failure_count": len(failures),
        "failure_samples": failures,
        "checks": {
            "signals_recomputed": len(signals),
            "completed_trades_replayed": len(trades),
            "same_stock_overlap_count": overlap_count,
            "event_status_counts": status_counts,
            "event_reason_counts": reason_counts,
        },
    }
