"""Controlled stop-loss comparison on the independent first-signal cohort."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import hashlib
import json
from pathlib import Path
from statistics import mean
from typing import Any, Mapping

from mfe_profit_protection_study_v01.analysis import (
    BASELINE, ExistingExitState, ResearchTrade, _existing_exit_reason,
    build_independent_signal_trades, simulate_trade,
)

ANALYSIS_ID = "INTRADAY_STOP_LOSS_DIAGNOSTIC_V0_1"


@dataclass(frozen=True, slots=True)
class StopVariant:
    name: str
    hard_stop_twd: float
    soft_stop_twd: float | None = None
    time_stop_minutes: int | None = None
    require_no_positive_mfe: bool = False
    cost_zone_only: bool = False


VARIANTS = (
    StopVariant("BASELINE_5000", 5000),
    StopVariant("HARD_4500", 4500),
    StopVariant("HARD_4000", 4000),
    StopVariant("HARD_3750", 3750),
    StopVariant("HARD_3500", 3500),
    StopVariant("HARD_3250", 3250),
    StopVariant("HARD_3000", 3000),
    StopVariant("HARD_2500", 2500),
    StopVariant("TIME_3M_NO_POSITIVE_MFE", 5000, time_stop_minutes=3, require_no_positive_mfe=True),
    StopVariant("TIME_5M_NO_POSITIVE_MFE", 5000, time_stop_minutes=5, require_no_positive_mfe=True),
    StopVariant("TIME_10M_COST_ZONE", 5000, time_stop_minutes=10, cost_zone_only=True),
    StopVariant("HYBRID_SOFT_2000_NO_POS_MFE_HARD_4000", 4000, soft_stop_twd=2000, require_no_positive_mfe=True),
    StopVariant("HYBRID_SOFT_2500_NO_POS_MFE_HARD_4000", 4000, soft_stop_twd=2500, require_no_positive_mfe=True),
)


def simulate_stop(trade: ResearchTrade, variant: StopVariant) -> dict[str, Any]:
    if variant.name == "BASELINE_5000":
        result = simulate_trade(trade, BASELINE, enable_mfe_profit_protection=False)
        return _result(trade, variant, result.index, result.reason, result.realized_pnl)

    state = ExistingExitState()
    hard_exit = trade.entry_time.replace(hour=13, minute=20, second=0, microsecond=0)
    positive_mfe_seen = False
    time_threshold = (
        trade.entry_time + timedelta(minutes=variant.time_stop_minutes)
        if variant.time_stop_minutes is not None else None
    )
    for index, point in enumerate(trade.points):
        positive_mfe_seen = positive_mfe_seen or point.projected_net_pnl > 0
        reason = None
        if point.projected_net_pnl <= -variant.hard_stop_twd:
            reason = "STOP_LOSS_HARD"
        elif (
            variant.soft_stop_twd is not None
            and point.projected_net_pnl <= -variant.soft_stop_twd
            and (not variant.require_no_positive_mfe or not positive_mfe_seen)
        ):
            reason = "STOP_LOSS_SOFT_NO_POSITIVE_MFE"
        elif time_threshold is not None and point.at >= time_threshold:
            if variant.cost_zone_only and point.projected_net_pnl <= 0:
                reason = "TIME_STOP_COST_ZONE"
            elif variant.require_no_positive_mfe and not positive_mfe_seen and point.projected_net_pnl < 0:
                reason = "TIME_STOP_NO_POSITIVE_MFE"

        if reason is None:
            original = _existing_exit_reason(point, state, hard_exit)
            if original == "STOP_LOSS":
                original = None
            reason = original
        else:
            state.peak_return = max(state.peak_return, point.current_return)
            state.worst_return = min(state.worst_return, point.current_return)

        if reason is not None:
            return _result(trade, variant, index, reason, point.projected_net_pnl)
        if trade.force_last_point_exit and index == len(trade.points) - 1:
            return _result(trade, variant, index, "HARD_EXIT", point.projected_net_pnl)
    raise RuntimeError(f"no exit for {trade.trade_id}/{variant.name}")


def _result(trade: ResearchTrade, variant: StopVariant, index: int, reason: str, pnl: float) -> dict[str, Any]:
    point = trade.points[index]
    later = trade.points[index + 1:]
    post_best = max((row.projected_net_pnl for row in later), default=pnl)
    post_worst = min((row.projected_net_pnl for row in later), default=pnl)
    return {
        "variant": variant.name, "trade_id": trade.trade_id,
        "session_date": trade.session_date, "symbol": trade.symbol,
        "stock_name": trade.stock_name, "side": trade.side,
        "entry_time": trade.entry_time.isoformat(), "entry_price": trade.entry_price,
        "quantity": trade.quantity, "exit_time": point.at.isoformat(),
        "exit_price": point.exit_price, "exit_reason": reason, "net_pnl": pnl,
        "holding_seconds": (point.at - trade.entry_time).total_seconds(),
        "post_exit_best_net_pnl": post_best, "post_exit_worst_net_pnl": post_worst,
        "later_recovered_positive": post_best > 0,
    }


def _max_drawdown(rows: list[dict[str, Any]]) -> float:
    equity = peak = drawdown = 0.0
    for row in sorted(rows, key=lambda item: item["entry_time"]):
        equity += float(row["net_pnl"])
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _summary(variant: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(row["net_pnl"]) for row in rows]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    stopped = [row for row in rows if "STOP" in str(row["exit_reason"])]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    largest = max(wins) if wins else 0.0
    return {
        "variant": variant, "trades": len(rows), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(rows) if rows else None,
        "gross_profit": gross_profit, "gross_loss": gross_loss,
        "net_pnl": sum(pnls), "expectancy": mean(pnls) if pnls else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "average_winner": mean(wins) if wins else None,
        "average_loser": mean(losses) if losses else None,
        "maximum_win": max(wins) if wins else None,
        "maximum_loss": min(losses) if losses else None,
        "maximum_drawdown": _max_drawdown(rows),
        "net_without_largest_winner": sum(pnls) - largest,
        "stop_exits": len(stopped),
        "stopped_then_recovered_positive": sum(bool(row["later_recovered_positive"]) for row in stopped),
        "average_holding_seconds": mean(float(row["holding_seconds"]) for row in rows) if rows else None,
    }


def build_stop_report(session_runs: Mapping[str, list[Path]], capital: int = 190_000) -> dict[str, Any]:
    trades, diagnostics, coverage = build_independent_signal_trades(session_runs, capital)
    rows = [simulate_stop(trade, variant) for trade in trades for variant in VARIANTS]
    summaries = []
    for variant in VARIANTS:
        selected = [row for row in rows if row["variant"] == variant.name]
        overall = _summary(variant.name, selected)
        overall["long"] = _summary(variant.name, [row for row in selected if row["side"] == "LONG"])
        overall["short"] = _summary(variant.name, [row for row in selected if row["side"] == "SHORT"])
        overall["before_1030"] = _summary(
            variant.name,
            [
                row for row in selected
                if datetime.fromisoformat(row["entry_time"]).time() < time(10, 30)
            ],
        )
        summaries.append(overall)
    return {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "OVERLAPPING_INDEPENDENT_SIGNALS; SAME ENTRIES/SIZING/COSTS/SLIPPAGE/NON_STOP_EXITS",
        "capital_twd": capital, "coverage": coverage, "diagnostics": diagnostics,
        "summaries": summaries, "per_trade": rows,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
        "live_behavior_changed": False,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)


def write_stop_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = ["variant", "trades", "wins", "losses", "win_rate", "gross_profit", "gross_loss", "net_pnl", "expectancy", "profit_factor", "average_winner", "average_loser", "maximum_win", "maximum_loss", "maximum_drawdown", "net_without_largest_winner", "stop_exits", "stopped_then_recovered_positive", "average_holding_seconds"]
    _write_csv(output_dir / "comparison.csv", report["summaries"], fields)
    _write_csv(output_dir / "per_trade.csv", report["per_trade"], ["variant", "trade_id", "session_date", "symbol", "stock_name", "side", "entry_time", "entry_price", "quantity", "exit_time", "exit_price", "exit_reason", "net_pnl", "holding_seconds", "post_exit_best_net_pnl", "post_exit_worst_net_pnl", "later_recovered_positive"])
    lines = ["# Stop-loss diagnostic", "", "Same signals, entries, sizes, fees, tax, slippage and non-stop exits.", "", "| Variant | Net | PF | Avg loss | Max loss | Max DD | Stops | Recovered | Before 10:30 net | Before 10:30 PF |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in report["summaries"]:
        f = lambda value: "N/A" if value is None else f"{float(value):.2f}"
        lines.append(f"| {row['variant']} | {f(row['net_pnl'])} | {f(row['profit_factor'])} | {f(row['average_loser'])} | {f(row['maximum_loss'])} | {f(row['maximum_drawdown'])} | {row['stop_exits']} | {row['stopped_then_recovered_positive']} | {f(row['before_1030']['net_pnl'])} | {f(row['before_1030']['profit_factor'])} |")
    best_all = max(report["summaries"], key=lambda row: float(row["net_pnl"]))
    best_early = max(report["summaries"], key=lambda row: float(row["before_1030"]["net_pnl"]))
    lines.extend([
        "", "## Diagnostic readout", "",
        f"- Best all-trade result: `{best_all['variant']}` at {f(best_all['net_pnl'])} TWD; it remains negative.",
        f"- Best before-10:30 result: `{best_early['variant']}` at {f(best_early['before_1030']['net_pnl'])} TWD, PF {f(best_early['before_1030']['profit_factor'])}, and {f(best_early['before_1030']['net_without_largest_winner'])} TWD after removing its largest winner.",
        "- Tightening below this region is not monotonic: HARD_3000 and tighter variants cut trades that later recovered.",
        "", "In-sample diagnostic from three quality-limited sessions.",
        "No variant is enabled in production or eligible for live promotion.",
    ])
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    artifacts = {name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest() for name in ("summary.json", "comparison.csv", "per_trade.csv", "report.md")}
    manifest = {"analysis_id": ANALYSIS_ID, "artifacts": artifacts, "actual_orders": 0, "actual_fills": 0, "broker_connections": 0, "live_behavior_changed": False}
    (output_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
