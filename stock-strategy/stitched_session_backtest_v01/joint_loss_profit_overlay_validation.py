"""Backtest-only joint validation of early loss control and MFE profit protection.

Entries, sizing, path data, fees, tax and slippage are held fixed.  This module
does not alter the production strategy or broker execution path.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from statistics import mean, median
from typing import Any, Mapping

from mfe_profit_protection_study_v01.overlay import (
    MFEProtectionState,
    VARIANTS as MFE_VARIANTS,
)
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC

from .exit_first_historical_validation import _cohort_summary
from .exit_first_profitability_study import ONE_R_NET_TWD, ExitVariant, _result
from .path_failure_stop_validation import _prepare_items


ANALYSIS_ID = "JOINT_LOSS_PROFIT_OVERLAY_VALIDATION_V0_1"
CHECKPOINT_SECONDS = 120
MAXIMUM_PROGRESS_R = 0.10
RECOVERY_LOSS_THRESHOLD_R = 0.20
MAXIMUM_RECOVERY_FROM_MAE_R = 0.20


@dataclass(frozen=True, slots=True)
class JointVariant:
    loss_overlay: str
    profit_overlay: str

    @property
    def name(self) -> str:
        return f"{self.loss_overlay}__{self.profit_overlay}"


LOSS_OVERLAYS = ("CURRENT_LOSS", "PLAIN_120S", "RECOVERY_AWARE_120S")
PROFIT_OVERLAYS = (
    "NO_MFE", "MFE_LOOSE", "MFE_V1", "MFE_AGGRESSIVE",
    "NET_MFE_LOOSE", "NET_MFE_V1", "NET_MFE_AGGRESSIVE",
)
REFERENCE_VARIANT = "CURRENT_LOSS__MFE_V1"


def variants() -> tuple[JointVariant, ...]:
    return tuple(
        JointVariant(loss, profit)
        for loss in LOSS_OVERLAYS
        for profit in PROFIT_OVERLAYS
    )


def _net_candidate_locked_r(profile: str, mfe_net_r: float) -> float | None:
    """Cost-aware counterpart to the existing price-R overlay."""
    if mfe_net_r < 1.0:
        return None
    if mfe_net_r < 1.5:
        return 0.0
    rates = {
        "NET_MFE_LOOSE": (0.50, 0.60, 0.70),
        "NET_MFE_V1": (0.60, 0.70, 0.75),
        "NET_MFE_AGGRESSIVE": (0.70, 0.75, 0.80),
    }[profile]
    if mfe_net_r < 2.0:
        return rates[0] * mfe_net_r
    if mfe_net_r < 3.0:
        return rates[1] * mfe_net_r
    return rates[2] * mfe_net_r


def simulate_joint(trade, variant: JointVariant) -> dict[str, Any]:
    """Replay a fixed trade path with independent loss and profit overlays."""
    net_mfe_mode = variant.profit_overlay.startswith("NET_MFE_")
    overlay = (
        MFEProtectionState(trade.basis, MFE_VARIANTS[variant.profit_overlay])
        if variant.profit_overlay in MFE_VARIANTS else None
    )
    if overlay is not None:
        overlay.observe(
            price=trade.entry_price,
            at=trade.entry_time,
            projected_net_pnl=trade.pnl_at_price(trade.entry_price),
            pnl_at_price=trade.pnl_at_price,
        )
    checkpoint_at = trade.entry_time + timedelta(seconds=CHECKPOINT_SECONDS)
    checkpoint_evaluated = False
    checkpoint_action = "NOT_APPLICABLE"
    checkpoint_details: dict[str, Any] = {}
    mfe_net = trade.pnl_at_price(trade.entry_price)
    mae_net = mfe_net
    net_locked_r: float | None = None
    net_mfe_activation_time = None
    hard_hour, hard_minute = map(int, str(SPEC["hard_exit_time"]).split(":"))
    hard_exit = trade.entry_time.replace(
        hour=hard_hour, minute=hard_minute, second=0, microsecond=0,
    )

    for index, point in enumerate(trade.points):
        mfe_net = max(mfe_net, float(point.projected_net_pnl))
        mae_net = min(mae_net, float(point.projected_net_pnl))
        if overlay is not None:
            overlay.observe(
                price=point.exit_price,
                at=point.at,
                projected_net_pnl=point.projected_net_pnl,
                pnl_at_price=trade.pnl_at_price,
            )
        if net_mfe_mode:
            candidate = _net_candidate_locked_r(
                variant.profit_overlay, max(0.0, mfe_net / ONE_R_NET_TWD)
            )
            if candidate is not None:
                if net_mfe_activation_time is None:
                    net_mfe_activation_time = point.at
                net_locked_r = (
                    candidate if net_locked_r is None
                    else max(net_locked_r, candidate)
                )

        reason = None
        if point.projected_net_pnl <= -ONE_R_NET_TWD:
            reason = "DISASTER_STOP_NEG_1R"
        elif overlay is not None and overlay.triggered(point.exit_price):
            reason = "MFE_PROFIT_PROTECTION"
        elif (
            net_locked_r is not None
            and point.projected_net_pnl <= net_locked_r * ONE_R_NET_TWD
        ):
            reason = "NET_MFE_PROFIT_PROTECTION"
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
            checkpoint_details = {
                "checkpoint_pnl_twd": float(point.projected_net_pnl),
                "checkpoint_mfe_twd": mfe_net,
                "checkpoint_mae_twd": mae_net,
                "checkpoint_recovery_from_mae_twd": recovery,
            }
            if triggered:
                reason = "RECOVERY_AWARE_EARLY_FAILURE"

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

        locked_r = (
            overlay.locked_profit_r
            if overlay is not None and overlay.armed else net_locked_r
        )
        row = _result(
            trade,
            ExitVariant(variant.name),
            index,
            reason,
            locked_floor=locked_r,
            mfe_net=mfe_net,
        )
        full_mfe = max(float(item.projected_net_pnl) for item in trade.points)
        realized = float(row["net_pnl_twd"])
        retention = realized / full_mfe if full_mfe > 0 and realized > 0 else None
        row.update({
            "loss_overlay": variant.loss_overlay,
            "profit_overlay": variant.profit_overlay,
            "mfe_protection_armed": bool(
                (overlay and overlay.armed) or net_locked_r is not None
            ),
            "mfe_activation_time": (
                overlay.activation_time.isoformat()
                if overlay and overlay.activation_time else None
            ) or (
                net_mfe_activation_time.isoformat()
                if net_mfe_activation_time else None
            ),
            "max_locked_profit_r": locked_r,
            "profit_retention_ratio": retention,
            "profit_giveback_ratio": None if retention is None else 1.0 - retention,
            "checkpoint_action": checkpoint_action,
            **checkpoint_details,
        })
        return row
    raise RuntimeError(f"no exit for {trade.trade_id}/{variant.name}")


def _joint_summary(
    name: str,
    rows: list[dict[str, Any]],
    reference_rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    summary = _cohort_summary(name, rows, reference_rows)
    retentions = [
        float(row["profit_retention_ratio"])
        for row in rows if row["profit_retention_ratio"] is not None
    ]
    reference_winners = [
        row for row in rows
        if float(reference_rows[row["validation_trade_id"]]["net_pnl_twd"]) > 0
    ]
    summary.update({
        "average_profit_retention_ratio": (
            round(mean(retentions), 6) if retentions else None
        ),
        "median_profit_retention_ratio": (
            round(median(retentions), 6) if retentions else None
        ),
        "mfe_activations": sum(row["mfe_protection_armed"] for row in rows),
        "mfe_exits": sum(
            row["exit_reason"] in {
                "MFE_PROFIT_PROTECTION", "NET_MFE_PROFIT_PROTECTION"
            } for row in rows
        ),
        "negative_mfe_exits": sum(
            row["exit_reason"] in {
                "MFE_PROFIT_PROTECTION", "NET_MFE_PROFIT_PROTECTION"
            } and float(row["net_pnl_twd"]) < 0
            for row in rows
        ),
        "pnl_from_mfe_exits_twd": round(sum(
            float(row["net_pnl_twd"])
            for row in rows if row["exit_reason"] in {
                "MFE_PROFIT_PROTECTION", "NET_MFE_PROFIT_PROTECTION"
            }
        ), 2),
        "reference_winners_improved": sum(
            float(row["net_pnl_twd"])
            > float(reference_rows[row["validation_trade_id"]]["net_pnl_twd"])
            for row in reference_winners
        ),
        "reference_winners_cut_early": sum(
            float(row["net_pnl_twd"])
            < float(reference_rows[row["validation_trade_id"]]["net_pnl_twd"])
            for row in reference_winners
        ),
    })
    return summary


def build_report(
    historical_runs: Mapping[str, list[Path]],
    run_20260929: Path,
    early_20260930: Path,
    late_20260930: Path,
    run_20261001: Path,
    *,
    capital_twd: int = 190_000,
) -> dict[str, Any]:
    items = _prepare_items(
        historical_runs, run_20260929, early_20260930, late_20260930,
        run_20261001, capital_twd,
    )
    rows: list[dict[str, Any]] = []
    ids_by_group = {
        "base_signals": set(), "additional_near_misses": set(), "expanded": set(),
    }
    for index, item in enumerate(items):
        trade = item["trade"]
        validation_trade_id = f"{item['source_signal_type']}::{trade.trade_id}::{index}"
        group = (
            "additional_near_misses"
            if item["source_signal_type"] == "NEAR_MISS" else "base_signals"
        )
        ids_by_group[group].add(validation_trade_id)
        ids_by_group["expanded"].add(validation_trade_id)
        for variant in variants():
            row = simulate_joint(trade, variant)
            row.update({
                "validation_trade_id": validation_trade_id,
                "source_signal_type": item["source_signal_type"],
                "gate_reason": item.get("gate_reason"),
            })
            rows.append(row)

    lenses: dict[str, list[dict[str, Any]]] = {}
    for group, ids in ids_by_group.items():
        selected = [row for row in rows if row["validation_trade_id"] in ids]
        reference = {
            row["validation_trade_id"]: row
            for row in selected if row["variant"] == REFERENCE_VARIANT
        }
        lenses[group] = [
            _joint_summary(
                variant.name,
                [row for row in selected if row["variant"] == variant.name],
                reference,
            )
            for variant in variants()
        ]

    expanded = lenses["expanded"]
    robust = [
        row for row in expanded
        if row["reference_winners_cut_early"] == 0
        and row["improvement_survives_every_leave_one_out"]
    ]
    robust.sort(key=lambda row: (-float(row["net_pnl_twd"]), row["maximum_drawdown_twd"]))
    report = {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "BACKTEST_ONLY_FIXED_ENTRY_JOINT_EXIT_OVERLAY_DIAGNOSTIC",
        "capital_twd": capital_twd,
        "one_r_net_twd": ONE_R_NET_TWD,
        "reference_variant": REFERENCE_VARIANT,
        "checkpoint_seconds": CHECKPOINT_SECONDS,
        "maximum_progress_r": MAXIMUM_PROGRESS_R,
        "recovery_loss_threshold_r": RECOVERY_LOSS_THRESHOLD_R,
        "maximum_recovery_from_mae_r": MAXIMUM_RECOVERY_FROM_MAE_R,
        "variants": [
            {"name": variant.name, "loss_overlay": variant.loss_overlay,
             "profit_overlay": variant.profit_overlay}
            for variant in variants()
        ],
        "counts": {group: len(ids) for group, ids in ids_by_group.items()},
        "lenses": lenses,
        "top_robust_expanded": robust,
        "per_trade": rows,
        "limitations": [
            "Twenty-eight unique signals are insufficient for production promotion.",
            "The eighteen near-miss entries overlap and are not feasible portfolio PnL.",
            "The 20260930 session is stitched and has four untimestamped callback errors.",
            "MFE retention uses the full observed path, including movement after an earlier simulated exit.",
            "The recovery-aware thresholds are declared diagnostics, not fitted parameters.",
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
        "# Joint loss-control and MFE profit-protection validation", "",
        "The same 28 fixed entries, quantities, market paths and cost model are replayed. The production reference is CURRENT_LOSS + MFE_V1; no live behavior changed.", "",
    ]
    for lens, title in (
        ("base_signals", "Base signals"),
        ("additional_near_misses", "Additional near-misses"),
        ("expanded", "Expanded unique signals"),
    ):
        lines.extend([
            f"## {title}", "",
            "| Loss overlay | Profit overlay | Net PnL | vs reference | PF | Avg winner | Avg loser | Max DD | Retention | MFE exits (loss) | Winners cut |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for row in report["lenses"][lens]:
            loss, profit = row["variant"].split("__", 1)
            retention = row["average_profit_retention_ratio"]
            lines.append(
                f"| {loss} | {profit} | {row['net_pnl_twd']:.0f} | "
                f"{row['net_difference_vs_baseline_twd']:+.0f} | {row['profit_factor']} | "
                f"{(row['average_winner_twd'] or 0):.0f} | {(row['average_loser_twd'] or 0):.0f} | "
                f"{row['maximum_drawdown_twd']:.0f} | "
                f"{('-' if retention is None else f'{retention:.1%}')} | "
                f"{row['mfe_exits']} ({row['negative_mfe_exits']}) | "
                f"{row['reference_winners_cut_early']} |"
            )
        lines.append("")
    base = {row["variant"]: row for row in report["lenses"]["base_signals"]}
    expanded = {row["variant"]: row for row in report["lenses"]["expanded"]}
    reference = expanded[report["reference_variant"]]
    best = max(
        (row for row in expanded.values() if row["reference_winners_cut_early"] == 0),
        key=lambda row: float(row["net_pnl_twd"]),
    )
    lines.extend([
        "## Finding", "",
        f"- Production-reference replay: {reference['net_pnl_twd']:.0f} TWD, PF {reference['profit_factor']}, max drawdown {reference['maximum_drawdown_twd']:.0f} TWD.",
        f"- On the ten base signals, PLAIN_120S__NET_MFE_LOOSE reduced the loss from {base[REFERENCE_VARIANT]['net_pnl_twd']:.0f} to {base['PLAIN_120S__NET_MFE_LOOSE']['net_pnl_twd']:.0f} TWD, raised PF from {base[REFERENCE_VARIANT]['profit_factor']} to {base['PLAIN_120S__NET_MFE_LOOSE']['profit_factor']}, and cut no reference winner.",
        f"- Best result without cutting a reference winner: {best['variant']} at {best['net_pnl_twd']:.0f} TWD ({best['net_difference_vs_baseline_twd']:+.0f}).",
        f"- Its average profitable-trade retention is {best['average_profit_retention_ratio']:.1%}." if best["average_profit_retention_ratio"] is not None else "- No profitable-trade retention value was available.",
        f"- Existing MFE_V1 produced {reference['negative_mfe_exits']} negative MFE exits after activation. This is consistent with price-R breakeven not covering fees/tax and with gaps between observations.",
        "- The cost-aware result is diagnostic only. Its best loss overlay differs between the base and near-miss cohorts, so there is no stable production combination yet.",
        "- A negative net result or PF below 1 is loss reduction only, not a profitable strategy.", "",
        "## Limitations", "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.extend(["", "No production or live behavior changed.", ""])
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "joint_loss_profit_overlay_validation_20260922_20261001.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (output_dir / "report_joint_loss_profit_overlay_validation.md").write_text(
        markdown_report(report), encoding="utf-8",
    )
    fields = [
        "source_signal_type", "gate_reason", "validation_trade_id", "variant",
        "loss_overlay", "profit_overlay", "session_date", "symbol", "entry_time",
        "entry_price", "quantity", "exit_time", "exit_price", "exit_reason",
        "net_pnl_twd", "holding_seconds", "full_path_mfe_net_pnl_twd",
        "full_path_mae_net_pnl_twd", "mfe_protection_armed", "mfe_activation_time",
        "max_locked_profit_r", "profit_retention_ratio", "profit_giveback_ratio",
        "checkpoint_action", "checkpoint_pnl_twd", "checkpoint_mfe_twd",
        "checkpoint_mae_twd", "checkpoint_recovery_from_mae_twd",
        "post_exit_best_net_pnl_twd", "post_exit_worst_net_pnl_twd",
    ]
    with (output_dir / "joint_loss_profit_overlay_validation_per_trade.csv").open(
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
    print(json.dumps(report["lenses"]["expanded"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
