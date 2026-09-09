from __future__ import annotations

import numpy as np


LATER = (
    "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS",
    "STRESS_PREVALENCE_SEEN_NOT_BLIND",
)


def _one(rows: list[dict], **criteria) -> dict:
    matches = [row for row in rows if all(row.get(key) == value for key, value in criteria.items())]
    if len(matches) != 1:
        raise RuntimeError(f"expected one row for {criteria}, got {len(matches)}")
    return matches[0]


def build_validation(
    comparison: list[dict],
    calibration: list[dict],
    separation: list[dict],
    frequency: list[dict],
    n_compact: list[dict],
    regimes: list[dict],
    gaps: list[dict],
    coefficient_stability: dict,
    selected_c: float,
) -> dict:
    stage = {
        label: _one(comparison, time_slice_type="PERIOD", time_slice=label, cohort="STAGE_A_TOP30")
        for label in LATER
    }
    conditional = {
        label: _one(comparison, time_slice_type="PERIOD", time_slice=label, cohort="CONDITIONAL_TOP5")
        for label in LATER
    }
    previous = {
        label: _one(comparison, time_slice_type="PERIOD", time_slice=label, cohort="PREVIOUS_TWO_STAGE_TOP5")
        for label in LATER
    }
    path_gate = {"later_period_refit_count_zero": True, "stage_a_refit_count_zero": True}
    payoff_signs = [int(np.sign(conditional[label]["net_mean"])) for label in LATER]
    same_payoff_direction = payoff_signs[0] == payoff_signs[1]
    payoff_direction = "POSITIVE" if payoff_signs[0] > 0 else "NEGATIVE" if payoff_signs[0] < 0 else "ZERO"
    trade_gate = {"same_payoff_direction": same_payoff_direction}
    period_diagnostics = {}
    for label in LATER:
        short = "2023_2024" if label.startswith("RETROSPECTIVE") else "2025"
        base = stage[label]
        top = conditional[label]
        retention = top["mfe10_mean"] / base["mfe10_mean"]
        mae_improvement = top["mae10_mean"] - base["mae10_mean"]
        path_gate[f"{short}_success_above_stage_a"] = bool(top["path_success_rate"] > base["path_success_rate"])
        path_gate[f"{short}_downside_first_below_stage_a"] = bool(top["downside_first_rate"] < base["downside_first_rate"])
        path_gate[f"{short}_mae_better_than_stage_a"] = bool(mae_improvement > 0)
        path_gate[f"{short}_mfe_retention_at_least_80pct"] = bool(retention >= 0.80)
        trade_gate[f"{short}_net_mean_positive"] = bool(top["net_mean"] > 0)
        trade_gate[f"{short}_net_pf_above_one"] = bool(top["net_profit_factor"] > 1)
        trade_gate[f"{short}_top1_removed_edge_remains"] = bool(
            top["top1_removed_net_mean"] > 0 and top["top1_removed_net_pf"] > 1
        )
        trade_gate[f"{short}_not_single_date_dependent"] = bool(
            top["remove_best_signal_date_net_mean"] > 0 and top["remove_best_signal_date_net_pf"] > 1
        )
        trade_gate[f"{short}_not_single_month_dependent"] = bool(
            top["remove_best_calendar_month_net_mean"] > 0 and top["remove_best_calendar_month_net_pf"] > 1
        )
        period_diagnostics[label] = {
            "stage_a_top30": {key: base[key] for key in (
                "path_success_rate", "downside_first_rate", "timeout_rate", "mfe10_mean",
                "mae10_mean", "gross_mean", "net_mean", "gross_profit_factor", "net_profit_factor",
            )},
            "conditional_top5": {key: top[key] for key in (
                "path_success_rate", "downside_first_rate", "timeout_rate",
                "plus10_before_minus5_rate", "plus15_before_minus5_rate", "mfe10_mean",
                "mae10_mean", "gross_mean", "net_mean", "gross_profit_factor", "net_profit_factor",
                "top1_removed_net_mean", "top1_removed_net_pf", "top5_removed_net_mean",
                "top5_removed_net_pf", "remove_best_signal_date_net_mean",
                "remove_best_calendar_month_net_mean",
            )},
            "previous_two_stage_top5": {key: previous[label][key] for key in (
                "path_success_rate", "downside_first_rate", "mfe10_mean", "mae10_mean",
                "gross_mean", "net_mean", "gross_profit_factor", "net_profit_factor",
            )},
            "deltas_vs_stage_a": {
                "path_success": top["path_success_rate"] - base["path_success_rate"],
                "plus10": top["plus10_before_minus5_rate"] - base["plus10_before_minus5_rate"],
                "plus15": top["plus15_before_minus5_rate"] - base["plus15_before_minus5_rate"],
                "downside_first": top["downside_first_rate"] - base["downside_first_rate"],
                "mfe10": top["mfe10_mean"] - base["mfe10_mean"],
                "mae10": mae_improvement,
                "net_mean": top["net_mean"] - base["net_mean"],
                "net_pf": top["net_profit_factor"] - base["net_profit_factor"],
            },
            "mfe_retention_ratio": retention,
            "mae_absolute_improvement": mae_improvement,
            "mae_percentage_improvement": mae_improvement / abs(base["mae10_mean"]),
        }

    path_pass = all(path_gate.values())
    trade_pass = all(trade_gate.values())
    stable_core_path = all(
        conditional[label]["path_success_rate"] > stage[label]["path_success_rate"]
        and conditional[label]["downside_first_rate"] < stage[label]["downside_first_rate"]
        and conditional[label]["mae10_mean"] > stage[label]["mae10_mean"]
        for label in LATER
    )
    if path_pass and trade_pass:
        classification = "PROMISING_FOR_PROSPECTIVE_CONDITIONAL_SHADOW"
    elif path_pass:
        classification = "PATH_SIGNAL_FOUND_BUT_NOT_TRADEABLE"
    elif stable_core_path:
        classification = "DESCRIPTIVE_ONLY"
    else:
        classification = "NO_CONDITIONAL_PATH_EDGE"

    calibration_answer = {}
    for label in LATER:
        deciles = [
            row for row in calibration
            if row["time_slice"] == label and row["row_type"] == "PROBABILITY_DECILE"
        ]
        calibration_answer[label] = {
            "decile_success_spearman": deciles[0]["decile_success_spearman"],
            "strictly_nondecreasing": deciles[0]["strictly_nondecreasing_realized_success"],
            "expected_calibration_error": deciles[0]["expected_calibration_error"],
            "bottom_decile_realized": _one(deciles, probability_decile=1)["event_rate"],
            "top_decile_realized": _one(deciles, probability_decile=10)["event_rate"],
        }
    separation_answer = {
        label: {
            key: _one(separation, time_slice_type="PERIOD", time_slice=label)[key]
            for key in (
                "top5_path_success_rate", "bottom5_path_success_rate",
                "top_minus_bottom_path_success_rate", "top5_downside_first_rate",
                "bottom5_downside_first_rate", "top_minus_bottom_downside_first_rate",
                "top5_mfe10_mean", "bottom5_mfe10_mean", "top5_mae10_mean",
                "bottom5_mae10_mean", "top5_net_mean", "bottom5_net_mean",
            )
        }
        for label in LATER
    }
    overall_frequency = _one(
        frequency, time_slice_type="OVERALL", time_slice="2020_2025",
        ranking_mode="10D_COOLDOWN_TRADE_PROXY",
    )
    details = [row for row in n_compact if row.get("row_type") == "SIGNAL_DETAIL"]
    cohort = {
        row["cohort"]: row for row in n_compact
        if row.get("row_type") == "COHORT_SUMMARY" and row.get("time_slice") == "2020_2025"
    }
    overlap = (
        cohort["N_COMPACT_ONLY"]["observations"]
        + cohort["CONDITIONAL_TOP5_ONLY"]["observations"]
        - cohort["N_COMPACT_UNION_CONDITIONAL_TOP5"]["observations"]
    )
    regime_answer = {}
    for label in LATER:
        local = [row for row in regimes if row["time_slice"] == label]
        nets = [row["net_mean"] for row in local if row["net_mean"] is not None]
        successes = [row["path_success_rate"] for row in local if row["path_success_rate"] is not None]
        regime_answer[label] = {
            "net_min": min(nets), "net_max": max(nets),
            "success_min": min(successes), "success_max": max(successes),
            "net_sign_changes": min(nets) < 0 < max(nets),
        }
    regime_sensitive = any(value["net_sign_changes"] for value in regime_answer.values())
    gap_answer = {
        label: [
            {
                "bucket": row["entry_gap_bucket"],
                "path_success_rate": row["path_success_rate"],
                "mfe10": row["mfe10_mean"],
                "mae10": row["mae10_mean"],
                "net_mean": row["net_mean"],
            }
            for row in gaps if row["time_slice"] == label
        ] for label in LATER
    }
    high_gap_bottleneck = all(
        _one(gaps, time_slice=label, entry_gap_bucket="GE_3PCT")["net_mean"]
        < _one(gaps, time_slice=label, entry_gap_bucket="0_TO_1PCT")["net_mean"]
        for label in LATER
    )
    evidence_ohlcv_direction_insufficient = classification == "NO_CONDITIONAL_PATH_EDGE"
    return {
        "study_id": "CONDITIONAL_PATH_QUALITY_RANKING_V0_1",
        "status": "COMPLETE",
        "final_classification": classification,
        "selected_regularization_c": selected_c,
        "primary_target": "SUCCESS_EQUALS_PLUS_8_BEFORE_MINUS_5__TIMEOUT_IS_NON_SUCCESS",
        "path_promotion_gate": path_gate,
        "tradeability_gate": trade_gate,
        "period_diagnostics": period_diagnostics,
        "calibration_diagnostics": calibration_answer,
        "top_bottom_separation": separation_answer,
        "coefficient_stability": coefficient_stability,
        "answers": {
            "1_path_success_stably_predictable": stable_core_path,
            "2_conditional_top5_hit_rate": {label: conditional[label]["path_success_rate"] for label in LATER},
            "3_hit_rate_improvement_vs_stage_a": {label: conditional[label]["path_success_rate"] - stage[label]["path_success_rate"] for label in LATER},
            "4_downside_first_change": {label: conditional[label]["downside_first_rate"] - stage[label]["downside_first_rate"] for label in LATER},
            "5_mae10": {label: conditional[label]["mae10_mean"] for label in LATER},
            "6_7_mfe_and_retention": {label: {"mfe10": conditional[label]["mfe10_mean"], "retention_ratio": period_diagnostics[label]["mfe_retention_ratio"]} for label in LATER},
            "8_9_payoff": {label: {key: conditional[label][key] for key in ("gross_mean", "net_mean", "gross_profit_factor", "net_profit_factor")} for label in LATER},
            "10_same_payoff_direction": {
                "same_direction": same_payoff_direction,
                "direction": payoff_direction if same_payoff_direction else "MIXED",
            },
            "11_tail_removal": {label: {key: conditional[label][key] for key in ("top1_removed_net_mean", "top1_removed_net_pf", "top5_removed_net_mean", "top5_removed_net_pf")} for label in LATER},
            "12_top_bottom_separation": separation_answer,
            "13_probability_calibration": calibration_answer,
            "14_directional_features": coefficient_stability.get("largest_absolute_final_coefficients", []),
            "15_better_than_previous_net_stage_b": {label: {"net_mean_difference": conditional[label]["net_mean"] - previous[label]["net_mean"], "path_success_difference": conditional[label]["path_success_rate"] - previous[label]["path_success_rate"]} for label in LATER},
            "16_cooldown_opportunities_per_month": overall_frequency["cooldown_opportunities_per_month"],
            "17_n_compact_overlap": {"n_compact": cohort["N_COMPACT_ONLY"]["observations"], "conditional_top5": cohort["CONDITIONAL_TOP5_ONLY"]["observations"], "overlap": overlap, "n_compact_in_stage_a_top30": int(sum(row["in_stage_a_top30"] for row in details)), "n_compact_in_conditional_top5": int(sum(row["in_conditional_top5"] for row in details))},
            "18_regime_sensitive": {"flag": regime_sensitive, "details": regime_answer},
            "19_t_plus_1_gap_primary_bottleneck": {"flag": high_gap_bottleneck, "details": gap_answer},
            "20_build_prospective_conditional_path_shadow": classification == "PROMISING_FOR_PROSPECTIVE_CONDITIONAL_SHADOW",
            "21_ohlcv_direction_insufficient_consider_chip_incremental_study": evidence_ohlcv_direction_insufficient,
        },
        "later_period_refit_count": 0,
        "stage_a_refit_count": 0,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
