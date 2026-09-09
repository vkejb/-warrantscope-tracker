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
from cross_sectional_alpha_ranking_v01.ranking import build_daily_ranks
from surge_event_study_v01.data import sha256_file

from .analysis import (
    OI,
    bootstrap_rows,
    comparison_rows,
    complementarity_rows,
    cooldown_rows,
    daily_stage_a_rows,
    frequency_rows,
    gap_rows,
    metric_summary,
    n_compact_rows,
    quadrant_rows,
    regime_rows,
    stage_a_topk_rows,
    two_stage_daily_rows,
)
from .config import ALPHA_CANDIDATES, CFG
from .data import array_digest, load_reused_inputs, protected_hashes
from .models import fit_ic_weighted_mfe, fit_ridge_mfe
from .ranking import cooldown_proxy, same_day_quadrants, two_stage_ranks
from .validation import build_validation


TRACKED = (
    "stage_a_model_spec.json",
    "stage_a_coefficients.csv",
    "stage_a_walkforward.csv",
    "stage_a_topk_summary.csv",
    "stage_a_year_summary.csv",
    "stage_a_period_summary.csv",
    "two_stage_summary.csv",
    "two_stage_year_summary.csv",
    "two_stage_period_summary.csv",
    "complementarity_summary.csv",
    "quadrant_analysis.csv",
    "cooldown_trade_proxy_summary.csv",
    "frequency_summary.csv",
    "n_compact_overlay.csv",
    "regime_diagnostics.csv",
    "gap_diagnostics.csv",
    "cluster_bootstrap_summary.csv",
    "validation_summary.json",
    "run_manifest.json",
)


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise RuntimeError(f"refusing empty output: {path.name}")
    fields = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _git_head(repo: Path) -> str | None:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _subset(arrays: dict[str, np.ndarray], mask: np.ndarray) -> dict[str, np.ndarray]:
    size = len(mask)
    return {
        key: value[mask] if isinstance(value, np.ndarray) and value.ndim > 0 and len(value) == size else value
        for key, value in arrays.items()
    }


def _evaluate_model(arrays: dict[str, np.ndarray], scores: np.ndarray, start: str, end: str) -> dict:
    mask = (arrays["meta"]["signal_date"] >= int(start)) & (arrays["meta"]["signal_date"] <= int(end))
    local = _subset(arrays, mask)
    ranking, _ = build_daily_ranks(scores[mask], local["meta"]["signal_date"], local["meta"]["stock_code"])
    daily = daily_stage_a_rows("EVALUATION", ranking, local)
    result = metric_summary(local, ranking["rank"] <= CFG.stage_a_primary_top_k, np.ones(len(local["meta"]), dtype=bool))
    values = np.asarray([row["daily_spearman_ic_score_mfe10"] for row in daily], dtype=float)
    result["mean_daily_mfe_ic"] = float(np.nanmean(values))
    result["median_daily_mfe_ic"] = float(np.nanmedian(values))
    return result


def _fit_models(arrays: dict[str, np.ndarray]) -> tuple[object, object, list[object], list[dict], dict]:
    transformed = arrays["transformed_features"]
    dates = arrays["meta"]["signal_date"]
    evaluable = arrays["meta"]["outcome_evaluable"]
    target = arrays["outcomes"][:, OI["mfe_10d"]]
    features = tuple(str(value) for value in arrays["feature_names"])
    folds = (
        ("TRAIN_2020_EVALUATE_2021", "20200101", "20201231", "20210101", "20211231"),
        ("TRAIN_2020_2021_EVALUATE_2022", "20200101", "20211231", "20220101", "20221231"),
    )
    rows = []
    alpha_ic = {alpha: [] for alpha in ALPHA_CANDIDATES}
    models_by_key = {}
    purge_audit = {}
    for label, train_start, train_end, eval_start, eval_end in folds:
        train, purge = purged_training_mask(dates, evaluable, train_start, train_end, CFG.label_purge_sessions)
        purge_audit[label] = purge
        for alpha in ALPHA_CANDIDATES:
            model = fit_ridge_mfe(transformed, target, train, alpha, features, label)
            models_by_key[(label, alpha)] = model
            evaluation = _evaluate_model(arrays, model.predict(transformed), eval_start, eval_end)
            alpha_ic[alpha].append(evaluation["mean_daily_mfe_ic"])
            rows.append({
                "fold": label, "model": model.name, "alpha": alpha,
                "alpha_selected": False, "train_start": train_start, "train_end": train_end,
                "evaluation_start": eval_start, "evaluation_end": eval_end,
                "selection_metric": "MEAN_DAILY_SPEARMAN_IC_SCORE_MFE10", **evaluation,
            })
        benchmark, _ = fit_ic_weighted_mfe(transformed, target, dates, train, features, label)
        evaluation = _evaluate_model(arrays, benchmark.predict(transformed), eval_start, eval_end)
        rows.append({
            "fold": label, "model": benchmark.name, "alpha": None, "alpha_selected": None,
            "train_start": train_start, "train_end": train_end,
            "evaluation_start": eval_start, "evaluation_end": eval_end,
            "selection_metric": "BENCHMARK_NOT_USED_FOR_ALPHA_SELECTION", **evaluation,
        })
    selected_alpha = min(ALPHA_CANDIDATES, key=lambda alpha: (-float(np.mean(alpha_ic[alpha])), alpha))
    for row in rows:
        if row["model"] == CFG.stage_a_primary_model:
            row["alpha_selected"] = row["alpha"] == selected_alpha
    selected_fold_models = [models_by_key[(fold[0], selected_alpha)] for fold in folds]
    final_mask, final_purge = purged_training_mask(
        dates, evaluable, CFG.discovery_start, CFG.discovery_end, CFG.label_purge_sessions
    )
    ridge = fit_ridge_mfe(
        transformed, target, final_mask, selected_alpha, features, "HISTORICAL_DISCOVERY_2020_2022"
    )
    benchmark, benchmark_audit = fit_ic_weighted_mfe(
        transformed, target, dates, final_mask, features, "HISTORICAL_DISCOVERY_2020_2022"
    )
    return ridge, benchmark, selected_fold_models, rows, {
        "alpha_candidates": list(ALPHA_CANDIDATES),
        "alpha_fold_mean_daily_mfe_ic": {str(alpha): values for alpha, values in alpha_ic.items()},
        "selection_metric": "MEAN_OF_2021_AND_2022_WALKFORWARD_MEAN_DAILY_MFE_IC",
        "tie_break": "SMALLEST_ALPHA",
        "selected_alpha": selected_alpha,
        "fold_purge_audit": purge_audit,
        "final_fit_purge": final_purge,
        "final_fit_count": 1,
        "later_period_refit_count": 0,
        "benchmark_audit": benchmark_audit,
    }


def _coefficient_rows(models: list[object]) -> list[dict]:
    rows = []
    for model in models:
        for feature, coefficient in zip(model.feature_names, model.coefficients):
            rows.append({
                "model": model.name, "fit_period": model.fit_period, "feature": feature,
                "coefficient_or_weight": coefficient,
                "direction": "POSITIVE" if coefficient > 0 else "NEGATIVE" if coefficient < 0 else "ZERO",
                "intercept": model.intercept, "regularization_alpha": model.regularization_alpha,
                "training_observations": model.training_observations,
            })
    return rows


def main(argv: list[str] | None = None) -> int:
    package = Path(__file__).resolve().parent
    stock_strategy = package.parent
    repo = stock_strategy.parent
    parser = argparse.ArgumentParser(description="Frozen two-stage upside opportunity ranking study")
    parser.add_argument("--output-dir", type=Path, default=package)
    parser.add_argument("--winner-store", type=Path, default=stock_strategy / "winner_coverage_taxonomy_v01/runtime/observation_store.npz")
    parser.add_argument("--cross-store", type=Path, default=stock_strategy / "cross_sectional_alpha_ranking_v01/runtime/ranking_store.npz")
    parser.add_argument("--stage-b-model-spec", type=Path, default=stock_strategy / "cross_sectional_alpha_ranking_v01/model_spec.json")
    parser.add_argument("--stage-b-manifest", type=Path, default=stock_strategy / "cross_sectional_alpha_ranking_v01/run_manifest.json")
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    existing = [name for name in TRACKED if (args.output_dir / name).exists()]
    if existing:
        raise FileExistsError("refusing to overwrite published output: " + ", ".join(existing))

    protected_before = protected_hashes(stock_strategy)
    arrays, stage_b_model, source_audit = load_reused_inputs(
        args.winner_store, args.cross_store, args.stage_b_model_spec, args.stage_b_manifest
    )
    regimes, regime_audit = load_market_regime(
        stock_strategy, arrays["meta"]["signal_date"], stock_strategy / "winner_coverage_taxonomy_v01/run_manifest.json"
    )
    ridge, benchmark, fold_models, walkforward, fit_audit = _fit_models(arrays)
    rankings = {}
    ranking_audit = {}
    stage_a_daily = []
    for model in (ridge, benchmark):
        ranking, audit = build_daily_ranks(
            model.predict(arrays["transformed_features"]), arrays["meta"]["signal_date"], arrays["meta"]["stock_code"]
        )
        rankings[model.name] = ranking
        ranking_audit[model.name] = audit
        stage_a_daily.extend(daily_stage_a_rows(model.name, ranking, arrays))
    primary_ranking = rankings[ridge.name]
    two_rank, two_audit = two_stage_ranks(
        primary_ranking["rank"], arrays["stage_b_scores"], arrays["meta"]["signal_date"], arrays["meta"]["stock_code"], 30
    )
    quadrants = same_day_quadrants(
        primary_ranking["scores"], arrays["stage_b_scores"], arrays["meta"]["signal_date"]
    )
    raw_two = (two_rank > 0) & (two_rank <= CFG.two_stage_primary_top_k)
    cooldown, cooldown_audit = cooldown_proxy(
        raw_two, two_rank, arrays["meta"]["signal_date"], arrays["meta"]["stock_code"], CFG.cooldown_sessions
    )

    stage_topk = stage_a_topk_rows(arrays, rankings, stage_a_daily)
    stage_period = [row for row in stage_topk if row["time_slice_type"] == "PERIOD" and row["top_k"] == 30]
    stage_year = [row for row in stage_topk if row["time_slice_type"] == "YEAR" and row["top_k"] == 30]
    comparisons = comparison_rows(arrays, primary_ranking["rank"], two_rank)
    two_period = [row for row in comparisons if row["time_slice_type"] == "PERIOD"]
    two_year = [row for row in comparisons if row["time_slice_type"] == "YEAR"]
    complementarity = complementarity_rows(arrays, primary_ranking)
    quadrant = quadrant_rows(arrays, quadrants)
    cooldown_table = cooldown_rows(arrays, cooldown)
    frequency = frequency_rows(arrays, raw_two, cooldown)
    compact = n_compact_rows(arrays, primary_ranking["rank"], two_rank)
    regime = regime_rows(arrays, primary_ranking["rank"], two_rank, regimes)
    gap = gap_rows(arrays, primary_ranking["rank"], two_rank)
    two_daily = two_stage_daily_rows(arrays, two_rank)
    bootstrap = bootstrap_rows(
        [row for row in stage_a_daily if row["model"] == CFG.stage_a_primary_model], two_daily
    )
    validation = build_validation(
        stage_period, two_period,
        [row for row in stage_a_daily if row["model"] == CFG.stage_a_primary_model],
        two_daily, complementarity, quadrant, frequency, compact, regime, gap,
        ridge.regularization_alpha,
    )
    protected_after = protected_hashes(stock_strategy)
    if protected_before != protected_after:
        raise RuntimeError("protected frozen module or prospective ledger changed during study")
    if source_audit["stage_b_refit_count"] != 0 or validation["later_period_refit_count"] != 0:
        raise RuntimeError("later or Stage B refit invariant failed")

    tables = {
        "stage_a_coefficients.csv": _coefficient_rows([*fold_models, ridge, benchmark]),
        "stage_a_walkforward.csv": walkforward,
        "stage_a_topk_summary.csv": stage_topk,
        "stage_a_year_summary.csv": stage_year,
        "stage_a_period_summary.csv": stage_period,
        "two_stage_summary.csv": comparisons,
        "two_stage_year_summary.csv": two_year,
        "two_stage_period_summary.csv": two_period,
        "complementarity_summary.csv": complementarity,
        "quadrant_analysis.csv": quadrant,
        "cooldown_trade_proxy_summary.csv": cooldown_table,
        "frequency_summary.csv": frequency,
        "n_compact_overlay.csv": compact,
        "regime_diagnostics.csv": regime,
        "gap_diagnostics.csv": gap,
        "cluster_bootstrap_summary.csv": bootstrap,
    }
    model_spec = {
        "study_id": CFG.study_id,
        "stage_a_primary_model": ridge.payload(),
        "stage_a_benchmark_model": benchmark.payload(),
        "fit_audit": fit_audit,
        "stage_b_frozen_model": stage_b_model.payload(),
        "stage_b_fingerprint": stage_b_model.fingerprint(),
        "stage_b_refit_count": 0,
        "preprocessing": "EXACT_PUBLISHED_SAME_DAY_PERCENTILE_MATRIX_REUSED",
        "stage_a_primary_target": "MFE10_T1_OPEN_DAY1_TO_DAY10_CLOSE_MAXIMUM",
        "stage_a_primary_pool": "TOP30",
        "two_stage_primary": "STAGE_B_TOP5_WITHIN_STAGE_A_TOP30",
    }

    with tempfile.TemporaryDirectory(prefix="upside_opportunity_v01_") as temporary:
        staging = Path(temporary)
        for name, rows in tables.items():
            _write_csv(staging / name, rows)
        _write_json(staging / "stage_a_model_spec.json", model_spec)
        _write_json(staging / "validation_summary.json", validation)
        local = package / "runtime/ranking_store.npz"
        local.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            local, meta=arrays["meta"], outcomes=arrays["outcomes"],
            stage_a_scores=primary_ranking["scores"], stage_a_ranks=primary_ranking["rank"],
            stage_b_scores=arrays["stage_b_scores"], stage_b_ranks=arrays["stage_b_ranks"],
            two_stage_ranks=two_rank, two_stage_cooldown=cooldown,
            quadrants=quadrants, n_compact=arrays["n_compact"], entry_gap=arrays["entry_gap"],
        )
        artifact_hashes = {name: sha256_file(staging / name) for name in TRACKED if name != "run_manifest.json"}
        source_paths = sorted(package.glob("*.py"))
        manifest = {
            "status": "COMPLETE", "study_id": CFG.study_id,
            "result_status": validation["final_classification"],
            "source_commit_before_run": _git_head(repo), "config_hash": CFG.fingerprint(),
            "source_audit": source_audit, "source_code_hashes": {str(path.relative_to(stock_strategy)): sha256_file(path) for path in source_paths},
            "input_array_digest": array_digest(arrays),
            "stage_a_model_fingerprints": {ridge.name: ridge.fingerprint(), benchmark.name: benchmark.fingerprint()},
            "stage_b_model_fingerprint": stage_b_model.fingerprint(),
            "fit_audit": fit_audit, "ranking_audit": ranking_audit,
            "two_stage_audit": two_audit, "cooldown_audit": cooldown_audit,
            "market_regime_audit": regime_audit,
            "protected_hashes_before": protected_before, "protected_hashes_after": protected_after,
            "artifact_sha256": artifact_hashes, "tracked_artifacts": list(TRACKED),
            "local_observation_store": {"path": str(local), "sha256": sha256_file(local), "tracked_by_git": False},
            "pipeline_validation": {
                "passed": True, "mother_reused": True, "stage_a_target_is_mfe10": True,
                "stage_b_exactly_reused": True, "stage_b_refit_count": 0,
                "later_period_refit_count": 0, "prospective_observations_excluded": True,
                "n_compact_unchanged": True, "protected_hashes_unchanged": True,
                "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
                "manifest_written_last": True,
            },
            "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
        }
        _write_json(staging / "run_manifest.json", manifest)
        for name in TRACKED:
            shutil.move(str(staging / name), args.output_dir / name)
    print(json.dumps(validation, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
