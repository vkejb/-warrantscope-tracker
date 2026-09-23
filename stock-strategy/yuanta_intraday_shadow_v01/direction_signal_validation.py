"""Independent first-signal validation across every affordable Top30 stock."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path

from .direction_follow_backtest import (
    SPEC, _decision_times, _entry_tick, _tick_size, load_session,
    signal_at, simulate_signal_trade,
)


INTERPRETATION = "INDEPENDENT_SIGNAL_VALIDATION_NOT_AN_EXECUTABLE_190K_PORTFOLIO"


def validate_session(stocks: dict[str, dict], coverage: dict, capital: int = 190000) -> tuple[list, dict]:
    session_date = coverage["session_date"]
    attempted: set[str] = set()
    trades = []
    diagnostics = {
        "session_date": session_date, "qualifying_signal_windows": 0,
        "unaffordable_signal_windows": 0, "independent_entries": 0,
        "scored_trades": 0, "unscorable_exit_data": 0,
    }
    for decision in _decision_times(session_date, SPEC["entry_start"], SPEC["last_entry_time"]):
        for stock_id, data in stocks.items():
            if stock_id in attempted:
                continue
            signal = signal_at(stock_id, data, decision)
            if signal is None:
                continue
            diagnostics["qualifying_signal_windows"] += 1
            entry_tick = _entry_tick(data, decision)
            if entry_tick is None:
                continue
            entry_price = (
                entry_tick["ask"] + _tick_size(entry_tick["ask"])
                if signal.side == "LONG"
                else max(_tick_size(entry_tick["bid"]), entry_tick["bid"] - _tick_size(entry_tick["bid"]))
            )
            if entry_price * 1000 > capital:
                diagnostics["unaffordable_signal_windows"] += 1
                continue
            attempted.add(stock_id)
            quantity = math.floor(capital / (entry_price * 1000)) * 1000
            diagnostics["independent_entries"] += 1
            trade, exit_diagnostics = simulate_signal_trade(
                session_date, data, signal, entry_tick, entry_price, quantity,
            )
            if trade is None:
                if exit_diagnostics.get("exit_data_insufficient"):
                    diagnostics["unscorable_exit_data"] += 1
                continue
            trades.append(trade)
            diagnostics["scored_trades"] += 1
    return trades, diagnostics


def build_validation_report(session_runs: dict[str, list[Path]], capital: int = 190000) -> dict:
    coverages, diagnostics, trades = [], [], []
    for _, run_dirs in sorted(session_runs.items()):
        stocks, coverage = load_session(run_dirs)
        session_trades, session_diagnostics = validate_session(stocks, coverage, capital)
        coverages.append(coverage); diagnostics.append(session_diagnostics); trades.extend(session_trades)

    side_summaries = []
    for side in ("LONG", "SHORT"):
        selected = [trade for trade in trades if trade.side == side]
        side_summaries.append({
            "side": side, "trade_count": len(selected),
            "profitable_trades": sum(trade.net_pnl > 0 for trade in selected),
            "profitable_rate": (sum(trade.net_pnl > 0 for trade in selected) / len(selected)) if selected else None,
            "diagnostic_sum_net_pnl_twd": round(sum(trade.net_pnl for trade in selected), 2),
        })
    return {
        "spec": SPEC, "capital_per_independent_signal_twd": capital,
        "interpretation": INTERPRETATION, "coverage": coverages, "diagnostics": diagnostics,
        "trades": [asdict(trade) for trade in trades], "scored_trade_count": len(trades),
        "profitable_trades": sum(trade.net_pnl > 0 for trade in trades),
        "profitable_rate": (sum(trade.net_pnl > 0 for trade in trades) / len(trades)) if trades else None,
        "diagnostic_sum_net_pnl_twd": round(sum(trade.net_pnl for trade in trades), 2),
        "side_summaries": side_summaries,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
    }


def _session_arg(value: str) -> tuple[str, list[Path]]:
    try:
        date, paths = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use YYYYMMDD=RUN_DIR[,RUN_DIR]") from exc
    return date, [Path(path) for path in paths.split(",")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate every affordable Top30 first direction signal independently")
    parser.add_argument("--session", action="append", required=True, type=_session_arg)
    parser.add_argument("--capital", type=int, default=190000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_validation_report(dict(args.session), args.capital)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
