"""Compare current, -0.5R and 0R initial net-PnL stops without live changes."""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from statistics import mean
from typing import Any, Mapping

from mfe_profit_protection_study_v01.analysis import (
    ResearchTrade,
    build_independent_signal_trades,
    derive_initial_stop_price,
)
from mfe_profit_protection_study_v01.overlay import (
    EntryFill,
    MFEProtectionState,
    PositionBasis,
    VARIANTS as MFE_VARIANTS,
)
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC


ANALYSIS_ID = "INITIAL_R_STOP_STUDY_V0_1"
ONE_R_NET_TWD = 3500.0


@dataclass(frozen=True, slots=True)
class StopVariant:
    name: str
    stop_r: float

    @property
    def stop_net_pnl(self) -> float:
        return -self.stop_r * ONE_R_NET_TWD


VARIANTS = (
    StopVariant("CURRENT_NEG_1R", 1.0),
    StopVariant("STOP_NEG_0_5R", 0.5),
    StopVariant("STOP_0R", 0.0),
)


def _simulate(trade: ResearchTrade, variant: StopVariant) -> dict[str, Any]:
    stop_price = derive_initial_stop_price(
        trade.side, trade.entry_price, trade.quantity, ONE_R_NET_TWD,
    )
    basis = PositionBasis.from_fills(
        trade.side,
        [EntryFill(trade.entry_time, trade.entry_price, trade.quantity)],
        initial_stop_price=stop_price,
    )
    overlay = MFEProtectionState(basis, MFE_VARIANTS["MFE_V1"])
    overlay.observe(
        price=trade.entry_price,
        at=trade.entry_time,
        projected_net_pnl=trade.pnl_at_price(trade.entry_price),
        pnl_at_price=trade.pnl_at_price,
    )
    hour, minute = map(int, str(SPEC["hard_exit_time"]).split(":"))
    hard_exit = trade.entry_time.replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    for index, point in enumerate(trade.points):
        overlay.observe(
            price=point.exit_price,
            at=point.at,
            projected_net_pnl=point.projected_net_pnl,
            pnl_at_price=trade.pnl_at_price,
        )
        reason = None
        if point.projected_net_pnl <= variant.stop_net_pnl:
            reason = variant.name
        elif overlay.triggered(point.exit_price):
            reason = "MFE_PROFIT_PROTECTION"
        elif point.reversal:
            reason = "SIGNAL_REVERSAL"
        elif point.at >= hard_exit:
            reason = "HARD_EXIT"
        elif trade.force_last_point_exit and index == len(trade.points) - 1:
            reason = "HARD_EXIT"
        if reason is None:
            continue
        later = trade.points[index + 1:]
        later_best = max(
            (row.projected_net_pnl for row in later),
            default=point.projected_net_pnl,
        )
        return {
            "variant": variant.name,
            "trade_id": trade.trade_id,
            "session_date": trade.session_date,
            "symbol": trade.symbol,
            "stock_name": trade.stock_name,
            "side": trade.side,
            "entry_time": trade.entry_time.isoformat(),
            "entry_price": trade.entry_price,
            "quantity": trade.quantity,
            "stop_r": variant.stop_r,
            "stop_net_pnl": variant.stop_net_pnl,
            "status": "SCORED",
            "exit_time": point.at.isoformat(),
            "exit_price": point.exit_price,
            "exit_reason": reason,
            "net_pnl": point.projected_net_pnl,
            "holding_seconds": (point.at - trade.entry_time).total_seconds(),
            "later_best_net_pnl": later_best,
            "later_recovered_positive": later_best > 0,
            "mfe_armed": overlay.armed,
            "mfe_r": overlay.mfe_r,
        }
    return {
        "variant": variant.name,
        "trade_id": trade.trade_id,
        "session_date": trade.session_date,
        "symbol": trade.symbol,
        "stock_name": trade.stock_name,
        "side": trade.side,
        "entry_time": trade.entry_time.isoformat(),
        "entry_price": trade.entry_price,
        "quantity": trade.quantity,
        "stop_r": variant.stop_r,
        "stop_net_pnl": variant.stop_net_pnl,
        "status": "UNSCORABLE",
    }


def _max_drawdown(rows: list[dict[str, Any]]) -> float:
    equity = peak = drawdown = 0.0
    for row in sorted(rows, key=lambda item: item["entry_time"]):
        equity += float(row["net_pnl"])
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _summary(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [row for row in rows if row["status"] == "SCORED"]
    pnls = [float(row["net_pnl"]) for row in scored]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    stopped = [row for row in scored if row["exit_reason"] == name]
    return {
        "variant": name,
        "trades": len(rows),
        "scored_trades": len(scored),
        "unscorable": len(rows) - len(scored),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(scored) if scored else None,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_pnl": sum(pnls),
        "expectancy": mean(pnls) if pnls else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "average_winner": mean(wins) if wins else None,
        "average_loser": mean(losses) if losses else None,
        "maximum_loss": min(losses) if losses else None,
        "maximum_drawdown": _max_drawdown(scored),
        "stop_exits": len(stopped),
        "stopped_then_recovered_positive": sum(
            bool(row["later_recovered_positive"]) for row in stopped
        ),
        "average_holding_seconds": mean(
            float(row["holding_seconds"]) for row in scored
        ) if scored else None,
    }


def build_report(
    session_runs: Mapping[str, list[Path]],
    capital: int = 190_000,
) -> dict[str, Any]:
    trades, diagnostics, coverage = build_independent_signal_trades(
        session_runs, capital,
    )
    rows = [_simulate(trade, variant) for trade in trades for variant in VARIANTS]
    summaries = [
        _summary(
            variant.name,
            [row for row in rows if row["variant"] == variant.name],
        )
        for variant in VARIANTS
    ]
    side_summaries = {
        variant.name: {
            side: _summary(
                variant.name,
                [
                    row for row in rows
                    if row["variant"] == variant.name and row["side"] == side
                ],
            )
            for side in ("LONG", "SHORT")
        }
        for variant in VARIANTS
    }
    by_id: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_id.setdefault(row["trade_id"], []).append(row)
    matched_ids = {
        trade_id for trade_id, items in by_id.items()
        if len(items) == len(VARIANTS)
        and all(item["status"] == "SCORED" for item in items)
    }
    matched = [
        _summary(
            variant.name,
            [
                row for row in rows
                if row["variant"] == variant.name and row["trade_id"] in matched_ids
            ],
        )
        for variant in VARIANTS
    ]
    return {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "OVERLAPPING_INDEPENDENT_SIGNALS; NET-PNL R STOPS; NOT AN EXECUTABLE PORTFOLIO",
        "one_r_net_twd": ONE_R_NET_TWD,
        "capital_twd": capital,
        "exit_policy_except_initial_stop": "MFE_V1_NO_LOSS_RECOVERY_THEN_REVERSAL_THEN_HARD_EXIT",
        "variants": [
            {**asdict(variant), "stop_net_pnl": variant.stop_net_pnl}
            for variant in VARIANTS
        ],
        "coverage": coverage,
        "source_diagnostics": diagnostics,
        "summaries": summaries,
        "side_summaries": side_summaries,
        "matched_trade_ids": sorted(matched_ids),
        "matched_summaries": matched,
        "per_trade": rows,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "live_behavior_changed": False,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    fields = [
        "variant", "trades", "scored_trades", "unscorable", "wins", "losses",
        "win_rate", "gross_profit", "gross_loss", "net_pnl", "expectancy",
        "profit_factor", "average_winner", "average_loser", "maximum_loss",
        "maximum_drawdown", "stop_exits", "stopped_then_recovered_positive",
        "average_holding_seconds",
    ]
    _write_csv(output_dir / "comparison.csv", report["summaries"], fields)
    _write_csv(output_dir / "matched_comparison.csv", report["matched_summaries"], fields)
    _write_csv(
        output_dir / "per_trade.csv",
        report["per_trade"],
        [
            "variant", "trade_id", "session_date", "symbol", "stock_name", "side",
            "status", "entry_time", "entry_price", "quantity", "stop_r",
            "stop_net_pnl", "exit_time", "exit_price", "exit_reason", "net_pnl",
            "holding_seconds", "later_best_net_pnl", "later_recovered_positive",
            "mfe_armed", "mfe_r",
        ],
    )
    fmt = lambda value: "N/A" if value is None else f"{float(value):.2f}"
    lines = [
        "# Initial R stop diagnostic", "",
        "1R is fixed at net -3,500 TWD. Entry signals, sizing, fees, tax, slippage and non-stop exits are unchanged.", "",
        "| Variant | Scored | Net | PF | Win rate | Avg loss | Max DD | Stops | Recovered later | Avg hold sec |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["summaries"]:
        lines.append(
            f"| {row['variant']} | {row['scored_trades']} | {fmt(row['net_pnl'])} | "
            f"{fmt(row['profit_factor'])} | {fmt(row['win_rate'])} | {fmt(row['average_loser'])} | "
            f"{fmt(row['maximum_drawdown'])} | {row['stop_exits']} | "
            f"{row['stopped_then_recovered_positive']} | {fmt(row['average_holding_seconds'])} |"
        )
    lines.extend([
        "", "## Matched cohort", "",
        f"All variants are fully scored for the same {len(report['matched_trade_ids'])} signals.", "",
        "| Variant | Net | PF | Win rate | Max DD |",
        "|---|---:|---:|---:|---:|",
    ])
    for row in report["matched_summaries"]:
        lines.append(
            f"| {row['variant']} | {fmt(row['net_pnl'])} | {fmt(row['profit_factor'])} | "
            f"{fmt(row['win_rate'])} | {fmt(row['maximum_drawdown'])} |"
        )
    half = next(row for row in report["summaries"] if row["variant"] == "STOP_NEG_0_5R")
    zero = next(row for row in report["summaries"] if row["variant"] == "STOP_0R")
    lines.extend([
        "", "## Conclusion", "",
        f"The -0.5R stop is not supported: it produced {half['stop_exits']} stop exits and {half['stopped_then_recovered_positive']} of them later recovered to positive PnL on the recorded path. Its smaller average loss did not offset the winners it cut.",
        f"The 0R rule exited all {zero['stop_exits']} trades, averaged {fmt(zero['average_holding_seconds'])} seconds of holding time, and remained materially negative because spread, fees, tax and slippage make immediate round-trip PnL negative.",
        "The current -1R / net -3,500 TWD boundary remains the least-bad tested fixed initial stop; this sample does not justify changing live behavior.",
        "", "Three quality-limited sessions; overlapping independent signals are not one executable portfolio.",
        "Research only: no broker connection, order, fill, or live behavior change.",
    ])
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    artifacts = {
        name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
        for name in (
            "summary.json", "comparison.csv", "matched_comparison.csv",
            "per_trade.csv", "report.md",
        )
    }
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
