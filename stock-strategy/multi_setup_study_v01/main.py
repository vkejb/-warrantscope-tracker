#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from multi_setup_study_v01.analysis import (
        bootstrap_rows,
        entry_gap_rows,
        metric_summary,
        signal_clustering_rows,
        winner_dependence_rows,
    )
    from multi_setup_study_v01.config import CFG, PERIODS, SETUPS, period_label
    from multi_setup_study_v01.outcomes import evaluate_unified_outcome
    from multi_setup_study_v01.setup_detectors import (
        benchmark_context,
        detect_consolidation_breakout_v2,
        detect_trend_pullback,
        is_compact_retest,
    )
else:
    from .analysis import (
        bootstrap_rows,
        entry_gap_rows,
        metric_summary,
        signal_clustering_rows,
        winner_dependence_rows,
    )
    from .config import CFG, PERIODS, SETUPS, period_label
    from .outcomes import evaluate_unified_outcome
    from .setup_detectors import (
        benchmark_context,
        detect_consolidation_breakout_v2,
        detect_trend_pullback,
        is_compact_retest,
    )

from reversal_event_study_v01.config import CFG as REVERSAL_CFG
from reversal_event_study_v01.study import build_pattern_observation
from surge_event_study_v01.analysis import percentile_ranks
from surge_event_study_v01.data import load_ohlcv, prepare_stocks, sha256_file
from surge_event_study_v01.features import iter_signal_dates


SUMMARY_ARTIFACTS = (
    "setup_comparison.csv",
    "setup_year_summary.csv",
    "entry_gap_analysis.csv",
    "winner_dependence.csv",
    "signal_clustering.csv",
    "bootstrap_results.csv",
    "validation_summary.json",
    "run_manifest.json",
)
AUDIT_ARTIFACTS = (
    "analysis_spec.json",
    "data_audit.json",
    "pipeline_validation.json",
    "signal_observations.csv",
)


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
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        if not fields:
            return
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})


def _assert_outputs_absent(output_dir: Path) -> None:
    existing = [
        name for name in (*SUMMARY_ARTIFACTS, *AUDIT_ARTIFACTS)
        if (output_dir / name).exists()
    ]
    if existing:
        raise FileExistsError(
            "study outputs already exist; refusing to overwrite: " + ", ".join(existing)
        )


def _git_commit(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _momentum_selected(contexts: list[tuple]) -> list[dict]:
    feature_names = CFG.momentum_score_features
    scoreable = [
        index
        for index, (observation, _, _) in enumerate(contexts)
        if all(name in observation.features for name in feature_names)
    ]
    percentile_by_feature: dict[str, dict[int, float]] = {}
    for feature in feature_names:
        available = [
            (index, contexts[index][0].features[feature]) for index in scoreable
        ]
        ranks = percentile_ranks([value for _, value in available])
        percentile_by_feature[feature] = {
            index: rank for (index, _), rank in zip(available, ranks)
        }
    scored = []
    for context_index in scoreable:
        observation = contexts[context_index][0]
        percentiles = {
            feature: percentile_by_feature[feature][context_index]
            for feature in feature_names
        }
        score = sum(percentiles.values()) / len(percentiles)
        scored.append((score, observation.code, context_index, percentiles))
    scored.sort(key=lambda item: (-item[0], item[1]))
    output = []
    for rank, (score, _, context_index, percentiles) in enumerate(
        scored[: CFG.momentum_daily_selection_count], 1
    ):
        observation, stock, local_index = contexts[context_index]
        output.append(
            {
                "observation": observation,
                "stock": stock,
                "local_index": local_index,
                "momentum_score": score,
                "daily_rank": rank,
                "score_feature_percentiles": percentiles,
            }
        )
    return output


def _accepted(
    last_signal: dict[tuple[str, str], int],
    setup: str,
    code: str,
    calendar_index: int,
) -> bool:
    previous = last_signal.get((setup, code))
    included = previous is None or (
        calendar_index - previous >= CFG.causal_cooldown_sessions
    )
    if included:
        last_signal[(setup, code)] = calendar_index
    return included


def _signal_row(
    *,
    setup: str,
    stock,
    local_index: int,
    features: dict,
    status: str,
    benchmark,
    extra: dict | None = None,
) -> dict:
    bar = stock.bars[local_index]
    outcome = evaluate_unified_outcome(stock, local_index, CFG)
    clean_features = dict(features)
    clean_features.pop("entry_gap", None)
    row = {
        "period": period_label(bar.date),
        "signal_date": bar.date,
        "year": bar.date[:4],
        "month": bar.date[:6],
        "calendar_index": stock.calendar_indices[local_index],
        "signal_segment_id": stock.segment_ids[local_index],
        "code": stock.code,
        "name": bar.name,
        "setup": setup,
        "setup_status": status,
        "signal_close": bar.close,
        "cooldown_included": True,
        "cost_model": (
            "independent TWD30000 equal-notional V2.1 proxy; discounted commissions "
            "with TWD1 minimum, 0.3% sell tax, 0.1% one-way slippage; no allocator"
        ),
        "ownership_features_status": CFG.ownership_status,
        "is_actual_order": False,
        "is_actual_fill": False,
        **benchmark_context(benchmark, stock.calendar_indices[local_index]),
        **clean_features,
        **(extra or {}),
        **outcome,
    }
    return row


def scan_setups(prepared: list, benchmark) -> tuple[list[dict], dict]:
    """One continuous scan preserves cooldown state across all period boundaries."""

    rows: list[dict] = []
    last_signal: dict[tuple[str, str], int] = {}
    raw_counts: dict[str, int] = defaultdict(int)
    accepted_counts: dict[str, int] = defaultdict(int)
    censored_reasons: dict[str, int] = defaultdict(int)
    maximum_signal_date_read: str | None = None

    for day, calendar_index, contexts in iter_signal_dates(
        prepared, benchmark, CFG.warmup_start, CFG.stress_end, CFG
    ):
        in_output_period = CFG.discovery_start <= day <= CFG.stress_end

        for observation, stock, local_index in contexts:
            legacy = build_pattern_observation(
                stock, local_index, benchmark, REVERSAL_CFG
            )
            if legacy is not None:
                setup = legacy.pattern
                raw_counts[setup] += 1
                if _accepted(last_signal, setup, stock.code, calendar_index):
                    accepted_counts[setup] += 1
                    if in_output_period:
                        maximum_signal_date_read = max(maximum_signal_date_read or day, day)
                        base_features = {
                            **legacy.geometry,
                            **legacy.features,
                            "average_volume_20": legacy.average_volume_20,
                            "average_turnover_proxy_20": legacy.average_turnover_proxy_20,
                            "raw_overlap": legacy.raw_overlap,
                        }
                        parent = _signal_row(
                            setup=setup,
                            stock=stock,
                            local_index=local_index,
                            features=base_features,
                            status="LEGACY_BASELINE_UNCHANGED",
                            benchmark=benchmark,
                        )
                        rows.append(parent)
                        if setup == "N_RETEST" and is_compact_retest(legacy.geometry):
                            compact = dict(parent)
                            compact.update(
                                {
                                    "setup": "N_COMPACT_RETEST_HYPOTHESIS",
                                    "setup_status": (
                                        "DESCRIPTIVE_HYPOTHESIS_FROM_ALREADY_SEEN_2023_2024"
                                    ),
                                    "parent_setup": "N_RETEST",
                                    "compact_rule": (
                                        "pivot_separation_sessions <= 7 and bottom_difference > 0; "
                                        "confirmation and gap remain diagnostics, not filters"
                                    ),
                                }
                            )
                            rows.append(compact)
                            raw_counts["N_COMPACT_RETEST_HYPOTHESIS"] += 1
                            accepted_counts["N_COMPACT_RETEST_HYPOTHESIS"] += 1

            trend = detect_trend_pullback(stock, local_index, benchmark, CFG)
            if trend is not None:
                raw_counts["TREND_PULLBACK"] += 1
                if _accepted(
                    last_signal, "TREND_PULLBACK", stock.code, calendar_index
                ):
                    accepted_counts["TREND_PULLBACK"] += 1
                    if in_output_period:
                        maximum_signal_date_read = max(maximum_signal_date_read or day, day)
                        rows.append(
                            _signal_row(
                                setup="TREND_PULLBACK",
                                stock=stock,
                                local_index=local_index,
                                features=trend,
                                status="NEW_PREREGISTERED_ENGINEERING_BASELINE",
                                benchmark=benchmark,
                            )
                        )

            consolidation = detect_consolidation_breakout_v2(
                stock, local_index, benchmark, CFG
            )
            if consolidation is not None:
                raw_counts["CONSOLIDATION_BREAKOUT_V2"] += 1
                if _accepted(
                    last_signal,
                    "CONSOLIDATION_BREAKOUT_V2",
                    stock.code,
                    calendar_index,
                ):
                    accepted_counts["CONSOLIDATION_BREAKOUT_V2"] += 1
                    if in_output_period:
                        maximum_signal_date_read = max(maximum_signal_date_read or day, day)
                        rows.append(
                            _signal_row(
                                setup="CONSOLIDATION_BREAKOUT_V2",
                                stock=stock,
                                local_index=local_index,
                                features=consolidation,
                                status="NEW_PREREGISTERED_ENGINEERING_BASELINE",
                                benchmark=benchmark,
                            )
                        )

        for selected in _momentum_selected(contexts):
            observation = selected["observation"]
            stock = selected["stock"]
            local_index = selected["local_index"]
            raw_counts["MOMENTUM_DIRECTIONAL"] += 1
            if not _accepted(
                last_signal, "MOMENTUM_DIRECTIONAL", stock.code, calendar_index
            ):
                continue
            accepted_counts["MOMENTUM_DIRECTIONAL"] += 1
            if not in_output_period:
                continue
            maximum_signal_date_read = max(maximum_signal_date_read or day, day)
            feature_values = {
                feature: observation.features.get(feature)
                for feature in CFG.momentum_features
            }
            feature_values.update(
                {
                    f"{feature}_percentile": percentile
                    for feature, percentile in selected[
                        "score_feature_percentiles"
                    ].items()
                }
            )
            feature_values.update(
                {
                    "average_volume_20": observation.average_volume_20,
                    "average_turnover_proxy_20": observation.average_turnover_proxy_20,
                    "momentum_score": selected["momentum_score"],
                    "daily_rank": selected["daily_rank"],
                }
            )
            rows.append(
                _signal_row(
                    setup="MOMENTUM_DIRECTIONAL",
                    stock=stock,
                    local_index=local_index,
                    features=feature_values,
                    status="FROZEN_SURGE_V0_1_SCORE_RELABELED_WITH_DIRECTIONAL_OUTCOME",
                    benchmark=benchmark,
                    extra={"source_surge_rule_hash": CFG.source_surge_rule_hash},
                )
            )

    rows.sort(key=lambda row: (row["signal_date"], row["setup"], row["code"]))
    for row in rows:
        if row["outcome_status"] != "EVALUABLE":
            censored_reasons[row["outcome_reason"]] += 1
    return rows, {
        "raw_counts_including_warmup": dict(sorted(raw_counts.items())),
        "accepted_counts_including_warmup": dict(sorted(accepted_counts.items())),
        "censored_reasons": dict(sorted(censored_reasons.items())),
        "maximum_signal_date_read": maximum_signal_date_read,
        "continuous_cooldown_scan_started": CFG.warmup_start,
    }


def _period_calendar(benchmark) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for label, start, end in PERIODS:
        result[label] = [day for day in benchmark.calendar if start <= day <= end]
    return result


def _build_tables(rows: list[dict], benchmark) -> dict[str, list[dict]]:
    comparisons: list[dict] = []
    years: list[dict] = []
    gaps: list[dict] = []
    tails: list[dict] = []
    clustering: list[dict] = []
    bootstraps: list[dict] = []
    calendars = _period_calendar(benchmark)

    for period, _, _ in PERIODS:
        for setup in SETUPS:
            selected = [
                row for row in rows
                if row["period"] == period and row["setup"] == setup
            ]
            summary = metric_summary(selected)
            tail_rows = winner_dependence_rows(selected, setup, period)
            tails.extend(tail_rows)
            cluster_rows = signal_clustering_rows(
                selected, setup, period, calendars[period]
            )
            clustering.extend(cluster_rows)
            group_bootstrap = bootstrap_rows(selected, setup, period, CFG)
            bootstraps.extend(group_bootstrap)
            tail_by_case = {row["tail_case"]: row for row in tail_rows}
            cluster = cluster_rows[0]
            boot_by_key = {
                (row["metric"], row["cluster_unit"]): row
                for row in group_bootstrap
            }
            comparisons.append(
                {
                    "period": period,
                    "setup": setup,
                    **summary,
                    "active_signal_dates": summary["active_signal_dates"],
                    "concentration_effective_signal_dates": cluster[
                        "effective_signal_dates"
                    ],
                    "effective_signal_dates": cluster["effective_signal_dates"],
                    "plus8_before_minus5_rate": summary["primary_success_rate"],
                    "mfe": summary["average_mfe_10d"],
                    "mae": summary["average_mae_10d"],
                    "average_trade_mfe_abs_mae_ratio": summary[
                        "average_mfe_abs_mae"
                    ],
                    "aggregate_mfe_abs_mae_ratio": (
                        summary["average_mfe_10d"]
                        / abs(summary["average_mae_10d"])
                        if summary["average_mfe_10d"] is not None
                        and summary["average_mae_10d"] is not None
                        and abs(summary["average_mae_10d"]) > 1e-15
                        else None
                    ),
                    "mfe_abs_mae": (
                        summary["average_mfe_10d"]
                        / abs(summary["average_mae_10d"])
                        if summary["average_mfe_10d"] is not None
                        and summary["average_mae_10d"] is not None
                        and abs(summary["average_mae_10d"]) > 1e-15
                        else None
                    ),
                    "top1_removed_average_gross_return": tail_by_case[
                        "REMOVE_TOP_1PCT_WINNERS"
                    ]["average_gross_return"],
                    "top1_removed_pf": tail_by_case["REMOVE_TOP_1PCT_WINNERS"][
                        "gross_profit_factor"
                    ],
                    "top1_removed_average_net_return": tail_by_case[
                        "REMOVE_TOP_1PCT_WINNERS"
                    ]["average_net_return"],
                    "top1_removed_net_pf": tail_by_case[
                        "REMOVE_TOP_1PCT_WINNERS"
                    ]["net_profit_factor"],
                    "top5_removed_average_gross_return": tail_by_case[
                        "REMOVE_TOP_5PCT_WINNERS"
                    ]["average_gross_return"],
                    "top5_removed_pf": tail_by_case["REMOVE_TOP_5PCT_WINNERS"][
                        "gross_profit_factor"
                    ],
                    "top5_removed_average_net_return": tail_by_case[
                        "REMOVE_TOP_5PCT_WINNERS"
                    ]["average_net_return"],
                    "top5_removed_net_pf": tail_by_case[
                        "REMOVE_TOP_5PCT_WINNERS"
                    ]["net_profit_factor"],
                    "tail_dependent": tail_by_case[
                        "REMOVE_TOP_1PCT_WINNERS"
                    ]["tail_dependent"],
                    "TAIL_DEPENDENT": tail_by_case[
                        "REMOVE_TOP_1PCT_WINNERS"
                    ]["tail_dependent"],
                    "net_tail_dependent": tail_by_case[
                        "REMOVE_TOP_1PCT_WINNERS"
                    ]["net_tail_dependent"],
                    "remove_max_date_pf": cluster[
                        "remove_max_signal_date_gross_profit_factor"
                    ],
                    "remove_max_date_average_gross_return": cluster[
                        "remove_max_signal_date_average_gross_return"
                    ],
                    "signal_date_net_ci_low": boot_by_key[("net_return", "signal_date")][
                        "mean_ci_low"
                    ],
                    "signal_date_net_ci_high": boot_by_key[("net_return", "signal_date")][
                        "mean_ci_high"
                    ],
                    "month_net_ci_low": boot_by_key[("net_return", "month")][
                        "mean_ci_low"
                    ],
                    "month_net_ci_high": boot_by_key[("net_return", "month")][
                        "mean_ci_high"
                    ],
                    "signal_date_gross_ci_low": boot_by_key[("gross_return", "signal_date")][
                        "mean_ci_low"
                    ],
                    "signal_date_gross_ci_high": boot_by_key[("gross_return", "signal_date")][
                        "mean_ci_high"
                    ],
                    "month_gross_ci_low": boot_by_key[("gross_return", "month")][
                        "mean_ci_low"
                    ],
                    "month_gross_ci_high": boot_by_key[("gross_return", "month")][
                        "mean_ci_high"
                    ],
                    "signal_date_clusters": boot_by_key[
                        ("net_return", "signal_date")
                    ]["clusters"],
                    "month_clusters": boot_by_key[("net_return", "month")][
                        "clusters"
                    ],
                }
            )
            gaps.extend(entry_gap_rows(selected, setup, period))

    for year in ("2020", "2021", "2022", "2023", "2024", "2025"):
        period = period_label(year + "0101")
        for setup in SETUPS:
            selected = [
                row for row in rows if row["year"] == year and row["setup"] == setup
            ]
            years.append(
                {"year": year, "period": period, "setup": setup, **metric_summary(selected)}
            )
    return {
        "setup_comparison.csv": comparisons,
        "setup_year_summary.csv": years,
        "entry_gap_analysis.csv": gaps,
        "winner_dependence.csv": tails,
        "signal_clustering.csv": clustering,
        "bootstrap_results.csv": bootstraps,
    }


def _lookup(rows: list[dict], period: str, setup: str) -> dict:
    return next(row for row in rows if row["period"] == period and row["setup"] == setup)


def _edge_assessment(comparison: dict, yearly: list[dict]) -> dict:
    relevant_years = [row for row in yearly if row["year"] in {"2023", "2024"}]
    gross_positive_each_year = bool(relevant_years) and all(
        row["gross_average_return"] is not None
        and row["gross_average_return"] > 0
        and row["gross_profit_factor"] is not None
        and row["gross_profit_factor"] > 1
        for row in relevant_years
    )
    net_positive_each_year = bool(relevant_years) and all(
        row["net_average_return"] is not None
        and row["net_average_return"] > 0
        and row["net_profit_factor"] is not None
        and row["net_profit_factor"] > 1
        for row in relevant_years
    )
    gross_positive = bool(
        comparison["gross_average_return"] is not None
        and comparison["gross_average_return"] > 0
        and comparison["gross_profit_factor"] is not None
        and comparison["gross_profit_factor"] > 1
    )
    net_positive = bool(
        comparison["net_average_return"] is not None
        and comparison["net_average_return"] > 0
        and comparison["net_profit_factor"] is not None
        and comparison["net_profit_factor"] > 1
    )
    gross_ci_positive = bool(
        comparison["signal_date_gross_ci_low"] is not None
        and comparison["signal_date_gross_ci_low"] > 0
        and comparison["month_gross_ci_low"] is not None
        and comparison["month_gross_ci_low"] > 0
    )
    net_ci_positive = bool(
        comparison["signal_date_net_ci_low"] is not None
        and comparison["signal_date_net_ci_low"] > 0
        and comparison["month_net_ci_low"] is not None
        and comparison["month_net_ci_low"] > 0
    )
    gross_tail_survives = bool(
        comparison["top1_removed_pf"] is not None
        and comparison["top1_removed_pf"] > 1
        and comparison["top1_removed_average_gross_return"] is not None
        and comparison["top1_removed_average_gross_return"] > 0
    )
    net_tail_survives = bool(
        comparison["top1_removed_net_pf"] is not None
        and comparison["top1_removed_net_pf"] > 1
        and comparison["top1_removed_average_net_return"] is not None
        and comparison["top1_removed_average_net_return"] > 0
    )
    sample_sufficient = bool(
        comparison["evaluable_signals"] >= CFG.edge_minimum_evaluable
        and comparison["signal_date_clusters"]
        >= CFG.edge_minimum_signal_date_clusters
        and comparison["month_clusters"] >= CFG.edge_minimum_month_clusters
    )
    gross_statistically_supported = bool(
        sample_sufficient
        and gross_positive
        and gross_positive_each_year
        and gross_ci_positive
        and gross_tail_survives
    )
    net_robust = bool(
        sample_sufficient
        and net_positive
        and net_positive_each_year
        and net_ci_positive
        and net_tail_survives
    )
    if net_robust:
        status = "NET_ROBUST_RETROSPECTIVE_CANDIDATE_NOT_BLIND_OOS"
    elif gross_statistically_supported:
        status = "GROSS_EDGE_BUT_COST_ADJUSTED_EDGE_NOT_CONFIRMED"
    elif gross_positive:
        status = "DESCRIPTIVE_GROSS_POSITIVE_NOT_STATISTICALLY_CONFIRMED"
    else:
        status = "NO_DIRECTIONAL_EDGE_IN_RETROSPECTIVE_CONFIRMATION"
    return {
        "status": status,
        "gross_point_positive": gross_positive,
        "gross_positive_each_year": gross_positive_each_year,
        "gross_cluster_ci_positive": gross_ci_positive,
        "gross_top1_tail_survives": gross_tail_survives,
        "net_point_positive": net_positive,
        "net_positive_each_year": net_positive_each_year,
        "net_cluster_ci_positive": net_ci_positive,
        "net_top1_tail_survives": net_tail_survives,
        "gross_statistically_supported": gross_statistically_supported,
        "net_robust": net_robust,
        "minimum_sample_gate_passed": sample_sufficient,
        "minimum_sample_gate": {
            "evaluable": CFG.edge_minimum_evaluable,
            "signal_date_clusters": CFG.edge_minimum_signal_date_clusters,
            "month_clusters": CFG.edge_minimum_month_clusters,
        },
    }


def _validation_summary(
    tables: dict[str, list[dict]], scan_audit: dict, signal_rows: list[dict]
) -> dict:
    comparison = tables["setup_comparison.csv"]
    yearly = tables["setup_year_summary.csv"]
    retrospective = "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS"
    stress = "STRESS_PREVALENCE_SEEN_NOT_BLIND"
    statuses = {}
    for setup in SETUPS:
        assessment = _edge_assessment(
            _lookup(comparison, retrospective, setup),
            [row for row in yearly if row["setup"] == setup],
        )
        statuses[setup] = {
            "retrospective_status": assessment["status"],
            "edge_assessment": assessment,
            "retrospective_metrics": _lookup(comparison, retrospective, setup),
            "stress_metrics": _lookup(comparison, stress, setup),
        }

    descriptive_gross_positive = [
        setup for setup in SETUPS
        if statuses[setup]["edge_assessment"]["gross_point_positive"]
    ]
    descriptive_gross_positive.sort(
        key=lambda setup: (
            statuses[setup]["retrospective_metrics"]["gross_profit_factor"] or 0
        ),
        reverse=True,
    )
    gross_supported = [
        setup for setup in SETUPS
        if statuses[setup]["edge_assessment"]["gross_statistically_supported"]
    ]
    net_robust = [
        setup for setup in SETUPS
        if statuses[setup]["edge_assessment"]["net_robust"]
    ]
    n_base = statuses["N_RETEST"]["retrospective_metrics"]
    n_compact = statuses["N_COMPACT_RETEST_HYPOTHESIS"]["retrospective_metrics"]
    compact_better = bool(
        n_compact["gross_average_return"] is not None
        and n_base["gross_average_return"] is not None
        and n_compact["gross_average_return"] > n_base["gross_average_return"]
        and (n_compact["gross_profit_factor"] or 0) > (n_base["gross_profit_factor"] or 0)
    )
    retrospective_n = [
        row for row in signal_rows
        if row["period"] == retrospective and row["setup"] == "N_RETEST"
    ]
    retrospective_compact_keys = {
        (row["signal_date"], row["code"])
        for row in signal_rows
        if row["period"] == retrospective
        and row["setup"] == "N_COMPACT_RETEST_HYPOTHESIS"
    }
    retrospective_noncompact_n = [
        row for row in retrospective_n
        if (row["signal_date"], row["code"]) not in retrospective_compact_keys
    ]
    noncompact_summary = metric_summary(retrospective_noncompact_n)
    compact_return_sum = sum(
        float(row["gross_return"])
        for row in signal_rows
        if row["period"] == retrospective
        and row["setup"] == "N_COMPACT_RETEST_HYPOTHESIS"
        and row["outcome_status"] == "EVALUABLE"
    )
    n_return_sum = sum(
        float(row["gross_return"])
        for row in retrospective_n
        if row["outcome_status"] == "EVALUABLE"
    )
    compact_return_sum_share = (
        compact_return_sum / n_return_sum if n_return_sum > 0 else None
    )
    compact_is_primary_source = bool(
        n_base["gross_average_return"] is not None
        and n_base["gross_average_return"] > 0
        and (n_base["gross_profit_factor"] or 0) > 1
        and n_compact["gross_average_return"] is not None
        and n_compact["gross_average_return"] > 0
        and (n_compact["gross_profit_factor"] or 0) > 1
        and (
            noncompact_summary["gross_average_return"] is None
            or noncompact_summary["gross_average_return"] <= 0
            or (noncompact_summary["gross_profit_factor"] or 0) <= 1
        )
        and compact_return_sum_share is not None
        and compact_return_sum_share > 0.5
    )
    momentum_status = statuses["MOMENTUM_DIRECTIONAL"]["retrospective_status"]
    trend = statuses["TREND_PULLBACK"]["retrospective_metrics"]
    consolidation = statuses["CONSOLIDATION_BREAKOUT_V2"]["retrospective_metrics"]
    v_cluster = next(
        row for row in tables["signal_clustering.csv"]
        if row["period"] == retrospective and row["setup"] == "V_REVERSAL"
    )
    tail_survivors = [
        setup for setup in SETUPS
        if statuses[setup]["retrospective_metrics"]["top1_removed_pf"] is not None
        and statuses[setup]["retrospective_metrics"]["top1_removed_pf"] > 1
        and statuses[setup]["retrospective_metrics"][
            "top1_removed_average_gross_return"
        ] is not None
        and statuses[setup]["retrospective_metrics"][
            "top1_removed_average_gross_return"
        ] > 0
    ]
    net_tail_survivors = [
        setup for setup in SETUPS
        if statuses[setup]["retrospective_metrics"]["top1_removed_net_pf"] is not None
        and statuses[setup]["retrospective_metrics"]["top1_removed_net_pf"] > 1
        and statuses[setup]["retrospective_metrics"][
            "top1_removed_average_net_return"
        ] is not None
        and statuses[setup]["retrospective_metrics"][
            "top1_removed_average_net_return"
        ] > 0
    ]
    descriptive_net_positive = [
        setup for setup in SETUPS
        if statuses[setup]["edge_assessment"]["net_point_positive"]
    ]
    return {
        "study_id": CFG.strategy_id,
        "status": "COMPLETE_DESCRIPTIVE_RESEARCH_NOT_STRATEGY_VALIDATION",
        "result_status": CFG.result_status,
        "period_discipline": {
            "2020_2022": "HISTORICAL_DISCOVERY",
            "2023_2024": "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS",
            "2025": "STRESS_PREVALENCE_SEEN_NOT_BLIND",
            "new_confirmation": (
                "Must use prospective Shadow observations after 2026-09-06"
            ),
        },
        "feature_selection": "NONE; BH-FDR_NOT_APPLICABLE",
        "setup_statuses": statuses,
        "answers": {
            "A_descriptive_gross_point_positive": descriptive_gross_positive,
            "A_statistically_supported_gross_edge_candidates": gross_supported,
            "A_descriptive_net_point_positive": descriptive_net_positive,
            "A_cost_adjusted_robust_candidates": net_robust,
            "A_interpretation": (
                "Point-positive lists are descriptive. Statistically-supported lists "
                "also require the fixed sample gate, positive 2023 and 2024 results, "
                "positive signal-date and month cluster-CI lower bounds, and survival "
                "after removal of the top 1% winners. None is blind-OOS validation."
            ),
            "B_compact_retest_better_than_n_baseline": compact_better,
            "B_n_advantage_primarily_from_compact_retest": compact_is_primary_source,
            "B_compact_share_of_n_signals": (
                n_compact["signals"] / n_base["signals"]
                if n_base["signals"]
                else None
            ),
            "B_compact_gross_return_sum_share_of_n": compact_return_sum_share,
            "B_noncompact_n_metrics": noncompact_summary,
            "B_interpretation": (
                "Descriptive comparison only because compact structure was found after "
                "2023-2024 had already been inspected."
            ),
            "C_momentum_directional_status": momentum_status,
            "D_trend_pullback_worth_next_version": bool(
                statuses["TREND_PULLBACK"]["edge_assessment"][
                    "gross_statistically_supported"
                ]
            ),
            "E_consolidation_v2_descriptive_improvement": bool(
                statuses["CONSOLIDATION_BREAKOUT_V2"]["edge_assessment"][
                    "gross_statistically_supported"
                ]
            ),
            "E_legacy_comparison_warning": (
                "Legacy surge_compression portfolio returned -57.15% (-25.72% zero-cost), "
                "but its detector/execution differs; this flag is not a paired validation."
            ),
            "F_v_clustering": v_cluster,
            "G_top1_tail_survivors": tail_survivors,
            "G_top1_net_tail_survivors": net_tail_survivors,
            "H_ownership": (
                "TDCC_OWNERSHIP_FEATURES=NOT_TESTED_DATA_UNAVAILABLE; "
                "MARGIN_SHORT_FEATURES=AVAILABLE_OFFICIAL_NOT_INGESTED"
            ),
        },
        "scan_audit": scan_audit,
        "execution": {
            "portfolio_allocator": "NOT_RUN",
            "broker_connection": "ABSENT",
            "actual_orders": 0,
            "actual_fills": 0,
        },
    }


def _validate(rows: list[dict], tables: dict[str, list[dict]], data_audit: dict) -> dict:
    failures = []
    if data_audit.get("broad_source_gap_dates"):
        failures.append("broad_source_gap_dates_present")
    if any(row.get("is_actual_order") or row.get("is_actual_fill") for row in rows):
        failures.append("live_execution_flag_present")
    if any(row["signal_date"] > CFG.stress_end for row in rows):
        failures.append("post_2025_historical_signal_present")
    expected = {(period, setup) for period, _, _ in PERIODS for setup in SETUPS}
    observed = {
        (row["period"], row["setup"])
        for row in tables["setup_comparison.csv"]
    }
    if observed != expected:
        failures.append("comparison_matrix_incomplete")
    compact_keys = {
        (row["signal_date"], row["code"])
        for row in rows if row["setup"] == "N_COMPACT_RETEST_HYPOTHESIS"
    }
    n_keys = {
        (row["signal_date"], row["code"])
        for row in rows if row["setup"] == "N_RETEST"
    }
    if not compact_keys.issubset(n_keys):
        failures.append("compact_retest_not_subset_of_n_retest")
    if CFG.ownership_status != "NOT_TESTED_DATA_UNAVAILABLE":
        failures.append("ownership_fail_closed_status_changed")
    return {
        "passed": not failures,
        "failures": failures,
        "checks": {
            "single_shared_outcome_schema": True,
            "all_setup_period_cells_present": observed == expected,
            "compact_is_n_subset": compact_keys.issubset(n_keys),
            "no_live_order_or_fill": not any(
                row.get("is_actual_order") or row.get("is_actual_fill") for row in rows
            ),
            "no_portfolio_allocator": True,
            "ownership_fail_closed": CFG.ownership_status
            == "NOT_TESTED_DATA_UNAVAILABLE",
            "manifest_written_last": True,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fixed multi-setup directional event study; research only"
    )
    parser.add_argument("--archives", nargs="+", type=Path, required=True)
    parser.add_argument("--supplements", nargs="*", type=Path, default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _assert_outputs_absent(args.output_dir)
    package_dir = Path(__file__).resolve().parent
    repo = package_dir.parents[1]
    input_paths = args.archives + args.supplements
    input_hashes = [
        {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in input_paths
    ]
    code_hashes = {
        path.name: sha256_file(path) for path in sorted(package_dir.glob("*.py"))
    }
    shared_hashes = {
        "surge_event_study_v01/data.py": sha256_file(
            package_dir.parent / "surge_event_study_v01" / "data.py"
        ),
        "surge_event_study_v01/features.py": sha256_file(
            package_dir.parent / "surge_event_study_v01" / "features.py"
        ),
        "reversal_event_study_v01/study.py": sha256_file(
            package_dir.parent / "reversal_event_study_v01" / "study.py"
        ),
        "v21/backtest.py": sha256_file(package_dir.parent / "v21" / "backtest.py"),
        "v21/config.py": sha256_file(package_dir.parent / "v21" / "config.py"),
        "v21/diagnostic_analysis.py": sha256_file(
            package_dir.parent / "v21" / "diagnostic_analysis.py"
        ),
    }
    analysis_spec = {
        "status": "FROZEN_BEFORE_THIS_RUN",
        "strategy_id": CFG.strategy_id,
        "source_commit_before_run": _git_commit(repo),
        "config_hash": CFG.fingerprint(),
        "config": CFG.snapshot(),
        "input_hashes": input_hashes,
        "implementation_code_hashes": code_hashes,
        "shared_code_hashes": shared_hashes,
        "signal_clock": "T Close using T and earlier data only",
        "entry_proxy": "T+1 regular-session Open; not an odd-lot actual fill",
        "primary_outcome": "+8% before -5% within 10 trading days using Close",
        "descriptive_outcomes": [
            "+10% before -5% using Close",
            "+15% before -5% using Close",
        ],
        "parameter_search": "NONE",
        "feature_selection": "NONE",
        "portfolio_allocator": "NONE",
        "resolved_cost_model": {
            "per_trade_notional": CFG.per_trade_notional,
            "discounted_commission_rate_per_side": CFG.commission_rate,
            "minimum_commission_twd": CFG.minimum_commission,
            "sell_tax_rate": CFG.sell_tax_rate,
            "slippage_one_way": CFG.slippage_one_way,
            "semantics": (
                "same-close theoretical independent-signal proxy; not an executable fill"
            ),
        },
        "ownership_features": CFG.ownership_status,
        "period_disclosure": {
            label: [start, end] for label, start, end in PERIODS
        },
    }

    with tempfile.TemporaryDirectory(prefix="multi_setup_v01_") as staging_name:
        staging = Path(staging_name)
        _write_json(staging / "analysis_spec.json", analysis_spec)

        stocks, benchmark_bars, load_audit = load_ohlcv(
            args.archives, supplement_paths=args.supplements, cfg=CFG
        )
        prepared, benchmark, prepare_audit = prepare_stocks(stocks, benchmark_bars, CFG)
        data_audit = {
            **load_audit,
            **prepare_audit,
            "input_hashes": input_hashes,
            "shared_code_hashes": shared_hashes,
            "ownership_features": CFG.ownership_status,
            "margin_short_features": CFG.margin_short_status,
        }
        _write_json(staging / "data_audit.json", data_audit)

        signal_rows, scan_audit = scan_setups(prepared, benchmark)
        _write_csv(staging / "signal_observations.csv", signal_rows)
        tables = _build_tables(signal_rows, benchmark)
        for name, rows in tables.items():
            _write_csv(staging / name, rows)

        validation_summary = _validation_summary(tables, scan_audit, signal_rows)
        _write_json(staging / "validation_summary.json", validation_summary)
        pipeline_validation = _validate(signal_rows, tables, data_audit)
        _write_json(staging / "pipeline_validation.json", pipeline_validation)
        if not pipeline_validation["passed"]:
            raise RuntimeError(
                f"pipeline validation failed: {pipeline_validation['failures']}"
            )

        artifact_names = [
            *AUDIT_ARTIFACTS,
            *[name for name in SUMMARY_ARTIFACTS if name != "run_manifest.json"],
        ]
        artifact_hashes = {
            name: sha256_file(staging / name) for name in artifact_names
        }
        manifest = {
            "status": "COMPLETE",
            "strategy_id": CFG.strategy_id,
            "result_status": CFG.result_status,
            "config_hash": CFG.fingerprint(),
            "source_commit_before_run": analysis_spec["source_commit_before_run"],
            "input_hashes": input_hashes,
            "implementation_code_hashes": code_hashes,
            "shared_code_hashes": shared_hashes,
            "artifact_sha256": artifact_hashes,
            "artifacts": artifact_names + ["run_manifest.json"],
            "actual_orders": 0,
            "actual_fills": 0,
        }
        _write_json(staging / "run_manifest.json", manifest)

        # Publish only after every calculation and invariant has passed. The
        # COMPLETE manifest is deliberately moved last.
        for name in artifact_names:
            shutil.move(str(staging / name), args.output_dir / name)
        shutil.move(
            str(staging / "run_manifest.json"), args.output_dir / "run_manifest.json"
        )

    print(
        json.dumps(
            {
                "manifest": manifest,
                "answers": validation_summary["answers"],
                "signal_rows": len(signal_rows),
            },
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
