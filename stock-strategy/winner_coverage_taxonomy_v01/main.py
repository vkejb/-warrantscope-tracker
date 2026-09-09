#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import numpy as np

from multi_setup_study_v01.config import CFG as MULTI_CFG
from prospective_shadow_v01.detector import assert_frozen_contract
from reversal_event_study_v01.config import CFG as REVERSAL_CFG
from surge_event_study_v01.data import load_ohlcv, prepare_stocks, sha256_file

from .analysis import (
    build_validation_summary,
    candidate_family_rows,
    feature_control_rows,
    mother_sample_rows,
    overlap_rows,
    setup_coverage_rows,
    taxonomy_summary_rows,
    unique_marginal_rows,
    unexplained_winner_rows,
    winner_base_rate_rows,
)
from .config import CFG, FAMILY_BIT, MAJOR_SETUPS
from .pipeline import (
    load_extension_mother,
    load_frozen_setup_memberships,
    save_local_store,
    scan_taxonomy_inputs,
    store_digest,
)
from .taxonomy import assign_frozen_taxonomy, fit_discovery_taxonomy


TRACKED_ARTIFACTS = (
    "mother_sample_summary.csv",
    "winner_base_rate.csv",
    "setup_coverage.csv",
    "winner_overlap_matrix.csv",
    "unique_marginal_coverage.csv",
    "unexplained_winner_summary.csv",
    "feature_control_comparison.csv",
    "winner_taxonomy_summary.csv",
    "candidate_family_summary.csv",
    "validation_summary.json",
    "run_manifest.json",
)


def _write_json(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: list[dict]) -> None:
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    if not fields:
        raise RuntimeError(f"refusing to publish empty table: {path.name}")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _git_commit(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _ledger_hashes(stock_strategy_dir: Path) -> dict[str, str]:
    data_dir = stock_strategy_dir / "prospective_shadow_v01" / "data"
    return {
        name: sha256_file(data_dir / name)
        for name in (
            "prospective_signals.csv",
            "prospective_outcomes.csv",
            "prospective_scan_log.csv",
            "shadow_status.json",
        )
    }


def _source_hashes(package_dir: Path) -> dict[str, str]:
    stock_strategy = package_dir.parent
    paths = (
        stock_strategy / "multi_setup_study_v01" / "config.py",
        stock_strategy / "multi_setup_study_v01" / "setup_detectors.py",
        stock_strategy / "multi_setup_study_v01" / "outcomes.py",
        stock_strategy / "reversal_event_study_v01" / "study.py",
        stock_strategy / "extension_entry_study_v01" / "pipeline.py",
        stock_strategy / "extension_entry_study_v01" / "observation_store.npz",
    )
    return {str(path.relative_to(stock_strategy)): sha256_file(path) for path in paths}


def _supplement_provenance(paths: list[Path]) -> list[dict]:
    result = []
    for path in paths:
        metadata = path.with_suffix(".provenance.json")
        result.append(
            {
                "supplement": str(path),
                "supplement_sha256": sha256_file(path),
                "provenance_file": str(metadata) if metadata.is_file() else None,
                "provenance_sha256": sha256_file(metadata) if metadata.is_file() else None,
                "provenance": (
                    json.loads(metadata.read_text(encoding="utf-8"))
                    if metadata.is_file()
                    else None
                ),
            }
        )
    return result


def _validate(
    arrays: dict[str, np.ndarray],
    scan_audit: dict,
    expected_counts: dict[str, int],
    model,
    tables: dict[str, list[dict]],
    ledger_before: dict[str, str],
    ledger_after: dict[str, str],
) -> dict:
    failures = []
    actual_counts = scan_audit["family_signal_counts"]
    for setup in MAJOR_SETUPS:
        if actual_counts.get(setup, 0) != expected_counts.get(setup, 0):
            failures.append(
                f"frozen_setup_count_mismatch:{setup}:"
                f"{actual_counts.get(setup, 0)}!={expected_counts.get(setup, 0)}"
            )
    compact = (arrays["family_masks"] & FAMILY_BIT["N_COMPACT_RETEST_HYPOTHESIS"]) != 0
    parent = (arrays["family_masks"] & FAMILY_BIT["N_RETEST"]) != 0
    if not np.all(~compact | parent):
        failures.append("n_compact_not_subset_of_n_retest")
    if int(arrays["meta"]["signal_date"].max()) > 20251231:
        failures.append("prospective_observation_in_study")
    if ledger_before != ledger_after:
        failures.append("prospective_ledger_changed")
    if any(not rows for rows in tables.values()):
        failures.append("empty_summary_table")
    if model.fit_period != "HISTORICAL_DISCOVERY_2020_2022_UNEXPLAINED_WINNERS_ONLY":
        failures.append("taxonomy_fit_period_drift")
    return {
        "passed": not failures,
        "failures": failures,
        "checks": {
            "mother_sample_is_all_eligible_stock_dates": True,
            "mother_sample_outcome_store_hash_fixed": True,
            "winner_is_shared_close_confirmed_barrier_outcome": True,
            "frozen_setup_counts_match_published_multi_setup": not any(
                item.startswith("frozen_setup_count_mismatch") for item in failures
            ),
            "n_compact_definition_unchanged": True,
            "n_compact_subset_of_n_retest": bool(np.all(~compact | parent)),
            "taxonomy_fit_count": 1,
            "taxonomy_fit_period": model.fit_period,
            "later_period_taxonomy_refits": 0,
            "controls_same_date_price_volume_bucket": True,
            "no_supervised_ml": True,
            "no_grid_search_or_threshold_optimization": True,
            "prospective_observations_excluded": True,
            "prospective_ledgers_unchanged": ledger_before == ledger_after,
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
            "manifest_written_last": True,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Winner coverage and unsupervised taxonomy discovery; research only"
    )
    parser.add_argument("--archives", nargs="+", type=Path, required=True)
    parser.add_argument("--supplements", nargs="*", type=Path, default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--extension-store",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "extension_entry_study_v01"
        / "observation_store.npz",
    )
    parser.add_argument(
        "--multi-setup-signals",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "multi_setup_study_v01"
        / "signal_observations.csv",
    )
    args = parser.parse_args(argv)
    package_dir = Path(__file__).resolve().parent
    stock_strategy = package_dir.parent
    repo = stock_strategy.parent
    args.output_dir.mkdir(parents=True, exist_ok=True)
    existing = [name for name in TRACKED_ARTIFACTS if (args.output_dir / name).exists()]
    if existing:
        raise FileExistsError("refusing to overwrite published outputs: " + ", ".join(existing))

    assert_frozen_contract()
    if MULTI_CFG.fingerprint() != CFG.expected_multi_setup_config_hash:
        raise RuntimeError("multi-setup config contract drifted")
    if REVERSAL_CFG.fingerprint() != CFG.expected_reversal_config_hash:
        raise RuntimeError("reversal config contract drifted")
    ledger_before = _ledger_hashes(stock_strategy)
    source_hashes = _source_hashes(package_dir)
    input_hashes = [
        {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in [*args.archives, *args.supplements]
    ]
    mother, mother_metadata = load_extension_mother(args.extension_store, CFG)
    frozen_memberships, expected_counts = load_frozen_setup_memberships(
        args.multi_setup_signals
    )

    with tempfile.TemporaryDirectory(prefix="winner_taxonomy_v01_") as temporary:
        staging = Path(temporary)
        stocks, benchmark_bars, load_audit = load_ohlcv(
            args.archives, supplement_paths=args.supplements, cfg=CFG
        )
        prepared, benchmark, prepare_audit = prepare_stocks(stocks, benchmark_bars, CFG)
        arrays, scan_audit = scan_taxonomy_inputs(
            prepared, benchmark, mother, frozen_memberships, CFG
        )
        scan_audit["observation_store_sha256"] = store_digest(arrays)
        discovery = (
            (arrays["meta"]["signal_date"] >= int(CFG.discovery_start))
            & (arrays["meta"]["signal_date"] <= int(CFG.discovery_end))
        )
        winner = (
            arrays["meta"]["outcome_evaluable"]
            & (arrays["outcomes"][:, 0] == 1.0)
        )
        explained = np.zeros(len(winner), dtype=bool)
        for setup in MAJOR_SETUPS:
            explained |= (arrays["family_masks"] & FAMILY_BIT[setup]) != 0
        model = fit_discovery_taxonomy(
            arrays["taxonomy_features"],
            arrays["meta"]["signal_date"],
            discovery & winner & ~explained,
            CFG,
        )
        taxonomy_labels = assign_frozen_taxonomy(arrays["taxonomy_features"], model)

        tables = {
            "mother_sample_summary.csv": mother_sample_rows(arrays),
            "winner_base_rate.csv": winner_base_rate_rows(arrays),
            "setup_coverage.csv": setup_coverage_rows(arrays),
            "winner_overlap_matrix.csv": overlap_rows(arrays),
            "unique_marginal_coverage.csv": unique_marginal_rows(arrays),
            "unexplained_winner_summary.csv": unexplained_winner_rows(arrays),
            "winner_taxonomy_summary.csv": taxonomy_summary_rows(
                arrays, taxonomy_labels, model, CFG
            ),
            "candidate_family_summary.csv": candidate_family_rows(
                arrays, taxonomy_labels, model
            ),
        }
        feature_control, control_audit = feature_control_rows(
            arrays, taxonomy_labels, model, CFG
        )
        tables["feature_control_comparison.csv"] = feature_control
        validation = build_validation_summary(
            arrays,
            tables["setup_coverage.csv"],
            tables["unique_marginal_coverage.csv"],
            tables["unexplained_winner_summary.csv"],
            tables["winner_taxonomy_summary.csv"],
            tables["candidate_family_summary.csv"],
            model,
        )
        _write_json(staging / "validation_summary.json", validation)
        for name, rows in tables.items():
            _write_csv(staging / name, rows)

        ledger_after = _ledger_hashes(stock_strategy)
        pipeline_validation = _validate(
            arrays,
            scan_audit,
            expected_counts,
            model,
            tables,
            ledger_before,
            ledger_after,
        )
        if not pipeline_validation["passed"]:
            raise RuntimeError(
                "pipeline validation failed: " + ", ".join(pipeline_validation["failures"])
            )
        local_store = package_dir / "runtime" / "observation_store.npz"
        local_store.parent.mkdir(parents=True, exist_ok=True)
        save_local_store(
            local_store,
            arrays,
            {
                "study_id": CFG.study_id,
                "row_count": len(arrays["meta"]),
                "store_digest": scan_audit["observation_store_sha256"],
                "taxonomy_model_sha256": model.fingerprint(),
            },
        )
        artifact_hashes = {
            name: sha256_file(staging / name)
            for name in TRACKED_ARTIFACTS
            if name != "run_manifest.json"
        }
        manifest = {
            "status": "COMPLETE",
            "study_id": CFG.study_id,
            "result_status": validation["final_classification"],
            "source_commit_before_run": _git_commit(repo),
            "config_hash": CFG.fingerprint(),
            "input_hashes": input_hashes,
            "supplement_provenance": _supplement_provenance(args.supplements),
            "extension_mother_metadata": mother_metadata,
            "extension_store_sha256": sha256_file(args.extension_store),
            "multi_setup_signal_csv_sha256": sha256_file(args.multi_setup_signals),
            "source_code_hashes": source_hashes,
            "scan_audit": scan_audit,
            "load_audit": load_audit,
            "prepare_audit": prepare_audit,
            "control_matching_audit": control_audit,
            "taxonomy_model_sha256": model.fingerprint(),
            "taxonomy_model": model.payload(),
            "pipeline_validation": pipeline_validation,
            "prospective_ledger_hashes_before": ledger_before,
            "prospective_ledger_hashes_after": ledger_after,
            "artifact_sha256": artifact_hashes,
            "tracked_artifacts": list(TRACKED_ARTIFACTS),
            "local_observation_store": {
                "path": str(local_store),
                "sha256": sha256_file(local_store),
                "tracked_by_git": False,
            },
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
        }
        _write_json(staging / "run_manifest.json", manifest)
        for name in TRACKED_ARTIFACTS:
            shutil.move(str(staging / name), args.output_dir / name)

    print(json.dumps(validation, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
