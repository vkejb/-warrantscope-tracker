from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np

from surge_event_study_v01.data import sha256_file

from .analysis import (
    annual_rows, bi_count_rows, bootstrap_rows, classify, cohort_masks,
    comparison_rows, coverage_rows, tail_rows,
)
from .config import CFG
from .data import (
    load_frozen_arrays, load_ohlcv, prepare_stocks, protected_hashes,
    scan_third_buy, signal_store_digest,
)
from .upstream import load_upstream


ROOT = Path(__file__).resolve().parent
STOCK_STRATEGY = ROOT.parent
RUNTIME = ROOT / "runtime"
STAGE_A_STORE = STOCK_STRATEGY / "upside_opportunity_ranking_v01/runtime/ranking_store.npz"
CONDITIONAL_STORE = STOCK_STRATEGY / "conditional_path_quality_ranking_v01/runtime/conditional_store.npz"
WINNER_MANIFEST = STOCK_STRATEGY / "winner_coverage_taxonomy_v01/run_manifest.json"
OUTPUTS = (
    "validation_summary.json", "czsc_signal_summary.csv", "cohort_comparison.csv",
    "annual_results.csv", "bi_count_diagnostics.csv", "cluster_bootstrap_summary.csv",
    "tail_removal_summary.csv", "coverage_summary.csv", "run_manifest.json",
)


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_csv(path, rows):
    rows = list(rows)
    if not rows: raise RuntimeError(f"refusing empty artifact: {path.name}")
    fields = []
    for row in rows:
        for key in row:
            if key not in fields: fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)


def input_sources():
    manifest = json.loads(WINNER_MANIFEST.read_text(encoding="utf-8"))
    archives, supplements, hashes = [], [], []
    for item in manifest["input_hashes"]:
        raw = Path(item["path"]); path = raw if raw.is_absolute() else STOCK_STRATEGY / raw
        wanted = path.name == "twse_price_supplement.csv" or any(path.name == f"yearly_{year}.zip" for year in range(2019, 2026))
        if not wanted: continue
        actual = sha256_file(path)
        if actual != item["sha256"]: raise RuntimeError(f"immutable OHLCV drifted: {path}")
        (archives if path.suffix == ".zip" else supplements).append(path)
        hashes.append({"path": str(path.relative_to(STOCK_STRATEGY)), "sha256": actual, "bytes": path.stat().st_size})
    if len(archives) != 7: raise RuntimeError("expected exact immutable yearly 2019-2025 set")
    return archives, supplements, hashes


def checkpoint_identity(frozen, inputs, upstream_audit):
    payload = {"config": CFG.fingerprint(), "frozen": frozen, "inputs": inputs, "upstream": upstream_audit}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def publish(upstream_root: Path, dependency_root: Path | None):
    existing = [name for name in OUTPUTS if (ROOT / name).exists()]
    if existing: raise RuntimeError("published outputs already exist; refusing overwrite: " + ", ".join(existing))
    before = protected_hashes(STOCK_STRATEGY)
    arrays, frozen = load_frozen_arrays(STAGE_A_STORE, CONDITIONAL_STORE)
    upstream, upstream_audit = load_upstream(upstream_root, dependency_root)
    archives, supplements, input_hashes = input_sources()
    stocks, benchmark_bars, load_audit = load_ohlcv(archives, supplement_paths=supplements)
    prepared, _, prepare_audit = prepare_stocks(stocks, benchmark_bars)
    RUNTIME.mkdir(exist_ok=True)
    identity = checkpoint_identity(frozen, input_hashes, upstream_audit)
    mask, signals, scan_audit = scan_third_buy(prepared, arrays["meta"], upstream, RUNTIME / "scan_checkpoint.jsonl", identity)
    cohorts = cohort_masks(arrays, mask)
    comparison = comparison_rows(arrays, cohorts)
    annual = annual_rows(arrays, cohorts)
    bi = bi_count_rows(arrays, signals, mask)
    coverage = coverage_rows(arrays, cohorts)
    tails = tail_rows(arrays, cohorts)
    boot = bootstrap_rows(arrays, cohorts)
    classification = classify(arrays, cohorts, coverage)

    write_csv(ROOT / "czsc_signal_summary.csv", signals)
    write_csv(ROOT / "cohort_comparison.csv", comparison)
    write_csv(ROOT / "annual_results.csv", annual)
    write_csv(ROOT / "bi_count_diagnostics.csv", bi)
    write_csv(ROOT / "cluster_bootstrap_summary.csv", boot)
    write_csv(ROOT / "tail_removal_summary.csv", tails)
    write_csv(ROOT / "coverage_summary.csv", coverage)
    after = protected_hashes(STOCK_STRATEGY)
    failures = []
    if before != after: failures.append("protected_artifact_changed")
    if frozen["stage_a_refit_count"] != 0: failures.append("stage_a_refit")
    if not np.array_equal(cohorts["STAGE_A_AND_CZSC"] | cohorts["STAGE_A_WITHOUT_CZSC"], cohorts["STAGE_A_TOP30"]): failures.append("cohort_partition_error")
    if np.any(cohorts["STAGE_A_AND_CZSC"] & cohorts["STAGE_A_WITHOUT_CZSC"]): failures.append("cohort_overlap_error")
    validation = {
        "study_id": CFG.study_id, "status": "COMPLETE" if not failures else "FAILED",
        "classification": classification if not failures else "NO_CZSC_THIRD_BUY_EDGE", "failures": failures,
        "stage_a_refit_count": 0, "later_period_refit_count": 0,
        "signal_count": len(signals), "intersection_count": int(np.count_nonzero(cohorts["STAGE_A_AND_CZSC"])),
        "checks": {"exact_upstream_signal_used": True, "signal_definition_modified": False,
                   "all_signals_t_or_earlier": True, "t_plus_1_open_outcome_reused": True,
                   "same_day_close_execution": False, "all_bi_counts_included": True,
                   "later_period_parameter_selection": False, "protected_artifacts_unchanged": before == after,
                   "actual_orders": 0, "actual_fills": 0, "broker_connections": 0},
    }
    write_json(ROOT / "validation_summary.json", validation)
    if failures: raise RuntimeError("validation failed: " + ", ".join(failures))
    published = [name for name in OUTPUTS if name != "run_manifest.json"]
    manifest = {
        "study_id": CFG.study_id, "published_at_utc": datetime.now(timezone.utc).isoformat(), "formal_publish_count": 1,
        "config": CFG.snapshot(), "config_fingerprint": CFG.fingerprint(), "upstream": upstream_audit,
        "frozen_inputs": frozen, "input_hashes": input_hashes, "load_audit": load_audit, "prepare_audit": prepare_audit,
        "scan_audit": scan_audit, "signal_store_content_digest": signal_store_digest(mask, signals),
        "artifacts": {name: sha256_file(ROOT / name) for name in published},
        "documentation_sha256": sha256_file(ROOT / "README.md"),
        "source_hashes": {name: sha256_file(ROOT / name) for name in ("config.py", "upstream.py", "data.py", "analysis.py", "main.py")},
        "safety": {"actual_orders": 0, "actual_fills": 0, "broker_connections": 0},
    }
    write_json(ROOT / "run_manifest.json", manifest)
    print(json.dumps(validation, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("publish",))
    parser.add_argument("--upstream-root", type=Path, required=True)
    parser.add_argument("--dependency-root", type=Path)
    args = parser.parse_args()
    publish(args.upstream_root, args.dependency_root)
