"""Backtest-only initial-stop width sensitivity on the buffered exit stack."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from yuanta_intraday_shadow_v01.collector import canonical_bytes

from .exit_first_historical_validation import _cohort_summary
from .joint_loss_profit_overlay_validation import JointVariant, simulate_joint
from .net_mfe_gap_buffer_validation import BufferVariant, simulate_buffered
from .path_failure_stop_validation import _prepare_items


ANALYSIS_ID = "INITIAL_STOP_WIDTH_VALIDATION_V0_1"
HARD_STOP_WIDTHS_R = (0.75, 0.90, 1.00, 1.10, 1.25, 1.50, 1.75, 2.00)
BUFFERS_R = (0.30, 0.40)
REFERENCE = "CURRENT_LOSS__MFE_V1"


def variant_name(stop_r: float, buffer_r: float) -> str:
    return f"RECOVERY_BUFFERED__STOP_{stop_r:.2f}R__BUFFER_{buffer_r:.2f}R"


def _summary(
    name: str, rows: list[dict[str, Any]], reference: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    output = _cohort_summary(name, rows, reference)
    output.update({
        "hard_stop_exits": sum(row["exit_reason"] == "DISASTER_STOP_NEG_1R" for row in rows),
        "mfe_exits": sum(
            row["exit_reason"] == "BUFFERED_NET_MFE_PROFIT_PROTECTION" for row in rows
        ),
        "negative_mfe_exits": sum(
            row["exit_reason"] == "BUFFERED_NET_MFE_PROFIT_PROTECTION"
            and float(row["net_pnl_twd"]) < 0 for row in rows
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
        for stop_r in HARD_STOP_WIDTHS_R:
            for buffer_r in BUFFERS_R:
                name = variant_name(stop_r, buffer_r)
                row = simulate_buffered(
                    trade,
                    BufferVariant("RECOVERY_AWARE_120S", 0.75, buffer_r),
                    hard_stop_r=stop_r,
                )
                row.update({
                    "variant": name,
                    "hard_stop_r": stop_r,
                    "initial_lock_buffer_r": buffer_r,
                    "validation_trade_id": trade_id,
                    "source_signal_type": item["source_signal_type"],
                })
                rows.append(row)

    lenses = {}
    for group, selected_ids in ids.items():
        selected = [row for row in rows if row["validation_trade_id"] in selected_ids]
        reference = {
            row["validation_trade_id"]: row
            for row in selected if row["variant"] == REFERENCE
        }
        summaries = []
        for stop_r in HARD_STOP_WIDTHS_R:
            for buffer_r in BUFFERS_R:
                name = variant_name(stop_r, buffer_r)
                row = _summary(
                    name,
                    [item for item in selected if item["variant"] == name],
                    reference,
                )
                row.update({"hard_stop_r": stop_r, "initial_lock_buffer_r": buffer_r})
                summaries.append(row)
        summaries.sort(key=lambda row: -float(row["net_pnl_twd"]))
        lenses[group] = summaries

    report = {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "BACKTEST_ONLY_INITIAL_STOP_WIDTH_SENSITIVITY",
        "capital_twd": capital_twd,
        "production_reference": REFERENCE,
        "hard_stop_widths_r": HARD_STOP_WIDTHS_R,
        "profit_buffers_r": BUFFERS_R,
        "counts": {key: len(value) for key, value in ids.items()},
        "lenses": lenses,
        "per_trade": rows,
        "limitations": [
            "Position size is intentionally fixed, so wider stops increase per-trade capital risk.",
            "A profitable cell driven by one stopped-then-recovered trade is fragile.",
            "Only ten base signals and eighteen overlapping near-misses are available.",
            "The study does not authorize a production stop change.",
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
        "# Initial-stop width sensitivity", "",
        "Fixed entries, quantities, costs, recovery-aware early failure and buffered net-MFE exits are retained. Only the disaster-stop width changes.", "",
    ]
    for lens, title in (("base_signals", "Base signals"), ("expanded", "Expanded signals")):
        lines.extend([
            f"## {title}", "",
            "| Rank | Stop | Profit buffer | Net PnL | PF | Avg winner | Avg loser | Max DD | Hard-stop exits | Winners harmed | LOTO min benefit |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for rank, row in enumerate(report["lenses"][lens], 1):
            lines.append(
                f"| {rank} | {row['hard_stop_r']:.2f}R | {row['initial_lock_buffer_r']:.2f}R | "
                f"{row['net_pnl_twd']:.0f} | {row['profit_factor']} | "
                f"{(row['average_winner_twd'] or 0):.0f} | {(row['average_loser_twd'] or 0):.0f} | "
                f"{row['maximum_drawdown_twd']:.0f} | {row['hard_stop_exits']} | "
                f"{row['baseline_winners_harmed']} | {row['leave_one_trade_out_min_difference_twd']:.0f} |"
            )
        lines.append("")
    best_base = report["lenses"]["base_signals"][0]
    best_expanded = report["lenses"]["expanded"][0]
    lines.extend([
        "## Finding", "",
        f"- Best base cell: {best_base['hard_stop_r']:.2f}R / buffer {best_base['initial_lock_buffer_r']:.2f}R = {best_base['net_pnl_twd']:.0f} TWD.",
        f"- Best expanded cell: {best_expanded['hard_stop_r']:.2f}R / buffer {best_expanded['initial_lock_buffer_r']:.2f}R = {best_expanded['net_pnl_twd']:.0f} TWD.",
        "- On base signals, every stop from 0.90R through 2.00R produced the same -2044 TWD result; widening the stop did not recover the stopped-then-rallied trade because the recovery-aware checkpoint still exited it.",
        "- Tightening to 0.75R worsened the base result to -6633 TWD. The expanded 0.90R result improved only 848 TWD versus 1.00R and did not improve the base cohort.",
        "- Keep the production 1.00R hard stop unchanged; the evidence supports the buffered profit overlay more than a stop-width change.",
        "- Wider is acceptable only if improvement survives leave-one-trade-out and does not materially worsen average loss or drawdown.", "",
        "## Limitations", "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.extend(["", "No production or live behavior changed.", ""])
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "initial_stop_width_validation_20260922_20261001.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (output_dir / "report_initial_stop_width_validation.md").write_text(
        markdown_report(report), encoding="utf-8",
    )
    fields = [
        "validation_trade_id", "source_signal_type", "variant", "session_date", "symbol",
        "entry_time", "exit_time", "exit_reason", "net_pnl_twd", "hard_stop_r",
        "initial_lock_buffer_r", "full_path_mfe_net_pnl_twd", "full_path_mae_net_pnl_twd",
        "post_exit_best_net_pnl_twd", "post_exit_worst_net_pnl_twd",
    ]
    with (output_dir / "initial_stop_width_validation_per_trade.csv").open(
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
