"""Causal Yuanta flow confirmation layered over the current exit policy."""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
import hashlib
import json
from pathlib import Path
from statistics import mean
from types import SimpleNamespace
from typing import Any, Mapping

from intraday_loss_reduction_study_v01.indicator_stop_analysis import (
    IndicatorVariant,
    _indicator_triggered,
)
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
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC, load_session
from yuanta_intraday_shadow_v01.flow_exit_sweep import FlowSnapshot, build_flow_snapshots


ANALYSIS_ID = "CURRENT_INDICATOR_STOP_STUDY_V0_1"
ONE_R_NET_TWD = 3500.0


@dataclass(frozen=True, slots=True)
class CurrentIndicatorVariant:
    name: str
    activation_loss_r: float | None
    require_vwap_break: bool = False

    @property
    def activation_loss_twd(self) -> float | None:
        if self.activation_loss_r is None:
            return None
        return self.activation_loss_r * ONE_R_NET_TWD


VARIANTS = (
    CurrentIndicatorVariant("CURRENT_NEG_1R", None),
    CurrentIndicatorVariant("FLOW_2OF3_2X_AFTER_NEG_0_25R", 0.25),
    CurrentIndicatorVariant("FLOW_2OF3_2X_AFTER_NEG_0_25R_VWAP", 0.25, True),
    CurrentIndicatorVariant("FLOW_2OF3_2X_AFTER_NEG_0_5R", 0.5),
)


def _indicator_variant(variant: CurrentIndicatorVariant) -> IndicatorVariant:
    if variant.activation_loss_twd is None:
        raise ValueError("baseline has no indicator variant")
    return IndicatorVariant(
        variant.name,
        hard_stop_twd=ONE_R_NET_TWD,
        minimum_loss_twd=variant.activation_loss_twd,
        adverse_components=2,
        consecutive=2,
        require_vwap_break=variant.require_vwap_break,
    )


def simulate_current_indicator_stop(
    trade: ResearchTrade,
    data: dict[str, Any],
    variant: CurrentIndicatorVariant,
    snapshots: tuple[FlowSnapshot, ...] | None = None,
) -> dict[str, Any]:
    if snapshots is None:
        snapshots = build_flow_snapshots(
            data, SimpleNamespace(decision_time=trade.entry_time),
        )
    indicator = (
        _indicator_variant(variant)
        if variant.activation_loss_twd is not None
        else None
    )
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
    snapshot_index = 0
    previous = current = None
    hour, minute = map(int, str(SPEC["hard_exit_time"]).split(":"))
    hard_exit = trade.entry_time.replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    for index, point in enumerate(trade.points):
        while snapshot_index < len(snapshots) and snapshots[snapshot_index].at <= point.at:
            previous, current = current, snapshots[snapshot_index]
            snapshot_index += 1
        overlay.observe(
            price=point.exit_price,
            at=point.at,
            projected_net_pnl=point.projected_net_pnl,
            pnl_at_price=trade.pnl_at_price,
        )
        reason = None
        if point.projected_net_pnl <= -ONE_R_NET_TWD:
            reason = "DISASTER_STOP_NEG_1R"
        elif overlay.triggered(point.exit_price):
            reason = "MFE_PROFIT_PROTECTION"
        elif (
            indicator is not None
            and current is not None
            and point.at - current.at <= timedelta(seconds=30)
            and _indicator_triggered(
                indicator,
                trade,
                data,
                current,
                previous,
                point.projected_net_pnl,
            )
        ):
            reason = "INDICATOR_STOP"
        elif point.reversal:
            reason = "SIGNAL_REVERSAL"
        elif point.at >= hard_exit:
            reason = "HARD_EXIT"
        elif trade.force_last_point_exit and index == len(trade.points) - 1:
            reason = "HARD_EXIT"
        if reason is None:
            continue
        later = trade.points[index + 1:]
        post_best = max(
            (row.projected_net_pnl for row in later),
            default=point.projected_net_pnl,
        )
        post_worst = min(
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
            "activation_loss_r": variant.activation_loss_r,
            "activation_loss_twd": variant.activation_loss_twd,
            "require_vwap_break": variant.require_vwap_break,
            "status": "SCORED",
            "exit_time": point.at.isoformat(),
            "exit_price": point.exit_price,
            "exit_reason": reason,
            "net_pnl": point.projected_net_pnl,
            "holding_seconds": (point.at - trade.entry_time).total_seconds(),
            "post_exit_best_net_pnl": post_best,
            "post_exit_worst_net_pnl": post_worst,
            "later_recovered_positive": post_best > 0,
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
        "activation_loss_r": variant.activation_loss_r,
        "activation_loss_twd": variant.activation_loss_twd,
        "require_vwap_break": variant.require_vwap_break,
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
    indicator_exits = [
        row for row in scored if row["exit_reason"] == "INDICATOR_STOP"
    ]
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
        "indicator_exits": len(indicator_exits),
        "indicator_exits_then_recovered_positive": sum(
            bool(row["later_recovered_positive"]) for row in indicator_exits
        ),
        "disaster_stops": sum(
            row["exit_reason"] == "DISASTER_STOP_NEG_1R" for row in scored
        ),
        "mfe_exits": sum(
            row["exit_reason"] == "MFE_PROFIT_PROTECTION" for row in scored
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
    session_stocks = {}
    for _date, paths in sorted(session_runs.items()):
        stocks, manifest = load_session(paths)
        session_stocks[str(manifest["session_date"])] = stocks
    rows = []
    for trade in trades:
        data = session_stocks[trade.session_date][trade.symbol]
        snapshots = build_flow_snapshots(
            data, SimpleNamespace(decision_time=trade.entry_time),
        )
        rows.extend(
            simulate_current_indicator_stop(trade, data, variant, snapshots)
            for variant in VARIANTS
        )
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
    before_1030 = {
        variant.name: _summary(
            variant.name,
            [
                row for row in rows
                if row["variant"] == variant.name
                and datetime.fromisoformat(row["entry_time"]).time() < time(10, 30)
            ],
        )
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
    matched_side = {
        variant.name: {
            side: _summary(
                variant.name,
                [
                    row for row in rows
                    if row["variant"] == variant.name
                    and row["trade_id"] in matched_ids
                    and row["side"] == side
                ],
            )
            for side in ("LONG", "SHORT")
        }
        for variant in VARIANTS
    }
    return {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "CURRENT EXIT POLICY PLUS CAUSAL YUANTA FLOW CONFIRMATION; OVERLAPPING INDEPENDENT SIGNALS",
        "one_r_net_twd": ONE_R_NET_TWD,
        "capital_twd": capital,
        "variants": [
            {**asdict(variant), "activation_loss_twd": variant.activation_loss_twd}
            for variant in VARIANTS
        ],
        "flow_rule": {
            "components": [
                "60-second signed-volume direction",
                "60-second large-trade direction",
                "five-level aggregate book imbalance",
            ],
            "required_adverse_components": 2,
            "consecutive_30_second_observations": 2,
        },
        "coverage": coverage,
        "source_diagnostics": diagnostics,
        "summaries": summaries,
        "side_summaries": side_summaries,
        "before_1030_summaries": before_1030,
        "matched_trade_ids": sorted(matched_ids),
        "matched_summaries": matched,
        "matched_side_summaries": matched_side,
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
        "maximum_drawdown", "indicator_exits",
        "indicator_exits_then_recovered_positive", "disaster_stops", "mfe_exits",
        "average_holding_seconds",
    ]
    _write_csv(output_dir / "comparison.csv", report["summaries"], fields)
    _write_csv(output_dir / "matched_comparison.csv", report["matched_summaries"], fields)
    _write_csv(
        output_dir / "per_trade.csv",
        report["per_trade"],
        [
            "variant", "trade_id", "session_date", "symbol", "stock_name", "side",
            "status", "entry_time", "entry_price", "quantity", "activation_loss_r",
            "activation_loss_twd", "require_vwap_break", "exit_time", "exit_price",
            "exit_reason", "net_pnl", "holding_seconds", "post_exit_best_net_pnl",
            "post_exit_worst_net_pnl", "later_recovered_positive", "mfe_armed", "mfe_r",
        ],
    )
    fmt = lambda value: "N/A" if value is None else f"{float(value):.2f}"
    lines = [
        "# Current-policy indicator-stop diagnostic", "",
        "Current -3,500 TWD disaster stop and MFE_V1 are retained. Only the causal early-stop overlay changes.", "",
        "| Variant | Scored | Net | PF | Win rate | Max DD | Indicator exits | Recovered later | Disaster stops |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["summaries"]:
        lines.append(
            f"| {row['variant']} | {row['scored_trades']} | {fmt(row['net_pnl'])} | "
            f"{fmt(row['profit_factor'])} | {fmt(row['win_rate'])} | "
            f"{fmt(row['maximum_drawdown'])} | {row['indicator_exits']} | "
            f"{row['indicator_exits_then_recovered_positive']} | {row['disaster_stops']} |"
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
    baseline = next(row for row in report["matched_summaries"] if row["variant"] == "CURRENT_NEG_1R")
    quarter = next(row for row in report["matched_summaries"] if row["variant"] == "FLOW_2OF3_2X_AFTER_NEG_0_25R")
    baseline_long = report["matched_side_summaries"]["CURRENT_NEG_1R"]["LONG"]
    quarter_long = report["matched_side_summaries"]["FLOW_2OF3_2X_AFTER_NEG_0_25R"]["LONG"]
    baseline_short = report["matched_side_summaries"]["CURRENT_NEG_1R"]["SHORT"]
    quarter_short = report["matched_side_summaries"]["FLOW_2OF3_2X_AFTER_NEG_0_25R"]["SHORT"]
    lines.extend([
        "", "## Conclusion", "",
        f"No tested overlay improved matched-cohort net PnL or Profit Factor. The -0.25R flow rule changed net PnL by {fmt(quarter['net_pnl'] - baseline['net_pnl'])} TWD and maximum drawdown by {fmt(quarter['maximum_drawdown'] - baseline['maximum_drawdown'])} TWD versus current policy.",
        f"Direction split is decisive: matched LONG changed from {fmt(baseline_long['net_pnl'])} to {fmt(quarter_long['net_pnl'])} TWD, while theoretical SHORT changed from {fmt(baseline_short['net_pnl'])} to {fmt(quarter_short['net_pnl'])} TWD. Current production is long-only and historical short eligibility was not captured, so the short improvement cannot justify promotion.",
        "The current live exit policy remains unchanged.",
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
