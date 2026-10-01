"""Backtest-only validation of a short recovery grace after the 120s stop.

The entry, hard stop, MFE protection and all ordinary exits stay unchanged.
Only a recovery-aware 120-second early-stop trigger is made pending for a
fixed grace period.  A sufficiently strong observed recovery cancels that
pending stop once; otherwise the trade exits at the first observed point at
or after expiry, using the existing executable-price and cost model.
"""
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
from .net_mfe_gap_buffer_validation import (
    BufferVariant,
    candidate_floor_r,
    simulate_buffered,
)
from .path_failure_stop_validation import _prepare_items


ANALYSIS_ID = "EARLY_STOP_GRACE_VALIDATION_V0_1"
REFERENCE_NAME = "IMMEDIATE_RECOVERY_AWARE_120S"
BASE_POLICY = BufferVariant("RECOVERY_AWARE_120S", 0.75, 0.30)
CHECKPOINT_SECONDS = 120
GRACE_SECONDS = (15, 30, 60)
RECOVERY_THRESHOLDS_R = (0.10, 0.20, 0.30)
MAXIMUM_PROGRESS_R = 0.10
LOSS_THRESHOLD_R = 0.20
MAXIMUM_RECOVERY_FROM_MAE_R = 0.20


@dataclass(frozen=True, slots=True)
class GraceVariant:
    grace_seconds: int
    cancel_recovery_r: float

    @property
    def name(self) -> str:
        return (
            f"EARLY_STOP_GRACE_{self.grace_seconds}S"
            f"__CANCEL_RECOVERY_{self.cancel_recovery_r:.2f}R"
        )


def variants() -> tuple[GraceVariant, ...]:
    return tuple(
        GraceVariant(grace, recovery)
        for grace in GRACE_SECONDS
        for recovery in RECOVERY_THRESHOLDS_R
    )


def simulate_grace(
    trade, variant: GraceVariant, *, hard_stop_r: float = 1.0,
) -> dict[str, Any]:
    """Simulate one pending early stop; hard/MFE exits always remain first."""
    if hard_stop_r <= 0:
        raise ValueError("hard_stop_r must be positive")
    checkpoint_at = trade.entry_time + timedelta(seconds=CHECKPOINT_SECONDS)
    checkpoint_evaluated = False
    pending_at = None
    pending_started_at = None
    pending_pnl = None
    pending_mae = None
    pending_cancelled = False
    cancellation_at = None
    cancellation_pnl = None
    mfe_net = mae_net = float(trade.pnl_at_price(trade.entry_price))
    locked_r: float | None = None
    activation_time = None
    hard_hour, hard_minute = map(int, str(SPEC["hard_exit_time"]).split(":"))
    hard_exit = trade.entry_time.replace(
        hour=hard_hour, minute=hard_minute, second=0, microsecond=0,
    )
    checkpoint_action = "NOT_APPLICABLE"

    for index, point in enumerate(trade.points):
        pnl = float(point.projected_net_pnl)
        mfe_net = max(mfe_net, pnl)
        mae_net = min(mae_net, pnl)
        candidate = candidate_floor_r(
            max(0.0, mfe_net / ONE_R_NET_TWD),
            activation_r=BASE_POLICY.activation_r,
            initial_buffer_r=BASE_POLICY.initial_lock_buffer_r,
        )
        if candidate is not None:
            if activation_time is None:
                activation_time = point.at
            locked_r = candidate if locked_r is None else max(locked_r, candidate)

        reason = None
        if pnl <= -hard_stop_r * ONE_R_NET_TWD:
            reason = "DISASTER_STOP_NEG_1R"
        elif locked_r is not None and pnl <= locked_r * ONE_R_NET_TWD:
            reason = "BUFFERED_NET_MFE_PROFIT_PROTECTION"

        if reason is None and pending_at is not None:
            recovered = (
                pnl - float(pending_pnl)
                >= variant.cancel_recovery_r * ONE_R_NET_TWD
                or pnl >= 0
            )
            if recovered:
                pending_cancelled = True
                cancellation_at = point.at
                cancellation_pnl = pnl
                pending_at = None
                checkpoint_action = "PENDING_CANCELLED_BY_RECOVERY"
            elif (point.at - pending_at).total_seconds() >= variant.grace_seconds:
                reason = "RECOVERY_GRACE_EXPIRED"
                checkpoint_action = "PENDING_EXPIRED"

        if (
            reason is None
            and not checkpoint_evaluated
            and point.at >= checkpoint_at
        ):
            checkpoint_evaluated = True
            recovery_from_mae = pnl - mae_net
            triggered = (
                pnl <= -LOSS_THRESHOLD_R * ONE_R_NET_TWD
                and mfe_net <= MAXIMUM_PROGRESS_R * ONE_R_NET_TWD
                and recovery_from_mae <= MAXIMUM_RECOVERY_FROM_MAE_R * ONE_R_NET_TWD
            )
            if triggered:
                pending_at = point.at
                pending_started_at = point.at
                pending_pnl = pnl
                pending_mae = mae_net
                checkpoint_action = "PENDING"
            else:
                checkpoint_action = "HOLD"

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
        if reason is None:
            continue

        row = _result(
            trade, ExitVariant(variant.name), index, reason,
            locked_floor=locked_r, mfe_net=mfe_net,
        )
        full_mfe = float(row["full_path_mfe_net_pnl_twd"])
        realized = float(row["net_pnl_twd"])
        row.update({
            "grace_seconds": variant.grace_seconds,
            "cancel_recovery_r": variant.cancel_recovery_r,
            "checkpoint_action": checkpoint_action,
            "pending_started": pending_pnl is not None,
            "pending_start_time": pending_started_at.isoformat() if pending_started_at else None,
            "pending_start_pnl_twd": pending_pnl,
            "pending_start_mae_twd": pending_mae,
            "pending_cancelled": pending_cancelled,
            "pending_cancel_time": cancellation_at.isoformat() if cancellation_at else None,
            "pending_cancel_pnl_twd": cancellation_pnl,
            "mfe_protection_armed": locked_r is not None,
            "mfe_activation_time": activation_time.isoformat() if activation_time else None,
            "max_locked_profit_r": locked_r,
            "profit_retention_ratio": (
                realized / full_mfe if realized > 0 and full_mfe > 0 else None
            ),
        })
        return row
    raise RuntimeError(f"no exit for {trade.trade_id}/{variant.name}")


def _summary(
    name: str, rows: list[dict[str, Any]], reference: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    output = _cohort_summary(name, rows, reference)
    output.update({
        "pending_stops": sum(bool(row["pending_started"]) for row in rows),
        "pending_cancelled": sum(bool(row["pending_cancelled"]) for row in rows),
        "pending_expired": sum(
            row["exit_reason"] == "RECOVERY_GRACE_EXPIRED" for row in rows
        ),
        "cancelled_then_profitable": sum(
            bool(row["pending_cancelled"]) and float(row["net_pnl_twd"]) > 0
            for row in rows
        ),
        "cancelled_then_loss": sum(
            bool(row["pending_cancelled"]) and float(row["net_pnl_twd"]) <= 0
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
        reference = simulate_buffered(trade, BASE_POLICY)
        reference.update({
            "variant": REFERENCE_NAME,
            "validation_trade_id": trade_id,
            "source_signal_type": item["source_signal_type"],
            "gate_reason": item.get("gate_reason", ""),
        })
        rows.append(reference)
        for variant in variants():
            result = simulate_grace(trade, variant)
            result.update({
                "validation_trade_id": trade_id,
                "source_signal_type": item["source_signal_type"],
                "gate_reason": item.get("gate_reason", ""),
            })
            rows.append(result)

    lenses = {}
    reference_metrics = {}
    for group, selected_ids in ids.items():
        selected = [row for row in rows if row["validation_trade_id"] in selected_ids]
        reference = {
            row["validation_trade_id"]: row
            for row in selected if row["variant"] == REFERENCE_NAME
        }
        reference_metrics[group] = _cohort_summary(
            REFERENCE_NAME, list(reference.values()), reference,
        )
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

    best = lenses["base_signals"][0]
    best_rows = {
        row["validation_trade_id"]: row
        for row in rows
        if row["variant"] == best["variant"]
        and row["validation_trade_id"] in ids["base_signals"]
    }
    base_reference_rows = {
        row["validation_trade_id"]: row
        for row in rows
        if row["variant"] == REFERENCE_NAME
        and row["validation_trade_id"] in ids["base_signals"]
    }
    notable_base_impacts = sorted(
        ({
            "validation_trade_id": trade_id,
            "symbol": row["symbol"],
            "reference_exit_reason": base_reference_rows[trade_id]["exit_reason"],
            "grace_exit_reason": row["exit_reason"],
            "reference_net_pnl_twd": float(base_reference_rows[trade_id]["net_pnl_twd"]),
            "grace_net_pnl_twd": float(row["net_pnl_twd"]),
            "difference_twd": round(
                float(row["net_pnl_twd"])
                - float(base_reference_rows[trade_id]["net_pnl_twd"]), 2,
            ),
            "full_path_mfe_net_pnl_twd": float(row["full_path_mfe_net_pnl_twd"]),
            "pending_cancelled": bool(row["pending_cancelled"]),
        } for trade_id, row in best_rows.items()
         if float(row["net_pnl_twd"])
         != float(base_reference_rows[trade_id]["net_pnl_twd"])),
        key=lambda item: item["difference_twd"],
    )
    report = {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "BACKTEST_ONLY_PENDING_EARLY_STOP_RECOVERY_GRACE",
        "capital_twd": capital_twd,
        "one_r_net_twd": ONE_R_NET_TWD,
        "reference": REFERENCE_NAME,
        "base_exit_policy": asdict(BASE_POLICY),
        "variants": [asdict(variant) | {"name": variant.name} for variant in variants()],
        "rule": {
            "checkpoint_seconds": CHECKPOINT_SECONDS,
            "loss_threshold_r": LOSS_THRESHOLD_R,
            "maximum_progress_r": MAXIMUM_PROGRESS_R,
            "maximum_recovery_from_mae_r": MAXIMUM_RECOVERY_FROM_MAE_R,
            "hard_stop_stays_immediate": True,
            "mfe_exit_stays_immediate": True,
            "pending_can_cancel_once": True,
        },
        "counts": {group: len(value) for group, value in ids.items()},
        "classification": (
            "GRACE_EDGE_REQUIRES_SHADOW_VALIDATION"
            if float(best["net_difference_vs_baseline_twd"]) > 0
            and bool(best["improvement_survives_every_leave_one_out"])
            and int(best["baseline_winners_harmed"]) == 0
            else "NO_ROBUST_EARLY_STOP_GRACE_EDGE"
        ),
        "reference_metrics": reference_metrics,
        "lenses": lenses,
        "notable_base_impacts_for_best_variant": notable_base_impacts,
        "per_trade": rows,
        "limitations": [
            "Only ten base signals and eighteen overlapping near-misses are available.",
            "The fixed grace grid is diagnostic and was not optimized on a separate test set.",
            "Older independent signals and near-misses are not one executable portfolio.",
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
        "# Early-stop recovery-grace validation", "",
        "The recovery-aware 120-second stop is held briefly only when it would otherwise trigger. Hard stop, MFE protection, reversal and EOD exits remain active. A measured recovery cancels the pending stop once.", "",
    ]
    for lens, title in (("base_signals", "Base signals"), ("expanded", "Expanded signals")):
        lines.extend([
            f"## {title}", "",
            "| Rank | Grace | Cancel recovery | Net PnL | vs immediate | PF | Avg loser | Max DD | Pending | Cancelled | Cancelled profitable | Winners harmed | LOTO robust |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ])
        for rank, row in enumerate(report["lenses"][lens], 1):
            variant = next(item for item in report["variants"] if item["name"] == row["variant"])
            lines.append(
                f"| {rank} | {variant['grace_seconds']}s | {variant['cancel_recovery_r']:.2f}R | "
                f"{row['net_pnl_twd']:.0f} | {row['net_difference_vs_baseline_twd']:+.0f} | "
                f"{row['profit_factor']} | {(row['average_loser_twd'] or 0):.0f} | "
                f"{row['maximum_drawdown_twd']:.0f} | {row['pending_stops']} | "
                f"{row['pending_cancelled']} | {row['cancelled_then_profitable']} | "
                f"{row['baseline_winners_harmed']} | "
                f"{'YES' if row['improvement_survives_every_leave_one_out'] else 'NO'} |"
            )
        lines.append("")
    best = report["lenses"]["base_signals"][0]
    reference = report["reference_metrics"]["base_signals"]
    lines.extend([
        "## Finding", "",
        f"- Immediate-stop reference: {reference['net_pnl_twd']:.0f} TWD, PF {reference['profit_factor']}, max drawdown {reference['maximum_drawdown_twd']:.0f} TWD.",
        f"- Best base-signal grace: {best['variant']} at {best['net_pnl_twd']:.0f} TWD ({best['net_difference_vs_baseline_twd']:+.0f}).",
        f"- Classification: {report['classification']}.",
        f"- It delayed {best['pending_stops']} base-signal stops, cancelled {best['pending_cancelled']}, and improved {best['trades_improved']} trades while worsening {best['trades_worsened']}.",
        "- 2221 never met even the smallest 0.10R recovery threshold inside 15/30/60 seconds; the grace exit lost more before the later rally.",
        "- This result does not change production, paper-shadow or live behavior.", "",
        "## Material base-trade impacts for the least-damaging grace", "",
        "| Symbol | Immediate PnL | Grace PnL | Difference | Full-path MFE | Grace exit |",
        "|---|---:|---:|---:|---:|---|",
    ])
    for row in report["notable_base_impacts_for_best_variant"]:
        lines.append(
            f"| {row['symbol']} | {row['reference_net_pnl_twd']:.0f} | "
            f"{row['grace_net_pnl_twd']:.0f} | {row['difference_twd']:+.0f} | "
            f"{row['full_path_mfe_net_pnl_twd']:.0f} | {row['grace_exit_reason']} |"
        )
    lines.extend([
        "",
        "## Limitations", "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "early_stop_grace_validation_20260922_20261001.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (output_dir / "report_early_stop_grace_validation.md").write_text(
        markdown_report(report), encoding="utf-8",
    )
    fields = [
        "validation_trade_id", "source_signal_type", "gate_reason", "variant",
        "session_date", "symbol", "entry_time", "exit_time", "exit_price",
        "exit_reason", "net_pnl_twd", "full_path_mfe_net_pnl_twd",
        "full_path_mae_net_pnl_twd", "grace_seconds", "cancel_recovery_r",
        "checkpoint_action", "pending_started", "pending_start_time",
        "pending_start_pnl_twd", "pending_start_mae_twd", "pending_cancelled",
        "pending_cancel_time", "pending_cancel_pnl_twd", "max_locked_profit_r",
        "profit_retention_ratio",
    ]
    with (output_dir / "early_stop_grace_validation_per_trade.csv").open(
        "w", encoding="utf-8", newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(
            {field: row.get(field) for field in fields}
            for row in report["per_trade"]
        )


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
    print(json.dumps(
        {key: value[0] for key, value in report["lenses"].items()},
        ensure_ascii=False, indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
