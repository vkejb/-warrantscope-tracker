"""Backtest-only recovery-aware refinement of the 120-second failure exit."""
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

from .exit_first_historical_validation import _cohort_summary
from .exit_first_profitability_study import (
    ONE_R_NET_TWD,
    ExitVariant,
    _result,
    simulate_exit,
)
from .path_failure_stop_validation import (
    _prepare_items,
    build_causal_states,
)


ANALYSIS_ID = "RECOVERY_AWARE_EXIT_VALIDATION_V0_1"
CHECKPOINT_SECONDS = 120
MAXIMUM_MFE_R = 0.10
LOSS_THRESHOLDS_R = (0.0, 0.20, 0.30, 0.40)
MAXIMUM_RECOVERY_FROM_MAE_R = (0.10, 0.20, 0.30)
FLOW_REQUIREMENTS = (False, True)


@dataclass(frozen=True, slots=True)
class RecoveryVariant:
    name: str
    loss_threshold_r: float
    maximum_recovery_from_mae_r: float
    require_flow_2of3: bool


def variants() -> tuple[RecoveryVariant, ...]:
    return tuple(
        RecoveryVariant(
            name=(
                f"RECOVERY_AWARE_120S_LOSS_{int(loss * 100):02d}PCT_R_"
                f"RECOVERY_{int(recovery * 100):02d}PCT_R_"
                f"{'FLOW_2OF3' if flow else 'NO_FLOW'}"
            ),
            loss_threshold_r=loss,
            maximum_recovery_from_mae_r=recovery,
            require_flow_2of3=flow,
        )
        for loss in LOSS_THRESHOLDS_R
        for recovery in MAXIMUM_RECOVERY_FROM_MAE_R
        for flow in FLOW_REQUIREMENTS
    )


def simulate_recovery_aware(
    trade,
    data: dict[str, Any],
    breakout_boundary_price: float,
    variant: RecoveryVariant,
    states,
) -> dict[str, Any]:
    """Use one causal checkpoint; any earlier baseline exit always wins."""
    baseline = simulate_exit(
        trade, data, ExitVariant("CURRENT_BASELINE"), breakout_boundary_price,
    )
    baseline_exit = baseline["exit_time"]
    checkpoint_at = trade.entry_time + timedelta(seconds=CHECKPOINT_SECONDS)
    mfe_net = trade.pnl_at_price(trade.entry_price)
    mae_net = mfe_net
    for index, (point, state) in enumerate(zip(trade.points, states)):
        mfe_net = max(mfe_net, float(point.projected_net_pnl))
        mae_net = min(mae_net, float(point.projected_net_pnl))
        if point.at < checkpoint_at:
            continue
        if baseline_exit <= point.at.isoformat():
            baseline["variant"] = variant.name
            baseline.update({
                "loss_threshold_r": variant.loss_threshold_r,
                "maximum_recovery_from_mae_r": variant.maximum_recovery_from_mae_r,
                "require_flow_2of3": variant.require_flow_2of3,
                "checkpoint_action": "EARLIER_BASELINE_EXIT_WON",
                "checkpoint_pnl_twd": None,
                "checkpoint_mfe_twd": None,
                "checkpoint_mae_twd": None,
                "checkpoint_recovery_from_mae_twd": None,
            })
            return baseline
        recovery = float(point.projected_net_pnl) - mae_net
        triggered = (
            point.projected_net_pnl
            <= -variant.loss_threshold_r * ONE_R_NET_TWD
            and mfe_net <= MAXIMUM_MFE_R * ONE_R_NET_TWD
            and recovery
            <= variant.maximum_recovery_from_mae_r * ONE_R_NET_TWD
            and (not variant.require_flow_2of3 or state.flow_2of3_failed)
        )
        if triggered:
            row = _result(
                trade,
                ExitVariant(variant.name),
                index,
                "RECOVERY_AWARE_EARLY_FAILURE",
                locked_floor=None,
                mfe_net=mfe_net,
            )
        else:
            row = dict(baseline)
            row["variant"] = variant.name
        row.update({
            "loss_threshold_r": variant.loss_threshold_r,
            "maximum_recovery_from_mae_r": variant.maximum_recovery_from_mae_r,
            "require_flow_2of3": variant.require_flow_2of3,
            "checkpoint_action": "EXIT" if triggered else "HOLD_BASELINE",
            "checkpoint_pnl_twd": float(point.projected_net_pnl),
            "checkpoint_mfe_twd": mfe_net,
            "checkpoint_mae_twd": mae_net,
            "checkpoint_recovery_from_mae_twd": recovery,
            "checkpoint_flow_2of3_failed": state.flow_2of3_failed,
            "checkpoint_adverse_flow_components": state.adverse_flow_components,
        })
        return row
    baseline["variant"] = variant.name
    baseline.update({
        "loss_threshold_r": variant.loss_threshold_r,
        "maximum_recovery_from_mae_r": variant.maximum_recovery_from_mae_r,
        "require_flow_2of3": variant.require_flow_2of3,
        "checkpoint_action": "NO_CHECKPOINT_DATA",
    })
    return baseline


def _summaries(rows: list[dict[str, Any]], ids: set[str]) -> list[dict[str, Any]]:
    selected = [row for row in rows if row["validation_trade_id"] in ids]
    baseline_rows = {
        row["validation_trade_id"]: row for row in selected
        if row["variant"] == "CURRENT_BASELINE"
    }
    names = ["CURRENT_BASELINE", "EARLY_FAILURE_120S_REFERENCE"] + [
        variant.name for variant in variants()
    ]
    return [
        _cohort_summary(
            name,
            [row for row in selected if row["variant"] == name],
            baseline_rows,
        )
        for name in names
    ]


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
        states = build_causal_states(
            trade, item["data"], item.get("benchmark"),
            item["breakout_boundary_price"],
        )
        baseline = simulate_exit(
            trade, item["data"], ExitVariant("CURRENT_BASELINE"),
            item["breakout_boundary_price"],
        )
        timed = simulate_exit(
            trade,
            item["data"],
            ExitVariant(
                "EARLY_FAILURE_120S_REFERENCE",
                early_failure_seconds=120,
                maximum_progress_r=0.10,
            ),
            item["breakout_boundary_price"],
        )
        for row in (baseline, timed):
            row.update({
                "validation_trade_id": validation_trade_id,
                "source_signal_type": item["source_signal_type"],
                "gate_reason": item.get("gate_reason"),
            })
            rows.append(row)
        for variant in variants():
            row = simulate_recovery_aware(
                trade, item["data"], item["breakout_boundary_price"],
                variant, states,
            )
            row.update({
                "validation_trade_id": validation_trade_id,
                "source_signal_type": item["source_signal_type"],
                "gate_reason": item.get("gate_reason"),
            })
            rows.append(row)
    lenses = {
        group: _summaries(rows, ids) for group, ids in ids_by_group.items()
    }
    robust = [
        row for row in lenses["expanded"]
        if row["variant"].startswith("RECOVERY_AWARE_")
        and row["baseline_winners_harmed"] == 0
        and row["improvement_survives_every_leave_one_out"]
    ]
    robust.sort(
        key=lambda row: (
            -float(row["net_pnl_twd"]),
            float(row["maximum_drawdown_twd"]),
        )
    )
    report = {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "BACKTEST_ONLY_RECOVERY_AWARE_120_SECOND_CHECKPOINT",
        "capital_twd": capital_twd,
        "one_r_net_twd": ONE_R_NET_TWD,
        "checkpoint_seconds": CHECKPOINT_SECONDS,
        "maximum_mfe_r": MAXIMUM_MFE_R,
        "variants": [asdict(variant) for variant in variants()],
        "counts": {group: len(ids) for group, ids in ids_by_group.items()},
        "lenses": lenses,
        "top_robust_expanded": robust,
        "per_trade": rows,
        "limitations": [
            "The recovery thresholds are a diagnostic grid, not fitted production parameters.",
            "Additional near-miss entries overlap and are not feasible portfolio PnL.",
            "The 20260930 session is stitched and has four untimestamped callback errors.",
            "Twenty-eight unique signals remain too small for production selection.",
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
        "# Recovery-aware 120-second exit validation", "",
        "At the one-time 120-second checkpoint, a losing/no-progress trade exits only when it has not recovered sufficiently from its post-entry MAE. Any earlier existing stop remains authoritative.", "",
        "| Rank | Variant | Net PnL | vs baseline | W/L | PF | Avg loser | Max DD | Winners harmed |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(report["top_robust_expanded"][:15], start=1):
        lines.append(
            f"| {rank} | {row['variant']} | {row['net_pnl_twd']:.0f} | "
            f"{row['net_difference_vs_baseline_twd']:+.0f} | {row['wins']}/{row['losses']} | "
            f"{row['profit_factor']} | {row['average_loser_twd']:.0f} | "
            f"{row['maximum_drawdown_twd']:.0f} | {row['baseline_winners_harmed']} |"
        )
    lines.extend(["", "## Cohort comparison", ""])
    for lens, title in (
        ("base_signals", "Base signals"),
        ("additional_near_misses", "Additional near-misses"),
        ("expanded", "Expanded unique signals"),
    ):
        rows = report["lenses"][lens]
        by_name = {row["variant"]: row for row in rows}
        eligible = [
            row for row in rows
            if row["variant"].startswith("RECOVERY_AWARE_")
            and row["baseline_winners_harmed"] == 0
        ]
        best = max(eligible, key=lambda row: float(row["net_pnl_twd"]))
        baseline = by_name["CURRENT_BASELINE"]
        timed = by_name["EARLY_FAILURE_120S_REFERENCE"]
        lines.extend([
            f"### {title}", "",
            f"- Baseline: {baseline['net_pnl_twd']:.0f} TWD.",
            f"- Plain 120-second reference: {timed['net_pnl_twd']:.0f} TWD.",
            f"- Best recovery-aware result: {best['net_pnl_twd']:.0f} TWD ({best['variant']}).", "",
        ])
    best = report["top_robust_expanded"][0]
    best_name = best["variant"]
    by_id: dict[str, dict[str, dict[str, Any]]] = {}
    for row in report["per_trade"]:
        by_id.setdefault(row["validation_trade_id"], {})[row["variant"]] = row
    impacts = []
    for items in by_id.values():
        if best_name not in items or "EARLY_FAILURE_120S_REFERENCE" not in items:
            continue
        recovery = items[best_name]
        timed = items["EARLY_FAILURE_120S_REFERENCE"]
        difference = float(recovery["net_pnl_twd"]) - float(timed["net_pnl_twd"])
        if difference:
            impacts.append((recovery, timed, difference))
    timed_expanded = next(
        row for row in report["lenses"]["expanded"]
        if row["variant"] == "EARLY_FAILURE_120S_REFERENCE"
    )
    lines.extend([
        "## Difference versus plain 120 seconds", "",
        f"- Expanded net improvement: {best['net_pnl_twd'] - timed_expanded['net_pnl_twd']:+.0f} TWD.",
        f"- Changed trades: {len(impacts)}.", "",
    ])
    for recovery, timed, difference in impacts:
        lines.append(
            f"- {recovery['session_date']} {recovery['symbol']}: plain 120s "
            f"{timed['net_pnl_twd']:.0f} TWD, recovery-aware "
            f"{recovery['net_pnl_twd']:.0f} TWD ({difference:+.0f})."
        )
    lines.extend([
        "",
        "The improvement is small and includes one avoided exit that later lost more. It is suitable for shadow validation only, not production promotion.", "",
    ])
    lines.extend([
        "## Limitations", "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.extend(["", "No production or live behavior changed.", ""])
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "recovery_aware_exit_validation_20260922_20261001.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (output_dir / "report_recovery_aware_exit_validation.md").write_text(
        markdown_report(report), encoding="utf-8",
    )
    fields = [
        "source_signal_type", "gate_reason", "validation_trade_id", "variant",
        "session_date", "symbol", "entry_time", "entry_price", "quantity",
        "exit_time", "exit_price", "exit_reason", "net_pnl_twd",
        "holding_seconds", "loss_threshold_r", "maximum_recovery_from_mae_r",
        "require_flow_2of3", "checkpoint_action", "checkpoint_pnl_twd",
        "checkpoint_mfe_twd", "checkpoint_mae_twd",
        "checkpoint_recovery_from_mae_twd", "checkpoint_flow_2of3_failed",
    ]
    with (output_dir / "recovery_aware_exit_validation_per_trade.csv").open(
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
        {
            "20260922": args.run_20260922,
            "20260923": args.run_20260923,
            "20260924": args.run_20260924,
        },
        args.run_20260929, args.early_20260930, args.late_20260930,
        args.run_20261001, capital_twd=args.capital,
    )
    write_report(report, args.output_dir)
    print(json.dumps(report["top_robust_expanded"][:20], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
