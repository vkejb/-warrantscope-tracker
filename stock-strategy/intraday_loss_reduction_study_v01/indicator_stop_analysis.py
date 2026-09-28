"""Indicator-driven stop overlays using causal Yuanta tick and five-level flow."""
from __future__ import annotations

from bisect import bisect_right
import csv
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from mfe_profit_protection_study_v01.analysis import (
    BASELINE, ExistingExitState, ResearchTrade, _existing_exit_reason,
    build_independent_signal_trades, simulate_trade,
)
from yuanta_intraday_shadow_v01.direction_follow_backtest import load_session
from yuanta_intraday_shadow_v01.flow_exit_sweep import FlowSnapshot, build_flow_snapshots

from .stop_loss_analysis import _summary

ANALYSIS_ID = "INTRADAY_INDICATOR_STOP_DIAGNOSTIC_V0_1"


@dataclass(frozen=True, slots=True)
class IndicatorVariant:
    name: str
    hard_stop_twd: float = 5000
    minimum_loss_twd: float = 0
    adverse_components: int = 2
    consecutive: int = 2
    require_vwap_break: bool = False
    require_book_depletion: bool = False
    require_no_positive_mfe: bool = False
    diagnostic_status: str = "PREDECLARED"


VARIANTS = (
    IndicatorVariant("BASELINE_5000"),
    IndicatorVariant("ADVERSE_2OF3_2X_WHILE_LOSS"),
    IndicatorVariant("ADVERSE_2OF3_2X_AFTER_1000_LOSS", minimum_loss_twd=1000),
    IndicatorVariant("ADVERSE_3OF3_1X_WHILE_LOSS", adverse_components=3, consecutive=1),
    IndicatorVariant("VWAP_BREAK_ADVERSE_2OF3_2X", require_vwap_break=True),
    IndicatorVariant("BOOK_DEPLETION_ADVERSE_2OF3_2X", require_book_depletion=True),
    IndicatorVariant("VWAP_BREAK_ADVERSE_2OF3_2X_HARD_4000", hard_stop_twd=4000, require_vwap_break=True),
    IndicatorVariant(
        "ADVERSE_2OF3_2X_AFTER_1000_LOSS_NO_POSITIVE_MFE",
        minimum_loss_twd=1000,
        require_no_positive_mfe=True,
        diagnostic_status="EXPLORATORY_POST_HOC",
    ),
    IndicatorVariant(
        "VWAP_BREAK_ADVERSE_2OF3_2X_NO_POSITIVE_MFE",
        require_vwap_break=True,
        require_no_positive_mfe=True,
        diagnostic_status="EXPLORATORY_POST_HOC",
    ),
    IndicatorVariant(
        "VWAP_BREAK_ADVERSE_2OF3_2X_NO_POSITIVE_MFE_HARD_4000",
        hard_stop_twd=4000,
        require_vwap_break=True,
        require_no_positive_mfe=True,
        diagnostic_status="EXPLORATORY_POST_HOC",
    ),
)


def _adverse_components(side: str, snapshot: FlowSnapshot) -> int:
    direction = 1 if side == "LONG" else -1
    values = (
        snapshot.normalized_delta_60 * direction < 0,
        snapshot.large_trade_delta_60 * direction < 0,
        snapshot.book_imbalance is not None and snapshot.book_imbalance * direction < 0,
    )
    return sum(bool(value) for value in values)


def _vwap_broken(side: str, data: dict, at: datetime) -> bool:
    index = bisect_right(data["tick_times"], at)
    rows = data["ticks"][:index]
    volume = sum(float(row["volume"]) for row in rows)
    if not rows or volume <= 0:
        return False
    vwap = sum(float(row["price"]) * float(row["volume"]) for row in rows) / volume
    current = float(rows[-1]["price"])
    return current < vwap if side == "LONG" else current > vwap


def _book_depleted(side: str, previous: FlowSnapshot | None, current: FlowSnapshot) -> bool:
    if previous is None or previous.book_imbalance is None or current.book_imbalance is None:
        return False
    direction = 1 if side == "LONG" else -1
    shift = (current.book_imbalance - previous.book_imbalance) * direction
    return current.book_imbalance * direction < 0 and shift <= -0.10


def _indicator_triggered(
    variant: IndicatorVariant,
    trade: ResearchTrade,
    data: dict,
    current: FlowSnapshot,
    previous: FlowSnapshot | None,
    pnl: float,
    positive_mfe_seen: bool = False,
) -> bool:
    if pnl >= -variant.minimum_loss_twd:
        return False
    if variant.require_no_positive_mfe and positive_mfe_seen:
        return False
    current_adverse = _adverse_components(trade.side, current) >= variant.adverse_components
    if not current_adverse:
        return False
    if variant.consecutive >= 2:
        if previous is None:
            return False
        interval = timedelta(seconds=30)
        if current.at - previous.at != interval:
            return False
        if _adverse_components(trade.side, previous) < variant.adverse_components:
            return False
    if variant.require_vwap_break and not _vwap_broken(trade.side, data, current.at):
        return False
    if variant.require_book_depletion and not _book_depleted(trade.side, previous, current):
        return False
    return True


def _result(trade: ResearchTrade, variant: str, index: int, reason: str) -> dict[str, Any]:
    point = trade.points[index]
    later = trade.points[index + 1:]
    post_best = max((row.projected_net_pnl for row in later), default=point.projected_net_pnl)
    return {
        "variant": variant, "trade_id": trade.trade_id,
        "session_date": trade.session_date, "symbol": trade.symbol,
        "stock_name": trade.stock_name, "side": trade.side,
        "entry_time": trade.entry_time.isoformat(), "entry_price": trade.entry_price,
        "quantity": trade.quantity, "exit_time": point.at.isoformat(),
        "exit_price": point.exit_price, "exit_reason": reason,
        "net_pnl": point.projected_net_pnl,
        "holding_seconds": (point.at - trade.entry_time).total_seconds(),
        "post_exit_best_net_pnl": post_best,
        "later_recovered_positive": post_best > 0,
    }


def simulate_indicator_stop(
    trade: ResearchTrade,
    data: dict,
    variant: IndicatorVariant,
    snapshots: tuple[FlowSnapshot, ...] | None = None,
) -> dict[str, Any]:
    if variant.name == "BASELINE_5000":
        result = simulate_trade(trade, BASELINE, enable_mfe_profit_protection=False)
        return _result(trade, variant.name, result.index, result.reason)

    if snapshots is None:
        path_proxy = SimpleNamespace(decision_time=trade.entry_time)
        snapshots = build_flow_snapshots(data, path_proxy)
    snapshot_index = 0
    previous = current = None
    positive_mfe_seen = False
    state = ExistingExitState()
    hard_exit = trade.entry_time.replace(hour=13, minute=20, second=0, microsecond=0)

    for index, point in enumerate(trade.points):
        positive_mfe_seen = positive_mfe_seen or point.projected_net_pnl > 0
        while snapshot_index < len(snapshots) and snapshots[snapshot_index].at <= point.at:
            previous, current = current, snapshots[snapshot_index]
            snapshot_index += 1
        reason = None
        if point.projected_net_pnl <= -variant.hard_stop_twd:
            reason = "DISASTER_HARD_STOP"
        elif current is not None and point.at - current.at <= timedelta(seconds=30):
            if _indicator_triggered(
                variant, trade, data, current, previous,
                point.projected_net_pnl, positive_mfe_seen,
            ):
                reason = "INDICATOR_STOP"
        if reason is None:
            original = _existing_exit_reason(point, state, hard_exit)
            if original == "STOP_LOSS":
                original = None
            reason = original
        else:
            state.peak_return = max(state.peak_return, point.current_return)
            state.worst_return = min(state.worst_return, point.current_return)
        if reason is not None:
            return _result(trade, variant.name, index, reason)
        if trade.force_last_point_exit and index == len(trade.points) - 1:
            return _result(trade, variant.name, index, "HARD_EXIT")
    raise RuntimeError(f"no exit for {trade.trade_id}/{variant.name}")


def build_indicator_report(session_runs: Mapping[str, list[Path]], capital: int = 190_000) -> dict[str, Any]:
    trades, diagnostics, coverage = build_independent_signal_trades(session_runs, capital)
    session_stocks = {}
    for _date, paths in sorted(session_runs.items()):
        stocks, manifest = load_session(paths)
        session_stocks[str(manifest["session_date"])] = stocks
    rows = []
    for trade in trades:
        data = session_stocks[trade.session_date][trade.symbol]
        path_proxy = SimpleNamespace(decision_time=trade.entry_time)
        snapshots = build_flow_snapshots(data, path_proxy)
        rows.extend(
            simulate_indicator_stop(trade, data, variant, snapshots)
            for variant in VARIANTS
        )
    summaries = []
    for variant in VARIANTS:
        selected = [row for row in rows if row["variant"] == variant.name]
        summary = _summary(variant.name, selected)
        summary["diagnostic_status"] = variant.diagnostic_status
        summary["indicator_exits"] = sum(row["exit_reason"] == "INDICATOR_STOP" for row in selected)
        summary["indicator_exits_then_recovered_positive"] = sum(
            row["exit_reason"] == "INDICATOR_STOP" and row["later_recovered_positive"]
            for row in selected
        )
        summary["before_1030"] = _summary(variant.name, [
            row for row in selected
            if datetime.fromisoformat(row["entry_time"]).time() < time(10, 30)
        ])
        summaries.append(summary)
    return {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "CAUSAL YUANTA TICK/FIVE_LEVEL INDICATOR STOPS; OVERLAPPING DIAGNOSTIC COHORT",
        "historical_yuanta_fields_used": [
            "tick price", "tick volume", "tick bid", "tick ask",
            "tick in/out flag", "aggregated five-level bid volume",
            "aggregated five-level ask volume",
        ],
        "audited_but_not_historically_available": [
            "GetStockInformation day-trade eligibility and reference fields",
            "GetWatchListAll cumulative inside/outside volume",
            "GetWatchListAll bid/ask order counts",
            "per-level five-level queue changes",
        ],
        "coverage": coverage, "diagnostics": diagnostics,
        "summaries": summaries, "per_trade": rows,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
        "live_behavior_changed": False,
    }


def write_indicator_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = ["variant", "diagnostic_status", "trades", "wins", "losses", "win_rate", "net_pnl", "expectancy", "profit_factor", "average_winner", "average_loser", "maximum_loss", "maximum_drawdown", "net_without_largest_winner", "indicator_exits", "indicator_exits_then_recovered_positive"]
    with (output_dir / "comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader(); writer.writerows({field: row.get(field) for field in fields} for row in report["summaries"])
    per_fields = ["variant", "trade_id", "session_date", "symbol", "stock_name", "side", "entry_time", "exit_time", "exit_reason", "net_pnl", "holding_seconds", "post_exit_best_net_pnl", "later_recovered_positive"]
    with (output_dir / "per_trade.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=per_fields, lineterminator="\n")
        writer.writeheader(); writer.writerows({field: row.get(field) for field in per_fields} for row in report["per_trade"])
    lines = ["# Indicator stop diagnostic", "", "Uses causal Yuanta tick flow, large-trade flow and aggregated five-level imbalance.", "", "| Variant | Status | Net | PF | Max DD | Indicator exits | Recovered later | Before 10:30 net | Before 10:30 PF |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    f = lambda value: "N/A" if value is None else f"{float(value):.2f}"
    for row in report["summaries"]:
        lines.append(f"| {row['variant']} | {row['diagnostic_status']} | {f(row['net_pnl'])} | {f(row['profit_factor'])} | {f(row['maximum_drawdown'])} | {row['indicator_exits']} | {row['indicator_exits_then_recovered_positive']} | {f(row['before_1030']['net_pnl'])} | {f(row['before_1030']['profit_factor'])} |")
    by_name = {row["variant"]: row for row in report["summaries"]}
    baseline = by_name["BASELINE_5000"]
    structural = by_name["VWAP_BREAK_ADVERSE_2OF3_2X"]
    structural_cap = by_name["VWAP_BREAK_ADVERSE_2OF3_2X_HARD_4000"]
    gate = by_name["ADVERSE_2OF3_2X_AFTER_1000_LOSS"]
    def reduction(row):
        return 1.0 - abs(float(row["net_pnl"])) / abs(float(baseline["net_pnl"]))
    baseline_rows = {
        row["trade_id"]: row for row in report["per_trade"]
        if row["variant"] == "BASELINE_5000"
    }
    structural_rows = [
        row for row in report["per_trade"]
        if row["variant"] == "VWAP_BREAK_ADVERSE_2OF3_2X"
    ]
    worst_cut = min(
        structural_rows,
        key=lambda row: float(row["net_pnl"]) - float(baseline_rows[row["trade_id"]]["net_pnl"]),
    )
    original = baseline_rows[worst_cut["trade_id"]]
    lines.extend([
        "", "## Diagnostic readout", "",
        f"- Pure VWAP plus consecutive 2-of-3 adverse flow reduced sampled loss by {reduction(structural):.1%}, from {f(baseline['net_pnl'])} to {f(structural['net_pnl'])} TWD, but remained negative.",
        f"- The same evidence with a 4,000 TWD disaster cap reduced sampled loss by {reduction(structural_cap):.1%}, to {f(structural_cap['net_pnl'])} TWD.",
        f"- The best predeclared loss result was the 1,000 TWD activation-zone variant at {f(gate['net_pnl'])} TWD and PF {f(gate['profit_factor'])}; this still uses a fixed-money gate and therefore is not a pure indicator stop.",
        f"- Largest premature cut: {worst_cut['session_date']} {worst_cut['symbol']} changed from {f(original['net_pnl'])} to {f(worst_cut['net_pnl'])} TWD, and later recovered positive.",
        "- The post-hoc no-positive-MFE check changed no exit because that recovering trade had not yet shown positive net PnL when the early indicator fired.",
    ])
    lines.extend([
        "", "## Data boundary", "",
        "Used: saved Yuanta tick price/volume/bid/ask/in-out flag and aggregated five-level bid/ask volume.",
        "Not used: newly audited GetStockInformation/GetWatchListAll fields and per-level queue changes, because they were not saved in these historical sessions.",
        "", "The fixed amount is only a disaster cap; indicator variants trigger from market evidence first.",
        "Variants marked EXPLORATORY_POST_HOC were added after reviewing the first comparison and require new out-of-sample sessions.",
        "No production or live behavior changed.",
    ])
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    artifacts = {name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest() for name in ("summary.json", "comparison.csv", "per_trade.csv", "report.md")}
    manifest = {"analysis_id": ANALYSIS_ID, "artifacts": artifacts, "actual_orders": 0, "actual_fills": 0, "broker_connections": 0, "live_behavior_changed": False}
    (output_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
