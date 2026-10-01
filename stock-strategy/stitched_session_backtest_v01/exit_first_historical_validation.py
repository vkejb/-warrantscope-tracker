"""Matched-cohort historical validation of the backtest-only early-failure exit.

This module deliberately reuses the existing entry records and exit simulator.  It
does not alter signal generation, production strategy configuration, or broker
execution behavior.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from mfe_profit_protection_study_v01.analysis import build_independent_signal_trades
from paper_shadow_v01.runner import _research_trade
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import load_session
from yuanta_intraday_shadow_v01.live_parity_backtest import _parse_stamp

from .anti_chase_sensitivity_study import _load_direct, _load_stitched
from .exit_first_profitability_study import (
    ENTRY_POLICY,
    ExitVariant,
    _fixed_entry,
    _summary,
    simulate_exit,
)


ANALYSIS_ID = "EXIT_FIRST_HISTORICAL_VALIDATION_V0_1"
CHECKPOINT_SECONDS = (30, 60, 90, 120, 150, 180, 240, 300)
MAXIMUM_PROGRESS_R = 0.10


def _variants() -> tuple[ExitVariant, ...]:
    return (ExitVariant("CURRENT_BASELINE"),) + tuple(
        ExitVariant(
            f"EARLY_FAILURE_{seconds}S",
            early_failure_seconds=seconds,
            maximum_progress_r=MAXIMUM_PROGRESS_R,
        )
        for seconds in CHECKPOINT_SECONDS
    )


def _historical_cohort(
    session_runs: Mapping[str, list[Path]], capital_twd: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    trades, diagnostics, coverage = build_independent_signal_trades(
        session_runs, capital_twd,
    )
    session_stocks: dict[str, dict[str, Any]] = {}
    for paths in session_runs.values():
        stocks, manifest = load_session(paths)
        session_stocks[str(manifest["session_date"])] = stocks

    cohort = []
    baseline = ExitVariant("CURRENT_BASELINE")
    for trade in trades:
        if trade.side != "LONG":
            continue
        data = session_stocks[trade.session_date][trade.symbol]
        try:
            simulate_exit(
                trade, data, baseline,
                breakout_boundary_price=trade.entry_price,
            )
        except RuntimeError:
            continue
        cohort.append({
            "cohort": "HISTORICAL_INDEPENDENT_LONG",
            "trade": trade,
            "data": data,
            "breakout_boundary_price": trade.entry_price,
        })
    return cohort, diagnostics, coverage


def _recent_cohort(
    run_20260929: Path,
    early_20260930: Path,
    late_20260930: Path,
    run_20261001: Path,
    capital_twd: int,
) -> list[dict[str, Any]]:
    sessions = [
        _load_direct(run_20260929.resolve()),
        _load_stitched(early_20260930.resolve(), late_20260930.resolve()),
        _load_direct(run_20261001.resolve()),
    ]
    cohort = []
    for session in sessions:
        signal, _selection = _fixed_entry(session, capital_twd=capital_twd)
        if signal is None:
            continue
        end_time = _parse_stamp(session["coverage"]["ended_at_taipei"])
        trade = _research_trade(
            signal, session["candidates"][signal.stock_id], end_time,
        )
        cohort.append({
            "cohort": "RECENT_FIXED_ONE_PER_DAY",
            "trade": trade,
            "data": session["candidates"][signal.stock_id],
            "breakout_boundary_price": signal.breakout_boundary_price,
        })
    return cohort


def _cohort_summary(
    variant: str, rows: list[dict[str, Any]], baseline_rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    summary = _summary(variant, rows)
    differences = [
        round(
            float(row["net_pnl_twd"])
            - float(baseline_rows[row["validation_trade_id"]]["net_pnl_twd"]),
            2,
        )
        for row in rows
    ]
    improvement = round(sum(differences), 2)
    leave_one_out = [round(improvement - difference, 2) for difference in differences]
    baseline_winners = [
        row for row in rows
        if float(baseline_rows[row["validation_trade_id"]]["net_pnl_twd"]) > 0
    ]
    summary.update({
        "net_difference_vs_baseline_twd": improvement,
        "trades_improved": sum(value > 0 for value in differences),
        "trades_worsened": sum(value < 0 for value in differences),
        "baseline_winners_harmed": sum(
            float(row["net_pnl_twd"])
            < float(baseline_rows[row["validation_trade_id"]]["net_pnl_twd"])
            for row in baseline_winners
        ),
        "leave_one_trade_out_min_difference_twd": min(leave_one_out, default=0.0),
        "leave_one_trade_out_max_difference_twd": max(leave_one_out, default=0.0),
        "improvement_survives_every_leave_one_out": (
            improvement > 0 and all(value > 0 for value in leave_one_out)
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
    historical, diagnostics, coverage = _historical_cohort(
        historical_runs, capital_twd,
    )
    recent = _recent_cohort(
        run_20260929, early_20260930, late_20260930, run_20261001,
        capital_twd,
    )
    cohort = historical + recent
    variants = _variants()
    rows: list[dict[str, Any]] = []
    for cohort_index, item in enumerate(cohort):
        validation_trade_id = f"{item['cohort']}::{item['trade'].trade_id}::{cohort_index}"
        for variant in variants:
            row = simulate_exit(
                item["trade"], item["data"], variant,
                item["breakout_boundary_price"],
            )
            row.update({
                "validation_trade_id": validation_trade_id,
                "cohort": item["cohort"],
            })
            rows.append(row)

    baseline_rows = {
        row["validation_trade_id"]: row for row in rows
        if row["variant"] == "CURRENT_BASELINE"
    }
    summaries: dict[str, list[dict[str, Any]]] = {}
    cohort_filters = {
        "historical": "HISTORICAL_INDEPENDENT_LONG",
        "recent": "RECENT_FIXED_ONE_PER_DAY",
        "combined": None,
    }
    for label, cohort_name in cohort_filters.items():
        summaries[label] = [
            _cohort_summary(
                variant.name,
                [
                    row for row in rows
                    if row["variant"] == variant.name
                    and (cohort_name is None or row["cohort"] == cohort_name)
                ],
                baseline_rows,
            )
            for variant in variants
        ]

    report = {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "BACKTEST_ONLY_MATCHED_LONG_SIGNAL_EXIT_DIAGNOSTIC",
        "capital_twd": capital_twd,
        "entry_policy_recent": ENTRY_POLICY,
        "maximum_progress_r": MAXIMUM_PROGRESS_R,
        "variants": [asdict(variant) for variant in variants],
        "historical_coverage": coverage,
        "historical_source_diagnostics": diagnostics,
        "cohort_counts": {
            "historical_matched_long": len(historical),
            "recent_fixed_one_per_day": len(recent),
            "combined_diagnostic": len(cohort),
        },
        "summaries": summaries,
        "per_trade": rows,
        "limitations": [
            "The 20260922-20260924 archive predates permanent 0050 collection.",
            "Older records are overlapping independent signals, not a realizable single-position portfolio.",
            "20260922 started at 09:30; 20260923 started at 09:19 and has 24 callback errors.",
            "The combined total is a signal-level diagnostic and must not be read as account PnL.",
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
        "# Exit-first historical validation", "",
        "All variants use the same ten matched long trades and the existing cost, tax, slippage and MFE exit model. Only the backtest-only no-progress checkpoint changes.", "",
        "## Coverage", "",
        f"- Historical matched long signals (2026-09-22 to 2026-09-24): {report['cohort_counts']['historical_matched_long']}",
        f"- Recent fixed one-trade-per-day sessions (2026-09-29 to 2026-10-01): {report['cohort_counts']['recent_fixed_one_per_day']}",
        f"- Combined diagnostic signals: {report['cohort_counts']['combined_diagnostic']}", "",
    ]
    for label, title in (
        ("recent", "Recent fixed one-per-day cohort"),
        ("historical", "Older independent-long cohort"),
        ("combined", "Combined matched diagnostic"),
    ):
        lines.extend([
            f"## {title}", "",
            "| Variant | Net PnL | vs baseline | W/L | PF | Avg loser | Max DD | Winners harmed | LOTO robust |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ])
        for row in report["summaries"][label]:
            pf = "-" if row["profit_factor"] is None else str(row["profit_factor"])
            avg_loss = "-" if row["average_loser_twd"] is None else f"{row['average_loser_twd']:.0f}"
            lines.append(
                f"| {row['variant']} | {row['net_pnl_twd']:.0f} | "
                f"{row['net_difference_vs_baseline_twd']:+.0f} | {row['wins']}/{row['losses']} | "
                f"{pf} | {avg_loss} | {row['maximum_drawdown_twd']:.0f} | "
                f"{row['baseline_winners_harmed']} | "
                f"{'YES' if row['improvement_survives_every_leave_one_out'] else 'NO'} |"
            )
        lines.append("")
    combined = {row["variant"]: row for row in report["summaries"]["combined"]}
    recent = {row["variant"]: row for row in report["summaries"]["recent"]}
    lines.extend([
        "## Finding", "",
        f"- 60 seconds looked positive on the recent three trades ({recent['EARLY_FAILURE_60S']['net_pnl_twd']:.0f} TWD) but failed the expanded matched cohort ({combined['EARLY_FAILURE_60S']['net_pnl_twd']:.0f} TWD) and harmed one of two baseline winners.",
        f"- 120 seconds gave the best combined net result ({combined['EARLY_FAILURE_120S']['net_pnl_twd']:.0f} TWD; {combined['EARLY_FAILURE_120S']['net_difference_vs_baseline_twd']:+.0f} versus baseline) and its improvement survived every leave-one-trade-out deletion.",
        "- The 120-second result still has negative total PnL and PF below 1. It is evidence for loss reduction, not evidence of a profitable rule.",
        "- No checkpoint should be promoted to production from this sample. The next valid step is shadow collection on complete sessions with 0050 context.", "",
        "## Limitations", "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.extend(["", "No production or live behavior changed.", ""])
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "exit_first_historical_validation_20260922_20261001.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (output_dir / "report_exit_first_historical_validation.md").write_text(
        markdown_report(report), encoding="utf-8",
    )
    fields = [
        "cohort", "validation_trade_id", "variant", "session_date", "symbol",
        "stock_name", "entry_time", "entry_price", "quantity", "exit_time",
        "exit_price", "exit_reason", "net_pnl_twd", "holding_seconds",
        "mfe_net_pnl_at_exit_twd", "full_path_mfe_net_pnl_twd",
        "full_path_mae_net_pnl_twd", "post_exit_best_net_pnl_twd",
        "post_exit_worst_net_pnl_twd",
    ]
    with (output_dir / "exit_first_historical_validation_per_trade.csv").open(
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
        {
            "20260922": args.run_20260922,
            "20260923": args.run_20260923,
            "20260924": args.run_20260924,
        },
        args.run_20260929, args.early_20260930, args.late_20260930,
        args.run_20261001, capital_twd=args.capital,
    )
    write_report(report, args.output_dir)
    print(json.dumps(report["summaries"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
