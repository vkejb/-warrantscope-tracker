#!/usr/bin/env python3
"""Predefined OHLCV strategy research. No parameter grid or 2025 optimization."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from run_backtest import END_DATE, START_DATE, bar_on_or_after, cost, last_trading_days, load_archives, next_date


VARIANTS = [
    {"id": "low_vol_5_q", "name": "低波動趨勢5檔／季換股", "count": 5, "months": 3, "market": True, "breakout": False},
    {"id": "low_vol_10_q", "name": "低波動趨勢10檔／季換股", "count": 10, "months": 3, "market": True, "breakout": False},
    {"id": "breakout_5_q", "name": "52週新高5檔／季換股", "count": 5, "months": 3, "market": True, "breakout": True},
    {"id": "low_vol_5_m", "name": "低波動趨勢5檔／月換股", "count": 5, "months": 1, "market": True, "breakout": False},
    {"id": "low_vol_5_q_no_filter", "name": "低波動趨勢5檔／季換股／無大盤濾網", "count": 5, "months": 3, "market": False, "breakout": False},
]


def idx(bars, date):
    return bisect.bisect_right([bar.date for bar in bars], date) - 1


def regime(benchmark, date):
    i = idx(benchmark, date)
    if i < 200:
        return False
    sma200 = statistics.fmean(x.norm for x in benchmark[i - 199:i + 1])
    return benchmark[i].norm > sma200


def rank_stocks(stocks, date, breakout=False):
    ranked = []
    for code, bars in stocks.items():
        i = idx(bars, date)
        if i < 252:
            continue
        bar = bars[i]
        if (datetime.strptime(date, "%Y%m%d") - datetime.strptime(bar.date, "%Y%m%d")).days > 5:
            continue
        avg_value = statistics.fmean(x.close * x.volume for x in bars[i - 59:i + 1])
        sma200 = statistics.fmean(x.norm for x in bars[i - 199:i + 1])
        momentum_12_1 = bars[i - 20].norm / bars[i - 252].norm - 1
        returns = [math.log(bars[j].norm / bars[j - 1].norm) for j in range(i - 59, i + 1)]
        vol = statistics.pstdev(returns) * math.sqrt(252)
        high252 = max(x.norm for x in bars[i - 251:i + 1])
        near_high = bar.norm / high252
        if not (bar.close >= 20 and avg_value >= 50_000_000 and bar.norm > sma200 and momentum_12_1 > 0 and 0 < vol <= 0.55):
            continue
        if breakout and near_high < 0.90:
            continue
        # Reward persistent momentum and proximity to highs, penalize volatility.
        score = momentum_12_1 / vol + (near_high - 0.9 if breakout else 0)
        ranked.append({"code": code, "name": bar.name, "score": score})
    return sorted(ranked, key=lambda x: x["score"], reverse=True)


def simulate(stocks, benchmark, variant, capital=30_000):
    dates = sorted({bar.date for bars in stocks.values() for bar in bars if START_DATE <= bar.date <= END_DATE})
    signals = last_trading_days(dates)
    signals = [date for n, date in enumerate(signals) if n % variant["months"] == variant["months"] - 1]
    executions = {}
    for signal in signals:
        execution = next_date(dates, signal)
        if execution and execution <= END_DATE:
            picks = rank_stocks(stocks, signal, variant["breakout"])[:variant["count"]] if (not variant["market"] or regime(benchmark, signal)) else []
            executions[execution] = picks
    cash = capital
    positions = {}
    trades = []
    curve = []
    year_start, year_end = {}, {}

    def value(code, position, date, at_open=False):
        bar = bar_on_or_after(stocks[code], date)
        if not bar:
            j = idx(stocks[code], date)
            bar = stocks[code][j] if j >= 0 else None
        if not bar:
            return 0, None
        norm = bar.norm * (bar.open / bar.close) if at_open else bar.norm
        return position["gross"] * norm / position["entry_norm"], bar

    for date in dates:
        if date in executions:
            for code, position in list(positions.items()):
                gross, bar = value(code, position, date, True)
                if not bar:
                    continue
                fee = cost(gross, "SELL")
                pnl = gross - fee - position["gross"] - position["fee"]
                cash += gross - fee
                trades.append({"code": code, "pnl": pnl, "costs": fee + position["fee"]})
                del positions[code]
            picks = executions[date]
            allocation = cash / len(picks) if picks else 0
            for pick in picks:
                bar = bar_on_or_after(stocks[pick["code"]], date)
                if not bar:
                    continue
                shares = math.floor(allocation / bar.open)
                gross = shares * bar.open
                fee = cost(gross, "BUY") if shares else 0
                if not shares or gross + fee > cash:
                    continue
                cash -= gross + fee
                positions[pick["code"]] = {"gross": gross, "fee": fee, "entry_norm": bar.norm * bar.open / bar.close}
        equity = cash + sum(value(code, position, date)[0] for code, position in positions.items())
        curve.append(equity)
        year = date[:4]
        year_start.setdefault(year, equity)
        year_end[year] = equity

    for code, position in list(positions.items()):
        gross, _ = value(code, position, dates[-1])
        fee = cost(gross, "SELL")
        pnl = gross - fee - position["gross"] - position["fee"]
        cash += gross - fee
        trades.append({"code": code, "pnl": pnl, "costs": fee + position["fee"]})
    curve[-1] = cash
    year_end["2025"] = cash
    peak, dd = 0, 0
    for equity in curve:
        peak = max(peak, equity)
        dd = max(dd, (peak - equity) / peak if peak else 0)
    years = 6
    wins = sum(t["pnl"] > 0 for t in trades)
    annual = {year: round((year_end[year] / year_start[year] - 1) * 100, 2) for year in year_start}
    return {
        "id": variant["id"], "name": variant["name"],
        "summary": {
            "final_equity": round(cash, 2), "total_return_pct": round((cash / capital - 1) * 100, 2),
            "cagr_pct": round(((cash / capital) ** (1 / years) - 1) * 100, 2),
            "max_drawdown_pct": round(dd * 100, 2), "trades": len(trades),
            "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0,
            "total_costs": round(sum(t["costs"] for t in trades), 2),
        },
        "annual": annual,
        "phase": {
            "design_2020_2023": round((year_end["2023"] / year_start["2020"] - 1) * 100, 2),
            "validation_2024": annual["2024"],
            "stress_2025": annual["2025"],
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    stocks, benchmark = load_archives(args.archives)
    results = [simulate(stocks, benchmark, variant) for variant in VARIANTS]
    output = {
        "method": "Five economically distinct variants fixed before execution; no parameter grid; 2025 not used for selection.",
        "variants": results,
    }
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
