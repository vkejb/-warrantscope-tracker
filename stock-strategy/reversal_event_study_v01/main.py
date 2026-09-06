#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import subprocess

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from reversal_event_study_v01.analysis import (
        bootstrap_table,
        feature_diagnostic,
        summary_table,
        validation_decision,
    )
    from reversal_event_study_v01.backtest import simulate_portfolio
    from reversal_event_study_v01.config import CFG
    from reversal_event_study_v01.report import build_report
    from reversal_event_study_v01.study import (
        PATTERNS,
        rank_portfolio_signals,
        scan_period,
    )
    from reversal_event_study_v01.validation import validate_research_run
    from surge_event_study_v01.data import load_ohlcv, prepare_stocks, sha256_file
else:
    from .analysis import (
        bootstrap_table,
        feature_diagnostic,
        summary_table,
        validation_decision,
    )
    from .backtest import simulate_portfolio
    from .config import CFG
    from .report import build_report
    from .study import PATTERNS, rank_portfolio_signals, scan_period
    from .validation import validate_research_run
    from surge_event_study_v01.data import load_ohlcv, prepare_stocks, sha256_file


def _assert_fresh_output(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"output directory already exists; choose a fresh path: {path}")


def _write_json(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _csv_value(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return value


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(value) for key, value in row.items()})


def _git_commit(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _write_portfolio(
    pattern: str, scenario: str, payload: dict, output: Path, artifacts: list[str]
) -> None:
    prefix = f"validation_{pattern.lower()}_{scenario}"
    for suffix, key in (
        ("trades", "trades"),
        ("equity_curve", "equity_curve"),
        ("unresolved", "unresolved"),
    ):
        name = f"{prefix}_{suffix}.csv"
        _write_csv(output / name, payload[key])
        artifacts.append(name)
    name = f"{prefix}_summary.json"
    _write_json(
        output / name,
        {
            "summary": payload["summary"],
            "order_audit": payload["order_audit"],
            "benchmark": payload["benchmark"],
        },
    )
    artifacts.append(name)


def _run_portfolios(
    rows: list[dict], prepared, benchmark, start_date: str, end_date: str
) -> dict:
    result: dict[str, dict] = {}
    for pattern in PATTERNS:
        ranked = rank_portfolio_signals(rows, pattern)
        result[pattern] = {
            scenario: simulate_portfolio(
                ranked,
                prepared,
                benchmark,
                start_date,
                end_date,
                scenario=scenario,
                cfg=CFG,
            )
            for scenario in ("baseline", "stress")
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Locked causal V/N relative-low study; research only, no broker access"
    )
    parser.add_argument("--archives", nargs="+", type=Path, required=True)
    parser.add_argument("--supplements", nargs="*", type=Path, default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    _assert_fresh_output(args.output_dir)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    artifacts: list[str] = []
    package_dir = Path(__file__).resolve().parent
    stock_strategy_dir = package_dir.parent
    repo = stock_strategy_dir.parent
    generic_dir = stock_strategy_dir / "surge_event_study_v01"
    all_inputs = args.archives + args.supplements
    input_hashes = [
        {"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size}
        for path in all_inputs
    ]
    shared_code_hashes = {
        "surge_event_study_v01/data.py": sha256_file(generic_dir / "data.py"),
        "surge_event_study_v01/models.py": sha256_file(generic_dir / "models.py"),
    }
    implementation_code_hashes = {
        path.name: sha256_file(path)
        for path in sorted(package_dir.glob("*.py"))
    }
    frozen_rule = {
        "status": "FROZEN_BEFORE_DATA_ANALYSIS",
        "strategy_id": CFG.strategy_id,
        "source_commit_before_run": _git_commit(repo),
        "config_hash": CFG.fingerprint(),
        "config": CFG.snapshot(),
        "shared_code_hashes": shared_code_hashes,
        "implementation_code_hashes": implementation_code_hashes,
        "input_hashes": input_hashes,
        "signal_clock": "T Close; only T and earlier OHLCV",
        "entry_reference": "T+1 regular-session Open proxy",
        "primary_label": (
            "Day1-Day10 Close reaches +8% before any Close reaches -5%; "
            "otherwise stop-first or timeout failure"
        ),
        "v_rule": (
            "latest Close low in T-3..T-1; pivot <= -10% from prior20 high and "
            "<= -6% over 5 sessions; T Close > T-1 High, 2.5%-8% above pivot, "
            "close location >= 0.65"
        ),
        "n_rule": (
            "latest five-bar local Close low 6-30 sessions before the recent pivot; "
            "first drawdown <= -10%, bottoms within +/-3%, intervening bounce >=6%, "
            "then same T confirmation; N priority on overlap"
        ),
        "portfolio_priority": (
            "within pattern/day: signal_return_1 desc, average_turnover_proxy_20 desc, code asc"
        ),
        "feature_policy": (
            "12 commonality features are discovery diagnostics only and never filter or rerank V0.1 trades"
        ),
        "cooldown_policy": "10 market sessions independently within each pattern and code",
        "periods": {
            "discovery": [CFG.discovery_start, CFG.discovery_end],
            "validation": [CFG.validation_start, CFG.validation_end],
            "conditional_feature_oos": [CFG.feature_oos_start, CFG.feature_oos_end],
        },
    }
    _write_json(args.output_dir / "analysis_spec.json", frozen_rule)
    artifacts.append("analysis_spec.json")

    stocks, benchmark_bars, load_audit = load_ohlcv(
        args.archives, supplement_paths=args.supplements, cfg=CFG
    )
    prepared, benchmark, prepare_audit = prepare_stocks(stocks, benchmark_bars, CFG)
    data_audit = {**load_audit, **prepare_audit, "shared_code_hashes": shared_code_hashes}
    _write_json(args.output_dir / "data_audit.json", data_audit)
    artifacts.append("data_audit.json")

    discovery = scan_period(
        prepared,
        benchmark,
        CFG.discovery_start,
        CFG.discovery_end,
        "discovery",
        CFG,
    )
    validation = scan_period(
        prepared,
        benchmark,
        CFG.validation_start,
        CFG.validation_end,
        "validation",
        CFG,
    )
    for prefix, period in (("discovery", discovery), ("validation", validation)):
        for name, rows in (
            (f"{prefix}_signals.csv", period["signal_rows"]),
            (f"{prefix}_daily_signal_counts.csv", period["daily_rows"]),
        ):
            _write_csv(args.output_dir / name, rows)
            artifacts.append(name)
        name = f"{prefix}_scan_audit.json"
        _write_json(
            args.output_dir / name,
            {
                "outcome_reasons": period["outcome_reasons"],
                "raw_pattern_counts": period["raw_pattern_counts"],
                "maximum_signal_date_read": period["maximum_signal_date_read"],
                "cooldown_seed_count": period["cooldown_seed_count"],
            },
        )
        artifacts.append(name)

    discovery_summary_rows, discovery_lookup = summary_table(
        discovery["signal_rows"], "discovery"
    )
    validation_summary_rows, validation_lookup = summary_table(
        validation["signal_rows"], "validation"
    )
    summary_rows = discovery_summary_rows + validation_summary_rows
    _write_csv(args.output_dir / "pattern_summary.csv", summary_rows)
    artifacts.append("pattern_summary.csv")

    validation_bootstrap_rows, validation_bootstrap_lookup = bootstrap_table(
        validation["signal_rows"], CFG
    )
    _write_csv(args.output_dir / "validation_bootstrap.csv", validation_bootstrap_rows)
    artifacts.append("validation_bootstrap.csv")

    diagnostic = feature_diagnostic(
        discovery["signal_rows"], validation["signal_rows"], CFG
    )
    _write_csv(
        args.output_dir / "common_feature_quintiles.csv", diagnostic["quintile_rows"]
    )
    _write_csv(
        args.output_dir / "common_feature_selection.csv", diagnostic["selection_rows"]
    )
    _write_json(
        args.output_dir / "common_feature_research_candidates.json",
        {
            "policy": diagnostic["policy"],
            "research_candidates": diagnostic["research_candidates"],
        },
    )
    artifacts.extend(
        [
            "common_feature_quintiles.csv",
            "common_feature_selection.csv",
            "common_feature_research_candidates.json",
        ]
    )

    # January 2025 is used only as an exit/label buffer for late-December 2024
    # validation signals. No 2025 signal is scored here.
    portfolios = _run_portfolios(
        validation["signal_rows"],
        prepared,
        benchmark,
        CFG.validation_start,
        "20250131",
    )
    for pattern in PATTERNS:
        for scenario in ("baseline", "stress"):
            _write_portfolio(
                pattern,
                scenario,
                portfolios[pattern][scenario],
                args.output_dir,
                artifacts,
            )

    decision = validation_decision(
        validation["signal_rows"],
        validation_lookup,
        validation_bootstrap_lookup,
        portfolios,
        CFG,
    )
    _write_json(args.output_dir / "validation_decision.json", decision)
    artifacts.append("validation_decision.json")

    oos = None
    oos_lookup = None
    oos_portfolios = None
    allowed = set(decision["patterns_allowed_into_2025"])
    if allowed:
        oos = scan_period(
            prepared,
            benchmark,
            CFG.feature_oos_start,
            CFG.feature_oos_end,
            "feature_oos_prevalence_seen",
            CFG,
            allowed_patterns=allowed,
        )
        _write_csv(args.output_dir / "feature_oos_2025_signals.csv", oos["signal_rows"])
        artifacts.append("feature_oos_2025_signals.csv")
        oos_summary_rows, oos_lookup = summary_table(
            oos["signal_rows"], "feature_oos_prevalence_seen"
        )
        _write_csv(args.output_dir / "feature_oos_2025_summary.csv", oos_summary_rows)
        artifacts.append("feature_oos_2025_summary.csv")
        oos_portfolios = _run_portfolios(
            oos["signal_rows"],
            prepared,
            benchmark,
            CFG.feature_oos_start,
            CFG.maximum_input_date,
        )
        _write_json(
            args.output_dir / "feature_oos_2025_portfolio_summary.json",
            {
                pattern: {
                    scenario: oos_portfolios[pattern][scenario]["summary"]
                    for scenario in ("baseline", "stress")
                }
                for pattern in allowed
            },
        )
        artifacts.append("feature_oos_2025_portfolio_summary.json")

    integrity = validate_research_run(
        data_audit,
        discovery,
        validation,
        diagnostic,
        decision,
        oos,
        CFG,
    )
    _write_json(args.output_dir / "pipeline_validation.json", integrity)
    artifacts.append("pipeline_validation.json")
    if not integrity["passed"]:
        raise RuntimeError(f"pipeline validation failed: {integrity['failures']}")

    report = build_report(
        data_audit=data_audit,
        discovery_lookup=discovery_lookup,
        validation_lookup=validation_lookup,
        validation_bootstrap=validation_bootstrap_lookup,
        feature_diagnostic=diagnostic,
        validation_decision=decision,
        portfolios=portfolios,
        oos_lookup=oos_lookup,
        integrity=integrity,
        cfg=CFG,
    )
    (args.output_dir / "research_report.md").write_text(report, encoding="utf-8")
    artifacts.append("research_report.md")

    artifact_hashes = {
        name: sha256_file(args.output_dir / name) for name in artifacts
    }
    manifest = {
        "status": "COMPLETE",
        "strategy_id": CFG.strategy_id,
        "result_status": CFG.result_status,
        "config_hash": CFG.fingerprint(),
        "shared_code_hashes": shared_code_hashes,
        "implementation_code_hashes": implementation_code_hashes,
        "validation_status": decision["status"],
        "patterns_allowed_into_2025": decision["patterns_allowed_into_2025"],
        "feature_oos_2025_opened": bool(oos),
        "artifact_sha256": artifact_hashes,
        "artifacts": artifacts + ["run_manifest.json"],
    }
    _write_json(args.output_dir / "run_manifest.json", manifest)
    print(
        json.dumps(
            {
                "manifest": manifest,
                "discovery": discovery_lookup,
                "validation": validation_lookup,
                "validation_decision": decision,
                "portfolio_summaries": {
                    pattern: {
                        scenario: portfolios[pattern][scenario]["summary"]
                        for scenario in ("baseline", "stress")
                    }
                    for pattern in PATTERNS
                },
                "common_feature_research_candidates": diagnostic[
                    "research_candidates"
                ],
            },
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
