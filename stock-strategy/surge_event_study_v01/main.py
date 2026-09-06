#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from surge_event_study_v01.analysis import (
        cluster_bootstrap,
        discover_features,
        oos_decision,
        score_period,
        summarize_edge,
        validation_decision,
    )
    from surge_event_study_v01.backtest import (
        execution_proxy_decision,
        simulate_portfolio,
    )
    from surge_event_study_v01.config import CFG
    from surge_event_study_v01.data import load_ohlcv, prepare_stocks, sha256_file
    from surge_event_study_v01.report import build_report
    from surge_event_study_v01.validation import validate_research_run
else:
    from .analysis import (
        cluster_bootstrap,
        discover_features,
        oos_decision,
        score_period,
        summarize_edge,
        validation_decision,
    )
    from .backtest import execution_proxy_decision, simulate_portfolio
    from .config import CFG
    from .data import load_ohlcv, prepare_stocks, sha256_file
    from .report import build_report
    from .validation import validate_research_run


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
    fields = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(value) for key, value in row.items()})


def _rule_fingerprint(rule: dict) -> str:
    payload = json.dumps(
        rule, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_period(prefix: str, period: dict, output: Path, artifacts: list[str]) -> None:
    names = {
        f"{prefix}_daily_edge.csv": period["daily_rows"],
        f"{prefix}_top30_signals.csv": period["signal_rows"],
        f"{prefix}_threshold_matrix.csv": period["threshold_rows"],
    }
    for name, rows in names.items():
        _write_csv(output / name, rows)
        artifacts.append(name)
    outcome_name = f"{prefix}_outcome_reasons.json"
    _write_json(output / outcome_name, period["outcome_reasons"])
    artifacts.append(outcome_name)


def _write_portfolio(prefix: str, payload: dict, output: Path, artifacts: list[str]) -> None:
    for scenario in ("baseline", "stress"):
        scenario_payload = payload[scenario]
        for suffix, key in (
            ("trades", "trades"),
            ("equity_curve", "equity_curve"),
            ("unresolved", "unresolved"),
        ):
            name = f"{prefix}_{scenario}_{suffix}.csv"
            _write_csv(output / name, scenario_payload[key])
            artifacts.append(name)
    name = f"{prefix}_portfolio_summary.json"
    _write_json(output / name, payload)
    artifacts.append(name)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Locked SURGE_EVENT_STUDY_V0_1: common pre-surge features, "
            "2023-24 validation, conditional 2025 feature-OOS"
        )
    )
    parser.add_argument("--archives", nargs="+", type=Path, required=True)
    parser.add_argument("--supplements", nargs="*", type=Path, default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    _assert_fresh_output(args.output_dir)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    artifacts: list[str] = []

    all_inputs = args.archives + args.supplements
    input_hashes = [
        {"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size}
        for path in all_inputs
    ]
    analysis_spec = {
        "status": "FROZEN_BEFORE_DATA_ANALYSIS",
        "strategy_id": CFG.strategy_id,
        "config_hash": CFG.fingerprint(),
        "config": CFG.snapshot(),
        "input_hashes": input_hashes,
        "confirmatory_primary_label": (
            "T+1 Open reference; any Day1-Day10 Close at or above +15%"
        ),
        "selection_boundary": "2020-2022 only",
        "validation_boundary": "2023-2024; rules immutable",
        "oos_boundary": "2025 opened only after every validation gate passes",
    }
    _write_json(args.output_dir / "analysis_spec.json", analysis_spec)
    artifacts.append("analysis_spec.json")

    stocks, benchmark_bars, load_audit = load_ohlcv(
        args.archives, supplement_paths=args.supplements, cfg=CFG
    )
    prepared, benchmark, prepare_audit = prepare_stocks(stocks, benchmark_bars, CFG)
    data_audit = {**load_audit, **prepare_audit}
    _write_json(args.output_dir / "data_audit.json", data_audit)
    artifacts.append("data_audit.json")

    discovery = discover_features(prepared, benchmark, CFG)
    selected_rule = discovery["selected_rule"]
    selected_rule["config_hash"] = CFG.fingerprint()
    selected_rule["rule_hash"] = _rule_fingerprint(selected_rule)
    # This artifact is persisted before validation functions are called.
    _write_json(args.output_dir / "selected_rule.json", selected_rule)
    artifacts.append("selected_rule.json")
    _write_csv(args.output_dir / "discovery_feature_quintiles.csv", discovery["quintile_rows"])
    _write_csv(args.output_dir / "discovery_feature_selection.csv", discovery["selection_rows"])
    _write_csv(args.output_dir / "discovery_threshold_matrix.csv", discovery["threshold_rows"])
    _write_csv(args.output_dir / "discovery_descriptive_surge_events.csv", discovery["descriptive_events"])
    _write_csv(args.output_dir / "discovery_daily_signal_counts.csv", discovery["daily_signal_counts"])
    _write_json(args.output_dir / "discovery_outcome_reasons.json", discovery["outcome_reasons"])
    artifacts.extend(
        [
            "discovery_feature_quintiles.csv",
            "discovery_feature_selection.csv",
            "discovery_threshold_matrix.csv",
            "discovery_descriptive_surge_events.csv",
            "discovery_daily_signal_counts.csv",
            "discovery_outcome_reasons.json",
        ]
    )

    discovery_score = None
    validation_period = None
    validation_result: dict
    validation_portfolio = None
    execution_result = None
    oos_period = None
    oos_result = None
    oos_portfolio = None
    if selected_rule["features"]:
        discovery_score = score_period(
            prepared,
            benchmark,
            selected_rule,
            CFG.discovery_start,
            CFG.discovery_end,
            "discovery_scored",
            CFG,
        )
        _write_period("discovery_scored", discovery_score, args.output_dir, artifacts)
        discovery_edge = {
            "summary": summarize_edge(
                discovery_score["daily_rows"], discovery_score["signal_rows"]
            ),
            "bootstrap": cluster_bootstrap(discovery_score["daily_rows"], CFG),
            "in_sample": True,
        }
        _write_json(args.output_dir / "discovery_scored_summary.json", discovery_edge)
        artifacts.append("discovery_scored_summary.json")

        validation_period = score_period(
            prepared,
            benchmark,
            selected_rule,
            CFG.validation_start,
            CFG.validation_end,
            "validation",
            CFG,
        )
        _write_period("validation", validation_period, args.output_dir, artifacts)
        validation_result = validation_decision(
            validation_period["daily_rows"],
            validation_period["signal_rows"],
            benchmark,
            CFG,
        )
        _write_json(args.output_dir / "validation_summary.json", validation_result)
        artifacts.append("validation_summary.json")

        baseline = simulate_portfolio(
            validation_period["signal_rows"],
            prepared,
            benchmark,
            CFG.validation_start,
            CFG.validation_end,
            scenario="baseline",
            cfg=CFG,
        )
        stress = simulate_portfolio(
            validation_period["signal_rows"],
            prepared,
            benchmark,
            CFG.validation_start,
            CFG.validation_end,
            scenario="stress",
            cfg=CFG,
        )
        execution_result = execution_proxy_decision(baseline, stress)
        validation_portfolio = {
            "baseline": baseline,
            "stress": stress,
            "decision": execution_result,
        }
        _write_portfolio(
            "validation", validation_portfolio, args.output_dir, artifacts
        )

        if validation_result["status"] == "PASS":
            oos_period = score_period(
                prepared,
                benchmark,
                selected_rule,
                CFG.feature_oos_start,
                CFG.feature_oos_end,
                "feature_oos_prevalence_seen",
                CFG,
            )
            _write_period("feature_oos_2025", oos_period, args.output_dir, artifacts)
            oos_result = oos_decision(
                oos_period["daily_rows"], oos_period["signal_rows"], benchmark, CFG
            )
            _write_json(args.output_dir / "feature_oos_2025_summary.json", oos_result)
            artifacts.append("feature_oos_2025_summary.json")
            oos_baseline = simulate_portfolio(
                oos_period["signal_rows"],
                prepared,
                benchmark,
                CFG.feature_oos_start,
                CFG.feature_oos_end,
                scenario="baseline",
                cfg=CFG,
            )
            oos_stress = simulate_portfolio(
                oos_period["signal_rows"],
                prepared,
                benchmark,
                CFG.feature_oos_start,
                CFG.feature_oos_end,
                scenario="stress",
                cfg=CFG,
            )
            oos_portfolio = {
                "baseline": oos_baseline,
                "stress": oos_stress,
                "decision": execution_proxy_decision(oos_baseline, oos_stress),
            }
            _write_portfolio(
                "feature_oos_2025", oos_portfolio, args.output_dir, artifacts
            )
    else:
        validation_result = {
            "status": "NOT_RUN_NO_ROBUST_FEATURES",
            "all_gates_passed": False,
            "reason": "No feature passed the frozen discovery gate; no strategy was forced.",
            "oos_policy": "2025 remains unopened",
        }
        _write_json(args.output_dir / "validation_summary.json", validation_result)
        artifacts.append("validation_summary.json")

    integrity = validate_research_run(
        data_audit,
        discovery,
        validation_period,
        validation_result,
        oos_period,
        CFG,
    )
    _write_json(args.output_dir / "pipeline_validation.json", integrity)
    artifacts.append("pipeline_validation.json")
    if not integrity["passed"]:
        raise RuntimeError(f"pipeline validation failed: {integrity['failures']}")

    report = build_report(
        data_audit=data_audit,
        discovery=discovery,
        validation=validation_result,
        validation_portfolio=validation_portfolio,
        execution_decision=execution_result,
        oos=oos_result,
        oos_portfolio=oos_portfolio,
        run_validation=integrity,
        cfg=CFG,
    )
    (args.output_dir / "research_report.md").write_text(report, encoding="utf-8")
    artifacts.append("research_report.md")
    manifest = {
        "status": "COMPLETE",
        "strategy_id": CFG.strategy_id,
        "result_status": CFG.result_status,
        "config_hash": CFG.fingerprint(),
        "selected_rule_hash": selected_rule["rule_hash"],
        "validation_status": validation_result["status"],
        "feature_oos_opened": oos_period is not None,
        "artifacts": artifacts + ["run_manifest.json"],
    }
    _write_json(args.output_dir / "run_manifest.json", manifest)
    print(
        json.dumps(
            {
                "manifest": manifest,
                "selected_features": selected_rule["features"],
                "validation": validation_result,
                "execution_proxy": execution_result,
                "feature_oos": oos_result,
            },
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
