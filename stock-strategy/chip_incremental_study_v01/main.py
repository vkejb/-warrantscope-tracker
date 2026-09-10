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

from conditional_path_quality_ranking_v01.analysis import date_mask, metric_summary, n_compact_rows, regime_rows, gap_rows
from conditional_path_quality_ranking_v01.ranking import cooldown_proxy
from cross_sectional_alpha_ranking_v01.data import load_market_regime
from cross_sectional_alpha_ranking_v01.preprocessing import purged_training_mask
from surge_event_study_v01.data import sha256_file

from .analysis import (
    ablation_rows, comparison_rows, complete_pool_dates, discovery_diagnostics,
    frequency_rows, incremental_rows, lag_rows, rank_scores,
)
from .config import CFG, CHIP_FEATURES, INSTITUTIONAL_FEATURES, MARGIN_FEATURES
from .data import array_digest, load_frozen_research, protected_hashes
from .models import ALL_MODEL_FEATURES, fit_chip_logistic, frozen_ohlcv_feature_matrix
from .notifications import audit_runtime_cache, finalize_batch, test_notification, write_runtime_checkpoint
from .pit import archive_trading_dates, build_chip_features, load_needed_volumes, needed_codes
from .sources import SOURCE_ORDER, download_official_chip_store, phase0_audit_rows


CSV_OUTPUTS = (
    "chip_data_availability_audit.csv", "discovery_chip_diagnostics.csv",
    "chip_family_ablation.csv", "model_comparison.csv", "chip_topk_summary.csv",
    "chip_year_summary.csv", "chip_period_summary.csv", "incremental_value_summary.csv",
    "mfe_retention_summary.csv", "mae_improvement_summary.csv",
    "publication_lag_sensitivity.csv", "regime_diagnostics.csv",
    "cooldown_trade_proxy_summary.csv",
)
JSON_OUTPUTS = (
    "chip_source_manifest.json", "chip_feature_spec.json", "pit_lag_rules.json",
    "validation_summary.json", "run_manifest.json",
)
TRACKED = CSV_OUTPUTS + JSON_OUTPUTS


def run_to_completed_target(download_once, starting_completed: int, target_completed: int,
                            max_source_requests: int) -> dict:
    if target_completed < starting_completed or max_source_requests < 1:
        raise ValueError("invalid completed-pair target orchestration")
    current = starting_completed
    result = None
    while current < target_completed:
        request_cap = min(max_source_requests, target_completed - current)
        result = download_once(request_cap)
        updated = int(result.get("complete_source_date_pairs", current))
        if updated < current:
            raise RuntimeError("completed-pair count moved backwards")
        current = updated
        if current >= target_completed or result.get("status") == "COMPLETE":
            break
        if result.get("stop_reason") != "BOUNDED_BATCH_LIMIT_REACHED":
            break
    if result is None:
        raise RuntimeError("target orchestration made no acquisition attempt")
    return result


def cached_pair_count(cache_dir: Path) -> int:
    return sum(
        1
        for source in SOURCE_ORDER
        for entry in ((cache_dir / "entries" / source).iterdir() if (cache_dir / "entries" / source).exists() else ())
        if entry.is_dir() and (entry / "raw.json").exists() and (entry / "parsed.json").exists()
    )


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise RuntimeError(f"refusing empty output {path.name}")
    fields = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def git_head(repo: Path) -> str | None:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None


def load_context(stock_strategy: Path):
    arrays, frozen, audit = load_frozen_research(stock_strategy)
    pool = arrays["stage_a_ranks"] <= CFG.stage_a_pool_size
    return arrays, frozen, audit, pool


def command_download(args, stock_strategy: Path, package: Path) -> int:
    arrays, _frozen, _audit, pool = load_context(stock_strategy)
    input_dir = stock_strategy / "winner_coverage_taxonomy_v01/runtime/input"
    full_calendar = archive_trading_dates(input_dir, 20190101, 20251231)
    first = needed_codes(
        arrays["meta"]["signal_date"], arrays["meta"]["stock_code"], pool, 1,
        calendar_dates=full_calendar,
    )
    second = needed_codes(
        arrays["meta"]["signal_date"], arrays["meta"]["stock_code"], pool, 2,
        calendar_dates=full_calendar,
    )
    needed = {date: set(codes) for date, codes in first.items()}
    for date, codes in second.items():
        needed.setdefault(date, set()).update(codes)
    formal_dates = np.unique(arrays["meta"]["signal_date"]).astype(np.int32)
    for date in formal_dates:
        needed.setdefault(int(date), set())
    runtime = package / "runtime"
    runtime.mkdir(exist_ok=True)
    cache_dir = runtime / "official_cache"

    def download_once(request_cap):
        return download_official_chip_store(
            np.asarray(sorted(needed), dtype=np.int32), needed,
            runtime / "chip_daily_store.npz", runtime / "chip_raw_manifest.json",
            cache_dir=cache_dir,
            request_interval_seconds=args.request_interval_seconds,
            max_attempts=args.max_attempts,
            initial_backoff_seconds=args.initial_backoff_seconds,
            max_source_requests=request_cap,
        )
    if args.target_completed_pairs is None:
        manifest = download_once(args.max_source_requests)
    else:
        starting = cached_pair_count(cache_dir)
        preflight_errors = audit_runtime_cache(cache_dir, {"complete_source_date_pairs": starting})
        if preflight_errors:
            raise RuntimeError(f"target orchestration preflight failed: {preflight_errors[:3]}")
        manifest = run_to_completed_target(
            download_once, starting, args.target_completed_pairs, args.max_source_requests
        )
    integrity_errors = audit_runtime_cache(runtime / "official_cache", manifest)
    checkpoint = write_runtime_checkpoint(runtime, manifest, integrity_errors)
    notify_target = args.notify_target_pairs or args.target_completed_pairs
    finalize_batch(runtime, manifest, notify_target, integrity_errors, checkpoint.exists())
    if manifest.get("status") != "COMPLETE":
        print(json.dumps({
            "status": "CHECKPOINT_SAVED",
            "download_progress": manifest,
            "formal_study_run": False,
            "model_fit_count": 0,
        }, ensure_ascii=False))
        return 0
    volumes, volume_audit = load_needed_volumes(
        input_dir, needed
    )
    keys = np.asarray([(date, code, value) for (date, code), value in sorted(volumes.items())], dtype=np.float64)
    volume_path = runtime / "chip_volume_store.npz"
    if volume_path.exists():
        with np.load(volume_path, allow_pickle=False) as existing:
            if not np.array_equal(existing["keys"], keys):
                raise RuntimeError("immutable chip volume store differs from rebuilt input")
    else:
        np.savez_compressed(volume_path, keys=keys)
    write_json(runtime / "chip_download_summary.json", {
        "official_manifest": {key: value for key, value in manifest.items() if key != "date_payloads"},
        "volume_audit": volume_audit,
        "chip_daily_store_sha256": sha256_file(runtime / "chip_daily_store.npz"),
        "chip_volume_store_sha256": sha256_file(volume_path),
        "chip_raw_manifest_sha256": sha256_file(runtime / "chip_raw_manifest.json"),
    })
    print(json.dumps({"status": "DOWNLOADED", "dates": len(needed), "volume_audit": volume_audit}, ensure_ascii=False))
    return 0


def command_phase0(package: Path, repo: Path) -> int:
    target = package / "checkpoints/phase0"
    if target.exists():
        raise FileExistsError("refusing to overwrite immutable Phase 0 checkpoint")
    target.mkdir(parents=True)
    rows = phase0_audit_rows()
    write_csv(target / "chip_data_availability_audit.csv", rows)
    source_manifest = {
        "study_id": CFG.study_id,
        "checkpoint": "PHASE_0_POINT_IN_TIME_AUDIT",
        "official_only": True,
        "audit_date": "2026-09-09",
        "verified_historical_sample_date": "2020-01-02",
        "verified_samples": [
            {"source": "TWSE_T86", "url": "https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date=20200102&selectType=ALLBUT0999", "payload_sha256": "e9cd2da51ff2eea433184fa4b01059545ca833459f82a195fb2ec8f1703d8a53"},
            {"source": "TPEX_3ITRADE_HEDGE", "url": "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&o=json&se=EW&t=D&d=109%2F01%2F02&s=0%2Casc", "payload_sha256": "4843c0551d488eec1628f8907785f55be70dc2c03e8c9a5bd951f9c4630bdfdd"},
            {"source": "TWSE_MI_MARGN", "url": "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&date=20200102&selectType=ALL", "payload_sha256": "ab3a717d442547164105c3f33c5c888398e0678d2651b05712259c57e70d991e"},
            {"source": "TPEX_MARGIN_BALANCE", "url": "https://www.tpex.org.tw/www/zh-tw/margin/balance?date=2020%2F01%2F02&id=&response=json", "payload_sha256": "95587508c0a4605d698523766d89be66a995ba371ead51d9315401ce261a3605"},
        ],
        "bulk_acquisition": {
            "status": "PENDING",
            "reason": "Official public endpoints applied CDN Anti-DDoS throttling during the 2020-2025 bulk acquisition attempt; no partial payload was accepted as a research archive.",
            "third_party_fallback_used": False,
            "partial_history_used": False,
        },
    }
    write_json(target / "chip_source_manifest.json", source_manifest)
    write_json(target / "pit_lag_rules.json", {
        "historical_rule": "source_date <= previous_market_session(signal_date)",
        "required_lag_sessions": 1,
        "same_day_historical_use": False,
        "snapshot_backfill": False,
        "reason": "Historical report date does not prove same-day public availability; next-session lag is conservative.",
    })
    validation = {
        "study_id": CFG.study_id,
        "checkpoint_status": "PHASE_0_COMPLETE_PHASE_1_PENDING",
        "final_classification": None,
        "pit_usable_families": ["INSTITUTIONAL", "MARGIN_SHORT"],
        "pit_usable_with_lag_families": ["INSTITUTIONAL", "MARGIN_SHORT"],
        "rejected_or_unavailable_families": ["SECURITIES_LENDING", "TDCC_OWNERSHIP", "BROKER_BRANCH"],
        "model_fit_count": 0,
        "later_period_refit_count": 0,
        "official_bulk_archive_complete": False,
        "formal_study_run": False,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    write_json(target / "validation_summary.json", validation)
    artifacts = {
        path.name: sha256_file(path)
        for path in sorted(target.iterdir()) if path.name != "checkpoint_manifest.json"
    }
    write_json(target / "checkpoint_manifest.json", {
        "status": "CHECKPOINT",
        "study_id": CFG.study_id,
        "phase": "PHASE_0_POINT_IN_TIME_AUDIT",
        "source_commit_before_checkpoint": git_head(repo),
        "config_hash": CFG.fingerprint(),
        "artifact_sha256": artifacts,
        "resume_requirement": "Acquire complete official 2020-2025 TWSE and TPEx daily institutional and margin/short archives under rate-safe conditions, then run tests and exactly one formal publish.",
        "not_complete": True,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    })
    print(json.dumps(validation, ensure_ascii=False))
    return 0


def _fit(arrays, base_features, chip_features, common_pool):
    dates = arrays["meta"]["signal_date"]
    target = arrays["path_success"]
    evaluable = arrays["meta"]["outcome_evaluable"]
    combined = np.column_stack((base_features, chip_features))
    folds = (
        ("TRAIN_2020_EVAL_2021", "20200101", "20201231", "20210101", "20211231"),
        ("TRAIN_2020_2021_EVAL_2022", "20200101", "20211231", "20220101", "20221231"),
    )
    rows = []
    mean_auc = {value: [] for value in CFG.c_candidates}
    for label, train_start, train_end, eval_start, eval_end in folds:
        train, purge = purged_training_mask(dates, evaluable & common_pool, train_start, train_end, CFG.label_purge_sessions)
        evaluation = date_mask(arrays["meta"], eval_start, eval_end) & common_pool & evaluable
        for candidate in CFG.c_candidates:
            model = fit_chip_logistic(combined, target, train, candidate, label, ALL_MODEL_FEATURES)
            probability = model.predict_proba(combined)
            from conditional_path_quality_ranking_v01.analysis import binary_auc
            auc = binary_auc(target[evaluation], probability[evaluation])
            mean_auc[candidate].append(-1.0 if auc is None else auc)
            rows.append({
                "fold": label, "regularization_c": candidate, "selected": False,
                "training_observations": model.training_observations,
                "evaluation_observations": int(np.count_nonzero(evaluation)),
                "evaluation_auc": auc, "purged_tail_rows": purge["purged_tail_rows"],
            })
    selected = min(CFG.c_candidates, key=lambda value: (-float(np.mean(mean_auc[value])), value))
    for row in rows:
        row["selected"] = row["regularization_c"] == selected
    final_mask, purge = purged_training_mask(
        dates, evaluable & common_pool, CFG.discovery_start, CFG.discovery_end, CFG.label_purge_sessions
    )
    primary = fit_chip_logistic(combined, target, final_mask, selected, "HISTORICAL_DISCOVERY_2020_2022", ALL_MODEL_FEATURES)
    return primary, rows, {"selected_c": selected, "candidates": list(CFG.c_candidates), "fold_auc": mean_auc, "final_purge": purge, "later_period_refit_count": 0}, combined, final_mask


def _topk_rows(arrays, ranking, common_pool):
    rows = []
    from conditional_path_quality_ranking_v01.analysis import time_slices
    for kind, label, start, end in time_slices():
        period = date_mask(arrays["meta"], start, end)
        for k in (3, 5, 10):
            rows.append({"time_slice_type": kind, "time_slice": label, "top_k": k, "primary_selection": k == 5, **metric_summary(arrays, period & common_pool & (ranking["rank"] > 0) & (ranking["rank"] <= k))})
    return rows


def _update_audit_coverage(rows, pit_audit):
    for row in rows:
        if row["feature_family"] == "INSTITUTIONAL":
            row["coverage_pct"] = pit_audit["institutional_coverage_pct"]
            row["missing_pct"] = 1.0 - row["coverage_pct"]
        elif row["feature_family"] == "MARGIN_SHORT":
            row["coverage_pct"] = pit_audit["margin_coverage_pct"]
            row["missing_pct"] = 1.0 - row["coverage_pct"]


def command_publish(args, repo: Path, stock_strategy: Path, package: Path) -> int:
    existing = [name for name in TRACKED if (args.output_dir / name).exists()]
    if existing:
        raise FileExistsError("refusing to overwrite published study: " + ", ".join(existing))
    protected_before = protected_hashes(stock_strategy)
    arrays, frozen, source_audit, pool = load_context(stock_strategy)
    runtime = package / "runtime"
    chip_path = runtime / "chip_daily_store.npz"
    volume_path = runtime / "chip_volume_store.npz"
    raw_manifest_path = runtime / "chip_raw_manifest.json"
    if not all(path.exists() for path in (chip_path, volume_path, raw_manifest_path)):
        raise FileNotFoundError("Phase 1 source stores absent; run download-official first")
    with np.load(chip_path, allow_pickle=False) as payload:
        chip_daily = payload["chip_daily"].copy()
    with np.load(volume_path, allow_pickle=False) as payload:
        volume_rows = payload["keys"]
        volumes = {(int(row[0]), int(row[1])): float(row[2]) for row in volume_rows}
    normal, pit_audit = build_chip_features(arrays["meta"], pool, chip_daily, volumes, 1)
    extra, extra_audit = build_chip_features(arrays["meta"], pool, chip_daily, volumes, 2)
    common_pool = complete_pool_dates(arrays["meta"], normal["chip_valid"], pool)
    extra_pool = complete_pool_dates(arrays["meta"], extra["chip_valid"], pool)
    if np.count_nonzero(common_pool) < 10_000:
        raise RuntimeError("insufficient complete PIT Stage A pool")
    base_features = frozen_ohlcv_feature_matrix(arrays, frozen)
    primary, walkforward, fit_audit, combined, final_mask = _fit(
        arrays, base_features, normal["transformed_chip_features"], common_pool
    )
    probability = primary.predict_proba(combined)
    chip_ranking = rank_scores(probability, common_pool, arrays["meta"])
    base_probability = arrays["ohlcv_conditional_probabilities"]
    base_ranking = rank_scores(base_probability, common_pool, arrays["meta"])
    raw_top5 = common_pool & (chip_ranking["rank"] > 0) & (chip_ranking["rank"] <= 5)
    cooldown, cooldown_audit = cooldown_proxy(raw_top5, chip_ranking["rank"], arrays["meta"]["signal_date"], arrays["meta"]["stock_code"], CFG.cooldown_sessions)

    pools = {
        "COMMON_STAGE_A_TOP30": common_pool,
        "OHLCV_TOP5_COMMON_DATES": common_pool & (base_ranking["rank"] > 0) & (base_ranking["rank"] <= 5),
        "CHIP_TOP5": raw_top5,
    }
    scores = {"OHLCV_TOP5_COMMON_DATES": base_probability, "CHIP_TOP5": probability}
    model_comparison = comparison_rows(arrays, pools, scores)
    incremental = incremental_rows(model_comparison)
    topk = _topk_rows(arrays, chip_ranking, common_pool)

    family_rankings = {}
    family_models = {}
    slices = {
        "INSTITUTIONAL_ONLY": slice(0, len(INSTITUTIONAL_FEATURES)),
        "MARGIN_SHORT_ONLY": slice(len(INSTITUTIONAL_FEATURES), len(CHIP_FEATURES)),
        "ALL_CHIP": slice(0, len(CHIP_FEATURES)),
    }
    for family, feature_slice in slices.items():
        matrix = np.column_stack((base_features, normal["transformed_chip_features"][:, feature_slice]))
        names = tuple(frozen.feature_names) + tuple(CHIP_FEATURES[feature_slice])
        model = fit_chip_logistic(matrix, arrays["path_success"], final_mask, primary.regularization_c, "HISTORICAL_DISCOVERY_2020_2022", names)
        family_models[family] = model
        family_rankings[family] = rank_scores(model.predict_proba(matrix), common_pool, arrays["meta"])["rank"]
    ablation = ablation_rows(arrays, family_rankings, common_pool)

    extra_combined = np.column_stack((base_features, extra["transformed_chip_features"]))
    extra_probability = primary.predict_proba(extra_combined)
    extra_ranking = rank_scores(extra_probability, extra_pool, arrays["meta"])
    lag_sensitivity = lag_rows(arrays, chip_ranking["rank"], extra_ranking["rank"], common_pool, extra_pool)

    diagnostics = discovery_diagnostics(
        normal["raw_chip_features"], normal["transformed_chip_features"],
        CHIP_FEATURES, arrays, common_pool,
    )
    regimes, regime_audit = load_market_regime(
        stock_strategy, arrays["meta"]["signal_date"], stock_strategy / "winner_coverage_taxonomy_v01/run_manifest.json"
    )
    regime = regime_rows(arrays, chip_ranking["rank"], regimes)
    gap = gap_rows(arrays, chip_ranking["rank"])
    compact = n_compact_rows(arrays, chip_ranking["rank"])
    frequency = frequency_rows(arrays, raw_top5, cooldown)

    period_primary = {row["time_slice"]: row for row in model_comparison if row["time_slice_type"] == "PERIOD" and row["cohort"] == "CHIP_TOP5"}
    period_base = {row["time_slice"]: row for row in model_comparison if row["time_slice_type"] == "PERIOD" and row["cohort"] == "OHLCV_TOP5_COMMON_DATES"}
    later = ("2023_2024_RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", "2025_STRESS_PREVALENCE_SEEN_NOT_BLIND")
    gates = {}
    for label in later:
        chip, base = period_primary[label], period_base[label]
        stage = next(row for row in model_comparison if row["time_slice_type"] == "PERIOD" and row["time_slice"] == label and row["cohort"] == "COMMON_STAGE_A_TOP30")
        gates[label] = {
            "success_gt_ohlcv": chip["path_success_rate"] > base["path_success_rate"],
            "downside_lt_ohlcv": chip["downside_first_rate"] < base["downside_first_rate"],
            "mae_better_than_ohlcv": chip["mae10_mean"] > base["mae10_mean"],
            "mfe_retention_gte_80pct": chip["mfe10_mean"] / stage["mfe10_mean"] >= CFG.minimum_mfe_retention,
            "net_mean_positive": chip["net_mean"] > 0,
            "net_pf_gt_1": chip["net_profit_factor"] > 1,
            "top1_removed_net_positive": chip["top1_removed_net_mean"] > 0,
            "best_date_removed_net_positive": chip["remove_best_signal_date_net_mean"] > 0,
            "best_month_removed_net_positive": chip["remove_best_calendar_month_net_mean"] > 0,
        }
    all_gate = all(all(values.values()) for values in gates.values())
    path_incremental = all(
        gates[label]["success_gt_ohlcv"] and gates[label]["downside_lt_ohlcv"] and gates[label]["mae_better_than_ohlcv"]
        for label in later
    )
    classification = "PROMISING_FOR_CHIP_PROSPECTIVE_SHADOW" if all_gate else ("CHIP_INCREMENTAL_SIGNAL_BUT_NOT_TRADEABLE" if path_incremental else "NO_INCREMENTAL_CHIP_EDGE")
    validation = {
        "study_id": CFG.study_id, "final_classification": classification,
        "phase0_pit_audit_pass": True, "promotion_gates": gates,
        "later_period_refit_count": 0, "stage_a_refit_count": 0,
        "frozen_ohlcv_refit_count": 0, "common_observation_keys": int(np.count_nonzero(common_pool)),
        "pit_feature_audit": pit_audit, "extra_lag_audit": extra_audit,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
    }
    audit_rows = phase0_audit_rows()
    _update_audit_coverage(audit_rows, pit_audit)
    raw_manifest = json.loads(raw_manifest_path.read_text(encoding="utf-8"))
    source_manifest = {
        "study_id": CFG.study_id, "official_only": True,
        "download_summary": json.loads((runtime / "chip_download_summary.json").read_text(encoding="utf-8")),
        "raw_manifest_sha256": sha256_file(raw_manifest_path),
        "date_payload_count": len(raw_manifest["date_payloads"]),
        "source_urls": sorted({entry["url"].split("?")[0] for day in raw_manifest["date_payloads"] for entry in day["sources"].values()}),
    }
    protected_after = protected_hashes(stock_strategy)
    if protected_before != protected_after:
        raise RuntimeError("protected frozen research or prospective ledger changed")

    staging = Path(tempfile.mkdtemp(prefix=".chip_publish_", dir=args.output_dir.parent))
    try:
        write_csv(staging / "chip_data_availability_audit.csv", audit_rows)
        write_csv(staging / "discovery_chip_diagnostics.csv", diagnostics)
        write_csv(staging / "chip_family_ablation.csv", ablation)
        write_csv(staging / "model_comparison.csv", model_comparison)
        write_csv(staging / "chip_topk_summary.csv", topk)
        write_csv(staging / "chip_year_summary.csv", [row for row in topk if row["time_slice_type"] == "YEAR" and row["top_k"] == 5])
        write_csv(staging / "chip_period_summary.csv", [row for row in topk if row["time_slice_type"] == "PERIOD" and row["top_k"] == 5])
        write_csv(staging / "incremental_value_summary.csv", incremental)
        write_csv(staging / "mfe_retention_summary.csv", [{"time_slice_type": row["time_slice_type"], "time_slice": row["time_slice"], "mfe_retention": row["mfe_retention_vs_stage_a"]} for row in incremental])
        write_csv(staging / "mae_improvement_summary.csv", [{"time_slice_type": row["time_slice_type"], "time_slice": row["time_slice"], "mae_improvement": row["mae_improvement_vs_stage_a"]} for row in incremental])
        write_csv(staging / "publication_lag_sensitivity.csv", lag_sensitivity)
        write_csv(staging / "regime_diagnostics.csv", regime)
        write_csv(staging / "cooldown_trade_proxy_summary.csv", frequency)
        write_json(staging / "chip_source_manifest.json", source_manifest)
        write_json(staging / "chip_feature_spec.json", {
            "study_id": CFG.study_id, "features": list(CHIP_FEATURES),
            "normalization": "institutional net shares / source-date traded shares; margin balance delta shares / source-date traded shares",
            "transform": "same-day Stage A Top30 percentile minus 0.5; unavailable margin values neutral with availability flag",
            "feature_family_models": {name: model.payload() for name, model in family_models.items()},
            "primary_model": primary.payload(), "primary_model_fingerprint": primary.fingerprint(),
        })
        write_json(staging / "pit_lag_rules.json", {
            "decision_cutoff": "T_CLOSE_RESEARCH_DECISION_FOR_T_PLUS_1_OPEN",
            "historical_rule": "source_date <= previous_market_session(T)",
            "required_lag_sessions": 1, "sensitivity_extra_lag_sessions": 1,
            "same_day_historical_use": False, "snapshot_backfill": False,
        })
        write_json(staging / "validation_summary.json", validation)
        artifacts = {name: sha256_file(staging / name) for name in TRACKED if name != "run_manifest.json"}
        manifest = {
            "status": "COMPLETE", "study_id": CFG.study_id,
            "result_status": classification, "source_commit_before_run": git_head(repo),
            "config_hash": CFG.fingerprint(), "source_audit": source_audit,
            "input_array_digest": array_digest(arrays), "fit_audit": fit_audit,
            "pit_audit": pit_audit, "regime_audit": regime_audit,
            "cooldown_audit": cooldown_audit, "protected_hashes_before": protected_before,
            "protected_hashes_after": protected_after, "artifact_sha256": artifacts,
            "local_runtime": {
                "chip_daily_store_sha256": sha256_file(chip_path),
                "chip_volume_store_sha256": sha256_file(volume_path),
                "raw_manifest_sha256": sha256_file(raw_manifest_path),
            },
            "pipeline_validation": {
                "pit_audit_pass": True, "no_snapshot_backfill": True,
                "frozen_stage_a_exact_reuse": True, "stage_a_refit_count": 0,
                "frozen_ohlcv_baseline_exact_reuse": True, "frozen_ohlcv_refit_count": 0,
                "same_observation_keys": True, "later_period_refit_count": 0,
                "prospective_ledger_unchanged": protected_before == protected_after,
                "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
            },
            "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
        }
        write_json(staging / "run_manifest.json", manifest)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for name in TRACKED:
            shutil.move(staging / name, args.output_dir / name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(json.dumps({"status": "COMPLETE", "classification": classification, "selected_c": primary.regularization_c, "actual_orders": 0, "actual_fills": 0, "broker_connections": 0}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    package = Path(__file__).resolve().parent
    stock_strategy = package.parent
    repo = stock_strategy.parent
    parser = argparse.ArgumentParser(description="PIT chip incremental study")
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit-sources")
    download = sub.add_parser("download-official")
    download.add_argument("--request-interval-seconds", type=float, default=5.0)
    download.add_argument("--max-attempts", type=int, default=2)
    download.add_argument("--initial-backoff-seconds", type=float, default=30.0)
    download.add_argument("--max-source-requests", type=int, default=20)
    download.add_argument("--notify-target-pairs", type=int)
    download.add_argument("--target-completed-pairs", type=int)
    sub.add_parser("test-notification")
    sub.add_parser("publish-phase0-checkpoint")
    publish = sub.add_parser("publish")
    publish.add_argument("--output-dir", type=Path, default=package)
    args = parser.parse_args(argv)
    if args.command == "audit-sources":
        print(json.dumps(phase0_audit_rows(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "download-official":
        return command_download(args, stock_strategy, package)
    if args.command == "test-notification":
        ok = test_notification(package / "runtime")
        print("NOTIFICATION_TEST_SENT" if ok else "NOTIFICATION_TEST_FAILED")
        return 0 if ok else 1
    if args.command == "publish-phase0-checkpoint":
        return command_phase0(package, repo)
    return command_publish(args, repo, stock_strategy, package)


if __name__ == "__main__":
    raise SystemExit(main())
