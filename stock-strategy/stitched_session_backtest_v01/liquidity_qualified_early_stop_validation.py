"""Backtest-only liquidity qualification for the recovery-aware early stop.

The existing 120-second failure checkpoint is unchanged except that an exit
requires enough fresh trade activity in the causal 30-second window.  A thin
window is treated as insufficient evidence, not as recovery.  Hard stop, net
MFE protection, reversal and EOD exits remain authoritative.
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
from yuanta_intraday_shadow_v01.flow_exit_sweep import _window

from .exit_first_historical_validation import _cohort_summary
from .exit_first_profitability_study import ONE_R_NET_TWD, ExitVariant, _result
from .net_mfe_gap_buffer_validation import (
    BufferVariant,
    candidate_floor_r,
    simulate_buffered,
)
from .path_failure_stop_validation import _prepare_items


ANALYSIS_ID = "LIQUIDITY_QUALIFIED_EARLY_STOP_VALIDATION_V0_1"
REFERENCE_NAME = "IMMEDIATE_RECOVERY_AWARE_120S"
BASE_POLICY = BufferVariant("RECOVERY_AWARE_120S", 0.75, 0.30)
CHECKPOINT_SECONDS = 120
MINIMUM_TICK_COUNTS_30S = (1, 3, 5)
MINIMUM_VOLUME_RATIOS = (0.10, 0.25, 0.50)
MAXIMUM_PROGRESS_R = 0.10
LOSS_THRESHOLD_R = 0.20
MAXIMUM_RECOVERY_FROM_MAE_R = 0.20


@dataclass(frozen=True, slots=True)
class LiquidityVariant:
    minimum_tick_count_30s: int
    minimum_volume_ratio: float

    @property
    def name(self) -> str:
        return (
            f"LIQUIDITY_QUALIFIED_TICKS_{self.minimum_tick_count_30s}"
            f"__VOLUME_RATIO_{self.minimum_volume_ratio:.2f}"
        )


def variants() -> tuple[LiquidityVariant, ...]:
    return tuple(
        LiquidityVariant(ticks, ratio)
        for ticks in MINIMUM_TICK_COUNTS_30S
        for ratio in MINIMUM_VOLUME_RATIOS
    )


def causal_liquidity(data: dict[str, Any], at) -> dict[str, float | int | None]:
    current = _window(data, at - timedelta(seconds=30), at)
    previous = _window(
        data, at - timedelta(seconds=60),
        at - timedelta(seconds=30, microseconds=1),
    )
    current_volume = sum(float(row["volume"]) for row in current)
    previous_volume = sum(float(row["volume"]) for row in previous)
    return {
        "tick_count_30s": len(current),
        "tick_count_previous_30s": len(previous),
        "volume_30s": current_volume,
        "volume_previous_30s": previous_volume,
        "volume_ratio_30s_vs_previous": (
            current_volume / previous_volume if previous_volume > 0 else None
        ),
    }


def liquidity_passes(
    state: Mapping[str, float | int | None], variant: LiquidityVariant,
) -> bool:
    ratio = state["volume_ratio_30s_vs_previous"]
    return (
        int(state["tick_count_30s"] or 0) >= variant.minimum_tick_count_30s
        and ratio is not None
        and float(ratio) >= variant.minimum_volume_ratio
    )


def simulate_liquidity_qualified(
    trade, data: dict[str, Any], variant: LiquidityVariant,
    *, hard_stop_r: float = 1.0,
) -> dict[str, Any]:
    if hard_stop_r <= 0:
        raise ValueError("hard_stop_r must be positive")
    checkpoint_at = trade.entry_time + timedelta(seconds=CHECKPOINT_SECONDS)
    checkpoint_evaluated = False
    checkpoint_action = "NOT_APPLICABLE"
    checkpoint_liquidity: dict[str, float | int | None] = {}
    mfe_net = mae_net = float(trade.pnl_at_price(trade.entry_price))
    locked_r: float | None = None
    activation_time = None
    hard_hour, hard_minute = map(int, str(SPEC["hard_exit_time"]).split(":"))
    hard_exit = trade.entry_time.replace(
        hour=hard_hour, minute=hard_minute, second=0, microsecond=0,
    )

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
        elif not checkpoint_evaluated and point.at >= checkpoint_at:
            checkpoint_evaluated = True
            recovery = pnl - mae_net
            triggered = (
                pnl <= -LOSS_THRESHOLD_R * ONE_R_NET_TWD
                and mfe_net <= MAXIMUM_PROGRESS_R * ONE_R_NET_TWD
                and recovery <= MAXIMUM_RECOVERY_FROM_MAE_R * ONE_R_NET_TWD
            )
            if triggered:
                checkpoint_liquidity = causal_liquidity(data, point.at)
                if liquidity_passes(checkpoint_liquidity, variant):
                    reason = "LIQUIDITY_QUALIFIED_EARLY_FAILURE"
                    checkpoint_action = "EXIT_SUFFICIENT_ACTIVITY"
                else:
                    checkpoint_action = "HOLD_INSUFFICIENT_ACTIVITY"
            else:
                checkpoint_action = "HOLD_NO_FAILURE"

        if reason is None and point.reversal:
            reason = "SIGNAL_REVERSAL"
        elif reason is None and point.at >= hard_exit:
            reason = "HARD_EXIT"
        elif (
            reason is None and trade.force_last_point_exit
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
            "minimum_tick_count_30s": variant.minimum_tick_count_30s,
            "minimum_volume_ratio": variant.minimum_volume_ratio,
            "checkpoint_action": checkpoint_action,
            **checkpoint_liquidity,
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
        "qualified_exits": sum(
            row["exit_reason"] == "LIQUIDITY_QUALIFIED_EARLY_FAILURE"
            for row in rows
        ),
        "insufficient_activity_holds": sum(
            row["checkpoint_action"] == "HOLD_INSUFFICIENT_ACTIVITY"
            for row in rows
        ),
        "holds_later_profitable": sum(
            row["checkpoint_action"] == "HOLD_INSUFFICIENT_ACTIVITY"
            and float(row["net_pnl_twd"]) > 0 for row in rows
        ),
        "holds_later_losses": sum(
            row["checkpoint_action"] == "HOLD_INSUFFICIENT_ACTIVITY"
            and float(row["net_pnl_twd"]) <= 0 for row in rows
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
            result = simulate_liquidity_qualified(trade, item["data"], variant)
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
            row["validation_trade_id"]: row for row in selected
            if row["variant"] == REFERENCE_NAME
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

    best_base = lenses["base_signals"][0]
    best_expanded = next(
        row for row in lenses["expanded"] if row["variant"] == best_base["variant"]
    )
    report = {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "POST_HOC_HYPOTHESIS_BACKTEST_ONLY_LIQUIDITY_QUALIFIED_EARLY_STOP",
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
            "liquidity_window_seconds": 30,
            "hard_stop_stays_immediate": True,
            "mfe_exit_stays_immediate": True,
        },
        "counts": {group: len(value) for group, value in ids.items()},
        "classification": (
            "LIQUIDITY_HYPOTHESIS_REQUIRES_FUTURE_SHADOW_VALIDATION"
            if float(best_base["net_difference_vs_baseline_twd"]) > 0
            and bool(best_base["improvement_survives_every_leave_one_out"])
            and int(best_base["baseline_winners_harmed"]) == 0
            and float(best_expanded["net_difference_vs_baseline_twd"]) > 0
            and bool(best_expanded["improvement_survives_every_leave_one_out"])
            else "NO_ROBUST_LIQUIDITY_QUALIFICATION_EDGE"
        ),
        "reference_metrics": reference_metrics,
        "lenses": lenses,
        "per_trade": rows,
        "limitations": [
            "This hypothesis was motivated by inspecting the same sample and is explicitly post hoc.",
            "Only ten base signals and eighteen overlapping near-misses are available.",
            "Trade-count and volume-ratio thresholds need untouched future sessions before any promotion.",
            "Older independent signals and near-misses are not one executable portfolio.",
            "The 20260930 session is stitched and has four untimestamped callback errors.",
        ],
        "production_behavior_changed": False,
        "paper_behavior_changed": True,
        "paper_behavior_change": "FUTURE_POST_SESSION_SHADOW_VARIANT_ONLY",
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    report["report_hash"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Liquidity-qualified early-stop validation", "",
        "Post-hoc hypothesis: the unchanged 120-second failure stop may exit only when the latest 30 seconds contain enough fresh trades and volume relative to the preceding 30 seconds. Thin activity means insufficient evidence. Hard stop and MFE protection remain active.", "",
    ]
    for lens, title in (("base_signals", "Base signals"), ("expanded", "Expanded signals")):
        lines.extend([
            f"## {title}", "",
            "| Rank | Min ticks | Min volume ratio | Net PnL | vs immediate | PF | Avg loser | Max DD | Qualified exits | Thin holds | Holds profitable | Holds losses | Winners harmed | LOTO robust |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ])
        for rank, row in enumerate(report["lenses"][lens], 1):
            variant = next(item for item in report["variants"] if item["name"] == row["variant"])
            lines.append(
                f"| {rank} | {variant['minimum_tick_count_30s']} | "
                f"{variant['minimum_volume_ratio']:.2f} | {row['net_pnl_twd']:.0f} | "
                f"{row['net_difference_vs_baseline_twd']:+.0f} | {row['profit_factor']} | "
                f"{(row['average_loser_twd'] or 0):.0f} | {row['maximum_drawdown_twd']:.0f} | "
                f"{row['qualified_exits']} | {row['insufficient_activity_holds']} | "
                f"{row['holds_later_profitable']} | {row['holds_later_losses']} | "
                f"{row['baseline_winners_harmed']} | "
                f"{'YES' if row['improvement_survives_every_leave_one_out'] else 'NO'} |"
            )
        lines.append("")
    best = report["lenses"]["base_signals"][0]
    reference = report["reference_metrics"]["base_signals"]
    expanded = next(row for row in report["lenses"]["expanded"] if row["variant"] == best["variant"])
    lines.extend([
        "## Finding", "",
        f"- Immediate reference: {reference['net_pnl_twd']:.0f} TWD, PF {reference['profit_factor']}, max drawdown {reference['maximum_drawdown_twd']:.0f} TWD.",
        f"- Best in-sample base variant: {best['variant']} at {best['net_pnl_twd']:.0f} TWD ({best['net_difference_vs_baseline_twd']:+.0f}).",
        f"- The same variant on expanded signals: {expanded['net_pnl_twd']:.0f} TWD ({expanded['net_difference_vs_baseline_twd']:+.0f}).",
        f"- Classification: {report['classification']}.",
        "- Because the idea came from inspecting 2221, even a strong result is not production evidence; only untouched future shadow sessions can validate it.",
        "- No production or live behavior changed. The least-aggressive cell is added only as a future post-session paper-shadow challenger; it cannot place orders.", "",
        "## Limitations", "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "liquidity_qualified_early_stop_20260922_20261001.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (output_dir / "report_liquidity_qualified_early_stop.md").write_text(
        markdown_report(report), encoding="utf-8",
    )
    fields = [
        "validation_trade_id", "source_signal_type", "gate_reason", "variant",
        "session_date", "symbol", "entry_time", "exit_time", "exit_price",
        "exit_reason", "net_pnl_twd", "full_path_mfe_net_pnl_twd",
        "full_path_mae_net_pnl_twd", "minimum_tick_count_30s",
        "minimum_volume_ratio", "checkpoint_action", "tick_count_30s",
        "tick_count_previous_30s", "volume_30s", "volume_previous_30s",
        "volume_ratio_30s_vs_previous", "max_locked_profit_r",
        "profit_retention_ratio",
    ]
    with (output_dir / "liquidity_qualified_early_stop_per_trade.csv").open(
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
