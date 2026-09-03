#!/usr/bin/env python3
"""Walk-forward V2: market regime + quality momentum + low turnover."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from run_backtest import (
    START_DATE, END_DATE, bar_on_or_after, benchmark_result, cost,
    last_trading_days, load_archives, next_date,
)


def index_at(bars, date):
    return bisect.bisect_right([bar.date for bar in bars], date) - 1


def market_on(benchmark, date):
    i = index_at(benchmark, date)
    if i < 200:
        return False
    sma50 = statistics.fmean(bar.norm for bar in benchmark[i - 49:i + 1])
    sma200 = statistics.fmean(bar.norm for bar in benchmark[i - 199:i + 1])
    return benchmark[i].norm > sma200 and sma50 > sma200


def candidate_metrics(code, bars, date):
    i = index_at(bars, date)
    if i < 200:
        return None
    current = bars[i]
    if (datetime.strptime(date, "%Y%m%d") - datetime.strptime(current.date, "%Y%m%d")).days > 5:
        return None
    sma60 = statistics.fmean(bar.norm for bar in bars[i - 59:i + 1])
    sma200 = statistics.fmean(bar.norm for bar in bars[i - 199:i + 1])
    old_sma60 = statistics.fmean(bar.norm for bar in bars[i - 79:i - 19])
    average_value60 = statistics.fmean(bar.close * bar.volume for bar in bars[i - 59:i + 1])
    momentum_6_1 = bars[i - 20].norm / bars[i - 120].norm - 1
    returns = [math.log(bars[j].norm / bars[j - 1].norm) for j in range(i - 59, i + 1)]
    volatility = statistics.pstdev(returns) * math.sqrt(252)
    if not (
        current.close >= 20
        and average_value60 >= 50_000_000
        and current.norm > sma60 > sma200
        and sma60 > old_sma60
        and 0.05 <= momentum_6_1 <= 1.0
        and 0 < volatility <= 0.60
    ):
        return None
    return {
        "code": code,
        "name": current.name,
        "signal_date": date,
        "signal_close": current.close,
        "momentum_6_1": momentum_6_1,
        "volatility_60": volatility,
        "average_value_60": average_value60,
        "score": momentum_6_1 / volatility,
    }


def run_v2(stocks, benchmark, initial_capital=30_000):
    dates = sorted({bar.date for bars in stocks.values() for bar in bars if START_DATE <= bar.date <= END_DATE})
    signals = last_trading_days(dates)
    decisions = {}
    for signal_date in signals:
        ranked = []
        if market_on(benchmark, signal_date):
            for code, bars in stocks.items():
                item = candidate_metrics(code, bars, signal_date)
                if item:
                    ranked.append(item)
            ranked.sort(key=lambda item: item["score"], reverse=True)
        execution_date = next_date(dates, signal_date)
        if execution_date and execution_date <= END_DATE:
            decisions[execution_date] = {
                "signal_date": signal_date,
                "market_on": bool(ranked),
                "ranked": ranked,
            }

    cash = initial_capital
    positions = {}
    trades = []
    equity_curve = []
    yearly_start, yearly_end = {}, {}
    decision_log = []

    def current_value(code, position, date, use_open=False):
        bar = bar_on_or_after(stocks[code], date)
        if not bar:
            i = index_at(stocks[code], date)
            bar = stocks[code][i] if i >= 0 else None
        if not bar:
            return 0, None
        norm = bar.norm * (bar.open / bar.close) if use_open else bar.norm
        return position["entry_gross"] * norm / position["entry_norm"], bar

    def sell(code, date):
        nonlocal cash
        position = positions.get(code)
        if not position:
            return
        gross, bar = current_value(code, position, date, use_open=True)
        if not bar:
            return
        fee = cost(gross, "SELL")
        proceeds = gross - fee
        pnl = proceeds - position["entry_gross"] - position["buy_fee"]
        cash += proceeds
        trades.append({
            "code": code, "name": position["name"],
            "entry_date": position["entry_date"], "exit_date": date,
            "entry_price": round(position["entry_price"], 2), "exit_price": round(bar.open, 2),
            "shares": position["shares"], "pnl": round(pnl, 2),
            "return_pct": round(pnl / (position["entry_gross"] + position["buy_fee"]) * 100, 2),
            "costs": round(position["buy_fee"] + fee, 2),
        })
        del positions[code]

    for date in dates:
        if date in decisions:
            decision = decisions[date]
            ranked = decision["ranked"]
            rank = {item["code"]: index for index, item in enumerate(ranked)}
            # Hysteresis: an existing holding stays while it remains in the top
            # 30 and continues to satisfy all trend/risk filters.
            keep = {code for code in positions if code in rank and rank[code] < 30}
            for code in list(positions):
                if code not in keep:
                    sell(code, date)
            target = list(keep)
            for item in ranked:
                if len(target) >= 3:
                    break
                if item["code"] not in target:
                    target.append(item["code"])

            slots = max(1, 3 - len(positions))
            allocation = cash / slots
            selected_items = {item["code"]: item for item in ranked}
            for code in target:
                if code in positions:
                    continue
                bar = bar_on_or_after(stocks[code], date)
                if not bar:
                    continue
                shares = math.floor(allocation / bar.open)
                if shares < 1:
                    continue
                gross = shares * bar.open
                buy_fee = cost(gross, "BUY")
                while shares > 0 and gross + buy_fee > cash:
                    shares -= 1
                    gross = shares * bar.open
                    buy_fee = cost(gross, "BUY") if shares else 0
                if not shares:
                    continue
                cash -= gross + buy_fee
                positions[code] = {
                    "name": bar.name, "entry_date": date, "entry_price": bar.open,
                    "entry_norm": bar.norm * (bar.open / bar.close), "entry_gross": gross,
                    "shares": shares, "buy_fee": buy_fee,
                }
            decision_log.append({
                "signal_date": decision["signal_date"],
                "execution_date": date,
                "market_on": decision["market_on"],
                "holdings": [selected_items.get(code, {"code": code, "name": positions[code]["name"]}) for code in positions],
            })

        equity = cash
        for code, position in positions.items():
            value, _ = current_value(code, position, date)
            equity += value
        equity_curve.append({"date": date, "equity": round(equity, 2)})
        year = date[:4]
        yearly_start.setdefault(year, equity)
        yearly_end[year] = equity

    final_date = dates[-1]
    for code in list(positions):
        sell(code, final_date)
    final_equity = cash
    equity_curve[-1]["equity"] = round(final_equity, 2)
    yearly_end["2025"] = final_equity

    peak, max_drawdown = 0.0, 0.0
    for point in equity_curve:
        peak = max(peak, point["equity"])
        max_drawdown = max(max_drawdown, (peak - point["equity"]) / peak if peak else 0)
    wins = [trade for trade in trades if trade["pnl"] > 0]
    gross_profit = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in trades if trade["pnl"] < 0))
    years = (datetime.strptime(END_DATE, "%Y%m%d") - datetime.strptime(START_DATE, "%Y%m%d")).days / 365.25
    annual = [{
        "year": year,
        "phase": "design" if year <= "2022" else "validation" if year <= "2024" else "out_of_sample",
        "return_pct": round((yearly_end[year] / yearly_start[year] - 1) * 100, 2),
        "end_equity": round(yearly_end[year], 2),
    } for year in sorted(yearly_start)]

    return {
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "period": "2020-01-01 ~ 2025-12-31",
            "phases": {"design": "2020-2022", "validation": "2023-2024", "out_of_sample": "2025"},
            "universe_count": len(stocks),
            "source": "TWSE MI_INDEX + TPEX daily close; packaged by tw-stock-data-release",
            "source_url": "https://github.com/yukishirotsubasa/tw-stock-data-release/releases/tag/daily-close-csv",
            "dividends_included": False,
        },
        "strategy": {
            "name": "V2 大盤濾網＋品質動能低換手策略",
            "rules": [
                "0050 高於 200 日均線且 50 日均線高於 200 日均線才持股",
                "四碼普通股，股價至少 20 元，60 日平均成交金額至少 5,000 萬元",
                "股價高於 60 日均線，60 日均線高於 200 日均線且持續上升",
                "使用 6 個月至 1 個月前的動能，排除最近 20 日避免追高",
                "60 日年化波動不得超過 60%，依動能／波動排序",
                "最多持有 3 檔；持股仍在前 30 名就續抱，降低換手",
                "月底收盤決策、下一交易日開盤執行，三萬元零股配置並計入成本",
            ],
        },
        "summary": {
            "initial_capital": initial_capital,
            "final_equity": round(final_equity, 2),
            "total_return_pct": round((final_equity / initial_capital - 1) * 100, 2),
            "cagr_pct": round(((final_equity / initial_capital) ** (1 / years) - 1) * 100, 2),
            "max_drawdown_pct": round(max_drawdown * 100, 2),
            "trades": len(trades), "wins": len(wins),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0,
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
            "total_costs": round(sum(trade["costs"] for trade in trades), 2),
        },
        "benchmark": benchmark_result(benchmark),
        "annual": annual,
        "trades": trades,
        "equity_curve": equity_curve[::5] + ([equity_curve[-1]] if equity_curve[-1] not in equity_curve[::5] else []),
        "decisions": decision_log,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    stocks, benchmark = load_archives(args.archives)
    result = run_v2(stocks, benchmark)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": result["summary"], "annual": result["annual"], "benchmark": result["benchmark"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
