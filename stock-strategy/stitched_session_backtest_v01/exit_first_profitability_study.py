from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from statistics import mean
from types import SimpleNamespace
from typing import Any

from intraday_loss_reduction_study_v01.indicator_stop_analysis import (
    IndicatorVariant,
    _indicator_triggered,
)
from mfe_profit_protection_study_v01.analysis import ResearchTrade
from mfe_profit_protection_study_v01.overlay import (
    MFEProtectionState,
    VARIANTS as MFE_VARIANTS,
)
from paper_shadow_v01.runner import _research_trade, _replay_paper_track
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC
from yuanta_intraday_shadow_v01.flow_exit_sweep import build_flow_snapshots
from yuanta_intraday_shadow_v01.live_parity_backtest import _parse_stamp
from yuanta_live_runtime_v01.strategy import LiveSignal

from .anti_chase_sensitivity_study import _load_direct, _load_stitched
from .entry_confirmation_sensitivity_study import (
    RELAXED_OPENING_LIMIT,
    RELAXED_VWAP_LIMIT,
    _discover_events,
    _select_strong,
)


ANALYSIS_ID = "EXIT_FIRST_PROFITABILITY_STUDY_V0_1"
ONE_R_NET_TWD = 3500.0
ENTRY_POLICY = {
    "policy_id": "BROAD_BASE_SIGNAL_RELAXED_CHASE_DIAGNOSTIC",
    "description": (
        "Earliest daily production-approved signal or earliest base long signal "
        "passing 4% opening/2.5% VWAP chase limits and broad strength safety."
    ),
    "maximum_trades_per_day": 1,
    "opening_limit": RELAXED_OPENING_LIMIT,
    "vwap_limit": RELAXED_VWAP_LIMIT,
}


@dataclass(frozen=True, slots=True)
class ExitVariant:
    name: str
    small_mfe: str | None = None
    structure_minimum_loss_r: float | None = None
    structure_mode: str | None = None
    early_failure_seconds: int | None = None
    maximum_progress_r: float = 0.10


VARIANTS = (
    ExitVariant("CURRENT_BASELINE"),
    ExitVariant(
        "STRUCTURE_WHILE_NEGATIVE",
        structure_minimum_loss_r=0.0,
        structure_mode="VWAP_2OF3_2X",
    ),
    ExitVariant(
        "STRUCTURE_AFTER_NEG_0_25R",
        structure_minimum_loss_r=0.25,
        structure_mode="VWAP_2OF3_2X",
    ),
    ExitVariant(
        "BREAKOUT_INVALIDATION_FAST",
        structure_minimum_loss_r=0.0,
        structure_mode="BREAKOUT_ONLY",
    ),
    ExitVariant(
        "BREAKOUT_PLUS_FLOW_1OF3",
        structure_minimum_loss_r=0.0,
        structure_mode="BREAKOUT_FLOW_1OF3",
    ),
    ExitVariant(
        "VWAP_PLUS_FLOW_1OF3",
        structure_minimum_loss_r=0.0,
        structure_mode="VWAP_FLOW_1OF3",
    ),
    ExitVariant("EARLY_FAILURE_30S", early_failure_seconds=30),
    ExitVariant("EARLY_FAILURE_60S", early_failure_seconds=60),
    ExitVariant("EARLY_FAILURE_90S", early_failure_seconds=90),
    ExitVariant("EARLY_FAILURE_120S", early_failure_seconds=120),
    ExitVariant(
        "EARLY_FAILURE_60S_PROGRESS_0_20R",
        early_failure_seconds=60,
        maximum_progress_r=0.20,
    ),
    ExitVariant("LOW_MFE_GENTLE", small_mfe="GENTLE"),
    ExitVariant("LOW_MFE_TIGHT", small_mfe="TIGHT"),
    ExitVariant(
        "COMBINED_GENTLE_STRUCTURE_NEGATIVE",
        small_mfe="GENTLE",
        structure_minimum_loss_r=0.0,
        structure_mode="VWAP_2OF3_2X",
    ),
    ExitVariant(
        "COMBINED_GENTLE_STRUCTURE_NEG_0_25R",
        small_mfe="GENTLE",
        structure_minimum_loss_r=0.25,
        structure_mode="VWAP_2OF3_2X",
    ),
    ExitVariant(
        "COMBINED_TIGHT_STRUCTURE_NEG_0_25R",
        small_mfe="TIGHT",
        structure_minimum_loss_r=0.25,
        structure_mode="VWAP_2OF3_2X",
    ),
    ExitVariant(
        "COMBINED_GENTLE_BREAKOUT_FLOW",
        small_mfe="GENTLE",
        structure_minimum_loss_r=0.0,
        structure_mode="BREAKOUT_FLOW_1OF3",
    ),
    ExitVariant(
        "COMBINED_GENTLE_VWAP_FLOW_FAST",
        small_mfe="GENTLE",
        structure_minimum_loss_r=0.0,
        structure_mode="VWAP_FLOW_1OF3",
    ),
    ExitVariant(
        "COMBINED_GENTLE_EARLY_FAILURE_60S",
        small_mfe="GENTLE",
        early_failure_seconds=60,
    ),
)


def locked_floor_r(profile: str | None, mfe_r: float) -> float | None:
    """Net-PnL MFE floor. The returned floor never loosens in simulation."""
    if mfe_r < 0 or profile not in {None, "GENTLE", "TIGHT"}:
        raise ValueError("invalid MFE profile or MFE")
    if profile is None:
        return None
    if profile == "GENTLE":
        if mfe_r < 0.30:
            return None
        if mfe_r < 0.50:
            return -0.20
        if mfe_r < 0.75:
            return 0.0
        if mfe_r < 1.50:
            return 0.25
    else:
        if mfe_r < 0.25:
            return None
        if mfe_r < 0.50:
            return -0.10
        if mfe_r < 0.75:
            return 0.10
        if mfe_r < 1.50:
            return 0.35
    # Above 1R the unchanged production MFE_V1 overlay remains responsible.
    return None


def _live_signal(payload: dict[str, Any]) -> LiveSignal:
    allowed = {field.name for field in fields(LiveSignal)}
    values = {key: value for key, value in payload.items() if key in allowed}
    if isinstance(values.get("decision_time"), str):
        values["decision_time"] = datetime.fromisoformat(values["decision_time"])
    return LiveSignal(**values)


def _fixed_entry(
    session: dict[str, Any], *, capital_twd: int
) -> tuple[LiveSignal | None, dict[str, Any]]:
    baseline = _replay_paper_track(
        session["candidates"], session["market"], session["coverage"],
        capital_twd=capital_twd, confirmation_60s=False,
    )
    approved = (
        _live_signal(baseline["selected_signal"])
        if baseline.get("selected_signal") else None
    )
    targets, _directions = _discover_events(
        session["candidates"], session["market"], session["session_date"],
        capital_twd=capital_twd,
    )
    variant = {
        "minimum_relative_strength": 0.005,
        "opening_limit": RELAXED_OPENING_LIMIT,
        "vwap_limit": RELAXED_VWAP_LIMIT,
    }
    broad, selection = _select_strong(targets, variant)
    candidate = broad["signal"] if broad else None
    if approved is not None and (
        candidate is None or approved.decision_time <= candidate.decision_time
    ):
        return approved, {"selection_reason": "PRODUCTION_SIGNAL_WAS_EARLIEST"}
    if candidate is not None:
        return candidate, selection
    return None, {"selection_reason": "NO_BROAD_ENTRY"}


def _result(
    trade: ResearchTrade,
    variant: ExitVariant,
    index: int,
    reason: str,
    *,
    locked_floor: float | None,
    mfe_net: float,
) -> dict[str, Any]:
    point = trade.points[index]
    later = trade.points[index + 1:]
    return {
        "variant": variant.name,
        "trade_id": trade.trade_id,
        "session_date": trade.session_date,
        "symbol": trade.symbol,
        "stock_name": trade.stock_name,
        "entry_time": trade.entry_time.isoformat(),
        "entry_price": trade.entry_price,
        "quantity": trade.quantity,
        "exit_time": point.at.isoformat(),
        "exit_price": point.exit_price,
        "exit_reason": reason,
        "net_pnl_twd": point.projected_net_pnl,
        "holding_seconds": (point.at - trade.entry_time).total_seconds(),
        "mfe_net_pnl_at_exit_twd": mfe_net,
        "full_path_mfe_net_pnl_twd": max(
            row.projected_net_pnl for row in trade.points
        ),
        "full_path_mae_net_pnl_twd": min(
            row.projected_net_pnl for row in trade.points
        ),
        "locked_floor_r": locked_floor,
        "locked_floor_twd": (
            None if locked_floor is None else locked_floor * ONE_R_NET_TWD
        ),
        "post_exit_best_net_pnl_twd": max(
            (row.projected_net_pnl for row in later),
            default=point.projected_net_pnl,
        ),
        "post_exit_worst_net_pnl_twd": min(
            (row.projected_net_pnl for row in later),
            default=point.projected_net_pnl,
        ),
    }


def simulate_exit(
    trade: ResearchTrade,
    data: dict[str, Any],
    variant: ExitVariant,
    breakout_boundary_price: float,
) -> dict[str, Any]:
    snapshots = (
        build_flow_snapshots(data, SimpleNamespace(decision_time=trade.entry_time))
        if variant.structure_mode is not None else ()
    )
    fast = variant.structure_mode in {"BREAKOUT_FLOW_1OF3", "VWAP_FLOW_1OF3"}
    indicator = (
        IndicatorVariant(
            variant.name,
            hard_stop_twd=ONE_R_NET_TWD,
            minimum_loss_twd=(variant.structure_minimum_loss_r or 0.0)
            * ONE_R_NET_TWD,
            adverse_components=1 if fast else 2,
            consecutive=1 if fast else 2,
            require_vwap_break=variant.structure_mode in {
                "VWAP_2OF3_2X", "VWAP_FLOW_1OF3"
            },
        )
        if (
            variant.structure_minimum_loss_r is not None
            and variant.structure_mode != "BREAKOUT_ONLY"
        ) else None
    )
    snapshot_index = 0
    previous = current = None
    mfe_net = trade.pnl_at_price(trade.entry_price)
    locked_floor: float | None = None
    production_mfe = MFEProtectionState(trade.basis, MFE_VARIANTS["MFE_V1"])
    production_mfe.observe(
        price=trade.entry_price,
        at=trade.entry_time,
        projected_net_pnl=trade.pnl_at_price(trade.entry_price),
        pnl_at_price=trade.pnl_at_price,
    )
    hard_hour, hard_minute = map(int, str(SPEC["hard_exit_time"]).split(":"))
    hard_exit = trade.entry_time.replace(
        hour=hard_hour, minute=hard_minute, second=0, microsecond=0,
    )
    for index, point in enumerate(trade.points):
        while snapshot_index < len(snapshots) and snapshots[snapshot_index].at <= point.at:
            previous, current = current, snapshots[snapshot_index]
            snapshot_index += 1
        mfe_net = max(mfe_net, float(point.projected_net_pnl))
        production_mfe.observe(
            price=point.exit_price,
            at=point.at,
            projected_net_pnl=point.projected_net_pnl,
            pnl_at_price=trade.pnl_at_price,
        )
        candidate_floor = locked_floor_r(
            variant.small_mfe, max(0.0, mfe_net / ONE_R_NET_TWD)
        )
        if candidate_floor is not None:
            locked_floor = (
                candidate_floor if locked_floor is None
                else max(locked_floor, candidate_floor)
            )
        reason = None
        if point.projected_net_pnl <= -ONE_R_NET_TWD:
            reason = "DISASTER_STOP_NEG_1R"
        elif production_mfe.triggered(point.exit_price):
            reason = "MFE_PROFIT_PROTECTION"
        elif (
            locked_floor is not None
            and point.projected_net_pnl <= locked_floor * ONE_R_NET_TWD
        ):
            reason = "NET_MFE_PROFIT_PROTECTION"
        elif (
            variant.early_failure_seconds is not None
            and point.at
            >= trade.entry_time + timedelta(seconds=variant.early_failure_seconds)
            and point.projected_net_pnl < 0
            and mfe_net <= variant.maximum_progress_r * ONE_R_NET_TWD
        ):
            reason = "EARLY_FAILURE_NO_PROGRESS"
        else:
            loss_threshold = -(
                (variant.structure_minimum_loss_r or 0.0) * ONE_R_NET_TWD
            )
            breakout_broken = point.exit_price <= breakout_boundary_price
            if (
                variant.structure_mode == "BREAKOUT_ONLY"
                and point.projected_net_pnl < loss_threshold
                and breakout_broken
            ):
                reason = "BREAKOUT_INVALIDATION_STOP"
            elif (
                indicator is not None
                and current is not None
                and point.at - current.at <= timedelta(seconds=30)
                and _indicator_triggered(
                    indicator, trade, data, current, previous,
                    float(point.projected_net_pnl),
                )
                and (
                    variant.structure_mode != "BREAKOUT_FLOW_1OF3"
                    or breakout_broken
                )
            ):
                reason = "STRUCTURE_FLOW_STOP"
        if reason is None and point.reversal:
            reason = "SIGNAL_REVERSAL"
        elif reason is None and point.at >= hard_exit:
            reason = "HARD_EXIT"
        elif (
            reason is None
            and trade.force_last_point_exit
            and index == len(trade.points) - 1
        ):
            reason = "HARD_EXIT"
        if reason is not None:
            return _result(
                trade, variant, index, reason,
                locked_floor=locked_floor, mfe_net=mfe_net,
            )
    raise RuntimeError(f"no exit for {trade.trade_id}/{variant.name}")


def _max_drawdown(rows: list[dict[str, Any]]) -> float:
    equity = peak = drawdown = 0.0
    for row in sorted(rows, key=lambda item: item["entry_time"]):
        equity += float(row["net_pnl_twd"])
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return round(drawdown, 2)


def _summary(variant: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(row["net_pnl_twd"]) for row in rows]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    return {
        "variant": variant,
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(rows) if rows else None,
        "gross_profit_twd": round(gross_profit, 2),
        "gross_loss_twd": round(gross_loss, 2),
        "net_pnl_twd": round(sum(pnls), 2),
        "average_pnl_twd": round(mean(pnls), 2) if pnls else None,
        "average_winner_twd": round(mean(wins), 2) if wins else None,
        "average_loser_twd": round(mean(losses), 2) if losses else None,
        "profit_factor": (
            round(gross_profit / gross_loss, 6)
            if gross_loss else ("INFINITE" if wins else None)
        ),
        "maximum_drawdown_twd": _max_drawdown(rows),
        "exit_reason_counts": {
            reason: sum(row["exit_reason"] == reason for row in rows)
            for reason in sorted({row["exit_reason"] for row in rows})
        },
    }


def run_study(
    run_20260929: Path,
    early_20260930: Path,
    late_20260930: Path,
    run_20261001: Path,
    *,
    capital_twd: int = 190_000,
) -> dict[str, Any]:
    sessions = [
        _load_direct(run_20260929.resolve()),
        _load_stitched(early_20260930.resolve(), late_20260930.resolve()),
        _load_direct(run_20261001.resolve()),
    ]
    cohort = []
    for session in sessions:
        signal, selection = _fixed_entry(session, capital_twd=capital_twd)
        if signal is None:
            continue
        end_time = _parse_stamp(session["coverage"]["ended_at_taipei"])
        trade = _research_trade(
            signal, session["candidates"][signal.stock_id], end_time
        )
        cohort.append({
            "session": session,
            "signal": signal,
            "selection": selection,
            "trade": trade,
            "breakout_boundary_price": signal.breakout_boundary_price,
        })
    rows = [
        simulate_exit(
            item["trade"],
            item["session"]["candidates"][item["signal"].stock_id],
            variant,
            item["breakout_boundary_price"],
        )
        for variant in VARIANTS
        for item in cohort
    ]
    summaries = [
        _summary(
            variant.name,
            [row for row in rows if row["variant"] == variant.name],
        )
        for variant in VARIANTS
    ]
    baseline = float(summaries[0]["net_pnl_twd"])
    baseline_rows = {
        row["trade_id"]: row for row in rows
        if row["variant"] == VARIANTS[0].name
    }
    for summary in summaries:
        summary["net_difference_vs_baseline_twd"] = round(
            float(summary["net_pnl_twd"]) - baseline, 2
        )
        variant_rows = [
            row for row in rows if row["variant"] == summary["variant"]
        ]
        daily_differences = [
            round(
                float(row["net_pnl_twd"])
                - float(baseline_rows[row["trade_id"]]["net_pnl_twd"]),
                2,
            )
            for row in variant_rows
        ]
        improvement = float(summary["net_difference_vs_baseline_twd"])
        summary.update({
            "days_improved": sum(value > 0 for value in daily_differences),
            "days_worsened": sum(value < 0 for value in daily_differences),
            "daily_pnl_differences_twd": daily_differences,
            "fragile_best_day_deletion": (
                improvement > 0
                and any(improvement - value <= 0 for value in daily_differences)
            ),
        })
    report = {
        "analysis_id": ANALYSIS_ID,
        "capital_twd": capital_twd,
        "entry_policy": ENTRY_POLICY,
        "one_r_net_twd": ONE_R_NET_TWD,
        "variants": [asdict(variant) for variant in VARIANTS],
        "cohort": [
            {
                "session_date": item["trade"].session_date,
                "symbol": item["trade"].symbol,
                "stock_name": item["trade"].stock_name,
                "entry_time": item["trade"].entry_time.isoformat(),
                "entry_price": item["trade"].entry_price,
                "quantity": item["trade"].quantity,
                "breakout_boundary_price": item["breakout_boundary_price"],
                "selection": item["selection"],
            }
            for item in cohort
        ],
        "summaries": summaries,
        "per_trade": rows,
        "sample_warning": (
            "Three fixed-entry sessions only; 20260930 is a diagnostic stitch "
            "with four untimestamped callback errors."
        ),
        "production_behavior_changed": False,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    report["report_hash"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Exit-first profitability study",
        "",
        "The same three broad long entries are held fixed across all exit variants. All PnL is after the existing fee, tax and adverse-price execution model.",
        "",
        "## Fixed entry cohort",
        "",
        "| Date | Stock | Entry time | Entry | Qty |",
        "|---|---|---|---:|---:|",
    ]
    for row in report["cohort"]:
        lines.append(
            f"| {row['session_date']} | {row['symbol']} {row['stock_name']} | "
            f"{str(row['entry_time'])[11:19]} | {row['entry_price']:.2f} | {row['quantity']} |"
        )
    lines.extend([
        "",
        "## Aggregate comparison",
        "",
        "| Variant | Net PnL | vs baseline | W/L | Avg winner | Avg loser | PF | Max DD | Robust? |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    for row in report["summaries"]:
        def f(value: Any) -> str:
            return "-" if value is None else f"{float(value):.0f}"
        robust = "FRAGILE" if row["fragile_best_day_deletion"] else "-"
        lines.append(
            f"| {row['variant']} | {row['net_pnl_twd']:.0f} | "
            f"{row['net_difference_vs_baseline_twd']:+.0f} | {row['wins']}/{row['losses']} | "
            f"{f(row['average_winner_twd'])} | {f(row['average_loser_twd'])} | "
            f"{row['profit_factor'] if row['profit_factor'] is not None else '-'} | "
            f"{row['maximum_drawdown_twd']:.0f} | {robust} |"
        )
    lines.extend([
        "",
        "## Per-trade exits",
        "",
        "| Variant | Date | Stock | Exit | Reason | Net PnL | MFE before exit | Full-path MFE |",
        "|---|---|---|---:|---|---:|---:|---:|",
    ])
    for row in report["per_trade"]:
        lines.append(
            f"| {row['variant']} | {row['session_date']} | {row['symbol']} | "
            f"{row['exit_price']:.2f} | {row['exit_reason']} | {row['net_pnl_twd']:.0f} | "
            f"{row['mfe_net_pnl_at_exit_twd']:.0f} | {row['full_path_mfe_net_pnl_twd']:.0f} |"
        )
    positive = [row for row in report["summaries"] if row["net_pnl_twd"] > 0]
    summary_by_name = {row["variant"]: row for row in report["summaries"]}
    early_60 = summary_by_name["EARLY_FAILURE_60S"]
    early_30 = summary_by_name["EARLY_FAILURE_30S"]
    lines.extend([
        "",
        "## Diagnostic readout",
        "",
        f"- The 60-second no-progress rule changed aggregate net PnL from {report['summaries'][0]['net_pnl_twd']:.0f} to {early_60['net_pnl_twd']:.0f} TWD, with PF {early_60['profit_factor']} and max drawdown {early_60['maximum_drawdown_twd']:.0f} TWD.",
        f"- The 30-second rule was too early: aggregate net PnL fell to {early_30['net_pnl_twd']:.0f} TWD because it also exited the eventual 3094 winner.",
        "- Low-MFE floors did not change these three trades: both losers had no positive executable net MFE before the disaster stop, while the winner was already handled by production MFE_V1.",
        "- 3605 later had substantial full-path favorable excursion after the early exit. The 60-second rule improved the result versus the current hard stop, but it still cut a later recovery; this is the main false-exit risk to validate on new days.",
        "",
        "## Result",
        "",
        f"- Positive-net variants: {', '.join(row['variant'] for row in positive) if positive else 'none'}.",
        f"- {report['sample_warning']}",
        "- A FRAGILE label means removing one best day eliminates the measured improvement.",
        "- No live or production strategy behavior changed.",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exit-first fixed-entry backtest")
    parser.add_argument("--run-20260929", required=True, type=Path)
    parser.add_argument("--early-20260930", required=True, type=Path)
    parser.add_argument("--late-20260930", required=True, type=Path)
    parser.add_argument("--run-20261001", required=True, type=Path)
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    report = run_study(
        args.run_20260929,
        args.early_20260930,
        args.late_20260930,
        args.run_20261001,
        capital_twd=args.capital,
    )
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown_report(report), encoding="utf-8")
    if not args.json_output and not args.markdown_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
