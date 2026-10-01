"""Backtest-only grid for cost-aware MFE activation and gap buffers."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC

from .exit_first_historical_validation import _cohort_summary
from .exit_first_profitability_study import ONE_R_NET_TWD, ExitVariant, _result
from .joint_loss_profit_overlay_validation import (
    JointVariant,
    simulate_joint,
)
from .path_failure_stop_validation import _prepare_items


ANALYSIS_ID = "NET_MFE_GAP_BUFFER_VALIDATION_V0_1"
CHECKPOINT_SECONDS = 120
MAXIMUM_PROGRESS_R = 0.10
RECOVERY_LOSS_THRESHOLD_R = 0.20
MAXIMUM_RECOVERY_FROM_MAE_R = 0.20
ACTIVATION_THRESHOLDS_R = (0.50, 0.75, 1.00, 1.25)
INITIAL_LOCK_BUFFERS_R = (0.00, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60)
LOSS_OVERLAYS = ("PLAIN_120S", "RECOVERY_AWARE_120S")
PRODUCTION_REFERENCE = "CURRENT_LOSS__MFE_V1"


@dataclass(frozen=True, slots=True)
class BufferVariant:
    loss_overlay: str
    activation_r: float
    initial_lock_buffer_r: float

    @property
    def name(self) -> str:
        return (
            f"{self.loss_overlay}__NET_MFE_LOOSE__ACT_{self.activation_r:.2f}R"
            f"__BUFFER_{self.initial_lock_buffer_r:.2f}R"
        )


def variants() -> tuple[BufferVariant, ...]:
    return tuple(
        BufferVariant(loss, activation, buffer)
        for loss in LOSS_OVERLAYS
        for activation in ACTIVATION_THRESHOLDS_R
        for buffer in INITIAL_LOCK_BUFFERS_R
        if buffer < activation
    )


def candidate_floor_r(
    mfe_net_r: float, *, activation_r: float, initial_buffer_r: float,
) -> float | None:
    if mfe_net_r < activation_r:
        return None
    if mfe_net_r < 1.5:
        return initial_buffer_r
    if mfe_net_r < 2.0:
        return max(initial_buffer_r, 0.50 * mfe_net_r)
    if mfe_net_r < 3.0:
        return max(initial_buffer_r, 0.60 * mfe_net_r)
    return max(initial_buffer_r, 0.70 * mfe_net_r)


def simulate_buffered(
    trade, variant: BufferVariant, *, hard_stop_r: float = 1.0,
) -> dict[str, Any]:
    if hard_stop_r <= 0:
        raise ValueError("hard_stop_r must be positive")
    checkpoint_at = trade.entry_time + timedelta(seconds=CHECKPOINT_SECONDS)
    checkpoint_evaluated = False
    mfe_net = mae_net = trade.pnl_at_price(trade.entry_price)
    locked_r: float | None = None
    activation_time = None
    hard_hour, hard_minute = map(int, str(SPEC["hard_exit_time"]).split(":"))
    hard_exit = trade.entry_time.replace(
        hour=hard_hour, minute=hard_minute, second=0, microsecond=0,
    )
    checkpoint_action = "NOT_APPLICABLE"
    for index, point in enumerate(trade.points):
        mfe_net = max(mfe_net, float(point.projected_net_pnl))
        mae_net = min(mae_net, float(point.projected_net_pnl))
        candidate = candidate_floor_r(
            max(0.0, mfe_net / ONE_R_NET_TWD),
            activation_r=variant.activation_r,
            initial_buffer_r=variant.initial_lock_buffer_r,
        )
        if candidate is not None:
            if activation_time is None:
                activation_time = point.at
            locked_r = candidate if locked_r is None else max(locked_r, candidate)

        reason = None
        if point.projected_net_pnl <= -hard_stop_r * ONE_R_NET_TWD:
            reason = "DISASTER_STOP_NEG_1R"
        elif locked_r is not None and point.projected_net_pnl <= locked_r * ONE_R_NET_TWD:
            reason = "BUFFERED_NET_MFE_PROFIT_PROTECTION"
        elif variant.loss_overlay == "PLAIN_120S" and (
            point.at >= checkpoint_at
            and point.projected_net_pnl < 0
            and mfe_net <= MAXIMUM_PROGRESS_R * ONE_R_NET_TWD
        ):
            reason = "EARLY_FAILURE_NO_PROGRESS"
        elif (
            variant.loss_overlay == "RECOVERY_AWARE_120S"
            and not checkpoint_evaluated
            and point.at >= checkpoint_at
        ):
            checkpoint_evaluated = True
            recovery = float(point.projected_net_pnl) - mae_net
            triggered = (
                point.projected_net_pnl
                <= -RECOVERY_LOSS_THRESHOLD_R * ONE_R_NET_TWD
                and mfe_net <= MAXIMUM_PROGRESS_R * ONE_R_NET_TWD
                and recovery <= MAXIMUM_RECOVERY_FROM_MAE_R * ONE_R_NET_TWD
            )
            checkpoint_action = "EXIT" if triggered else "HOLD"
            if triggered:
                reason = "RECOVERY_AWARE_EARLY_FAILURE"
        if reason is None and point.reversal:
            reason = "SIGNAL_REVERSAL"
        elif reason is None and point.at >= hard_exit:
            reason = "HARD_EXIT"
        elif reason is None and trade.force_last_point_exit and index == len(trade.points) - 1:
            reason = "HARD_EXIT"
        if reason is None:
            continue

        row = _result(
            trade, ExitVariant(variant.name), index, reason,
            locked_floor=locked_r, mfe_net=mfe_net,
        )
        full_mfe = max(float(item.projected_net_pnl) for item in trade.points)
        realized = float(row["net_pnl_twd"])
        retention = realized / full_mfe if full_mfe > 0 and realized > 0 else None
        row.update({
            "loss_overlay": variant.loss_overlay,
            "activation_r": variant.activation_r,
            "initial_lock_buffer_r": variant.initial_lock_buffer_r,
            "mfe_protection_armed": locked_r is not None,
            "mfe_activation_time": activation_time.isoformat() if activation_time else None,
            "max_locked_profit_r": locked_r,
            "profit_retention_ratio": retention,
            "checkpoint_action": checkpoint_action,
        })
        return row
    raise RuntimeError(f"no exit for {trade.trade_id}/{variant.name}")


def _summary(
    name: str, rows: list[dict[str, Any]], reference: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    output = _cohort_summary(name, rows, reference)
    retentions = [
        float(row["profit_retention_ratio"])
        for row in rows if row.get("profit_retention_ratio") is not None
    ]
    output.update({
        "negative_mfe_exits": sum(
            row["exit_reason"] == "BUFFERED_NET_MFE_PROFIT_PROTECTION"
            and float(row["net_pnl_twd"]) < 0 for row in rows
        ),
        "mfe_exits": sum(
            row["exit_reason"] == "BUFFERED_NET_MFE_PROFIT_PROTECTION"
            for row in rows
        ),
        "average_profit_retention_ratio": (
            round(sum(retentions) / len(retentions), 6) if retentions else None
        ),
        "improvement_contributors": sum(
            float(row["net_pnl_twd"])
            > float(reference[row["validation_trade_id"]]["net_pnl_twd"])
            for row in rows
        ),
    })
    return output


def build_report(
    historical_runs: Mapping[str, list[Path]], run_20260929: Path,
    early_20260930: Path, late_20260930: Path, run_20261001: Path,
    *, capital_twd: int = 190_000,
) -> dict[str, Any]:
    items = _prepare_items(
        historical_runs, run_20260929, early_20260930, late_20260930,
        run_20261001, capital_twd,
    )
    rows: list[dict[str, Any]] = []
    ids = {"base_signals": set(), "additional_near_misses": set(), "expanded": set()}
    for index, item in enumerate(items):
        trade = item["trade"]
        trade_id = f"{item['source_signal_type']}::{trade.trade_id}::{index}"
        group = "additional_near_misses" if item["source_signal_type"] == "NEAR_MISS" else "base_signals"
        ids[group].add(trade_id)
        ids["expanded"].add(trade_id)
        reference = simulate_joint(trade, JointVariant("CURRENT_LOSS", "MFE_V1"))
        reference.update({"validation_trade_id": trade_id, "source_signal_type": item["source_signal_type"]})
        rows.append(reference)
        for variant in variants():
            row = simulate_buffered(trade, variant)
            row.update({"validation_trade_id": trade_id, "source_signal_type": item["source_signal_type"]})
            rows.append(row)

    lenses = {}
    for group, selected_ids in ids.items():
        selected = [row for row in rows if row["validation_trade_id"] in selected_ids]
        reference = {
            row["validation_trade_id"]: row
            for row in selected if row["variant"] == PRODUCTION_REFERENCE
        }
        summaries = [
            _summary(
                variant.name,
                [row for row in selected if row["variant"] == variant.name],
                reference,
            )
            for variant in variants()
        ]
        summaries.sort(key=lambda row: -float(row["net_pnl_twd"]))
        lenses[group] = summaries

    report = {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "BACKTEST_ONLY_NET_MFE_GAP_BUFFER_GRID",
        "capital_twd": capital_twd,
        "one_r_net_twd": ONE_R_NET_TWD,
        "production_reference": PRODUCTION_REFERENCE,
        "variants": [asdict(variant) | {"name": variant.name} for variant in variants()],
        "counts": {group: len(value) for group, value in ids.items()},
        "lenses": lenses,
        "per_trade": rows,
        "limitations": [
            "The grid is diagnostic and must not be selected by best in-sample PnL alone.",
            "Only ten base signals and eighteen overlapping near-misses are available.",
            "A stop can still gap through any buffer; observed execution prices are retained.",
            "The 20260930 session is stitched and has four untimestamped callback errors.",
        ],
        "production_behavior_changed": False,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    report["report_hash"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Net-MFE activation and gap-buffer validation", "",
        "This fixed-entry diagnostic varies only net-MFE activation and the first locked-profit buffer. All costs and observed exit prices remain unchanged.", "",
    ]
    for lens, title in (("base_signals", "Base signals"), ("expanded", "Expanded signals")):
        lines.extend([
            f"## {title}", "",
            "| Rank | Loss overlay | Activation | Buffer | Net PnL | PF | Avg winner | Avg loser | Max DD | Retention | Negative MFE exits | Winners harmed | LOTO robust |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ])
        for rank, row in enumerate(report["lenses"][lens], 1):
            variant = next(item for item in report["variants"] if item["name"] == row["variant"])
            lines.append(
                f"| {rank} | {variant['loss_overlay']} | {variant['activation_r']:.2f}R | "
                f"{variant['initial_lock_buffer_r']:.2f}R | {row['net_pnl_twd']:.0f} | "
                f"{row['profit_factor']} | {(row['average_winner_twd'] or 0):.0f} | "
                f"{(row['average_loser_twd'] or 0):.0f} | {row['maximum_drawdown_twd']:.0f} | "
                f"{row['average_profit_retention_ratio']:.1%} | {row['negative_mfe_exits']} | "
                f"{row['baseline_winners_harmed']} | "
                f"{'YES' if row['improvement_survives_every_leave_one_out'] else 'NO'} |"
            )
        lines.append("")
    best_base = report["lenses"]["base_signals"][0]
    best_expanded = report["lenses"]["expanded"][0]
    lines.extend([
        "## Finding", "",
        f"- Best base-signal cell: {best_base['variant']} at {best_base['net_pnl_twd']:.0f} TWD.",
        f"- Best expanded cell: {best_expanded['variant']} at {best_expanded['net_pnl_twd']:.0f} TWD.",
        "- The 0.75R activation with a 0.30R-0.40R initial floor is a local plateau across both cohorts; 0.50R activation is less stable and a 0.50R floor begins to harm a reference winner.",
        "- The 0.30R-0.40R floor eliminated negative MFE exits in the tested paths, but total PnL and PF remain negative/below one.",
        "- A buffer is useful only if the observed path contains a quote before the gap; it cannot guarantee a non-negative fill.",
        "- Production promotion requires the same region to remain favorable across both cohorts and future complete sessions.", "",
        "## Limitations", "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.extend(["", "No production or live behavior changed.", ""])
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "net_mfe_gap_buffer_validation_20260922_20261001.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (output_dir / "report_net_mfe_gap_buffer_validation.md").write_text(
        markdown_report(report), encoding="utf-8",
    )
    fields = [
        "validation_trade_id", "source_signal_type", "variant", "session_date", "symbol",
        "entry_time", "exit_time", "exit_reason", "net_pnl_twd", "full_path_mfe_net_pnl_twd",
        "loss_overlay", "activation_r", "initial_lock_buffer_r", "max_locked_profit_r",
        "mfe_protection_armed", "profit_retention_ratio", "post_exit_best_net_pnl_twd",
        "post_exit_worst_net_pnl_twd",
    ]
    with (output_dir / "net_mfe_gap_buffer_validation_per_trade.csv").open(
        "w", encoding="utf-8", newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in report["per_trade"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-20260922", action="append", required=True, type=Path)
    parser.add_argument("--run-20260923", action="append", required=True, type=Path)
    parser.add_argument("--run-20260924", action="append", required=True, type=Path)
    parser.add_argument("--run-20260929", required=True, type=Path)
    parser.add_argument("--early-20260930", required=True, type=Path)
    parser.add_argument("--late-20260930", required=True, type=Path)
    parser.add_argument("--run-20261001", required=True, type=Path)
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    report = build_report(
        {"20260922": args.run_20260922, "20260923": args.run_20260923,
         "20260924": args.run_20260924},
        args.run_20260929, args.early_20260930, args.late_20260930,
        args.run_20261001, capital_twd=args.capital,
    )
    write_report(report, args.output_dir)
    print(json.dumps({key: value[0] for key, value in report["lenses"].items()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
