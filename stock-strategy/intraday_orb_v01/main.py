#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, fields
from datetime import date
import csv
import json
from pathlib import Path

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from intraday_orb_v01.analysis import forward_returns, summarize_forward
    from intraday_orb_v01.config import CFG
    from intraday_orb_v01.data import (
        load_daily,
        load_index_daily,
        load_index_minutes,
        load_minutes,
        load_odd_quotes,
        load_trading_calendar,
    )
    from intraday_orb_v01.report import build_report
    from intraday_orb_v01.models import SignalEvaluation
    from intraday_orb_v01.shadow import replay_shadow_quotes
    from intraday_orb_v01.signals import generate_signals
    from intraday_orb_v01.universe import build_universes
    from intraday_orb_v01.validation import validate_results
else:
    from .analysis import forward_returns, summarize_forward
    from .config import CFG
    from .data import (
        load_daily,
        load_index_daily,
        load_index_minutes,
        load_minutes,
        load_odd_quotes,
        load_trading_calendar,
    )
    from .report import build_report
    from .models import SignalEvaluation
    from .shadow import replay_shadow_quotes
    from .signals import generate_signals
    from .universe import build_universes
    from .validation import validate_results


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _universe_rows(rows):
    result = []
    for item in rows:
        row = asdict(item)
        row["trade_date"] = item.trade_date.isoformat()
        result.append(row)
    return result


def _assert_fresh_output(path: Path) -> None:
    if path.exists():
        raise FileExistsError(
            f"output directory already exists; use a new run directory: {path}"
        )


def _initialize_output(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False)
    config_payload = {"config": CFG.snapshot(), "config_hash": CFG.fingerprint()}
    (path / "config_snapshot.json").write_text(
        json.dumps(config_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_complete_manifest(path: Path, mode: str, artifacts: list[str]) -> None:
    payload = {
        "status": "COMPLETE",
        "mode": mode,
        "strategy_id": CFG.strategy_id,
        "config_hash": CFG.fingerprint(),
        "artifacts": artifacts,
    }
    (path / "run_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _base_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Research-only INTRADAY_ORB_V0_1; contains no live-order mode"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="build T-1 Top-30 and minute-data needs")
    prepare.add_argument("--daily", type=Path, nargs="+", required=True)
    prepare.add_argument("--index-daily", type=Path, nargs="+", required=True)
    prepare.add_argument("--trading-calendar", type=Path, nargs="+", required=True)
    prepare.add_argument("--start-date")
    prepare.add_argument("--end-date")
    prepare.add_argument("--output-dir", type=Path, required=True)

    research = subparsers.add_parser(
        "research", help="replay entry signals and optional odd-lot shadow quotes"
    )
    research.add_argument("--daily", type=Path, nargs="+", required=True)
    research.add_argument("--index-daily", type=Path, nargs="+", required=True)
    research.add_argument("--trading-calendar", type=Path, nargs="+", required=True)
    research.add_argument("--minutes", type=Path, nargs="+", required=True)
    research.add_argument("--index-minutes", type=Path, nargs="+", required=True)
    research.add_argument("--odd-quotes", type=Path, nargs="+")
    research.add_argument("--start-date")
    research.add_argument("--end-date")
    research.add_argument("--bootstrap-iterations", type=int, default=2_000)
    research.add_argument("--output-dir", type=Path, required=True)
    return parser


def main():
    args = _base_parser().parse_args()
    if args.command == "research" and args.bootstrap_iterations <= 0:
        raise ValueError("bootstrap_iterations must be positive")
    _assert_fresh_output(args.output_dir)
    daily, daily_audit = load_daily(args.daily)
    index_daily, index_daily_audit = load_index_daily(args.index_daily)
    trading_calendar, calendar_audit = load_trading_calendar(args.trading_calendar)
    universe, universe_audit = build_universes(
        daily,
        index_daily,
        trading_calendar,
        start_date=_parse_date(args.start_date),
        end_date=_parse_date(args.end_date),
    )
    if args.command == "prepare":
        _initialize_output(args.output_dir)
        rows = _universe_rows(universe)
        needs = [
            {
                "trade_date": row.trade_date.isoformat(),
                "market": row.market,
                "symbol": row.symbol,
                "universe_rank": row.universe_rank,
                "requires_prior_complete_minute_sessions": CFG.rvol_history_sessions,
            }
            for row in universe
        ]
        audit = {
            "mode": "prepare",
            "daily": daily_audit,
            "index_daily": index_daily_audit,
            "trading_calendar": calendar_audit,
            "universe": universe_audit,
            "limitations": [
                "No intraday result is produced by prepare mode.",
                "Daily turnover must be actual reported turnover, not close multiplied by volume.",
                "Each target needs 20 complete prior regular-market minute sessions for RVOL.",
            ],
        }
        _write_csv(args.output_dir / "universe.csv", rows)
        _write_csv(args.output_dir / "minute_needs.csv", needs)
        (args.output_dir / "data_audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _write_complete_manifest(
            args.output_dir,
            "prepare",
            [
                "config_snapshot.json",
                "universe.csv",
                "minute_needs.csv",
                "data_audit.json",
            ],
        )
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        return

    _initialize_output(args.output_dir)
    minutes, minute_audit = load_minutes(args.minutes)
    index_minutes, index_minute_audit = load_index_minutes(args.index_minutes)
    evaluation_fields = [field.name for field in fields(SignalEvaluation)]
    with (args.output_dir / "signal_evaluations.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as evaluation_handle:
        evaluation_writer = csv.DictWriter(
            evaluation_handle,
            fieldnames=evaluation_fields,
            lineterminator="\n",
        )
        evaluation_writer.writeheader()
        evaluations, raw_signals, selected_signals, signal_audit = generate_signals(
            universe,
            minutes,
            index_minutes,
            trading_calendar,
            evaluation_sink=lambda row: evaluation_writer.writerow(row.to_row()),
            retain_failed_evaluations=False,
        )
    forward = forward_returns(selected_signals, minutes)
    summary, bootstrap = summarize_forward(
        forward, bootstrap_iterations=args.bootstrap_iterations
    )
    quotes = []
    quote_audit = {"status": "not supplied", "rows": 0}
    shadow_checks = []
    shadow_outcomes = []
    if args.odd_quotes:
        quotes, quote_audit = load_odd_quotes(args.odd_quotes)
        shadow_checks, shadow_outcomes = replay_shadow_quotes(selected_signals, quotes)

    validation = validate_results(
        universe,
        evaluations,
        raw_signals,
        selected_signals,
        forward,
        shadow_outcomes,
        total_evaluation_rows=signal_audit["evaluation_rows"],
    )
    if not validation["passed"]:
        (args.output_dir / "validation_summary.json").write_text(
            json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raise RuntimeError(f"intraday validation failed: {validation['failures']}")

    (args.output_dir / "validation_summary.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_csv(args.output_dir / "universe.csv", _universe_rows(universe))
    _write_csv(args.output_dir / "raw_signals.csv", [row.to_row() for row in raw_signals])
    _write_csv(
        args.output_dir / "selected_signals.csv",
        [row.to_row() for row in selected_signals],
    )
    _write_csv(args.output_dir / "forward_returns.csv", forward)
    _write_csv(args.output_dir / "forward_summary.csv", summary)
    _write_csv(args.output_dir / "bootstrap_results.csv", bootstrap)
    _write_csv(
        args.output_dir / "shadow_quote_checks.csv",
        [row.to_row() for row in shadow_checks],
    )
    _write_csv(
        args.output_dir / "shadow_outcomes.csv",
        [row.to_row() for row in shadow_outcomes],
    )
    audit = {
        "mode": "research",
        "daily": daily_audit,
        "index_daily": index_daily_audit,
        "trading_calendar": calendar_audit,
        "minutes": minute_audit,
        "index_minutes": index_minute_audit,
        "odd_quotes": quote_audit,
        "universe": universe_audit,
        "signals": signal_audit,
        "validation": validation,
    }
    (args.output_dir / "data_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output_dir / "research_report.md").write_text(
        build_report(
            universe_audit=universe_audit,
            signal_audit=signal_audit,
            summary_rows=summary,
            validation=validation,
            quote_rows=len(quotes),
            shadow_outcomes=len(shadow_outcomes),
        ),
        encoding="utf-8",
    )
    _write_complete_manifest(
        args.output_dir,
        "research",
        [
            "config_snapshot.json",
            "validation_summary.json",
            "universe.csv",
            "signal_evaluations.csv",
            "raw_signals.csv",
            "selected_signals.csv",
            "forward_returns.csv",
            "forward_summary.csv",
            "bootstrap_results.csv",
            "shadow_quote_checks.csv",
            "shadow_outcomes.csv",
            "data_audit.json",
            "research_report.md",
        ],
    )
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
