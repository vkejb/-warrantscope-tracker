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

from cross_sectional_alpha_ranking_v01.data import load_market_regime
from cross_sectional_alpha_ranking_v01.preprocessing import purged_training_mask
from surge_event_study_v01.data import sha256_file

from .analysis import (
    calibration_rows,
    comparison_rows,
    conditional_topk_rows,
    cooldown_rows,
    daily_path_rows,
    date_mask,
    frequency_rows,
    gap_rows,
    metric_summary,
    n_compact_rows,
    previous_stage_b_comparison_rows,
    probability_metrics,
    regime_rows,
    retention_rows,
    top_bottom_rows,
)
from .config import C_CANDIDATES, CFG, CONDITIONAL_FEATURE_NAMES
from .data import array_digest, load_reused_inputs, protected_hashes
from .models import (
    build_conditional_features,
    coefficient_cosine,
    fit_ic_weighted_path,
    fit_logistic_ridge,
)
from .ranking import conditional_ranks, cooldown_proxy
from .validation import build_validation


TRACKED = (
    "conditional_model_spec.json",
    "conditional_feature_spec.json",
    "discovery_walkforward.csv",
    "conditional_coefficients.csv",
    "conditional_topk_summary.csv",
    "conditional_year_summary.csv",
    "conditional_period_summary.csv",
    "calibration_summary.csv",
    "top_bottom_separation.csv",
    "mfe_retention_summary.csv",
    "mae_improvement_summary.csv",
    "comparison_vs_previous_stage_b.csv",
    "cooldown_trade_proxy_summary.csv",
    "frequency_summary.csv",
    "regime_diagnostics.csv",
    "gap_diagnostics.csv",
    "n_compact_overlay.csv",
    "validation_summary.json",
    "run_manifest.json",
)


def _write_json(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise RuntimeError(f"refusing empty output: {path.name}")
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _git_head(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _evaluation(
    arrays: dict[str, np.ndarray],
    scores: np.ndarray,
    probabilities: np.ndarray,
    start: str,
    end: str,
) -> dict:
    pool = arrays["stage_a_ranks"] <= CFG.stage_a_pool_size
    ranking, _ = conditional_ranks(
        scores, pool, arrays["meta"]["signal_date"], arrays["meta"]["stock_code"]
    )
    period = date_mask(arrays["meta"], start, end)
    daily = [
        row for row in daily_path_rows("EVALUATION", scores, pool, arrays)
        if int(start) <= row["signal_date"] <= int(end)
    ]
    ic = np.asarray([
        np.nan if row["daily_spearman_ic_path_success"] is None else row["daily_spearman_ic_path_success"]
        for row in daily
    ], dtype=np.float64)
    pool_eval = period & pool & arrays["meta"]["outcome_evaluable"]
    top5 = period & (ranking["rank"] > 0) & (ranking["rank"] <= 5)
    return {
        "mean_daily_path_ic": float(np.nanmean(ic)),
        "median_daily_path_ic": float(np.nanmedian(ic)),
        "path_ic_positive_day_rate": float(np.mean(ic[np.isfinite(ic)] > 0)),
        "probability_metrics": probability_metrics(
            arrays["path_success"][pool_eval], probabilities[pool_eval]
        ),
        "stage_a_pool": metric_summary(arrays, period & pool),
        "conditional_top5": metric_summary(arrays, top5),
    }


def _fit_models(arrays: dict[str, np.ndarray]) -> tuple[object, object, list[object], list[dict], dict, np.ndarray]:
    dates = arrays["meta"]["signal_date"]
    evaluable = arrays["meta"]["outcome_evaluable"]
    pool = arrays["stage_a_ranks"] <= CFG.stage_a_pool_size
    target = arrays["path_success"]
    folds = (
        ("TRAIN_2020_EVALUATE_2021", "20200101", "20201231", "20210101", "20211231"),
        ("TRAIN_2020_2021_EVALUATE_2022", "20200101", "20211231", "20220101", "20221231"),
    )
    rows: list[dict] = []
    c_ic = {candidate: [] for candidate in C_CANDIDATES}
    models_by_key = {}
    preprocessing_by_key = {}
    purge_audit = {}
    for label, train_start, train_end, eval_start, eval_end in folds:
        train, purge = purged_training_mask(
            dates, evaluable & pool, train_start, train_end, CFG.label_purge_sessions
        )
        purge_audit[label] = purge
        features, preprocessing = build_conditional_features(
            arrays["transformed_features"], arrays["stage_a_scores"],
            arrays["stage_a_ranks"], dates, train,
        )
        preprocessing_by_key[label] = preprocessing
        for candidate in C_CANDIDATES:
            model = fit_logistic_ridge(
                features, target, train, candidate, label, preprocessing
            )
            models_by_key[(label, candidate)] = model
            probability = model.predict_proba(features)
            evaluation = _evaluation(arrays, probability, probability, eval_start, eval_end)
            c_ic[candidate].append(evaluation["mean_daily_path_ic"])
            rows.append({
                "fold": label,
                "model": model.name,
                "regularization_c": candidate,
                "c_selected": False,
                "train_start": train_start,
                "train_end": train_end,
                "evaluation_start": eval_start,
                "evaluation_end": eval_end,
                "selection_metric": "MEAN_DAILY_SPEARMAN_IC_PATH_SUCCESS",
                "training_observations": model.training_observations,
                "iterations": model.iterations,
                "mean_daily_path_ic": evaluation["mean_daily_path_ic"],
                "median_daily_path_ic": evaluation["median_daily_path_ic"],
                "path_ic_positive_day_rate": evaluation["path_ic_positive_day_rate"],
                **{f"pool_{key}": value for key, value in evaluation["probability_metrics"].items()},
                "stage_a_pool_success_rate": evaluation["stage_a_pool"]["path_success_rate"],
                "top5_success_rate": evaluation["conditional_top5"]["path_success_rate"],
                "top5_downside_first_rate": evaluation["conditional_top5"]["downside_first_rate"],
                "top5_mfe10": evaluation["conditional_top5"]["mfe10_mean"],
                "top5_mae10": evaluation["conditional_top5"]["mae10_mean"],
                "top5_net_mean": evaluation["conditional_top5"]["net_mean"],
            })
        benchmark, _ = fit_ic_weighted_path(
            features, target, dates, train, label, preprocessing
        )
        score = benchmark.decision_function(features)
        evaluation = _evaluation(arrays, score, np.full(len(score), np.nan), eval_start, eval_end)
        rows.append({
            "fold": label,
            "model": benchmark.name,
            "regularization_c": None,
            "c_selected": None,
            "train_start": train_start,
            "train_end": train_end,
            "evaluation_start": eval_start,
            "evaluation_end": eval_end,
            "selection_metric": "BENCHMARK_NOT_USED_FOR_C_SELECTION",
            "training_observations": benchmark.training_observations,
            "iterations": 0,
            "mean_daily_path_ic": evaluation["mean_daily_path_ic"],
            "median_daily_path_ic": evaluation["median_daily_path_ic"],
            "path_ic_positive_day_rate": evaluation["path_ic_positive_day_rate"],
            "pool_observations": evaluation["stage_a_pool"]["evaluable_observations"],
            "stage_a_pool_success_rate": evaluation["stage_a_pool"]["path_success_rate"],
            "top5_success_rate": evaluation["conditional_top5"]["path_success_rate"],
            "top5_downside_first_rate": evaluation["conditional_top5"]["downside_first_rate"],
            "top5_mfe10": evaluation["conditional_top5"]["mfe10_mean"],
            "top5_mae10": evaluation["conditional_top5"]["mae10_mean"],
            "top5_net_mean": evaluation["conditional_top5"]["net_mean"],
        })
    selected_c = min(
        C_CANDIDATES,
        key=lambda candidate: (-float(np.mean(c_ic[candidate])), candidate),
    )
    for row in rows:
        if row["model"] == CFG.primary_model:
            row["c_selected"] = row["regularization_c"] == selected_c
    selected_fold_models = [models_by_key[(fold[0], selected_c)] for fold in folds]
    final_mask, final_purge = purged_training_mask(
        dates, evaluable & pool, CFG.discovery_start, CFG.discovery_end,
        CFG.label_purge_sessions,
    )
    final_features, final_preprocessing = build_conditional_features(
        arrays["transformed_features"], arrays["stage_a_scores"],
        arrays["stage_a_ranks"], dates, final_mask,
    )
    primary = fit_logistic_ridge(
        final_features, target, final_mask, selected_c,
        "HISTORICAL_DISCOVERY_2020_2022", final_preprocessing,
    )
    benchmark, benchmark_audit = fit_ic_weighted_path(
        final_features, target, dates, final_mask,
        "HISTORICAL_DISCOVERY_2020_2022", final_preprocessing,
    )
    return primary, benchmark, selected_fold_models, rows, {
        "c_candidates": list(C_CANDIDATES),
        "c_fold_mean_daily_path_ic": {str(key): value for key, value in c_ic.items()},
        "selection_metric": "MEAN_OF_2021_AND_2022_WALKFORWARD_MEAN_DAILY_PATH_IC",
        "tie_break": "SMALLEST_C",
        "selected_c": selected_c,
        "fold_purge_audit": purge_audit,
        "fold_preprocessing": preprocessing_by_key,
        "final_fit_purge": final_purge,
        "final_preprocessing": final_preprocessing,
        "final_fit_count": 1,
        "later_period_refit_count": 0,
        "stage_a_refit_count": 0,
        "benchmark_audit": benchmark_audit,
    }, final_features


def _coefficient_outputs(primary, benchmark, folds: list[object]) -> tuple[list[dict], dict]:
    first, second = folds
    final = np.asarray(primary.coefficients, dtype=np.float64)
    left = np.asarray(first.coefficients, dtype=np.float64)
    right = np.asarray(second.coefficients, dtype=np.float64)
    weights = np.asarray(benchmark.coefficients, dtype=np.float64)
    order = np.argsort(-np.abs(final), kind="stable")
    rows = []
    for rank, index in enumerate(order, 1):
        rows.append({
            "absolute_rank": rank,
            "feature": CONDITIONAL_FEATURE_NAMES[index],
            "final_coefficient": final[index],
            "train_2020_coefficient": left[index],
            "train_2020_2021_coefficient": right[index],
            "walkforward_sign_agreement": bool(np.sign(left[index]) == np.sign(right[index])),
            "all_three_sign_agreement": bool(
                np.sign(final[index]) == np.sign(left[index])
                and np.sign(final[index]) == np.sign(right[index])
            ),
            "ic_weighted_benchmark_weight": weights[index],
        })
    largest = [
        {"feature": CONDITIONAL_FEATURE_NAMES[index], "coefficient": float(final[index])}
        for index in order[:10]
    ]
    stability = {
        "walkforward_sign_agreement_fraction": float(np.mean(np.sign(left) == np.sign(right))),
        "all_three_sign_agreement_fraction": float(np.mean((np.sign(final) == np.sign(left)) & (np.sign(final) == np.sign(right)))),
        "walkforward_coefficient_cosine": coefficient_cosine(first, second),
        "final_vs_train_2020_cosine": coefficient_cosine(primary, first),
        "final_vs_train_2020_2021_cosine": coefficient_cosine(primary, second),
        "largest_absolute_final_coefficients": largest,
        "largest_positive_coefficients": sorted(
            [entry for entry in largest if entry["coefficient"] > 0], key=lambda entry: -entry["coefficient"]
        ),
        "largest_negative_coefficients": sorted(
            [entry for entry in largest if entry["coefficient"] < 0], key=lambda entry: entry["coefficient"]
        ),
    }
    return rows, stability


def main(argv: list[str] | None = None) -> int:
    package = Path(__file__).resolve().parent
    stock_strategy = package.parent
    repo = stock_strategy.parent
    parser = argparse.ArgumentParser(description="Frozen Stage A conditional path-quality study")
    parser.add_argument("--output-dir", type=Path, default=package)
    parser.add_argument("--winner-store", type=Path, default=stock_strategy / "winner_coverage_taxonomy_v01/runtime/observation_store.npz")
    parser.add_argument("--cross-store", type=Path, default=stock_strategy / "cross_sectional_alpha_ranking_v01/runtime/ranking_store.npz")
    parser.add_argument("--upside-store", type=Path, default=stock_strategy / "upside_opportunity_ranking_v01/runtime/ranking_store.npz")
    parser.add_argument("--stage-a-model-spec", type=Path, default=stock_strategy / "upside_opportunity_ranking_v01/stage_a_model_spec.json")
    parser.add_argument("--stage-a-manifest", type=Path, default=stock_strategy / "upside_opportunity_ranking_v01/run_manifest.json")
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    existing = [name for name in TRACKED if (args.output_dir / name).exists()]
    if existing:
        raise FileExistsError("refusing to overwrite published output: " + ", ".join(existing))

    protected_before = protected_hashes(stock_strategy)
    arrays, stage_a_model, source_audit = load_reused_inputs(
        args.winner_store, args.cross_store, args.upside_store,
        args.stage_a_model_spec, args.stage_a_manifest,
    )
    input_digest = array_digest(arrays)
    regimes, regime_audit = load_market_regime(
        stock_strategy, arrays["meta"]["signal_date"],
        stock_strategy / "winner_coverage_taxonomy_v01/run_manifest.json",
    )
    primary, benchmark, fold_models, walkforward, fit_audit, features = _fit_models(arrays)
    pool = arrays["stage_a_ranks"] <= CFG.stage_a_pool_size
    rankings = {}
    ranking_audit = {}
    daily = []
    for model in (primary, benchmark):
        if model.name == CFG.primary_model:
            score = model.predict_proba(features)
        else:
            score = model.decision_function(features)
        ranking, audit = conditional_ranks(
            score, pool, arrays["meta"]["signal_date"], arrays["meta"]["stock_code"]
        )
        rankings[model.name] = ranking
        ranking_audit[model.name] = audit
        daily.extend(daily_path_rows(model.name, score, pool, arrays))
    primary_ranking = rankings[CFG.primary_model]
    raw_top5 = (primary_ranking["rank"] > 0) & (primary_ranking["rank"] <= CFG.primary_top_k)
    cooldown, cooldown_audit = cooldown_proxy(
        raw_top5, primary_ranking["rank"], arrays["meta"]["signal_date"],
        arrays["meta"]["stock_code"], CFG.cooldown_sessions,
    )

    topk = conditional_topk_rows(arrays, rankings, daily)
    period_summary = [row for row in topk if row["time_slice_type"] == "PERIOD" and row["model"] == CFG.primary_model and row["top_k"] == 5]
    year_summary = [row for row in topk if row["time_slice_type"] == "YEAR" and row["model"] == CFG.primary_model and row["top_k"] == 5]
    comparison = comparison_rows(arrays, primary_ranking["rank"])
    mfe_retention, mae_improvement = retention_rows(comparison)
    calibration = calibration_rows(arrays, primary_ranking)
    separation = top_bottom_rows(arrays, primary_ranking["rank"])
    previous_comparison = previous_stage_b_comparison_rows(comparison)
    cooldown_table = cooldown_rows(arrays, cooldown)
    frequency = frequency_rows(arrays, raw_top5, cooldown)
    regime = regime_rows(arrays, primary_ranking["rank"], regimes)
    gap = gap_rows(arrays, primary_ranking["rank"])
    compact = n_compact_rows(arrays, primary_ranking["rank"])
    coefficient_rows, coefficient_stability = _coefficient_outputs(primary, benchmark, fold_models)
    validation = build_validation(
        comparison, calibration, separation, frequency, compact, regime, gap,
        coefficient_stability, primary.regularization_c,
    )

    protected_after = protected_hashes(stock_strategy)
    if protected_before != protected_after:
        raise RuntimeError("protected module, scheduler, or prospective ledger changed during study")
    if fit_audit["later_period_refit_count"] != 0 or fit_audit["stage_a_refit_count"] != 0:
        raise RuntimeError("Stage A or later-period refit invariant failed")

    source_hashes = {
        str(path.relative_to(stock_strategy)): sha256_file(path)
        for path in sorted(package.glob("*.py"))
    }
    output_parent = args.output_dir.parent
    staging = Path(tempfile.mkdtemp(prefix=".conditional_path_publish_", dir=output_parent))
    try:
        feature_spec = {
            "study_id": CFG.study_id,
            "features": list(CONDITIONAL_FEATURE_NAMES),
            "base_feature_source": "cross_sectional_alpha_ranking_v01 published same-day percentile matrix",
            "base_feature_transform": "same-day percentile minus 0.5",
            "context_features": {
                "frozen_stage_a_upside_score_z": "Stage A score z-scored with discovery training mask only",
                "frozen_stage_a_rank_percentile": "published same-day full-market Stage A rank percentile minus 0.5",
            },
            "feature_timestamp": "T close or earlier",
            "later_period_refit_count": 0,
        }
        model_spec = {
            "study_id": CFG.study_id,
            "primary_target": "PATH_SUCCESS_8_BEFORE_5; TIMEOUT_IS_NON_SUCCESS",
            "stage_a_contract": {
                "model": stage_a_model.payload(),
                "fingerprint": stage_a_model.fingerprint(),
                "pool": "EXACT_PUBLISHED_STAGE_A_RANK_LE_30",
                "refit_count": 0,
            },
            "primary_model": primary.payload(),
            "primary_model_fingerprint": primary.fingerprint(),
            "benchmark_model": benchmark.payload(),
            "benchmark_model_fingerprint": benchmark.fingerprint(),
            "fit_audit": fit_audit,
            "coefficient_stability": coefficient_stability,
        }
        _write_json(staging / "conditional_model_spec.json", model_spec)
        _write_json(staging / "conditional_feature_spec.json", feature_spec)
        csv_outputs = {
            "discovery_walkforward.csv": walkforward,
            "conditional_coefficients.csv": coefficient_rows,
            "conditional_topk_summary.csv": topk,
            "conditional_year_summary.csv": year_summary,
            "conditional_period_summary.csv": period_summary,
            "calibration_summary.csv": calibration,
            "top_bottom_separation.csv": separation,
            "mfe_retention_summary.csv": mfe_retention,
            "mae_improvement_summary.csv": mae_improvement,
            "comparison_vs_previous_stage_b.csv": previous_comparison,
            "cooldown_trade_proxy_summary.csv": cooldown_table,
            "frequency_summary.csv": frequency,
            "regime_diagnostics.csv": regime,
            "gap_diagnostics.csv": gap,
            "n_compact_overlay.csv": compact,
        }
        for name, rows in csv_outputs.items():
            _write_csv(staging / name, rows)
        _write_json(staging / "validation_summary.json", validation)
        runtime = staging / "runtime"
        runtime.mkdir()
        np.savez_compressed(
            runtime / "conditional_store.npz",
            meta=arrays["meta"], outcomes=arrays["outcomes"],
            descriptive_outcomes=arrays["descriptive_outcomes"],
            path_class=arrays["path_class"], stage_a_scores=arrays["stage_a_scores"],
            stage_a_ranks=arrays["stage_a_ranks"], stage_a_pool=pool,
            conditional_probabilities=primary_ranking["scores"],
            conditional_ranks=primary_ranking["rank"],
            conditional_probability_deciles=primary_ranking["probability_decile"],
            conditional_cooldown=cooldown,
            previous_two_stage_ranks=arrays["previous_two_stage_ranks"],
            n_compact=arrays["n_compact"], entry_gap=arrays["entry_gap"],
        )
        artifact_hashes = {
            name: sha256_file(staging / name)
            for name in TRACKED if name != "run_manifest.json"
        }
        manifest = {
            "status": "COMPLETE",
            "study_id": CFG.study_id,
            "result_status": validation["final_classification"],
            "source_commit_before_run": _git_head(repo),
            "config_hash": CFG.fingerprint(),
            "source_audit": source_audit,
            "source_code_hashes": source_hashes,
            "input_array_digest": input_digest,
            "model_fingerprints": {
                primary.name: primary.fingerprint(), benchmark.name: benchmark.fingerprint(),
            },
            "fit_audit": fit_audit,
            "ranking_audit": ranking_audit,
            "cooldown_audit": cooldown_audit,
            "market_regime_audit": regime_audit,
            "protected_hashes_before": protected_before,
            "protected_hashes_after": protected_after,
            "artifact_sha256": artifact_hashes,
            "tracked_artifacts": list(TRACKED),
            "local_observation_store": {
                "path": str(args.output_dir / "runtime/conditional_store.npz"),
                "sha256": sha256_file(runtime / "conditional_store.npz"),
                "tracked_by_git": False,
            },
            "pipeline_validation": {
                "passed": True,
                "stage_a_exactly_reused": True,
                "stage_a_refit_count": 0,
                "conditional_pool_exact_stage_a_top30": True,
                "timeout_is_non_success": True,
                "later_period_refit_count": 0,
                "prospective_observations_excluded": True,
                "n_compact_unchanged": True,
                "protected_hashes_unchanged": protected_before == protected_after,
                "actual_orders": 0,
                "actual_fills": 0,
                "broker_connections": 0,
                "manifest_written_last": True,
            },
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
        }
        _write_json(staging / "run_manifest.json", manifest)
        for name in TRACKED:
            shutil.move(str(staging / name), args.output_dir / name)
        runtime_target = args.output_dir / "runtime"
        runtime_target.mkdir(exist_ok=True)
        shutil.move(str(runtime / "conditional_store.npz"), runtime_target / "conditional_store.npz")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(json.dumps({
        "status": "COMPLETE",
        "study_id": CFG.study_id,
        "classification": validation["final_classification"],
        "selected_c": primary.regularization_c,
        "stage_a_refit_count": 0,
        "later_period_refit_count": 0,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
