#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile

import numpy as np

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from extension_entry_study_v01.analysis import (
        boundary_rows,
        bucket_analysis_rows,
        cluster_bootstrap_rows,
        cohort_summary_rows,
        interaction_rows,
        key_interaction_bootstrap_rows,
        shape_diagnostic_rows,
        year_direction_consistency_rows,
    )
    from extension_entry_study_v01.config import (
        CFG,
        COHORTS,
        FEATURE_NAMES,
        PERIODS,
    )
    from extension_entry_study_v01.pipeline import (
        COHORT_BIT,
        build_discovery_boundaries,
        save_observation_store,
        scan_full_market,
        validate_shared_contracts,
    )
else:
    from .analysis import (
        boundary_rows,
        bucket_analysis_rows,
        cluster_bootstrap_rows,
        cohort_summary_rows,
        interaction_rows,
        key_interaction_bootstrap_rows,
        shape_diagnostic_rows,
        year_direction_consistency_rows,
    )
    from .config import CFG, COHORTS, FEATURE_NAMES, PERIODS
    from .pipeline import (
        COHORT_BIT,
        build_discovery_boundaries,
        save_observation_store,
        scan_full_market,
        validate_shared_contracts,
    )

from surge_event_study_v01.data import load_ohlcv, prepare_stocks, sha256_file


TRACKED_ARTIFACTS = (
    "feature_boundaries.csv",
    "cohort_summary.csv",
    "bucket_analysis.csv",
    "yearly_bucket_analysis.csv",
    "interaction_momentum_bias20.csv",
    "interaction_bias20_entry_gap.csv",
    "bootstrap_results.csv",
    "shape_classification.csv",
    "year_direction_consistency.csv",
    "validation_summary.json",
    "run_manifest.json",
)
LOCAL_AUDIT_ARTIFACTS = (
    "analysis_spec.json",
    "data_audit.json",
    "pipeline_validation.json",
    "observation_store.npz",
)

EXPECTED_SHARED_HASHES = {
    "surge_event_study_v01/data.py": "7dd0665b4ce828e8da473d02ede5a6c1677395767090e93b2f6610bde4288ccc",
    "surge_event_study_v01/features.py": "c1a0e0138e294f009e8bbaa1dfb7425ee1dc7ff478f989d4bf8e219b0928efc5",
    "multi_setup_study_v01/outcomes.py": "2707a469e42c0e8b9489d37ac85c16ba991b29b6d54e9a14c56268030278e8c5",
    "multi_setup_study_v01/setup_detectors.py": "a025efcd65422e1651eb468b00cc8ebcb4a753b7bf5250df6a2ae5b31b389ec3",
    "multi_setup_study_v01/main.py": "5216ddf3959842ab35a2760e220863d79b0c1a2989c61734dc3e1e82e6b27861",
    "reversal_event_study_v01/study.py": "6dc42e7bd4ae3ce44f87a809cf905df20c106eb8d1044180a142728bb0589691",
}

EXPECTED_YEAR_SIGNAL_COUNTS = {
    "2020": 96_599,
    "2021": 128_800,
    "2022": 104_351,
    "2023": 119_115,
    "2024": 139_580,
    "2025": 115_882,
}


def _git_commit(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def _write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(_json_safe(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _csv_value(value):
    safe = _json_safe(value)
    if safe is None:
        return ""
    if isinstance(safe, (dict, list)):
        return json.dumps(safe, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return safe


def _write_csv(path: Path, rows: list[dict]) -> None:
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        if not fields:
            raise RuntimeError(f"refusing to write empty CSV: {path.name}")
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})


def _assert_outputs_absent(output_dir: Path) -> None:
    existing = [
        name for name in (*TRACKED_ARTIFACTS, *LOCAL_AUDIT_ARTIFACTS) if (output_dir / name).exists()
    ]
    if existing:
        raise FileExistsError(
            "study outputs already exist; refusing to overwrite: " + ", ".join(existing)
        )


def _shared_hashes(package_dir: Path) -> dict[str, str]:
    parent = package_dir.parent
    result = {
        name: sha256_file(parent / name)
        for name in EXPECTED_SHARED_HASHES
    }
    if result != EXPECTED_SHARED_HASHES:
        raise RuntimeError(f"shared implementation hash drifted: {result}")
    return result


def _source_hashes(package_dir: Path) -> dict[str, str]:
    return {
        path.name: sha256_file(path)
        for path in sorted(package_dir.glob("*.py"))
    }


def _shape_lookup(rows: list[dict], feature: str, cohort: str = "ALL_ELIGIBLE") -> dict:
    return next(row for row in rows if row["cohort"] == cohort and row["feature"] == feature)


def _interaction_evidence(rows: list[dict], comparison: str) -> dict:
    output = {}
    for period, _, _ in PERIODS:
        relevant = [
            row for row in rows if row["period"] == period and row["comparison"] == comparison
        ]
        point = next(
            (row["point_estimate"] for row in relevant if row["metric"] == "gross_average_return"),
            None,
        )
        gross_rows = [row for row in relevant if row["metric"] == "gross_average_return"]
        output[period] = {
            "gross_difference": point,
            "signal_date_95ci": next(
                ([row["ci_low"], row["ci_high"]] for row in gross_rows if row["cluster_unit"] == "signal_date"),
                None,
            ),
            "calendar_month_95ci": next(
                ([row["ci_low"], row["ci_high"]] for row in gross_rows if row["cluster_unit"] == "calendar_month"),
                None,
            ),
        }
    return output


def _validation_summary(
    arrays: dict[str, np.ndarray],
    shape_rows: list[dict],
    year_rows: list[dict],
    key_bootstrap: list[dict],
    boundary_audit: dict,
    scan_audit: dict,
) -> dict:
    bias_rows = [_shape_lookup(shape_rows, name) for name in ("bias_5", "bias_10", "bias_20")]
    ranked = sorted(
        bias_rows,
        key=lambda row: row["stable_information_score"]
        if row["stable_information_score"] is not None
        else -1.0,
        reverse=True,
    )
    informative = [
        row["feature"]
        for row in ranked
        if row["profile_stability_score"] is not None
        and row["profile_stability_score"] >= CFG.stable_profile_min_correlation
    ]
    inverted = [
        row["feature"] for row in bias_rows if row["stable_inverted_u_descriptive"]
    ]
    bias20 = _shape_lookup(shape_rows, "bias_20")
    comparisons = []
    adjusted_better = True
    for raw, adjusted in (
        ("bias_10", "atr_adjusted_bias10"),
        ("bias_20", "atr_adjusted_bias20"),
    ):
        raw_row = _shape_lookup(shape_rows, raw)
        adjusted_row = _shape_lookup(shape_rows, adjusted)
        better = bool(
            adjusted_row["profile_stability_score"] is not None
            and raw_row["profile_stability_score"] is not None
            and adjusted_row["profile_stability_score"] > raw_row["profile_stability_score"]
            and adjusted_row["stable_information_score"] is not None
            and raw_row["stable_information_score"] is not None
            and adjusted_row["stable_information_score"] > raw_row["stable_information_score"]
        )
        adjusted_better = adjusted_better and better
        comparisons.append(
            {
                "raw": raw,
                "atr_adjusted": adjusted,
                "raw_profile_stability": raw_row["profile_stability_score"],
                "adjusted_profile_stability": adjusted_row["profile_stability_score"],
                "raw_stable_information_score": raw_row["stable_information_score"],
                "adjusted_stable_information_score": adjusted_row["stable_information_score"],
                "adjusted_better_on_both_preregistered_measures": better,
            }
        )

    momentum_comparison = _interaction_evidence(
        key_bootstrap, "MOMENTUM_Q5_BIAS_Q5_MINUS_Q3"
    )
    momentum_high_worse_all_periods = all(
        evidence["gross_difference"] is not None and evidence["gross_difference"] < 0
        for evidence in momentum_comparison.values()
    )
    gap_direct = _interaction_evidence(
        key_bootstrap, "HIGH_BIAS_LARGE_GAP_MINUS_ZERO_TO_ONE_GAP"
    )
    gap_did = _interaction_evidence(key_bootstrap, "GAP_PENALTY_DID_Q5_MINUS_Q3")
    gap_direct_negative = all(
        evidence["gross_difference"] is not None and evidence["gross_difference"] < 0
        for evidence in gap_direct.values()
    )
    gap_did_negative = all(
        evidence["gross_difference"] is not None and evidence["gross_difference"] < 0
        for evidence in gap_did.values()
    )

    consistent = []
    for row in year_rows:
        if row["gross_direction_consistent_2020_2025"]:
            direction = "HIGH_MINUS_MID_POSITIVE" if row["gross_all_years_positive"] else "HIGH_MINUS_MID_NEGATIVE"
            consistent.append(
                {"cohort": row["cohort"], "feature": row["feature"], "gross_direction": direction}
            )
    all_market_consistent = [item for item in consistent if item["cohort"] == "ALL_ELIGIBLE"]

    return {
        "study_id": CFG.study_id,
        "status": "COMPLETE_DESCRIPTIVE_EXTENSION_RESEARCH_NOT_A_STRATEGY",
        "result_status": CFG.result_status,
        "boundary_sha256": boundary_audit["boundary_sha256"],
        "period_discipline": {
            "2020_2022": "HISTORICAL_DISCOVERY",
            "2023_2024": "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS",
            "2025": "STRESS_PREVALENCE_SEEN_NOT_BLIND",
            "prospective_confirmation": "AFTER_2026_09_06_ONLY",
        },
        "answers": {
            "1_bias_information": {
                "preregistered_measure": (
                    "minimum eta-squared across the three fixed periods multiplied by the "
                    "non-negative minimum discovery-profile Spearman correlation to later periods"
                ),
                "ranking": [
                    {
                        "feature": row["feature"],
                        "stable_information_score": row["stable_information_score"],
                        "profile_stability_score": row["profile_stability_score"],
                    }
                    for row in ranked
                ],
                "most_informative_with_stability_gate": informative[0] if informative else None,
                "interpretation": (
                    "A ranking is descriptive information content, not a trade threshold or strategy filter."
                ),
            },
            "2_stable_inverted_u": {
                "features_meeting_fixed_descriptive_criterion": inverted,
                "exists_descriptively_for_bias5_10_20": bool(inverted),
                "classifier": (
                    "Discovery mid D4-D7 must beat both tails with both cluster CIs above zero; "
                    "the same directions must remain in 2023-2024 and 2025."
                ),
                "multiplicity_warning": (
                    "Shape CIs are unadjusted exploratory diagnostics across preregistered features; "
                    "they are not feature selection or strategy validation."
                ),
            },
            "3_high_bias_mae_vs_mfe": {
                "bias20_mechanism": bias20["high_extension_mechanism"],
                "high_minus_mid_mfe": {
                    "discovery": bias20["high_minus_mid_mfe_discovery"],
                    "confirmation": bias20["high_minus_mid_mfe_confirmation"],
                    "stress": bias20["high_minus_mid_mfe_stress"],
                },
                "high_minus_mid_mae": {
                    "discovery": bias20["high_minus_mid_mae_discovery"],
                    "confirmation": bias20["high_minus_mid_mae_confirmation"],
                    "stress": bias20["high_minus_mid_mae_stress"],
                },
            },
            "4_atr_adjusted_stability": {
                "comparisons": comparisons,
                "atr_adjusted_more_stable_in_both_10d_and_20d_pairs": adjusted_better,
            },
            "5_momentum_strong_high_vs_mid_bias": {
                "comparison": "Momentum strength Q5, BIAS20 Q5 minus BIAS20 Q3",
                "high_bias_worse_in_all_three_periods": momentum_high_worse_all_periods,
                "cluster_bootstrap_evidence": momentum_comparison,
            },
            "6_high_bias_plus_large_gap": {
                "definitions": {
                    "high_bias": "frozen BIAS20 quintile Q5",
                    "large_gap": "T+1 entry gap >=2%",
                    "reference_gap": "T+1 entry gap 0% to <1%",
                    "did_reference_bias": "frozen BIAS20 quintile Q3",
                },
                "direct_penalty_negative_in_all_periods": gap_direct_negative,
                "incremental_penalty_did_negative_in_all_periods": gap_did_negative,
                "direct_cluster_bootstrap_evidence": gap_direct,
                "difference_in_differences_evidence": gap_did,
            },
            "7_cross_2020_2025_direction": {
                "all_market_consistent_high_minus_mid_gross_relationships": all_market_consistent,
                "all_cohort_consistent_relationship_count": len(consistent),
                "all_cohort_consistent_relationships": consistent,
                "warning": "Direction consistency alone is descriptive and is not a threshold-selection rule.",
            },
        },
        "mother_sample": scan_audit,
        "execution": {
            "broker_connection": "ABSENT",
            "portfolio_allocator": "NOT_RUN",
            "parameter_search": "NONE",
            "actual_orders": 0,
            "actual_fills": 0,
        },
    }


def _pipeline_validation(
    arrays: dict[str, np.ndarray],
    deciles: dict[str, tuple[float, ...]],
    quintiles: dict[str, tuple[float, ...]],
    tables: dict[str, list[dict]],
    scan_audit: dict,
) -> dict:
    meta = arrays["meta"]
    failures = []
    if len(meta) == 0:
        failures.append("empty_mother_sample")
    if int(meta["signal_date"].min()) < 20200101 or int(meta["signal_date"].max()) > 20251231:
        failures.append("feature_date_outside_2020_2025")
    if not np.all((meta["cohort_mask"] & COHORT_BIT["ALL_ELIGIBLE"]) != 0):
        failures.append("all_eligible_bit_missing")
    compact = (meta["cohort_mask"] & COHORT_BIT["N_COMPACT_RETEST_HYPOTHESIS"]) != 0
    n_retest = (meta["cohort_mask"] & COHORT_BIT["N_RETEST"]) != 0
    if not np.all(~compact | n_retest):
        failures.append("compact_not_subset_of_n")
    if set(scan_audit["year_signal_counts"]) != {str(year) for year in range(2020, 2026)}:
        failures.append("incomplete_year_coverage")
    if scan_audit["year_signal_counts"] != EXPECTED_YEAR_SIGNAL_COUNTS:
        failures.append(
            "canonical_mother_sample_count_drifted:"
            f"{scan_audit['year_signal_counts']}"
        )
    for feature in FEATURE_NAMES:
        if len(deciles[feature]) != 9 or len(quintiles[feature]) != 4:
            failures.append(f"boundary_count:{feature}")
        if not np.allclose(np.asarray(quintiles[feature]), np.asarray(deciles[feature])[1::2], rtol=0, atol=1e-14):
            failures.append(f"quintile_decile_drift:{feature}")
    if CFG.bootstrap_iterations < 5_000:
        failures.append("bootstrap_reps_below_5000")
    if any(not rows for rows in tables.values()):
        failures.append("empty_summary_table")
    return {
        "passed": not failures,
        "failures": failures,
        "checks": {
            "full_market_not_setup_limited": True,
            "canonical_year_counts": scan_audit["year_signal_counts"],
            "canonical_year_counts_match_prior_data_audit": (
                scan_audit["year_signal_counts"] == EXPECTED_YEAR_SIGNAL_COUNTS
            ),
            "features_before_outcome_attachment": True,
            "frozen_boundaries_from_2020_2022_only": True,
            "same_boundaries_reused_for_all_later_periods_and_cohorts": True,
            "entry_gap_is_t_plus_1_diagnostic_not_t_close_signal": True,
            "close_based_shared_barrier_outcome": True,
            "no_trade_iid_bootstrap": True,
            "signal_date_and_calendar_month_cluster_bootstrap": True,
            "bootstrap_reps": CFG.bootstrap_iterations,
            "no_threshold_search": True,
            "no_broker_or_orders": True,
            "compact_is_n_subset": bool(np.all(~compact | n_retest)),
            "manifest_written_last": True,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed full-market extension entry study; research only")
    parser.add_argument("--archives", nargs="+", type=Path, required=True)
    parser.add_argument("--supplements", nargs="*", type=Path, default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _assert_outputs_absent(args.output_dir)
    package_dir = Path(__file__).resolve().parent
    repo = package_dir.parents[1]
    shared_contracts = validate_shared_contracts(CFG)
    shared_hashes = _shared_hashes(package_dir)
    implementation_hashes = _source_hashes(package_dir)
    input_paths = args.archives + args.supplements
    input_hashes = [
        {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in input_paths
    ]

    with tempfile.TemporaryDirectory(prefix="extension_entry_v01_") as staging_name:
        staging = Path(staging_name)
        stocks, benchmark_bars, load_audit = load_ohlcv(
            args.archives, supplement_paths=args.supplements, cfg=CFG
        )
        prepared, benchmark, prepare_audit = prepare_stocks(stocks, benchmark_bars, CFG)
        deciles, quintiles, boundary_audit = build_discovery_boundaries(prepared, benchmark, CFG)
        analysis_spec = {
            "status": "FROZEN_BEFORE_FULL_OUTCOME_SCAN",
            "study_id": CFG.study_id,
            "source_commit_before_run": _git_commit(repo),
            "config_hash": CFG.fingerprint(),
            "config": CFG.snapshot(),
            "input_hashes": input_hashes,
            "implementation_code_hashes": implementation_hashes,
            "shared_code_hashes": shared_hashes,
            "shared_contracts": shared_contracts,
            "boundary_sha256": boundary_audit["boundary_sha256"],
            "boundary_clock": "2020-2022 pooled T-close features; no outcome/evaluability filter",
            "entry_gap_exception": "T+1 entry observed denominator, diagnostic only",
            "signal_clock": "T Close using T and earlier data only",
            "entry_proxy": "T+1 regular-session Open; not an executable odd-lot fill",
            "primary_outcome": "+8% before -5% within 10 trading days using Close",
            "conditional_cohorts": list(COHORTS),
            "shape_classifier": (
                "Frozen L=D1-D3, M=D4-D7, H=D8-D10; inverted-U requires discovery "
                "M>L and M>H with both signal-date and month CI lower bounds >0 and "
                "peak in D4-D7; later periods only confirm direction."
            ),
            "information_measure": (
                "min eta-squared across fixed periods times non-negative minimum "
                "discovery-profile Spearman correlation to confirmation/stress"
            ),
            "parameter_search": "NONE",
            "multiple_testing": (
                "UNADJUSTED_EXPLORATORY_SHAPE_DIAGNOSTICS; no feature is selected or promoted"
            ),
            "strategy_created": False,
            "broker_connection": "ABSENT",
        }
        _write_json(staging / "analysis_spec.json", analysis_spec)

        arrays, scan_audit = scan_full_market(prepared, benchmark, deciles, CFG)
        observation_metadata = {
            "study_id": CFG.study_id,
            "config_hash": CFG.fingerprint(),
            "boundary_sha256": boundary_audit["boundary_sha256"],
            "observation_sha256": scan_audit["observation_sha256"],
            "row_count": scan_audit["mother_sample_rows"],
            "notice": "Local compressed research matrix; not tracked by Git.",
        }
        save_observation_store(staging / "observation_store.npz", arrays, observation_metadata)

        boundary_table = boundary_rows(deciles, quintiles, boundary_audit)
        cohort_table = cohort_summary_rows(arrays)
        bucket_table = bucket_analysis_rows(arrays, deciles, quintiles)
        yearly_table = bucket_analysis_rows(arrays, deciles, quintiles, yearly=True)
        momentum_interaction, gap_interaction = interaction_rows(arrays, quintiles)
        bootstrap = cluster_bootstrap_rows(arrays, CFG)
        key_bootstrap = key_interaction_bootstrap_rows(arrays, quintiles, CFG)
        bootstrap.extend(key_bootstrap)
        shape_table = shape_diagnostic_rows(arrays, bucket_table, bootstrap, CFG)
        year_direction_table = year_direction_consistency_rows(arrays)
        tables = {
            "feature_boundaries.csv": boundary_table,
            "cohort_summary.csv": cohort_table,
            "bucket_analysis.csv": bucket_table,
            "yearly_bucket_analysis.csv": yearly_table,
            "interaction_momentum_bias20.csv": momentum_interaction,
            "interaction_bias20_entry_gap.csv": gap_interaction,
            "bootstrap_results.csv": bootstrap,
            "shape_classification.csv": shape_table,
            "year_direction_consistency.csv": year_direction_table,
        }
        for name, rows in tables.items():
            _write_csv(staging / name, rows)

        validation = _validation_summary(
            arrays, shape_table, year_direction_table, key_bootstrap, boundary_audit, scan_audit
        )
        _write_json(staging / "validation_summary.json", validation)
        data_audit = {
            **load_audit,
            **prepare_audit,
            "input_hashes": input_hashes,
            "shared_code_hashes": shared_hashes,
            "shared_contracts": shared_contracts,
            "boundary_audit": boundary_audit,
            "scan_audit": scan_audit,
            "security_master_status": "FOUR_DIGIT_CODE_PROXY_NOT_COMPLETE_POINT_IN_TIME_MASTER",
        }
        _write_json(staging / "data_audit.json", data_audit)
        pipeline_validation = _pipeline_validation(
            arrays, deciles, quintiles, tables, scan_audit
        )
        _write_json(staging / "pipeline_validation.json", pipeline_validation)
        if not pipeline_validation["passed"]:
            raise RuntimeError(f"pipeline validation failed: {pipeline_validation['failures']}")

        artifact_names = [
            *[name for name in TRACKED_ARTIFACTS if name != "run_manifest.json"],
            *LOCAL_AUDIT_ARTIFACTS,
        ]
        artifact_hashes = {name: sha256_file(staging / name) for name in artifact_names}
        manifest = {
            "status": "COMPLETE",
            "study_id": CFG.study_id,
            "result_status": CFG.result_status,
            "config_hash": CFG.fingerprint(),
            "source_commit_before_run": analysis_spec["source_commit_before_run"],
            "input_hashes": input_hashes,
            "implementation_code_hashes": implementation_hashes,
            "shared_code_hashes": shared_hashes,
            "boundary_sha256": boundary_audit["boundary_sha256"],
            "observation_sha256": scan_audit["observation_sha256"],
            "artifact_sha256": artifact_hashes,
            "tracked_artifacts": list(TRACKED_ARTIFACTS),
            "local_untracked_audit_artifacts": list(LOCAL_AUDIT_ARTIFACTS),
            "actual_orders": 0,
            "actual_fills": 0,
        }
        _write_json(staging / "run_manifest.json", manifest)

        # Publish only after all invariants pass. COMPLETE manifest is moved last.
        for name in artifact_names:
            shutil.move(str(staging / name), args.output_dir / name)
        shutil.move(str(staging / "run_manifest.json"), args.output_dir / "run_manifest.json")

    print(
        json.dumps(
            _json_safe(
                {
                    "manifest": manifest,
                    "answers": validation["answers"],
                    "mother_sample_rows": scan_audit["mother_sample_rows"],
                }
            ),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
