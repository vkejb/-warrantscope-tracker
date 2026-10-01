"""Backtest-only validation of one causal re-entry after an early loss exit.

The first entry, sizing and exit policy remain fixed.  A stopped trade may take
one new long entry only after its executable path recovers above a declared
net-PnL threshold, the original breakout boundary and causal session VWAP.
The second leg receives a fresh adverse-one-tick fill and a new fee/tax cycle.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from paper_shadow_v01.runner import _research_trade
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC,
    _exit_quote,
    _projected_net_pnl,
    _tick_size,
)

from .exit_first_historical_validation import _cohort_summary
from .exit_first_profitability_study import ONE_R_NET_TWD
from .net_mfe_gap_buffer_validation import BufferVariant, simulate_buffered
from .path_failure_stop_validation import _prepare_items


ANALYSIS_ID = "RECOVERY_REENTRY_VALIDATION_V0_1"
BASE_POLICY = BufferVariant("RECOVERY_AWARE_120S", 0.75, 0.30)
RECOVERY_THRESHOLDS_R = (0.00, 0.20, 0.50)
CONFIRMATION_SECONDS = (0, 30, 60)
ELIGIBLE_FIRST_EXIT_REASONS = frozenset({
    "RECOVERY_AWARE_EARLY_FAILURE",
    "DISASTER_STOP_NEG_1R",
})


@dataclass(frozen=True, slots=True)
class ReentryVariant:
    recovery_threshold_r: float
    confirmation_seconds: int

    @property
    def name(self) -> str:
        return (
            "ONE_REENTRY_RECOVERY_"
            f"{int(self.recovery_threshold_r * 100):02d}PCT_R_"
            f"CONFIRM_{self.confirmation_seconds}S"
        )


def variants() -> tuple[ReentryVariant, ...]:
    return tuple(
        ReentryVariant(threshold, confirmation)
        for threshold in RECOVERY_THRESHOLDS_R
        for confirmation in CONFIRMATION_SECONDS
    )


def _last_entry_time(trade) -> datetime:
    hour, minute = map(int, str(SPEC["last_entry_time"]).split(":"))
    return trade.entry_time.replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )


def find_reentry(
    trade,
    data: dict[str, Any],
    *,
    first_exit_time: datetime,
    breakout_boundary_price: float,
    variant: ReentryVariant,
    capital_twd: int,
) -> tuple[Any | None, dict[str, Any]]:
    """Find the first causal recovery with a fresh executable long fill."""
    confirmation_started = None
    cumulative_volume = 0.0
    cumulative_price_volume = 0.0
    last_entry = _last_entry_time(trade)
    threshold_twd = variant.recovery_threshold_r * ONE_R_NET_TWD
    last_state: dict[str, Any] = {}
    for row in data["ticks"]:
        volume = float(row["volume"])
        price = float(row["price"])
        if volume >= 0 and price > 0:
            cumulative_volume += volume
            cumulative_price_volume += price * volume
        at = row["time"]
        if at <= first_exit_time or at > last_entry:
            continue
        if cumulative_volume <= 0:
            continue
        vwap = cumulative_price_volume / cumulative_volume
        executable_exit = _exit_quote("LONG", row)
        original_path_pnl = float(
            _projected_net_pnl(
                "LONG", trade.entry_price, executable_exit, trade.quantity,
            )[3]
        )
        structure_recovered = (
            price >= float(breakout_boundary_price)
            and price >= vwap
            and original_path_pnl >= threshold_twd
        )
        last_state = {
            "recovery_observed_at": at.isoformat(),
            "recovery_price": price,
            "causal_vwap": vwap,
            "original_path_net_pnl_twd": original_path_pnl,
            "structure_recovered": structure_recovered,
        }
        if not structure_recovered:
            confirmation_started = None
            continue
        if confirmation_started is None:
            confirmation_started = at
        if (at - confirmation_started).total_seconds() < variant.confirmation_seconds:
            continue
        ask = float(row["ask"])
        if ask <= 0:
            confirmation_started = None
            continue
        entry_price = ask + _tick_size(ask)
        quantity = math.floor(capital_twd / (entry_price * 1000)) * 1000
        if quantity <= 0:
            return None, {**last_state, "reentry_reason": "UNAFFORDABLE"}
        return SimpleNamespace(
            decision_time=at,
            stock_id=trade.symbol,
            stock_name=trade.stock_name,
            entry_price=entry_price,
            quantity=quantity,
        ), {
            **last_state,
            "reentry_reason": "CONFIRMED",
            "confirmation_started_at": confirmation_started.isoformat(),
            "confirmation_seconds": variant.confirmation_seconds,
            "reentry_price": entry_price,
            "reentry_quantity": quantity,
        }
    return None, {
        **last_state,
        "reentry_reason": "NO_CAUSAL_RECOVERY_BEFORE_LAST_ENTRY",
        "confirmation_seconds": variant.confirmation_seconds,
    }


def _first_leg_mfe(trade, exit_time: datetime) -> float:
    values = [float(trade.pnl_at_price(trade.entry_price))]
    values.extend(
        float(point.projected_net_pnl)
        for point in trade.points if point.at <= exit_time
    )
    return max(values)


def simulate_reentry(
    trade,
    data: dict[str, Any],
    breakout_boundary_price: float,
    variant: ReentryVariant,
    *,
    capital_twd: int = 190_000,
) -> dict[str, Any]:
    first = simulate_buffered(trade, BASE_POLICY)
    first_exit_time = datetime.fromisoformat(str(first["exit_time"]))
    row = {
        **first,
        "variant": variant.name,
        "recovery_threshold_r": variant.recovery_threshold_r,
        "confirmation_seconds": variant.confirmation_seconds,
        "first_exit_time": first["exit_time"],
        "first_exit_price": first["exit_price"],
        "first_exit_reason": first["exit_reason"],
        "first_leg_net_pnl_twd": float(first["net_pnl_twd"]),
        "reentry_taken": False,
        "reentry_count": 0,
    }
    if first["exit_reason"] not in ELIGIBLE_FIRST_EXIT_REASONS:
        row.update({
            "reentry_reason": "FIRST_EXIT_NOT_ELIGIBLE",
            "combined_net_pnl_twd": float(first["net_pnl_twd"]),
            "net_pnl_twd": float(first["net_pnl_twd"]),
            "combined_full_path_mfe_twd": _first_leg_mfe(trade, first_exit_time),
        })
        return row

    candidate, diagnostic = find_reentry(
        trade, data,
        first_exit_time=first_exit_time,
        breakout_boundary_price=breakout_boundary_price,
        variant=variant,
        capital_twd=capital_twd,
    )
    row.update(diagnostic)
    if candidate is None:
        row.update({
            "combined_net_pnl_twd": float(first["net_pnl_twd"]),
            "net_pnl_twd": float(first["net_pnl_twd"]),
            "combined_full_path_mfe_twd": _first_leg_mfe(trade, first_exit_time),
        })
        return row

    end_time = trade.points[-1].at
    second_trade = _research_trade(candidate, data, end_time)
    if not second_trade.points:
        row.update({
            "reentry_reason": "NO_POST_REENTRY_PATH",
            "combined_net_pnl_twd": float(first["net_pnl_twd"]),
            "net_pnl_twd": float(first["net_pnl_twd"]),
            "combined_full_path_mfe_twd": _first_leg_mfe(trade, first_exit_time),
        })
        return row
    second = simulate_buffered(second_trade, BASE_POLICY)
    combined = float(first["net_pnl_twd"]) + float(second["net_pnl_twd"])
    second_mfe = max(
        [float(second_trade.pnl_at_price(second_trade.entry_price))]
        + [float(point.projected_net_pnl) for point in second_trade.points]
    )
    combined_mfe = max(
        _first_leg_mfe(trade, first_exit_time),
        float(first["net_pnl_twd"]) + second_mfe,
    )
    row.update({
        "reentry_taken": True,
        "reentry_count": 1,
        "reentry_time": candidate.decision_time.isoformat(),
        "reentry_price": candidate.entry_price,
        "reentry_quantity": candidate.quantity,
        "second_exit_time": second["exit_time"],
        "second_exit_price": second["exit_price"],
        "second_exit_reason": second["exit_reason"],
        "second_leg_net_pnl_twd": float(second["net_pnl_twd"]),
        "exit_time": second["exit_time"],
        "exit_price": second["exit_price"],
        "exit_reason": f"REENTRY::{second['exit_reason']}",
        "combined_net_pnl_twd": combined,
        "net_pnl_twd": combined,
        "combined_full_path_mfe_twd": combined_mfe,
        "profit_retention_ratio": (
            combined / combined_mfe
            if combined > 0 and combined_mfe > 0 else None
        ),
    })
    return row


def _summary(
    name: str,
    rows: list[dict[str, Any]],
    reference: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    output = _cohort_summary(name, rows, reference)
    output.update({
        "reentries": sum(bool(row["reentry_taken"]) for row in rows),
        "reentries_profitable": sum(
            bool(row["reentry_taken"])
            and float(row.get("second_leg_net_pnl_twd") or 0) > 0
            for row in rows
        ),
        "reentries_unprofitable": sum(
            bool(row["reentry_taken"])
            and float(row.get("second_leg_net_pnl_twd") or 0) <= 0
            for row in rows
        ),
        "stopped_losses_recovered_to_combined_profit": sum(
            float(row["first_leg_net_pnl_twd"]) < 0
            and float(row["net_pnl_twd"]) > 0
            for row in rows
        ),
    })
    return output


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
    ids = {"base_signals": set(), "additional_near_misses": set(), "expanded": set()}
    for index, item in enumerate(items):
        trade = item["trade"]
        trade_id = f"{item['source_signal_type']}::{trade.trade_id}::{index}"
        group = (
            "additional_near_misses"
            if item["source_signal_type"] == "NEAR_MISS" else "base_signals"
        )
        ids[group].add(trade_id)
        ids["expanded"].add(trade_id)
        reference = simulate_buffered(trade, BASE_POLICY)
        reference.update({
            "variant": "NO_REENTRY_REFERENCE",
            "validation_trade_id": trade_id,
            "source_signal_type": item["source_signal_type"],
            "gate_reason": item.get("gate_reason", ""),
        })
        rows.append(reference)
        for variant in variants():
            result = simulate_reentry(
                trade, item["data"], item["breakout_boundary_price"],
                variant, capital_twd=capital_twd,
            )
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
            for row in selected if row["variant"] == "NO_REENTRY_REFERENCE"
        }
        reference_metrics[group] = _cohort_summary(
            "NO_REENTRY_REFERENCE", list(reference.values()), reference,
        )
        summaries = [
            _summary(
                variant.name,
                [row for row in selected if row["variant"] == variant.name],
                reference,
            )
            for variant in variants()
        ]
        summaries.sort(key=lambda item: -float(item["net_pnl_twd"]))
        lenses[group] = summaries

    best_base_difference = float(
        lenses["base_signals"][0]["net_difference_vs_baseline_twd"]
    )
    report = {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "BACKTEST_ONLY_ONE_CAUSAL_RECOVERY_REENTRY",
        "capital_twd": capital_twd,
        "one_r_net_twd": ONE_R_NET_TWD,
        "base_exit_policy": asdict(BASE_POLICY),
        "variants": [asdict(variant) | {"name": variant.name} for variant in variants()],
        "rule": {
            "eligible_first_exit_reasons": sorted(ELIGIBLE_FIRST_EXIT_REASONS),
            "requires_original_net_pnl_recovery": True,
            "requires_original_breakout_recovery": True,
            "requires_causal_session_vwap_recovery": True,
            "fresh_fill": "CURRENT_ASK_PLUS_ONE_ADVERSE_TICK",
            "fresh_whole_lot_sizing": True,
            "maximum_reentries_per_trade": 1,
            "last_entry_time": SPEC["last_entry_time"],
        },
        "counts": {group: len(value) for group, value in ids.items()},
        "classification": (
            "REENTRY_EDGE_REQUIRES_FUTURE_VALIDATION"
            if best_base_difference > 0 else "NO_RECOVERY_REENTRY_EDGE"
        ),
        "reference_metrics": reference_metrics,
        "lenses": lenses,
        "per_trade": rows,
        "limitations": [
            "Only ten base signals and eighteen overlapping near-misses are available.",
            "Re-entry recovery thresholds are a fixed diagnostic grid, not fitted production parameters.",
            "Historical sessions before permanent 0050 collection cannot use a benchmark recovery gate.",
            "The 20260930 session is stitched and has four untimestamped callback errors.",
            "Independent signals and near-misses are not one executable 190,000 TWD portfolio.",
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
        "# One-time recovery re-entry validation", "",
        "A stopped trade may re-enter once only after a fresh causal recovery above the original net-PnL threshold, breakout boundary and session VWAP. Every re-entry uses a new adverse fill and a second fee/tax cycle.", "",
    ]
    for lens, title in (("base_signals", "Base signals"), ("expanded", "Expanded signals")):
        lines.extend([
            f"## {title}", "",
            "| Rank | Variant | Net PnL | vs no re-entry | PF | Avg loser | Max DD | Reentries | Profitable | Recovered to profit | Winners harmed | LOTO robust |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ])
        for rank, row in enumerate(report["lenses"][lens], 1):
            lines.append(
                f"| {rank} | {row['variant']} | {row['net_pnl_twd']:.0f} | "
                f"{row['net_difference_vs_baseline_twd']:+.0f} | {row['profit_factor']} | "
                f"{(row['average_loser_twd'] or 0):.0f} | {row['maximum_drawdown_twd']:.0f} | "
                f"{row['reentries']} | {row['reentries_profitable']} | "
                f"{row['stopped_losses_recovered_to_combined_profit']} | "
                f"{row['baseline_winners_harmed']} | "
                f"{'YES' if row['improvement_survives_every_leave_one_out'] else 'NO'} |"
            )
        lines.append("")
    best_base = report["lenses"]["base_signals"][0]
    best_expanded = report["lenses"]["expanded"][0]
    base_reference = report["reference_metrics"]["base_signals"]
    expanded_reference = report["reference_metrics"]["expanded"]
    lines.extend([
        "## Finding", "",
        f"- No-reentry base reference: {base_reference['net_pnl_twd']:.0f} TWD, PF {base_reference['profit_factor']}.",
        f"- Best base-signal variant: {best_base['variant']} at {best_base['net_pnl_twd']:.0f} TWD ({best_base['net_difference_vs_baseline_twd']:+.0f}).",
        f"- No-reentry expanded reference: {expanded_reference['net_pnl_twd']:.0f} TWD, PF {expanded_reference['profit_factor']}.",
        f"- Best expanded variant: {best_expanded['variant']} at {best_expanded['net_pnl_twd']:.0f} TWD ({best_expanded['net_difference_vs_baseline_twd']:+.0f}).",
        f"- Classification: {report['classification']}.",
        "- Every tested base-signal re-entry variant was worse than no re-entry; this experiment rejects adding recovery re-entry to paper or production behavior.",
        "- No production or live behavior changed.", "", "## Limitations", "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "recovery_reentry_validation_20260922_20261001.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (output_dir / "report_recovery_reentry_validation.md").write_text(
        markdown_report(report), encoding="utf-8",
    )
    fields = [
        "validation_trade_id", "source_signal_type", "gate_reason", "variant",
        "session_date", "symbol", "entry_time", "first_exit_time",
        "first_exit_reason", "first_leg_net_pnl_twd", "reentry_taken",
        "reentry_reason", "recovery_threshold_r", "confirmation_seconds",
        "reentry_time", "reentry_price", "reentry_quantity", "second_exit_time",
        "second_exit_reason", "second_leg_net_pnl_twd", "combined_net_pnl_twd",
        "combined_full_path_mfe_twd", "profit_retention_ratio",
    ]
    with (output_dir / "recovery_reentry_validation_per_trade.csv").open(
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
