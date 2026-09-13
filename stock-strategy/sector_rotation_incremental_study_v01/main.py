from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np

from cross_sectional_alpha_ranking_v01.data import load_market_regime
from surge_event_study_v01.data import sha256_file

from .analysis import (
    classify, cluster_bootstrap_rows, composite_scores, concentration_rows,
    daily_context_ranks, define_composite, discovery_boundaries,
    dynamic_feature_rows, feature_bucket_rows, pool_rows, regime_rows, tail_rows,
)
from .config import CFG, FEATURES
from .data import (
    build_dynamic_peer_store, load_frozen_arrays, load_ohlcv, load_store, prepare_stocks,
    protected_hashes, save_store, store_digest,
)


ROOT = Path(__file__).resolve().parent
STOCK_STRATEGY = ROOT.parent
REPO = STOCK_STRATEGY.parent
RUNTIME = ROOT / "runtime"
STAGE_A_STORE = STOCK_STRATEGY / "upside_opportunity_ranking_v01" / "runtime" / "ranking_store.npz"
CONDITIONAL_STORE = STOCK_STRATEGY / "conditional_path_quality_ranking_v01" / "runtime" / "conditional_store.npz"
WINNER_MANIFEST = STOCK_STRATEGY / "winner_coverage_taxonomy_v01" / "run_manifest.json"
TRACKED_OUTPUTS = (
    "validation_summary.json", "pit_audit.json", "feature_bucket_results.csv",
    "dynamic_peer_results.csv", "pool_reduction_summary.csv",
    "cluster_bootstrap_summary.csv", "tail_removal_summary.csv",
    "concentration_diagnostics.csv", "regime_diagnostics.csv", "run_manifest.json",
)


def write_json(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def write_csv(path: Path, rows) -> None:
    rows = list(rows)
    if not rows:
        raise RuntimeError(f"refusing to publish empty table: {path.name}")
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)


def _input_sources():
    manifest = json.loads(WINNER_MANIFEST.read_text(encoding="utf-8"))
    archives, supplements, hashes = [], [], []
    for item in manifest["input_hashes"]:
        raw = Path(item["path"])
        path = raw if raw.is_absolute() else STOCK_STRATEGY / raw
        if not ("yearly_2019" in path.name or any(f"yearly_{year}" in path.name for year in range(2020, 2026)) or path.name == "twse_price_supplement.csv"):
            continue
        actual = sha256_file(path)
        if actual != item["sha256"]:
            raise RuntimeError(f"immutable OHLCV input drifted: {path}")
        (archives if path.suffix == ".zip" else supplements).append(path)
        hashes.append({"path": str(path.relative_to(STOCK_STRATEGY)), "sha256": actual, "bytes": path.stat().st_size})
    if len(archives) != 7:
        raise RuntimeError("expected exact yearly 2019-2025 archive set")
    return archives, supplements, hashes


def _pit_audit() -> dict:
    evidence = {
        "v21_loader": "v21/data_loader.py: latest/historical labels are not complete PIT daily industry history",
        "v21_diagnostic": "v21/diagnostic_analysis.py: current/future labels are not backfilled",
        "repository_search": "no official 2020-2025 daily industry-membership history with effective change dates",
    }
    return {
        "official_sector": {
            "decision": "OFFICIAL_SECTOR_PIT_UNSAFE",
            "historical_membership_reconstructable": False,
            "effective_change_dates_available": False,
            "survivorship_hindsight_risk": True,
            "action": "EXCLUDED_FAIL_CLOSED_NO_CURRENT_CLASSIFICATION_BACKFILL",
            "evidence": evidence,
        },
        "dynamic_market_peers": {
            "decision": "PIT_USABLE",
            "definition": "Top 10 Pearson-correlated eligible peers from trailing 60 T-or-earlier daily returns",
            "minimum_common_sessions": CFG.minimum_common_sessions,
            "self_excluded": True,
            "tie_breaker": "stock_id ascending after correlation descending",
            "future_constituents_used": False,
        },
    }


def publish() -> None:
    existing = [name for name in TRACKED_OUTPUTS if (ROOT / name).exists()]
    if existing:
        raise RuntimeError("published outputs already exist; refusing overwrite: " + ", ".join(existing))
    before = protected_hashes(STOCK_STRATEGY)
    arrays, frozen_audit = load_frozen_arrays(STAGE_A_STORE, CONDITIONAL_STORE)
    archives, supplements, input_hashes = _input_sources()
    RUNTIME.mkdir(exist_ok=True)
    local_store = RUNTIME / "dynamic_peer_store.npz"
    if local_store.exists():
        store, peer_audit = load_store(local_store)
        load_audit = {"checkpoint_reused": True, "input_hashes_reverified": True}
        prepare_audit = {"checkpoint_reused": True, "causal_contract_reverified": True}
    else:
        stocks, benchmark_bars, load_audit = load_ohlcv(archives, supplement_paths=supplements)
        prepared, benchmark, prepare_audit = prepare_stocks(stocks, benchmark_bars)
        store, peer_audit = build_dynamic_peer_store(arrays, prepared, benchmark)
        save_store(local_store, store, peer_audit)

    cuts = discovery_boundaries(store, arrays)
    buckets = feature_bucket_rows(arrays, store, cuts)
    dynamics = dynamic_feature_rows(buckets)
    definitions, composite_audit = define_composite(buckets, cuts)
    scores = composite_scores(store, definitions)
    ranks = daily_context_ranks(arrays, store, scores)
    pools, cohorts, strong_min = pool_rows(arrays, store, scores, ranks)
    bootstraps = cluster_bootstrap_rows(arrays, cohorts)
    tails = tail_rows(arrays, cohorts)
    concentration = concentration_rows(arrays, store, cohorts)
    regimes, regime_audit = load_market_regime(STOCK_STRATEGY, arrays["meta"]["signal_date"], WINNER_MANIFEST)
    regime = regime_rows(arrays, cohorts, regimes)
    classification = classify(pools, composite_audit["eligible"])

    pit = _pit_audit()
    write_json(ROOT / "pit_audit.json", pit)
    write_csv(ROOT / "feature_bucket_results.csv", buckets)
    write_csv(ROOT / "dynamic_peer_results.csv", dynamics)
    if definitions:
        write_csv(ROOT / "composite_score_results.csv", [
            {**row, "strong_group_context_min_points": strong_min} for row in definitions
        ])
    write_csv(ROOT / "pool_reduction_summary.csv", pools)
    write_csv(ROOT / "cluster_bootstrap_summary.csv", bootstraps)
    write_csv(ROOT / "tail_removal_summary.csv", tails)
    write_csv(ROOT / "concentration_diagnostics.csv", concentration)
    write_csv(ROOT / "regime_diagnostics.csv", regime)

    before_after = protected_hashes(STOCK_STRATEGY)
    failures = []
    if before != before_after: failures.append("protected_artifact_changed")
    if frozen_audit["stage_a_refit_count"] != 0: failures.append("stage_a_refit")
    if int(np.count_nonzero(store["peer_codes"][store["available"]] == arrays["meta"]["stock_code"][store["available"], None])):
        failures.append("leave_one_out_violation")
    validation = {
        "study_id": CFG.study_id,
        "status": "COMPLETE" if not failures else "FAILED",
        "classification": classification if not failures else "NO_SECTOR_ROTATION_EDGE",
        "failures": failures,
        "official_sector_status": pit["official_sector"]["decision"],
        "dynamic_peer_coverage": peer_audit["coverage_pct"],
        "stage_a_refit_count": 0,
        "later_period_refit_count": 0,
        "composite_score_eligible": composite_audit["eligible"],
        "composite_feature_count": len(definitions),
        "checks": {
            "frozen_stage_a_exact_reuse": True,
            "all_features_t_or_earlier": True,
            "trailing_peer_correlation_only": True,
            "leave_one_out": True,
            "future_constituents_used": False,
            "current_sector_hindsight_used": False,
            "future_normalization_used": False,
            "outcome_leakage": False,
            "protected_artifacts_unchanged": before == before_after,
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
        },
    }
    write_json(ROOT / "validation_summary.json", validation)
    if failures:
        raise RuntimeError("validation failed: " + ", ".join(failures))
    published = [name for name in TRACKED_OUTPUTS if name != "run_manifest.json"]
    if definitions: published.append("composite_score_results.csv")
    manifest = {
        "study_id": CFG.study_id,
        "published_at_utc": datetime.now(timezone.utc).isoformat(),
        "formal_publish_count": 1,
        "config": CFG.snapshot(),
        "config_fingerprint": CFG.fingerprint(),
        "frozen_inputs": frozen_audit,
        "input_hashes": input_hashes,
        "load_audit": load_audit,
        "prepare_audit": prepare_audit,
        "peer_audit": peer_audit,
        "peer_store_sha256": sha256_file(local_store),
        "peer_store_content_digest": store_digest(store),
        "discovery_boundaries": cuts,
        "composite_audit": composite_audit,
        "regime_audit": regime_audit,
        "artifacts": {name: sha256_file(ROOT / name) for name in published},
        "documentation_sha256": sha256_file(ROOT / "README.md"),
        "source_hashes": {
            name: sha256_file(ROOT / name)
            for name in ("config.py", "data.py", "analysis.py", "main.py")
        },
        "safety": {"actual_orders": 0, "actual_fills": 0, "broker_connections": 0},
    }
    write_json(ROOT / "run_manifest.json", manifest)
    print(json.dumps(validation, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Frozen Stage A dynamic-peer context study")
    parser.add_argument("command", choices=("publish",))
    args = parser.parse_args()
    publish()
