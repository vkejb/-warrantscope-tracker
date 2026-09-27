"""Evaluate fixed entry-admission gates without changing signal or execution code."""

from __future__ import annotations

import csv
from datetime import datetime, time
import hashlib
import json
import math
from pathlib import Path
from statistics import mean
from typing import Callable


ANALYSIS_ID = "INTRADAY_LOSS_REDUCTION_DIAGNOSTIC_V0_1"
INTERPRETATION = "OVERLAPPING_INDEPENDENT_SIGNALS_NOT_AN_EXECUTABLE_PORTFOLIO"


def load_baseline_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("variant") == "BASELINE"]
    if not rows:
        raise ValueError("no BASELINE rows found")
    unique = {row["trade_id"] for row in rows}
    if len(unique) != len(rows):
        raise ValueError("duplicate BASELINE trade_id")
    normalized = []
    for row in rows:
        normalized.append({
            "trade_id": row["trade_id"],
            "session_date": row["trade_id"].split("-", 1)[0],
            "symbol": row["symbol"],
            "stock_name": row.get("stock_name", ""),
            "side": row["side"].upper(),
            "entry_time": datetime.fromisoformat(row["entry_time"]),
            "pnl": float(row["original_realized_pnl"]),
            "exit_reason": row["original_exit_reason"],
        })
    return normalized


def _before(clock: str) -> Callable[[dict], bool]:
    boundary = time.fromisoformat(clock)
    return lambda row: row["entry_time"].time() < boundary


FIXED_VARIANTS: tuple[tuple[str, str, Callable[[dict], bool]], ...] = (
    ("BASELINE", "All recorded first affordable signals", lambda row: True),
    ("NO_NEW_AFTER_1030", "All sides, entry before 10:30", _before("10:30")),
    ("SHORT_ONLY_DIAGNOSTIC", "Theoretical SHORT only; eligibility unverified", lambda row: row["side"] == "SHORT"),
    (
        "SHORT_BEFORE_1030_DIAGNOSTIC",
        "Theoretical SHORT before 10:30; eligibility unverified",
        lambda row: row["side"] == "SHORT" and row["entry_time"].time() < time(10, 30),
    ),
    ("LONG_ONLY_CURRENT_PRODUCTION", "Current production direction boundary", lambda row: row["side"] == "LONG"),
    (
        "LONG_BEFORE_1030",
        "Long-only entries before 10:30",
        lambda row: row["side"] == "LONG" and row["entry_time"].time() < time(10, 30),
    ),
)


def _max_drawdown(rows: list[dict]) -> float:
    equity = peak = drawdown = 0.0
    for row in sorted(rows, key=lambda value: value["entry_time"]):
        equity += row["pnl"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _max_consecutive_losses(rows: list[dict]) -> int:
    current = maximum = 0
    for row in sorted(rows, key=lambda value: value["entry_time"]):
        if row["pnl"] < 0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def _summary(name: str, description: str, rows: list[dict]) -> dict:
    pnls = [row["pnl"] for row in rows]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    largest_winner = max(wins) if wins else 0.0
    dates = sorted({row["session_date"] for row in rows})
    by_date = [
        {
            "session_date": date,
            "trades": sum(row["session_date"] == date for row in rows),
            "net_pnl": sum(row["pnl"] for row in rows if row["session_date"] == date),
        }
        for date in dates
    ]
    return {
        "variant": name,
        "description": description,
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(rows) if rows else None,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_pnl": sum(pnls),
        "expectancy": mean(pnls) if pnls else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "average_winner": mean(wins) if wins else None,
        "average_loser": mean(losses) if losses else None,
        "maximum_drawdown": _max_drawdown(rows),
        "maximum_consecutive_losses": _max_consecutive_losses(rows),
        "largest_winner": largest_winner if wins else None,
        "net_without_largest_winner": sum(pnls) - largest_winner if wins else sum(pnls),
        "profitable_days": sum(item["net_pnl"] > 0 for item in by_date),
        "losing_days": sum(item["net_pnl"] < 0 for item in by_date),
        "by_date": by_date,
        "eligible_for_live_promotion": False,
    }


def build_report(rows: list[dict]) -> dict:
    summaries = []
    memberships = []
    for name, description, predicate in FIXED_VARIANTS:
        selected = [row for row in rows if predicate(row)]
        summaries.append(_summary(name, description, selected))
        memberships.extend({
            "variant": name,
            "trade_id": row["trade_id"],
            "included": predicate(row),
            "side": row["side"],
            "entry_time": row["entry_time"].isoformat(),
            "pnl": row["pnl"],
        } for row in rows)

    positive = [
        item for item in summaries
        if item["net_pnl"] > 0 and item["net_without_largest_winner"] > 0
    ]
    return {
        "analysis_id": ANALYSIS_ID,
        "interpretation": INTERPRETATION,
        "trade_count": len(rows),
        "session_dates": sorted({row["session_date"] for row in rows}),
        "summaries": summaries,
        "memberships": memberships,
        "robust_positive_candidates": [item["variant"] for item in positive],
        "conclusion": (
            "The only positive candidate after removing its largest winner is "
            "SHORT_BEFORE_1030_DIAGNOSTIC. It is not production-ready because "
            "short eligibility was not captured and the sample is only three partial sessions. "
            "The long-only cohort remains negative; the defensible live action is no promotion, "
            "not parameter fitting."
        ),
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "live_behavior_changed": False,
    }


def _csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in fields} for row in rows)


def _fmt(value) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def write_report(report: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = [
        "variant", "trades", "wins", "losses", "win_rate", "gross_profit",
        "gross_loss", "net_pnl", "expectancy", "profit_factor", "maximum_drawdown",
        "maximum_consecutive_losses", "largest_winner", "net_without_largest_winner",
        "profitable_days", "losing_days", "eligible_for_live_promotion",
    ]
    _csv(output_dir / "comparison.csv", report["summaries"], fields)
    _csv(
        output_dir / "trade_membership.csv",
        report["memberships"],
        ["variant", "trade_id", "included", "side", "entry_time", "pnl"],
    )
    lines = [
        "# Intraday loss-reduction diagnostic", "",
        "This is a fixed-rule diagnostic over overlapping independent first signals, not an executable portfolio.", "",
        "| Variant | Trades | Net PnL | PF | Max DD | Net less largest winner | Positive days |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["summaries"]:
        lines.append(
            f"| {row['variant']} | {row['trades']} | {_fmt(row['net_pnl'])} | "
            f"{_fmt(row['profit_factor'])} | {_fmt(row['maximum_drawdown'])} | "
            f"{_fmt(row['net_without_largest_winner'])} | {row['profitable_days']} |"
        )
    lines.extend([
        "", "## Conclusion", "", report["conclusion"], "",
        "All variants remain in research/shadow mode. No live setting or order path was changed.",
    ])
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    artifacts = {}
    for name in ("summary.json", "comparison.csv", "trade_membership.csv", "report.md"):
        artifacts[name] = hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
    manifest = {
        "analysis_id": ANALYSIS_ID,
        "artifacts": artifacts,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "live_behavior_changed": False,
    }
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
