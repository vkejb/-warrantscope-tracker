#!/usr/bin/env python3
"""Event-driven exits: monthly scans never force a sale solely because time passed."""

from __future__ import annotations

import argparse, json, math, statistics
from pathlib import Path

from run_backtest import END_DATE, START_DATE, bar_on_or_after, cost, last_trading_days, load_archives, next_date
from research_variants import idx, rank_stocks, regime


RULES = [
    {"id": "trend100", "name": "100日趨勢破壞", "stop": .12, "trail": None, "sma": 100},
    {"id": "trend100_trail10", "name": "100日趨勢＋10%移動停利", "stop": .10, "trail": .10, "sma": 100},
    {"id": "trend60_trail12", "name": "60日趨勢＋12%移動停利", "stop": .10, "trail": .12, "sma": 60},
]


def simulate(stocks, benchmark, rule, capital=30_000):
    dates = sorted({b.date for bars in stocks.values() for b in bars if START_DATE <= b.date <= END_DATE})
    month_ends = set(last_trading_days(dates))
    scans = {next_date(dates, d): d for d in month_ends if next_date(dates, d)}
    cash, positions, trades, curve = capital, {}, [], []
    year_start, year_end = {}, {}

    def px(code, date, opening=False):
        if opening:
            b = bar_on_or_after(stocks[code], date)
        else:
            i = idx(stocks[code], date)
            b = stocks[code][i] if i >= 0 else None
        if not b:
            return None, None
        return b.norm * (b.open / b.close) if opening else b.norm, b

    for n, date in enumerate(dates):
        # Yesterday's close creates today's open order; this avoids look-ahead.
        if n:
            signal_date = dates[n - 1]
            market_off = not regime(benchmark, signal_date)
            for code, p in list(positions.items()):
                i = idx(stocks[code], signal_date)
                if i < rule["sma"]:
                    continue
                close = stocks[code][i].norm
                p["peak"] = max(p["peak"], close)
                sma = statistics.fmean(x.norm for x in stocks[code][i-rule["sma"]+1:i+1])
                loss = close / p["entry"] - 1
                draw = close / p["peak"] - 1
                trend_break = close < sma and draw <= -.05
                trailing = rule["trail"] is not None and p["peak"] / p["entry"] >= 1.10 and draw <= -rule["trail"]
                if market_off or loss <= -rule["stop"] or trend_break or trailing:
                    op, b = px(code, date, True)
                    if op is None:
                        op, b = px(code, signal_date)
                    gross = p["gross"] * op / p["entry"]
                    sell_cost = cost(gross, "SELL")
                    cash += gross - sell_cost
                    pnl = gross - sell_cost - p["gross"] - p["fee"]
                    trades.append({"code": code, "pnl": pnl, "costs": p["fee"] + sell_cost})
                    del positions[code]

        # Scan monthly, but only fill empty slots; existing winners are untouched.
        if date in scans and regime(benchmark, scans[date]) and len(positions) < 10:
            picks = [p for p in rank_stocks(stocks, scans[date]) if p["code"] not in positions]
            slots = 10 - len(positions)
            allocation = cash / slots if slots else 0
            for pick in picks[:slots]:
                _, b = px(pick["code"], date, True)
                if not b:
                    continue
                shares = math.floor(allocation / b.open)
                gross = shares * b.open
                fee = cost(gross, "BUY") if shares else 0
                if not shares or gross + fee > cash:
                    continue
                cash -= gross + fee
                positions[pick["code"]] = {"shares": shares, "gross": gross, "fee": fee, "entry": b.norm*b.open/b.close, "peak": b.norm*b.open/b.close}

        equity = cash
        for code, p in positions.items():
            close, _ = px(code, date)
            equity += p["gross"] * close / p["entry"]
        curve.append(equity)
        year_start.setdefault(date[:4], equity); year_end[date[:4]] = equity

    for code, p in list(positions.items()):
        _, b = px(code, dates[-1])
        gross = p["gross"] * b.norm / p["entry"]
        fee = cost(gross, "SELL"); cash += gross - fee
        trades.append({"code": code, "pnl": gross-fee-p["gross"]-p["fee"], "costs": fee+p["fee"]})
    curve[-1] = cash; year_end["2025"] = cash
    peak = dd = 0
    for e in curve:
        peak = max(peak, e); dd = max(dd, (peak-e)/peak if peak else 0)
    annual = {y: round((year_end[y]/year_start[y]-1)*100, 2) for y in year_start}
    return {"id": rule["id"], "name": rule["name"], "summary": {
        "final_equity": round(cash, 2), "total_return_pct": round((cash/capital-1)*100, 2),
        "cagr_pct": round(((cash/capital)**(1/6)-1)*100, 2), "max_drawdown_pct": round(dd*100, 2),
        "trades": len(trades), "win_rate_pct": round(sum(t["pnl"]>0 for t in trades)/len(trades)*100, 2),
        "total_costs": round(sum(t["costs"] for t in trades), 2)}, "annual": annual}


def main():
    p = argparse.ArgumentParser(); p.add_argument("archives", nargs="+", type=Path); p.add_argument("--output", type=Path, required=True)
    a = p.parse_args(); stocks, benchmark = load_archives(a.archives)
    out = {"method": "Monthly candidate scan; event-driven exits; no calendar-forced liquidation.", "variants": [simulate(stocks, benchmark, r) for r in RULES]}
    a.output.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"); print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
