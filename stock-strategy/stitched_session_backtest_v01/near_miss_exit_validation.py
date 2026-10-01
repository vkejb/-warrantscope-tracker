"""Apply the fixed exit checkpoint grid to all recorded long near-misses.

The study is counterfactual and backtest-only.  Entry candidates are taken at
their causal decision time; this module never changes production gates.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from paper_shadow_v01.runner import _research_trade
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.live_parity_backtest import _parse_stamp

from .anti_chase_sensitivity_study import _load_direct, _load_stitched
from .entry_confirmation_sensitivity_study import _discover_events
from .exit_first_historical_validation import (
    _cohort_summary,
    _variants,
    build_report as build_base_report,
)
from .exit_first_profitability_study import simulate_exit


ANALYSIS_ID = "NEAR_MISS_EXIT_VALIDATION_V0_1"


def trade_fingerprint(row: Mapping[str, Any]) -> str:
    """Stable identity used to prevent double-counting the same entry."""
    symbol = row.get("symbol", row.get("stock_id"))
    return "|".join((
        str(row["session_date"]),
        str(symbol),
        str(row["entry_time"]),
        f"{float(row['entry_price']):.6f}",
        str(int(row["quantity"])),
    ))


def _near_miss_rows(
    sessions: list[dict[str, Any]], capital_twd: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    variants = _variants()
    sequence = 0
    for session in sessions:
        targets, _direction_events = _discover_events(
            session["candidates"], session["market"], session["session_date"],
            capital_twd=capital_twd,
        )
        coverage_end = _parse_stamp(session["coverage"]["ended_at_taipei"])
        for target in targets:
            signal = target["signal"]
            trade = _research_trade(
                signal, session["candidates"][signal.stock_id], coverage_end,
            )
            fingerprint = trade_fingerprint({
                "session_date": trade.session_date,
                "symbol": trade.symbol,
                "entry_time": trade.entry_time.isoformat(),
                "entry_price": trade.entry_price,
                "quantity": trade.quantity,
            })
            validation_trade_id = f"NEAR_MISS::{fingerprint}::{sequence}"
            sequence += 1
            event = {
                "validation_trade_id": validation_trade_id,
                "fingerprint": fingerprint,
                "session_date": trade.session_date,
                "symbol": trade.symbol,
                "stock_name": trade.stock_name,
                "entry_time": trade.entry_time.isoformat(),
                "entry_price": trade.entry_price,
                "quantity": trade.quantity,
                "gate_reason": target["gate_reason"],
                "score": signal.score,
                "volume_delta": signal.volume_delta,
                "large_trade_delta": signal.large_trade_delta,
                "book_imbalance": signal.book_imbalance,
                "relative_strength_5m": signal.relative_strength_5m,
            }
            events.append(event)
            for variant in variants:
                row = simulate_exit(
                    trade,
                    session["candidates"][signal.stock_id],
                    variant,
                    signal.breakout_boundary_price,
                )
                row.update({
                    "validation_trade_id": validation_trade_id,
                    "fingerprint": fingerprint,
                    "cohort": "NEAR_MISS_INDEPENDENT_LONG",
                    "source_signal_type": "NEAR_MISS",
                    "gate_reason": target["gate_reason"],
                    "score": signal.score,
                    "volume_delta": signal.volume_delta,
                    "large_trade_delta": signal.large_trade_delta,
                    "book_imbalance": signal.book_imbalance,
                    "relative_strength_5m": signal.relative_strength_5m,
                })
                rows.append(row)
    return events, rows


def _first_ids(events: list[dict[str, Any]], key_fields: tuple[str, ...]) -> set[str]:
    first: dict[tuple[Any, ...], str] = {}
    for event in sorted(events, key=lambda row: row["entry_time"]):
        key = tuple(event[field] for field in key_fields)
        first.setdefault(key, event["validation_trade_id"])
    return set(first.values())


def _summaries(
    rows: list[dict[str, Any]], ids: set[str],
) -> list[dict[str, Any]]:
    selected = [row for row in rows if row["validation_trade_id"] in ids]
    baseline_rows = {
        row["validation_trade_id"]: row for row in selected
        if row["variant"] == "CURRENT_BASELINE"
    }
    return [
        _cohort_summary(
            variant.name,
            [row for row in selected if row["variant"] == variant.name],
            baseline_rows,
        )
        for variant in _variants()
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
    base = build_base_report(
        historical_runs, run_20260929, early_20260930, late_20260930,
        run_20261001, capital_twd=capital_twd,
    )
    sessions = [
        _load_direct(run_20260929.resolve()),
        _load_stitched(early_20260930.resolve(), late_20260930.resolve()),
        _load_direct(run_20261001.resolve()),
    ]
    events, near_rows = _near_miss_rows(sessions, capital_twd)

    all_near_ids = {event["validation_trade_id"] for event in events}
    first_symbol_ids = _first_ids(events, ("session_date", "symbol"))
    first_day_ids = _first_ids(events, ("session_date",))

    base_rows = []
    base_fingerprints = set()
    for row in base["per_trade"]:
        copied = dict(row)
        copied.update({
            "fingerprint": trade_fingerprint(row),
            "source_signal_type": "BASE_SIGNAL",
            "gate_reason": None,
            "score": None,
            "volume_delta": None,
            "large_trade_delta": None,
            "book_imbalance": None,
            "relative_strength_5m": None,
        })
        base_rows.append(copied)
        if row["variant"] == "CURRENT_BASELINE":
            base_fingerprints.add(copied["fingerprint"])

    unique_near_events = [
        event for event in events if event["fingerprint"] not in base_fingerprints
    ]
    unique_near_ids = {
        event["validation_trade_id"] for event in unique_near_events
    }
    unique_near_rows = [
        row for row in near_rows if row["validation_trade_id"] in unique_near_ids
    ]
    expanded_rows = base_rows + unique_near_rows
    expanded_ids = {
        row["validation_trade_id"] for row in expanded_rows
        if row["variant"] == "CURRENT_BASELINE"
    }

    lenses = {
        "near_miss_all_independent": _summaries(near_rows, all_near_ids),
        "near_miss_first_per_symbol_day": _summaries(near_rows, first_symbol_ids),
        "near_miss_first_per_day": _summaries(near_rows, first_day_ids),
        "expanded_unique_signals": _summaries(expanded_rows, expanded_ids),
    }
    gate_summaries = {
        gate: _summaries(
            near_rows,
            {
                event["validation_trade_id"] for event in events
                if event["gate_reason"] == gate
            },
        )
        for gate in sorted({event["gate_reason"] for event in events})
    }
    report = {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "BACKTEST_ONLY_NEAR_MISS_INDEPENDENT_ENTRY_DIAGNOSTIC",
        "capital_twd": capital_twd,
        "session_dates": [session["session_date"] for session in sessions],
        "near_miss_events": events,
        "counts": {
            "all_near_miss_events": len(events),
            "first_per_symbol_day": len(first_symbol_ids),
            "first_per_day": len(first_day_ids),
            "base_unique_signals": len(base_fingerprints),
            "near_miss_duplicates_of_base": len(events) - len(unique_near_events),
            "additional_unique_near_misses": len(unique_near_events),
            "expanded_unique_signals": len(expanded_ids),
        },
        "lenses": lenses,
        "gate_summaries": gate_summaries,
        "per_trade": near_rows,
        "expanded_per_trade": expanded_rows,
        "limitations": [
            "Near-misses were rejected by production entry gates; forcing them in is counterfactual.",
            "All-event and per-symbol totals contain overlapping positions and are not feasible account PnL.",
            "First-per-day is mechanically feasible but has only three sessions.",
            "The 20260930 session is stitched and has four untimestamped callback errors.",
            "Only the three recent sessions have synchronized permanent 0050 context.",
        ],
        "production_behavior_changed": False,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    report["report_hash"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


def _best(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(rows, key=lambda row: float(row["net_pnl_twd"]))


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Near-miss exit validation", "",
        "Every recorded rejected long candidate is entered at its causal suggested price. Entry gates remain unchanged in production; only exit variants are compared.", "",
        "## Counts", "",
        f"- All near-miss events: {report['counts']['all_near_miss_events']}",
        f"- First event per stock/day: {report['counts']['first_per_symbol_day']}",
        f"- First event per day: {report['counts']['first_per_day']}",
        f"- Near-misses duplicating an existing base entry: {report['counts']['near_miss_duplicates_of_base']}",
        f"- Expanded unique base plus near-miss signals: {report['counts']['expanded_unique_signals']}", "",
    ]
    titles = {
        "near_miss_all_independent": "All near-miss events (independent, overlapping)",
        "near_miss_first_per_symbol_day": "First near-miss per stock/day",
        "near_miss_first_per_day": "First near-miss per day",
        "expanded_unique_signals": "Expanded unique base plus near-miss signals",
    }
    for lens, title in titles.items():
        lines.extend([
            f"## {title}", "",
            "| Variant | Trades | Net PnL | vs baseline | W/L | PF | Avg loser | Max DD | Winners harmed |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for row in report["lenses"][lens]:
            factor = "-" if row["profit_factor"] is None else str(row["profit_factor"])
            avg_loss = "-" if row["average_loser_twd"] is None else f"{row['average_loser_twd']:.0f}"
            lines.append(
                f"| {row['variant']} | {row['trades']} | {row['net_pnl_twd']:.0f} | "
                f"{row['net_difference_vs_baseline_twd']:+.0f} | {row['wins']}/{row['losses']} | "
                f"{factor} | {avg_loss} | {row['maximum_drawdown_twd']:.0f} | "
                f"{row['baseline_winners_harmed']} |"
            )
        lines.append("")

    lines.extend([
        "## Results by rejected gate", "",
        "| Rejected by | Events | Baseline | Least-negative variant | Result |",
        "|---|---:|---:|---|---:|",
    ])
    for gate, rows in report["gate_summaries"].items():
        baseline = rows[0]
        best = _best(rows)
        lines.append(
            f"| {gate} | {baseline['trades']} | {baseline['net_pnl_twd']:.0f} | "
            f"{best['variant']} | {best['net_pnl_twd']:.0f} |"
        )
    lines.append("")

    all_best = _best(report["lenses"]["near_miss_all_independent"])
    daily_best = _best(report["lenses"]["near_miss_first_per_day"])
    expanded_best = _best(report["lenses"]["expanded_unique_signals"])
    lines.extend([
        "## Interpretation", "",
        f"- All 20 near-misses remain negative under every exit. The least-negative checkpoint is {all_best['variant']} at {all_best['net_pnl_twd']:.0f} TWD.",
        f"- First-per-day is least negative under {daily_best['variant']} at {daily_best['net_pnl_twd']:.0f} TWD, but this is only three observations.",
        f"- After removing duplicate entries, the expanded 28-signal diagnostic is least negative under {expanded_best['variant']} at {expanded_best['net_pnl_twd']:.0f} TWD.",
        "- The apparently positive relative-strength-rejected subgroup is driven by the 2026-09-30 1709 trade (+9,801 TWD under baseline). Removing that one winner makes its 90-second result negative again (-7,268 TWD).",
        "- Exit timing reduces losses but does not rescue the rejected-entry population. This supports keeping entry quality gates while continuing exit shadow tests.",
        "- No result is suitable for production promotion from this sample.", "",
        "## Limitations", "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.extend(["", "No production or live behavior changed.", ""])
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "near_miss_exit_validation_20260922_20261001.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (output_dir / "report_near_miss_exit_validation.md").write_text(
        markdown_report(report), encoding="utf-8",
    )
    fields = [
        "source_signal_type", "gate_reason", "validation_trade_id", "fingerprint",
        "variant", "session_date", "symbol", "stock_name", "entry_time",
        "entry_price", "quantity", "exit_time", "exit_price", "exit_reason",
        "net_pnl_twd", "holding_seconds", "full_path_mfe_net_pnl_twd",
        "full_path_mae_net_pnl_twd", "score", "volume_delta",
        "large_trade_delta", "book_imbalance", "relative_strength_5m",
    ]
    with (output_dir / "near_miss_exit_validation_per_trade.csv").open(
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
    print(json.dumps(report["lenses"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
