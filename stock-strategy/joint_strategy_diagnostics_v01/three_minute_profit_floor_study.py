"""Backtest-only isolation of a 3-minute continuation gate and net-MFE floor.

The study deliberately reuses the historical independent-signal cohort and the
existing execution/cost model.  It has no imports from broker or live-runtime
control modules and cannot submit orders.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any, Mapping

from current_indicator_stop_study_v01.analysis import (
    ONE_R_NET_TWD,
    VARIANTS as CURRENT_EXIT_VARIANTS,
    simulate_current_indicator_stop,
)
from mfe_profit_protection_study_v01.analysis import (
    ResearchTrade,
    _independent_trade_path,
    build_independent_signal_trades,
    derive_initial_stop_price,
)
from mfe_profit_protection_study_v01.overlay import (
    EntryFill,
    MFEProtectionState,
    PositionBasis,
    VARIANTS as MFE_VARIANTS,
)
from signal_quality_diagnostics_v01.analysis import build_report as build_quality_report
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC,
    _exit_quote,
    _projected_net_pnl,
    _tick_size,
    load_session,
    signal_at,
)


ANALYSIS_ID = "THREE_MINUTE_CONTINUATION_NET_MFE_STUDY_V0_1"
CONFIRMATION_SECONDS = 180
BASELINE_EXIT = CURRENT_EXIT_VARIANTS[0]
VARIANTS = (
    "BASELINE",
    "CONFIRM_3M",
    "COST_AWARE_BREAKEVEN",
    "CONFIRM_3M_PLUS_COST_AWARE_BREAKEVEN",
)
UNSCORABLE_CONFIRMATION_REASONS = {
    "QUOTE_MISSING",
    "QUOTE_STALE",
    "FLOW_WINDOW_MISSING",
    "SIGNAL_REBUILD_FAILED",
    "DELAYED_EXIT_PATH_UNSCORABLE",
}


def _signed_volume(row: dict[str, Any]) -> float:
    if str(row.get("flag")) == "1":
        return float(row["volume"])
    if str(row.get("flag")) == "0":
        return -float(row["volume"])
    midpoint = (float(row["bid"]) + float(row["ask"])) / 2
    return float(row["volume"]) if float(row["price"]) >= midpoint else -float(row["volume"])


def _percentile90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


def _max_drawdown(rows: list[dict[str, Any]]) -> float:
    equity = peak = drawdown = 0.0
    for row in sorted(rows, key=lambda item: str(item["original_entry_time"])):
        equity += float(row["new_pnl_twd"] or 0.0)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return round(drawdown, 2)


def _confirmation_trade(
    feature: dict[str, Any],
    data: dict[str, Any],
    original: ResearchTrade,
    capital_twd: int,
) -> tuple[ResearchTrade | None, dict[str, Any]]:
    """Require net progress, held structure/VWAP and non-adverse 3m flow."""
    decision = datetime.fromisoformat(str(feature["signal_time"]))
    checkpoint = decision + timedelta(seconds=CONFIRMATION_SECONDS)
    original_exit = datetime.fromisoformat(str(feature["exit_time"]))
    if original_exit <= checkpoint:
        return None, {
            "confirmation_reason": "ORIGINAL_EXIT_BEFORE_CHECKPOINT",
            "confirmation_time": checkpoint.isoformat(),
        }

    before = [row for row in data["ticks"] if row["time"] <= checkpoint]
    observation = before[-1] if before else None
    execution = next((row for row in data["ticks"] if row["time"] >= checkpoint), None)
    if observation is None or execution is None:
        return None, {"confirmation_reason": "QUOTE_MISSING", "confirmation_time": checkpoint.isoformat()}
    maximum_age = float(SPEC["maximum_tick_staleness_seconds"])
    observation_age = (checkpoint - observation["time"]).total_seconds()
    execution_delay = (execution["time"] - checkpoint).total_seconds()
    if not (0 <= observation_age <= maximum_age and 0 <= execution_delay <= maximum_age):
        return None, {
            "confirmation_reason": "QUOTE_STALE",
            "confirmation_time": checkpoint.isoformat(),
            "observation_age_seconds": observation_age,
            "execution_delay_seconds": execution_delay,
        }

    window = [row for row in data["ticks"] if decision < row["time"] <= checkpoint]
    if not window or sum(float(row["volume"]) for row in window) <= 0:
        return None, {"confirmation_reason": "FLOW_WINDOW_MISSING", "confirmation_time": checkpoint.isoformat()}
    direction = 1.0 if original.side == "LONG" else -1.0
    total = sum(float(row["volume"]) for row in window)
    volume_delta = direction * sum(_signed_volume(row) for row in window) / total
    reference = [
        row for row in data["ticks"]
        if checkpoint - timedelta(seconds=300) <= row["time"] <= checkpoint
    ]
    threshold = _percentile90([float(row["volume"]) for row in reference])
    large = [row for row in window if float(row["volume"]) >= threshold]
    large_total = sum(float(row["volume"]) for row in large)
    large_delta = (
        direction * sum(_signed_volume(row) for row in large) / large_total
        if large_total > 0 else None
    )

    causal = [row for row in data["ticks"] if row["time"] <= checkpoint]
    causal_volume = sum(float(row["volume"]) for row in causal)
    vwap = (
        sum(float(row["price"]) * float(row["volume"]) for row in causal) / causal_volume
        if causal_volume > 0 else None
    )
    current = float(observation["price"])
    boundary = float(feature["breakout_boundary_price"])
    structure_held = current >= boundary if original.side == "LONG" else current <= boundary
    vwap_held = bool(vwap is not None and (current >= vwap if original.side == "LONG" else current <= vwap))
    safe_exit = _exit_quote(original.side, observation)
    checkpoint_net_pnl = float(
        _projected_net_pnl(
            original.side, original.entry_price, safe_exit, original.quantity,
        )[3]
    )
    diagnostics = {
        "confirmation_time": checkpoint.isoformat(),
        "confirmation_price": current,
        "confirmation_safe_exit_price": safe_exit,
        "confirmation_net_pnl_twd": checkpoint_net_pnl,
        "confirmation_volume_delta": volume_delta,
        "confirmation_large_trade_delta": large_delta,
        "confirmation_structure_held": structure_held,
        "confirmation_vwap": vwap,
        "confirmation_vwap_held": vwap_held,
        "observation_age_seconds": observation_age,
        "execution_delay_seconds": execution_delay,
    }
    checks = {
        "NET_PROGRESS_NOT_POSITIVE": checkpoint_net_pnl >= 0,
        "BREAKOUT_NOT_HELD": structure_held,
        "VWAP_NOT_HELD": vwap_held,
        "VOLUME_FLOW_REVERSED": volume_delta >= 0,
        "LARGE_TRADE_FLOW_REVERSED": large_delta is not None and large_delta >= 0,
    }
    failed = [reason for reason, passed in checks.items() if not passed]
    if failed:
        return None, {**diagnostics, "confirmation_reason": "+".join(failed)}

    entry_price = (
        float(execution["ask"]) + _tick_size(float(execution["ask"]))
        if original.side == "LONG"
        else max(
            _tick_size(float(execution["bid"])),
            float(execution["bid"]) - _tick_size(float(execution["bid"])),
        )
    )
    quantity = math.floor(capital_twd / (entry_price * 1000)) * 1000
    if quantity <= 0:
        return None, {**diagnostics, "confirmation_reason": "DELAYED_ENTRY_UNAFFORDABLE"}
    signal = signal_at(original.symbol, data, decision)
    if signal is None:
        return None, {**diagnostics, "confirmation_reason": "SIGNAL_REBUILD_FAILED"}
    delayed, path_diagnostic = _independent_trade_path(
        original.session_date, data, signal, execution, entry_price, quantity,
    )
    if delayed is None:
        return None, {
            **diagnostics,
            **path_diagnostic,
            "confirmation_reason": "DELAYED_EXIT_PATH_UNSCORABLE",
        }
    return delayed, {
        **diagnostics,
        **path_diagnostic,
        "confirmation_reason": "CONFIRMED",
        "delayed_entry_time": delayed.entry_time.isoformat(),
        "delayed_entry_price": delayed.entry_price,
        "delayed_quantity": delayed.quantity,
    }


def _simulate_baseline(trade: ResearchTrade, data: dict[str, Any]) -> dict[str, Any]:
    return simulate_current_indicator_stop(trade, data, BASELINE_EXIT)


def _simulate_cost_aware_breakeven(trade: ResearchTrade) -> dict[str, Any]:
    """Keep the current price-MFE tiers and add only a net-zero floor."""
    initial_stop = derive_initial_stop_price(
        trade.side, trade.entry_price, trade.quantity,
        ONE_R_NET_TWD,
    )
    basis = PositionBasis.from_fills(
        trade.side,
        [EntryFill(trade.entry_time, trade.entry_price, trade.quantity)],
        initial_stop_price=initial_stop,
    )
    overlay = MFEProtectionState(basis, MFE_VARIANTS["MFE_V1"])
    overlay.observe(
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
        elif overlay.armed and point.projected_net_pnl <= 0:
            reason = "MFE_COST_AWARE_BREAKEVEN"
        elif point.reversal:
            reason = "SIGNAL_REVERSAL"
        elif point.at >= hard_exit:
            reason = "HARD_EXIT"
        elif trade.force_last_point_exit and index == len(trade.points) - 1:
            reason = "HARD_EXIT"
        if reason is not None:
            return {
                "status": "SCORED",
                "exit_time": point.at.isoformat(),
                "exit_price": point.exit_price,
                "exit_reason": reason,
                "net_pnl": point.projected_net_pnl,
            }
    return {"status": "UNSCORABLE"}


def _summary(
    variant: str,
    rows: list[dict[str, Any]],
    baseline_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    evaluable = [row for row in rows if row["action"] != "UNSCORABLE_DATA"]
    values = [float(row["new_pnl_twd"] or 0.0) for row in evaluable]
    entered = [row for row in rows if row["action"] == "ENTERED"]
    entered_values = [float(row["new_pnl_twd"]) for row in entered]
    wins = [value for value in entered_values if value > 0]
    losses = [value for value in entered_values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    differences = [
        float(row["new_pnl_twd"] or 0.0)
        - float(baseline_by_id[row["trade_id"]]["new_pnl_twd"])
        for row in evaluable
    ]
    total_difference = sum(differences)
    return {
        "variant": variant,
        "opportunities": len(rows),
        "evaluable_opportunities": len(evaluable),
        "unscorable_data": len(rows) - len(evaluable),
        "entered_trades": len(entered),
        "rejected_trades": sum(row["action"] == "REJECTED_CONFIRMATION" for row in rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(entered) if entered else None,
        "gross_profit_twd": round(gross_profit, 2),
        "gross_loss_twd": round(gross_loss, 2),
        "net_pnl_twd": round(sum(values), 2),
        "matched_baseline_net_pnl_twd": round(sum(
            float(baseline_by_id[row["trade_id"]]["new_pnl_twd"])
            for row in evaluable
        ), 2),
        "full_baseline_net_pnl_twd": round(sum(
            float(row["new_pnl_twd"]) for row in baseline_by_id.values()
        ), 2),
        "net_difference_vs_baseline_twd": round(total_difference, 2),
        "expectancy_per_opportunity_twd": round(mean(values), 2) if values else None,
        "expectancy_per_entry_twd": round(mean(entered_values), 2) if entered_values else None,
        "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss else None,
        "average_winner_twd": round(mean(wins), 2) if wins else None,
        "average_loser_twd": round(mean(losses), 2) if losses else None,
        "maximum_drawdown_twd": _max_drawdown(rows),
        "baseline_winners_rejected": sum(
            row["action"] == "REJECTED_CONFIRMATION"
            and float(baseline_by_id[row["trade_id"]]["new_pnl_twd"]) > 0
            for row in rows
        ),
        "baseline_losers_rejected": sum(
            row["action"] == "REJECTED_CONFIRMATION"
            and float(baseline_by_id[row["trade_id"]]["new_pnl_twd"]) < 0
            for row in rows
        ),
        "trades_improved": sum(value > 0 for value in differences),
        "trades_worsened": sum(value < 0 for value in differences),
        "leave_one_trade_out_min_difference_twd": round(
            min((total_difference - value for value in differences), default=0.0), 2
        ),
        "improvement_survives_every_leave_one_out": bool(
            total_difference > 0
            and all(total_difference - value > 0 for value in differences)
        ),
    }


def build_report(
    session_runs: Mapping[str, list[Path]], capital_twd: int = 190_000,
) -> dict[str, Any]:
    quality = build_quality_report(session_runs, capital_twd)
    features = {
        (str(row["session_date"]), str(row["symbol"])): row
        for row in quality["per_trade"]
        if row["outcome"] in {"WINNER", "LOSER"}
    }
    trades, diagnostics, coverage = build_independent_signal_trades(
        session_runs, capital_twd,
    )
    stocks_by_date: dict[str, dict[str, dict[str, Any]]] = {}
    for paths in session_runs.values():
        stocks, session_coverage = load_session(paths)
        stocks_by_date[str(session_coverage["session_date"])] = stocks

    rows_by_variant = {variant: [] for variant in VARIANTS}
    for trade in trades:
        key = (trade.session_date, trade.symbol)
        feature = features.get(key)
        if feature is None:
            continue
        data = stocks_by_date[trade.session_date][trade.symbol]
        baseline = _simulate_baseline(trade, data)
        cost_aware = _simulate_cost_aware_breakeven(trade)
        delayed, confirmation = _confirmation_trade(
            feature, data, trade, capital_twd,
        )
        common = {
            "trade_id": trade.trade_id,
            "session_date": trade.session_date,
            "symbol": trade.symbol,
            "stock_name": trade.stock_name,
            "side": trade.side,
            "original_entry_time": trade.entry_time.isoformat(),
            "original_entry_price": trade.entry_price,
            "original_quantity": trade.quantity,
            "original_exit_time": baseline["exit_time"],
            "original_exit_price": baseline["exit_price"],
            "original_exit_reason": baseline["exit_reason"],
            "original_pnl_twd": float(baseline["net_pnl"]),
        }
        rows_by_variant["BASELINE"].append({
            **common, "variant": "BASELINE", "action": "ENTERED",
            "new_entry_time": common["original_entry_time"],
            "new_entry_price": trade.entry_price,
            "new_quantity": trade.quantity,
            "new_exit_time": baseline["exit_time"],
            "new_exit_price": baseline["exit_price"],
            "new_exit_reason": baseline["exit_reason"],
            "new_pnl_twd": float(baseline["net_pnl"]),
        })
        rows_by_variant["COST_AWARE_BREAKEVEN"].append({
            **common, "variant": "COST_AWARE_BREAKEVEN", "action": "ENTERED",
            "new_entry_time": common["original_entry_time"],
            "new_entry_price": trade.entry_price,
            "new_quantity": trade.quantity,
            "new_exit_time": cost_aware["exit_time"],
            "new_exit_price": cost_aware["exit_price"],
            "new_exit_reason": cost_aware["exit_reason"],
            "new_pnl_twd": float(cost_aware["net_pnl"]),
        })
        if delayed is None:
            confirmation_reason = str(confirmation["confirmation_reason"])
            unscorable = confirmation_reason in UNSCORABLE_CONFIRMATION_REASONS
            rejected = {
                **common,
                "action": "UNSCORABLE_DATA" if unscorable else "REJECTED_CONFIRMATION",
                "new_entry_time": None,
                "new_entry_price": None,
                "new_quantity": 0,
                "new_exit_time": None,
                "new_exit_price": None,
                "new_exit_reason": confirmation["confirmation_reason"],
                "new_pnl_twd": None if unscorable else 0.0,
                **confirmation,
            }
            rows_by_variant["CONFIRM_3M"].append({**rejected, "variant": "CONFIRM_3M"})
            rows_by_variant["CONFIRM_3M_PLUS_COST_AWARE_BREAKEVEN"].append({
                **rejected, "variant": "CONFIRM_3M_PLUS_COST_AWARE_BREAKEVEN",
            })
            continue
        delayed_baseline = _simulate_baseline(delayed, data)
        delayed_cost_aware = _simulate_cost_aware_breakeven(delayed)
        for variant, result in (
            ("CONFIRM_3M", delayed_baseline),
            ("CONFIRM_3M_PLUS_COST_AWARE_BREAKEVEN", delayed_cost_aware),
        ):
            rows_by_variant[variant].append({
                **common, **confirmation, "variant": variant,
                "action": "ENTERED",
                "new_entry_time": delayed.entry_time.isoformat(),
                "new_entry_price": delayed.entry_price,
                "new_quantity": delayed.quantity,
                "new_exit_time": result["exit_time"],
                "new_exit_price": result["exit_price"],
                "new_exit_reason": result["exit_reason"],
                "new_pnl_twd": float(result["net_pnl"]),
            })

    baseline_by_id = {
        row["trade_id"]: row for row in rows_by_variant["BASELINE"]
    }
    summaries = [
        _summary(variant, rows_by_variant[variant], baseline_by_id)
        for variant in VARIANTS
    ]
    report = {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "BACKTEST_ONLY_FIXED_3M_CONTINUATION_AND_NET_MFE_DIAGNOSTIC",
        "capital_twd": capital_twd,
        "confirmation_seconds": CONFIRMATION_SECONDS,
        "variant_definitions": {
            "BASELINE": "current entry plus current -1R/MFE_V1/reversal/EOD exits",
            "CONFIRM_3M": (
                "delay entry 180s; require positive net mark, breakout and VWAP held, "
                "and non-negative directional volume/large-trade flow"
            ),
            "COST_AWARE_BREAKEVEN": (
                "current MFE_V1 unchanged, plus a net-PnL zero floor after protection arms"
            ),
            "CONFIRM_3M_PLUS_COST_AWARE_BREAKEVEN": (
                "the fixed 180s confirmation plus the net-PnL zero floor"
            ),
        },
        "summaries": summaries,
        "per_trade": [row for variant in VARIANTS for row in rows_by_variant[variant]],
        "source_diagnostics": diagnostics,
        "coverage": coverage,
        "limitations": [
            "Only seventeen scorable independent signals across three historical sessions are available.",
            "All three source sessions are partial and 20260923 contains callback errors.",
            "Signals overlap and therefore are not one simultaneously executable 190k portfolio.",
            "The 180-second rule is fixed from the stated hypothesis and was not parameter-optimized.",
            "Rejected trades contribute zero PnL to opportunity expectancy; entry expectancy is also reported separately.",
        ],
        "production_behavior_changed": False,
        "live_behavior_changed": False,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    report["report_hash"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


def _fmt(value: Any, digits: int = 0) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.{digits}f}"


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Three-minute continuation and net-MFE diagnostic", "",
        "This is a backtest-only controlled comparison. No LIVE or broker behavior changed.", "",
        "| Variant | Evaluable (unscorable) | Entered/rejected | W/L | Win rate | Net PnL | Matched baseline | Delta | Exp/opportunity | PF | Avg winner | Avg loser | Max DD | Winners rejected | LOO robust |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in report["summaries"]:
        rate = "-" if row["win_rate"] is None else f"{row['win_rate']:.1%}"
        lines.append(
            f"| {row['variant']} | {row['evaluable_opportunities']} ({row['unscorable_data']}) | "
            f"{row['entered_trades']}/{row['rejected_trades']} | "
            f"{row['wins']}/{row['losses']} | {rate} | {_fmt(row['net_pnl_twd'])} | "
            f"{_fmt(row['matched_baseline_net_pnl_twd'])} | "
            f"{_fmt(row['net_difference_vs_baseline_twd'])} | "
            f"{_fmt(row['expectancy_per_opportunity_twd'])} | "
            f"{_fmt(row['profit_factor'], 2)} | {_fmt(row['average_winner_twd'])} | "
            f"{_fmt(row['average_loser_twd'])} | {_fmt(row['maximum_drawdown_twd'])} | "
            f"{row['baseline_winners_rejected']} | "
            f"{'YES' if row['improvement_survives_every_leave_one_out'] else 'NO'} |"
        )
    lines.extend(["", "## Confirmation rejection reasons", ""])
    reasons: dict[str, int] = {}
    for row in report["per_trade"]:
        if row["variant"] != "CONFIRM_3M" or row["action"] != "REJECTED_CONFIRMATION":
            continue
        reason = str(row["new_exit_reason"])
        reasons[reason] = reasons.get(reason, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {reason}: {count}")
    lines.extend(["", "## Unscorable data reasons", ""])
    unscorable_reasons: dict[str, int] = {}
    for row in report["per_trade"]:
        if row["variant"] != "CONFIRM_3M" or row["action"] != "UNSCORABLE_DATA":
            continue
        reason = str(row["new_exit_reason"])
        unscorable_reasons[reason] = unscorable_reasons.get(reason, 0) + 1
    for reason, count in sorted(
        unscorable_reasons.items(), key=lambda item: (-item[1], item[0])
    ):
        lines.append(f"- {reason}: {count}")
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "three_minute_profit_floor_summary.json"
    csv_path = output_dir / "three_minute_profit_floor_per_trade.csv"
    md_path = output_dir / "report_three_minute_profit_floor.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    md_path.write_text(markdown_report(report), encoding="utf-8")
    fields: list[str] = []
    for row in report["per_trade"]:
        for key in row:
            if key not in fields:
                fields.append(key)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(report["per_trade"])
    manifest = {
        "analysis_id": ANALYSIS_ID,
        "artifact_hashes": {
            path.name: sha256_file(path) for path in (json_path, csv_path, md_path)
        },
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "production_behavior_changed": False,
        "live_behavior_changed": False,
    }
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (output_dir / "three_minute_profit_floor_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


def _session(value: str) -> tuple[str, list[Path]]:
    try:
        session_date, paths = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use YYYYMMDD=RUN_DIR[,RUN_DIR]") from exc
    return session_date, [Path(path).resolve() for path in paths.split(",")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="append", required=True, type=_session)
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    write_report(
        build_report(dict(args.session), args.capital), args.output_dir.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
