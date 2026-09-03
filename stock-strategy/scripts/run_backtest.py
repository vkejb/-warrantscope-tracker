#!/usr/bin/env python3
"""Backtest an independent Taiwan stock odd-lot momentum strategy.

Input files are yearly ZIP archives from tw-stock-data-release. Their rows are
compiled from TWSE MI_INDEX and TPEX daily close data. 2019 is used only as a
warm-up period; reported performance is 2020-01-01 through 2025-12-31.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import re
import statistics
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ORDINARY_STOCK = re.compile(r"^[1-9][0-9]{3}$")
START_DATE = "20200101"
END_DATE = "20251231"


@dataclass
class Bar:
    date: str
    name: str
    volume: int
    open: float
    high: float
    low: float
    close: float
    norm: float = 0.0


def parse_float(value: str) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return math.nan


def normalize_bars(bars: list[Bar]) -> list[Bar]:
    unique = {bar.date: bar for bar in bars}
    ordered = sorted(unique.values(), key=lambda bar: bar.date)
    if not ordered:
        return []
    ordered[0].norm = ordered[0].close
    for previous, current in zip(ordered, ordered[1:]):
        raw_ratio = current.close / previous.close
        clean_ratio = 1.0 if raw_ratio < 0.55 or raw_ratio > 1.80 else raw_ratio
        current.norm = previous.norm * clean_ratio
    return ordered


def load_archives(paths: list[Path]) -> tuple[dict[str, list[Bar]], list[Bar]]:
    stocks: dict[str, list[Bar]] = defaultdict(list)
    benchmark: list[Bar] = []
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            csv_name = next(name for name in archive.namelist() if name.endswith(".csv"))
            with archive.open(csv_name) as raw:
                rows = csv.DictReader((line.decode("utf-8-sig") for line in raw))
                for row in rows:
                    code = row["code"].strip()
                    if not ORDINARY_STOCK.fullmatch(code) and code != "0050":
                        continue
                    values = [parse_float(row[key]) for key in ("open", "high", "low", "close")]
                    if any(not math.isfinite(value) or value <= 0 for value in values):
                        continue
                    target = benchmark if code == "0050" else stocks[code]
                    target.append(Bar(
                        date=row["date"].strip(),
                        name=row["name"].strip(),
                        volume=int(parse_float(row["volume"]) or 0),
                        open=values[0], high=values[1], low=values[2], close=values[3],
                    ))
    for code, bars in list(stocks.items()):
        ordered = normalize_bars(bars)
        if len(ordered) < 120:
            del stocks[code]
            continue
        stocks[code] = ordered
    return stocks, normalize_bars(benchmark)


def last_trading_days(dates: list[str]) -> list[str]:
    result = []
    for index, date in enumerate(dates):
        if date < START_DATE or date > END_DATE:
            continue
        if index == len(dates) - 1 or dates[index + 1][:6] != date[:6]:
            result.append(date)
    return result


def next_date(dates: list[str], date: str) -> str | None:
    index = bisect.bisect_right(dates, date)
    return dates[index] if index < len(dates) else None


def metrics(bars: list[Bar], date: str) -> dict | None:
    dates = [bar.date for bar in bars]
    index = bisect.bisect_right(dates, date) - 1
    if index < 120 or (datetime.strptime(date, "%Y%m%d") - datetime.strptime(bars[index].date, "%Y%m%d")).days > 5:
        return None
    window120 = bars[index - 119:index + 1]
    window20 = bars[index - 19:index + 1]
    window5 = bars[index - 4:index + 1]
    current = bars[index]
    average_value20 = statistics.fmean(bar.close * bar.volume for bar in window20)
    average_volume20 = statistics.fmean(bar.volume for bar in window20)
    average_volume5 = statistics.fmean(bar.volume for bar in window5)
    sma120 = statistics.fmean(bar.norm for bar in window120)
    momentum120 = current.norm / bars[index - 120].norm - 1
    momentum20 = current.norm / bars[index - 20].norm - 1
    volume_ratio = average_volume5 / average_volume20 if average_volume20 else 0
    if not (
        current.close >= 15
        and average_value20 >= 30_000_000
        and current.norm > sma120
        and momentum20 > 0
        and volume_ratio >= 1.2
        and -0.30 < momentum120 < 1.50
    ):
        return None
    return {
        "code": "",
        "name": current.name,
        "signal_date": date,
        "signal_close": current.close,
        "momentum_120": momentum120,
        "momentum_20": momentum20,
        "volume_ratio": volume_ratio,
        "average_value_20": average_value20,
        "score": momentum120 * 0.7 + momentum20 * 0.3,
    }


def bar_on_or_after(bars: list[Bar], date: str) -> Bar | None:
    dates = [bar.date for bar in bars]
    index = bisect.bisect_left(dates, date)
    return bars[index] if index < len(bars) and bars[index].date == date else None


def cost(gross: float, side: str, minimum_fee: int = 1) -> float:
    commission = max(minimum_fee, round(gross * 0.001425 * 0.28))
    tax = round(gross * 0.003) if side == "SELL" else 0
    return commission + tax


def benchmark_result(bars: list[Bar]) -> dict:
    period = [bar for bar in bars if START_DATE <= bar.date <= END_DATE]
    start, end = period[0], period[-1]
    total_return = end.norm / start.norm - 1
    peak = 0.0
    drawdown = 0.0
    for bar in period:
        peak = max(peak, bar.norm)
        drawdown = max(drawdown, (peak - bar.norm) / peak)
    years = (datetime.strptime(END_DATE, "%Y%m%d") - datetime.strptime(START_DATE, "%Y%m%d")).days / 365.25
    return {
        "name": "0050 價格報酬（不含股息）",
        "total_return_pct": round(total_return * 100, 2),
        "cagr_pct": round(((1 + total_return) ** (1 / years) - 1) * 100, 2),
        "max_drawdown_pct": round(drawdown * 100, 2),
    }


def backtest(stocks: dict[str, list[Bar]], benchmark: list[Bar], initial_capital: float = 30_000) -> dict:
    all_dates = sorted({bar.date for bars in stocks.values() for bar in bars if START_DATE <= bar.date <= END_DATE})
    signal_dates = last_trading_days(all_dates)
    selections = {}
    for signal_date in signal_dates:
        candidates = []
        for code, bars in stocks.items():
            item = metrics(bars, signal_date)
            if item:
                item["code"] = code
                candidates.append(item)
        selections[signal_date] = sorted(candidates, key=lambda item: item["score"], reverse=True)[:5]

    rebalance = {}
    for signal_date, picks in selections.items():
        execution_date = next_date(all_dates, signal_date)
        if execution_date and execution_date <= END_DATE:
            rebalance[execution_date] = {"signal_date": signal_date, "picks": picks}

    cash = initial_capital
    positions = {}
    trades = []
    equity_curve = []
    yearly_start = {}
    yearly_end = {}

    for date in all_dates:
        if date in rebalance:
            for code, position in list(positions.items()):
                bar = bar_on_or_after(stocks[code], date)
                if not bar:
                    continue
                gross = position["entry_gross"] * (bar.norm / position["entry_norm"])
                fees = cost(gross, "SELL")
                proceeds = gross - fees
                pnl = proceeds - position["entry_gross"] - position["buy_fee"]
                cash += proceeds
                trades.append({
                    "code": code, "name": position["name"],
                    "entry_date": position["entry_date"], "exit_date": date,
                    "entry_price": round(position["entry_price"], 2), "exit_price": round(bar.open, 2),
                    "shares": position["shares"], "pnl": round(pnl, 2),
                    "return_pct": round(pnl / (position["entry_gross"] + position["buy_fee"]) * 100, 2),
                    "costs": round(position["buy_fee"] + fees, 2),
                })
                del positions[code]

            picks = rebalance[date]["picks"]
            allocation = cash / max(1, len(picks))
            for pick in picks:
                bar = bar_on_or_after(stocks[pick["code"]], date)
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
                positions[pick["code"]] = {
                    "name": pick["name"], "entry_date": date, "entry_price": bar.open,
                    "entry_norm": bar.norm * (bar.open / bar.close), "entry_gross": gross,
                    "shares": shares, "buy_fee": buy_fee, "signal": pick,
                }

        market_value = 0.0
        for code, position in positions.items():
            bars = stocks[code]
            dates = [bar.date for bar in bars]
            index = bisect.bisect_right(dates, date) - 1
            if index >= 0:
                market_value += position["entry_gross"] * (bars[index].norm / position["entry_norm"])
        equity = cash + market_value
        equity_curve.append({"date": date, "equity": round(equity, 2)})
        year = date[:4]
        yearly_start.setdefault(year, equity)
        yearly_end[year] = equity

    # Liquidate remaining positions at the final close for a closed-period result.
    final_date = all_dates[-1]
    for code, position in list(positions.items()):
        bar = bar_on_or_after(stocks[code], final_date)
        if not bar:
            bar = stocks[code][-1]
        gross = position["entry_gross"] * (bar.norm / position["entry_norm"])
        fees = cost(gross, "SELL")
        proceeds = gross - fees
        pnl = proceeds - position["entry_gross"] - position["buy_fee"]
        cash += proceeds
        trades.append({
            "code": code, "name": position["name"], "entry_date": position["entry_date"],
            "exit_date": final_date, "entry_price": round(position["entry_price"], 2),
            "exit_price": round(bar.close, 2), "shares": position["shares"],
            "pnl": round(pnl, 2),
            "return_pct": round(pnl / (position["entry_gross"] + position["buy_fee"]) * 100, 2),
            "costs": round(position["buy_fee"] + fees, 2),
        })
    final_equity = cash
    equity_curve[-1]["equity"] = round(final_equity, 2)
    yearly_end["2025"] = final_equity

    peak = 0.0
    max_drawdown = 0.0
    for point in equity_curve:
        peak = max(peak, point["equity"])
        if peak:
            max_drawdown = max(max_drawdown, (peak - point["equity"]) / peak)
    years = (datetime.strptime(END_DATE, "%Y%m%d") - datetime.strptime(START_DATE, "%Y%m%d")).days / 365.25
    wins = [trade for trade in trades if trade["pnl"] > 0]
    gross_profit = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in trades if trade["pnl"] < 0))
    annual = [{
        "year": year,
        "return_pct": round((yearly_end[year] / yearly_start[year] - 1) * 100, 2),
        "end_equity": round(yearly_end[year], 2),
    } for year in sorted(yearly_start)]

    return {
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "period": "2020-01-01 ~ 2025-12-31",
            "warmup": "2019",
            "source": "TWSE MI_INDEX + TPEX daily close; packaged by tw-stock-data-release",
            "source_url": "https://github.com/yukishirotsubasa/tw-stock-data-release/releases/tag/daily-close-csv",
            "universe_count": len(stocks),
            "ordinary_stock_rule": "four-digit codes beginning 1-9",
            "dividends_included": False,
            "corporate_action_method": "daily jumps outside 0.55x-1.80x treated as split/consolidation",
        },
        "strategy": {
            "name": "月頻價量動能零股策略",
            "rules": [
                "四碼普通股且股價至少 15 元",
                "20 日平均成交金額至少 3,000 萬元",
                "收盤高於 120 日均線，且 20 日動能為正",
                "5 日均量 / 20 日均量至少 1.2 倍",
                "以 70% 的 120 日動能加 30% 的 20 日動能排序，選前 5 名",
                "月底收盤產生訊號，下一交易日開盤換股",
                "三萬元等權零股配置，計入手續費折扣、最低 1 元手續費及 0.3% 賣出稅",
            ],
        },
        "summary": {
            "initial_capital": initial_capital,
            "final_equity": round(final_equity, 2),
            "total_return_pct": round((final_equity / initial_capital - 1) * 100, 2),
            "cagr_pct": round(((final_equity / initial_capital) ** (1 / years) - 1) * 100, 2),
            "max_drawdown_pct": round(max_drawdown * 100, 2),
            "trades": len(trades),
            "wins": len(wins),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0,
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
            "total_costs": round(sum(trade["costs"] for trade in trades), 2),
        },
        "benchmark": benchmark_result(benchmark),
        "annual": annual,
        "trades": trades,
        "equity_curve": equity_curve[::5] + ([equity_curve[-1]] if equity_curve[-1] not in equity_curve[::5] else []),
        "monthly_selections": [
            {"signal_date": date, "picks": picks}
            for date, picks in selections.items() if date >= START_DATE
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    stocks, benchmark = load_archives(args.archives)
    result = backtest(stocks, benchmark)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
