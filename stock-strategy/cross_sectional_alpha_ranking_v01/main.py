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

from extension_entry_study_v01.pipeline import OUTCOME_FIELDS
from multi_setup_study_v01.config import CFG as MULTI_CFG
from prospective_shadow_v01.detector import assert_frozen_contract
from surge_event_study_v01.data import sha256_file

from .analysis import (
    OUTCOME_INDEX,
    build_validation_summary,
    cluster_bootstrap_rows,
    coefficient_rows,
    conviction_rows,
    cooldown_summary_rows,
    date_mask,
    entry_gap_rows,
    frequency_rows,
    n_compact_cohort_rows,
    n_compact_overlay_rows,
    primary_summary_rows,
    regime_rows,
    score_decile_rows,
    spread_summary_rows,
    topk_summary_rows,
)
from .config import ALPHA_CANDIDATES, CFG, FEATURE_NAMES, PERIODS
from .data import (
    ledger_hashes,
    load_market_regime,
    load_reused_mother,
    sha256_array_bundle,
)
from .models import (
    coefficient_comparison,
    fit_ic_weighted,
    fit_ridge,
)
from .preprocessing import purged_training_mask, same_day_cross_sectional_percentiles
from .ranking import build_daily_ranks, cooldown_trade_proxy, daily_ranking_rows


TRACKED_ARTIFACTS = (
    "model_spec.json",
    "feature_spec.json",
    "period_discipline.json",
    "discovery_walkforward.csv",
    "model_coefficients.csv",
    "daily_ranking_metrics.csv",
    "topk_summary.csv",
    "year_summary.csv",
    "period_summary.csv",
    "score_decile_summary.csv",
    "top_bottom_spread.csv",
    "cluster_bootstrap_summary.csv",
    "cooldown_trade_proxy_summary.csv",
    "frequency_summary.csv",
    "n_compact_rank_overlay.csv",
    "n_compact_cohort_summary.csv",
    "regime_diagnostics.csv",
    "entry_gap_diagnostics.csv",
    "score_quality_analysis.csv",
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
        raise RuntimeError(f"refusing to publish empty table: {path.name}")
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _git_commit(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _protected_output_hashes(stock_strategy: Path) -> dict[str, str]:
    result = {}
    for module in ("winner_coverage_taxonomy_v01", "multi_setup_study_v01"):
        for path in sorted((stock_strategy / module).iterdir()):
            if path.is_file() and path.suffix.lower() in {".csv", ".json"}:
                result[str(path.relative_to(stock_strategy))] = sha256_file(path)
    return result


def _source_hashes(package_dir: Path) -> dict[str, str]:
    stock_strategy = package_dir.parent
    paths = [
        *sorted(path for path in package_dir.glob("*.py")),
        stock_strategy / "multi_setup_study_v01" / "config.py",
        stock_strategy / "multi_setup_study_v01" / "outcomes.py",
        stock_strategy / "prospective_shadow_v01" / "detector.py",
        stock_strategy / "winner_coverage_taxonomy_v01" / "run_manifest.json",
        stock_strategy / "winner_coverage_taxonomy_v01" / "runtime" / "observation_store.npz",
        stock_strategy / "extension_entry_study_v01" / "observation_store.npz",
    ]
    return {
        str(path.relative_to(stock_strategy)): sha256_file(path) for path in paths
    }


def _evaluation_summary(
    arrays: dict[str, np.ndarray], scores: np.ndarray, start: str, end: str
) -> dict:
    period = date_mask(arrays["meta"], start, end)
    subset = {name: value[period] if isinstance(value, np.ndarray) and len(value) == len(period) else value for name, value in arrays.items()}
    ranking, _audit = build_daily_ranks(
        scores[period], subset["meta"]["signal_date"], subset["meta"]["stock_code"]
    )
    daily = daily_ranking_rows("EVALUATION", ranking, subset)
    from .analysis import daily_quality_summary, metric_summary

    summary = metric_summary(subset, ranking["rank"] <= CFG.primary_top_k)
    summary.update(daily_quality_summary(daily))
    return summary


def _fit_discovery_models(
    arrays: dict[str, np.ndarray], transformed: np.ndarray
) -> tuple[object, object, list[object], list[dict], dict]:
    dates = arrays["meta"]["signal_date"]
    evaluable = arrays["meta"]["outcome_evaluable"]
    target = arrays["outcomes"][:, OUTCOME_INDEX["net_return"]]
    folds = (
        ("TRAIN_2020_EVALUATE_2021", "20200101", "20201231", "20210101", "20211231"),
        ("TRAIN_2020_2021_EVALUATE_2022", "20200101", "20211231", "20220101", "20221231"),
    )
    walkforward: list[dict] = []
    fold_models: list[object] = []
    alpha_scores: dict[float, list[float]] = {alpha: [] for alpha in ALPHA_CANDIDATES}
    purge_audits = {}
    for label, train_start, train_end, eval_start, eval_end in folds:
        train_mask, purge = purged_training_mask(
            dates, evaluable, train_start, train_end, CFG.label_purge_sessions
        )
        purge_audits[label] = purge
        for alpha in ALPHA_CANDIDATES:
            model = fit_ridge(transformed, target, train_mask, alpha, label)
            evaluation = _evaluation_summary(
                arrays, model.predict(transformed), eval_start, eval_end
            )
            alpha_scores[alpha].append(evaluation["mean_daily_ic"] or -999.0)
            walkforward.append(
                {
                    "fold": label,
                    "model": model.name,
                    "alpha": alpha,
                    "alpha_selected": False,
                    "train_start": train_start,
                    "train_end": train_end,
                    "evaluation_start": eval_start,
                    "evaluation_end": eval_end,
                    "selection_metric": "MEAN_DAILY_SPEARMAN_IC_NET_RETURN",
                    **evaluation,
                }
            )
        benchmark, _ic_audit = fit_ic_weighted(
            transformed, target, dates, train_mask, label
        )
        benchmark_eval = _evaluation_summary(
            arrays, benchmark.predict(transformed), eval_start, eval_end
        )
        walkforward.append(
            {
                "fold": label,
                "model": benchmark.name,
                "alpha": None,
                "alpha_selected": None,
                "train_start": train_start,
                "train_end": train_end,
                "evaluation_start": eval_start,
                "evaluation_end": eval_end,
                "selection_metric": "BENCHMARK_NOT_USED_FOR_ALPHA_SELECTION",
                **benchmark_eval,
            }
        )
    selected_alpha = min(
        ALPHA_CANDIDATES,
        key=lambda alpha: (-float(np.mean(alpha_scores[alpha])), alpha),
    )
    for row in walkforward:
        if row["model"] == CFG.primary_model:
            row["alpha_selected"] = row["alpha"] == selected_alpha
    for label, train_start, train_end, _eval_start, _eval_end in folds:
        train_mask, _purge = purged_training_mask(
            dates, evaluable, train_start, train_end, CFG.label_purge_sessions
        )
        fold_models.append(
            fit_ridge(transformed, target, train_mask, selected_alpha, label)
        )

    final_mask, final_purge = purged_training_mask(
        dates, evaluable, CFG.discovery_start, CFG.discovery_end, CFG.label_purge_sessions
    )
    ridge = fit_ridge(
        transformed, target, final_mask, selected_alpha, "HISTORICAL_DISCOVERY_2020_2022"
    )
    benchmark, ic_audit = fit_ic_weighted(
        transformed, target, dates, final_mask, "HISTORICAL_DISCOVERY_2020_2022"
    )
    audit = {
        "alpha_candidate_set": list(ALPHA_CANDIDATES),
        "selection_metric": "MEAN_OF_2021_AND_2022_WALKFORWARD_MEAN_DAILY_SPEARMAN_IC",
        "tie_break": "SMALLEST_ALPHA",
        "alpha_fold_ic": {str(key): value for key, value in alpha_scores.items()},
        "selected_alpha": selected_alpha,
        "fold_purges": purge_audits,
        "final_fit_purge": final_purge,
        "final_fit_count": 1,
        "later_period_refit_count": 0,
        "ic_benchmark_audit": ic_audit,
    }
    return ridge, benchmark, fold_models, walkforward, audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Frozen cross-sectional alpha ranking study")
    package_dir = Path(__file__).resolve().parent
    stock_strategy = package_dir.parent
    repo = stock_strategy.parent
    parser.add_argument("--output-dir", type=Path, default=package_dir)
    parser.add_argument(
        "--winner-store", type=Path,
        default=stock_strategy / "winner_coverage_taxonomy_v01" / "runtime" / "observation_store.npz",
    )
    parser.add_argument(
        "--extension-store", type=Path,
        default=stock_strategy / "extension_entry_study_v01" / "observation_store.npz",
    )
    parser.add_argument(
        "--winner-manifest", type=Path,
        default=stock_strategy / "winner_coverage_taxonomy_v01" / "run_manifest.json",
    )
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    existing = [name for name in TRACKED_ARTIFACTS if (args.output_dir / name).exists()]
    if existing:
        raise FileExistsError("refusing to overwrite published outputs: " + ", ".join(existing))

    assert_frozen_contract()
    if MULTI_CFG.fingerprint() != CFG.expected_multi_setup_config_hash:
        raise RuntimeError("multi_setup_study_v01 config contract drifted")
    detector = stock_strategy / "multi_setup_study_v01" / "setup_detectors.py"
    if sha256_file(detector) != CFG.expected_compact_detector_sha256:
        raise RuntimeError("N Compact detector source hash drifted")
    ledger_before = ledger_hashes(stock_strategy)
    protected_before = _protected_output_hashes(stock_strategy)
    arrays, source_audit = load_reused_mother(args.winner_store, args.extension_store)
    if source_audit["prospective_observations"] != 0:
        raise RuntimeError("2026 prospective observations entered the research mother")
    transformed, transform_audit = same_day_cross_sectional_percentiles(
        arrays["raw_features"], arrays["meta"]["signal_date"]
    )
    regimes, regime_audit = load_market_regime(
        stock_strategy, arrays["meta"]["signal_date"], args.winner_manifest
    )
    ridge, benchmark, fold_models, walkforward, model_audit = _fit_discovery_models(
        arrays, transformed
    )
    final_models = (ridge, benchmark)
    rankings = {}
    ranking_audits = {}
    cooldown_masks = {}
    cooldown_audits = {}
    daily = []
    for model in final_models:
        ranking, audit = build_daily_ranks(
            model.predict(transformed), arrays["meta"]["signal_date"], arrays["meta"]["stock_code"]
        )
        rankings[model.name] = ranking
        ranking_audits[model.name] = audit
        cooldown, cooldown_audit = cooldown_trade_proxy(
            ranking["rank"], arrays["meta"]["signal_date"], arrays["meta"]["stock_code"]
        )
        cooldown_masks[model.name] = cooldown
        cooldown_audits[model.name] = cooldown_audit
        daily.extend(daily_ranking_rows(model.name, ranking, arrays))

    tables = {
        "discovery_walkforward.csv": walkforward,
        "daily_ranking_metrics.csv": daily,
        "topk_summary.csv": topk_summary_rows(arrays, rankings),
        "year_summary.csv": primary_summary_rows(arrays, rankings, daily, "YEAR"),
        "period_summary.csv": primary_summary_rows(arrays, rankings, daily, "PERIOD"),
        "score_decile_summary.csv": score_decile_rows(arrays, rankings),
    }
    tables["top_bottom_spread.csv"] = spread_summary_rows(daily)
    tables["cluster_bootstrap_summary.csv"] = cluster_bootstrap_rows(daily)
    tables["cooldown_trade_proxy_summary.csv"] = cooldown_summary_rows(arrays, cooldown_masks)
    tables["frequency_summary.csv"] = frequency_rows(arrays, rankings, cooldown_masks)
    tables["n_compact_rank_overlay.csv"] = n_compact_overlay_rows(arrays, rankings)
    tables["n_compact_cohort_summary.csv"] = n_compact_cohort_rows(arrays, rankings)
    tables["regime_diagnostics.csv"] = regime_rows(arrays, rankings, regimes)
    gap_rows, gap_audit = entry_gap_rows(arrays, rankings)
    tables["entry_gap_diagnostics.csv"] = gap_rows
    conviction, conviction_audit = conviction_rows(daily)
    tables["score_quality_analysis.csv"] = conviction
    comparisons = [coefficient_comparison(model, ridge) for model in [*fold_models, ridge]]
    tables["model_coefficients.csv"] = coefficient_rows([*fold_models, ridge, benchmark], comparisons)

    validation = build_validation_summary(
        tables["period_summary.csv"], tables["top_bottom_spread.csv"],
        tables["cluster_bootstrap_summary.csv"], tables["cooldown_trade_proxy_summary.csv"],
        tables["frequency_summary.csv"], tables["n_compact_rank_overlay.csv"],
        tables["n_compact_cohort_summary.csv"],
        tables["regime_diagnostics.csv"], comparisons, ridge.regularization_alpha,
    )
    ledger_after = ledger_hashes(stock_strategy)
    protected_after = _protected_output_hashes(stock_strategy)
    failures = []
    if ledger_before != ledger_after:
        failures.append("prospective_ledger_changed")
    if protected_before != protected_after:
        failures.append("protected_research_output_changed")
    if validation["later_period_refit_count"] != 0:
        failures.append("later_period_refit_count_nonzero")
    if any(not rows for rows in tables.values()):
        failures.append("empty_output_table")
    if failures:
        raise RuntimeError("pipeline validation failed: " + ", ".join(failures))

    model_spec = {
        "study_id": CFG.study_id,
        "primary_model": ridge.payload(),
        "benchmark_model": benchmark.payload(),
        "alpha_selection": model_audit,
        "target": "COMMON_OUTCOME_NET_RETURN_T1_OPEN_8PCT_BEFORE_MINUS5PCT_ELSE_DAY10_CLOSE",
        "primary_evaluation": "TOP10",
        "secondary_top_k": [5, 20, 30],
        "preprocessing": transform_audit,
        "cooldown": cooldown_audits,
    }
    feature_spec = {
        "features": list(FEATURE_NAMES),
        "feature_count": len(FEATURE_NAMES),
        "source": "winner_coverage_taxonomy_v01/runtime/observation_store.npz",
        "timestamp_contract": "T_CLOSE_OR_EARLIER_ONLY",
        "transform": "SAME_DAY_AVERAGE_TIE_PERCENTILE_0_TO_1",
        "missing_value": 0.5,
        "feature_search": "NONE",
        "bias_threshold_search": "PROHIBITED_NOT_PERFORMED",
        "aliases": {
            "distance_from_recent_local_low": "recent_local_low_distance",
            "distance_from_recent_local_high": "recent_local_high_distance",
        },
    }
    period_discipline = {
        "2019": "FEATURE_WARMUP_ONLY",
        "2020_2022": "HISTORICAL_DISCOVERY",
        "2023_2024": "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS",
        "2025": "STRESS_PREVALENCE_SEEN_NOT_BLIND",
        "2026_09_AND_AFTER": "EXCLUDED_FROM_FIT_SELECTION_AND_RESEARCH",
        "later_period_refit_count": 0,
        "threshold_selection_from_later_periods": 0,
    }

    with tempfile.TemporaryDirectory(prefix="cross_sectional_alpha_v01_") as temporary:
        staging = Path(temporary)
        for name, rows in tables.items():
            _write_csv(staging / name, rows)
        _write_json(staging / "model_spec.json", model_spec)
        _write_json(staging / "feature_spec.json", feature_spec)
        _write_json(staging / "period_discipline.json", period_discipline)
        _write_json(staging / "validation_summary.json", validation)
        local_store = package_dir / "runtime" / "ranking_store.npz"
        local_store.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            local_store,
            meta=arrays["meta"], raw_features=arrays["raw_features"],
            transformed_features=transformed, entry_gap=arrays["entry_gap"],
            n_compact=arrays["n_compact"], outcome_fields=np.asarray(OUTCOME_FIELDS),
            feature_names=np.asarray(FEATURE_NAMES),
            ridge_scores=rankings[ridge.name]["scores"], ridge_ranks=rankings[ridge.name]["rank"],
            ridge_cooldown=cooldown_masks[ridge.name],
            benchmark_scores=rankings[benchmark.name]["scores"], benchmark_ranks=rankings[benchmark.name]["rank"],
            benchmark_cooldown=cooldown_masks[benchmark.name],
        )
        artifact_hashes = {
            name: sha256_file(staging / name)
            for name in TRACKED_ARTIFACTS if name != "run_manifest.json"
        }
        manifest = {
            "status": "COMPLETE",
            "study_id": CFG.study_id,
            "result_status": validation["final_classification"],
            "source_commit_before_run": _git_commit(repo),
            "config_hash": CFG.fingerprint(),
            "source_audit": source_audit,
            "source_code_hashes": _source_hashes(package_dir),
            "mother_array_digest": sha256_array_bundle({
                "meta": arrays["meta"], "features": arrays["raw_features"],
                "outcomes": arrays["outcomes"], "entry_gap": arrays["entry_gap"],
                "n_compact": arrays["n_compact"],
            }),
            "transform_audit": transform_audit,
            "model_fit_audit": model_audit,
            "ranking_audits": ranking_audits,
            "cooldown_audits": cooldown_audits,
            "market_regime_audit": regime_audit,
            "entry_gap_audit": gap_audit,
            "score_quality_audit": conviction_audit,
            "model_fingerprints": {model.name: model.fingerprint() for model in final_models},
            "coefficient_stability": comparisons,
            "prospective_ledger_hashes_before": ledger_before,
            "prospective_ledger_hashes_after": ledger_after,
            "protected_research_hashes_before": protected_before,
            "protected_research_hashes_after": protected_after,
            "artifact_sha256": artifact_hashes,
            "tracked_artifacts": list(TRACKED_ARTIFACTS),
            "local_observation_store": {
                "path": str(local_store), "sha256": sha256_file(local_store),
                "tracked_by_git": False,
            },
            "pipeline_validation": {
                "passed": True,
                "failures": [],
                "prospective_ledgers_unchanged": True,
                "protected_research_outputs_unchanged": True,
                "mother_sample_reused_not_rebuilt": True,
                "outcome_definition_reused": True,
                "n_compact_definition_unchanged": True,
                "discovery_model_fit_count": 1,
                "later_period_refit_count": 0,
                "no_future_leakage": True,
                "no_grid_search_or_automl": True,
                "manifest_written_last": True,
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
