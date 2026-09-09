from __future__ import annotations

import numpy as np

from cross_sectional_alpha_ranking_v01.config import PERIODS

from .analysis import finite_mean, remove_best_cluster_mean
from .config import CFG


LATER = (
    "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS",
    "STRESS_PREVALENCE_SEEN_NOT_BLIND",
)


def _one(rows: list[dict], **criteria) -> dict:
    matches = [row for row in rows if all(row.get(key) == value for key, value in criteria.items())]
    if len(matches) != 1:
        raise RuntimeError(f"expected one summary row for {criteria}, got {len(matches)}")
    return matches[0]


def build_validation(
    stage_a_period: list[dict],
    two_period: list[dict],
    stage_a_daily: list[dict],
    two_daily: list[dict],
    complementarity: list[dict],
    quadrants: list[dict],
    frequency: list[dict],
    n_compact: list[dict],
    regimes: list[dict],
    gaps: list[dict],
    selected_alpha: float,
) -> dict:
    stage = {
        label: _one(stage_a_period, model=CFG.stage_a_primary_model, time_slice=label)
        for label in LATER
    }
    two = {label: _one(two_period, cohort="TWO_STAGE_TOP5", time_slice=label) for label in LATER}
    stage_gate = {"later_period_refit_count_zero": True}
    two_gate = {"stage_b_refit_count_zero": True, "later_period_refit_count_zero": True}
    concentration = {}
    for label, start, end in PERIODS[1:]:
        daily_a = [r for r in stage_a_daily if r["model"] == CFG.stage_a_primary_model and int(start) <= r["signal_date"] <= int(end)]
        daily_t = [r for r in two_daily if int(start) <= r["signal_date"] <= int(end)]
        a_date = remove_best_cluster_mean(daily_a, "top30_mfe10_spread", "signal_date")
        a_month = remove_best_cluster_mean(daily_a, "top30_mfe10_spread", "month")
        t_date = remove_best_cluster_mean(daily_t, "two_stage_net_spread", "signal_date")
        t_month = remove_best_cluster_mean(daily_t, "two_stage_net_spread", "month")
        concentration[label] = {
            "stage_a_remove_best_date_mfe_spread": a_date,
            "stage_a_remove_best_month_mfe_spread": a_month,
            "two_stage_remove_best_date_net_spread": t_date,
            "two_stage_remove_best_month_net_spread": t_month,
        }
        short = "2023_2024" if label.startswith("RETROSPECTIVE") else "2025"
        row_a = stage[label]
        stage_gate[f"{short}_mfe10_above_universe"] = bool(row_a["mfe10_lift_difference"] > 0)
        stage_gate[f"{short}_winner_rate_above_universe"] = bool(row_a["winner_rate_lift"] > 1)
        stage_gate[f"{short}_mfe_ic_positive"] = bool(row_a["mean_daily_mfe_ic"] > 0)
        stage_gate[f"{short}_top1_extreme_mfe_removed_lift_positive"] = bool(row_a["top1_extreme_mfe_removed_lift"] > 0)
        stage_gate[f"{short}_not_single_date_dependent"] = bool(a_date is not None and a_date > 0)
        stage_gate[f"{short}_not_single_month_dependent"] = bool(a_month is not None and a_month > 0)
        row_t = two[label]
        two_gate[f"{short}_net_mean_positive"] = bool(row_t["net_mean"] > 0)
        two_gate[f"{short}_net_pf_above_one"] = bool(row_t["net_profit_factor"] > 1)
        two_gate[f"{short}_mfe10_above_universe"] = bool(row_t["mfe10_lift_difference"] > 0)
        two_gate[f"{short}_mae_better_than_stage_a"] = bool(row_t["mae10_mean"] > row_a["mae10_mean"])
        two_gate[f"{short}_top1_removed_edge_remains"] = bool(row_t["top1_removed_net_mean"] > 0 and row_t["top1_removed_net_pf"] > 1)
        two_gate[f"{short}_not_single_date_dependent"] = bool(t_date is not None and t_date > 0)
        two_gate[f"{short}_not_single_month_dependent"] = bool(t_month is not None and t_month > 0)

    stage_pass = all(stage_gate.values())
    two_pass = all(two_gate.values())
    partial_upside = all(stage[label]["mfe10_lift_difference"] > 0 and stage[label]["mean_daily_mfe_ic"] > 0 for label in LATER)
    if stage_pass and two_pass:
        classification = "PROMISING_FOR_TWO_STAGE_PROSPECTIVE_SHADOW"
    elif stage_pass:
        classification = "UPSIDE_SIGNAL_FOUND_BUT_NOT_TRADEABLE"
    elif partial_upside:
        classification = "DESCRIPTIVE_ONLY"
    else:
        classification = "NO_STABLE_UPSIDE_EDGE"

    comp = {label: _one(complementarity, time_slice=label) for label in LATER}
    quadrant_by_period = {
        label: [row for row in quadrants if row["time_slice"] == label] for label in LATER
    }
    high_high = {
        label: _one(quadrants, time_slice=label, quadrant="HIGH_UPSIDE_HIGH_RISK_QUALITY")
        for label in LATER
    }
    best_quadrant = {
        label: max(rows, key=lambda row: row["net_mean"] if row["net_mean"] is not None else -999)["quadrant"]
        for label, rows in quadrant_by_period.items()
    }
    overall_frequency = _one(frequency, time_slice="2020_2025", ranking_mode="10D_COOLDOWN_TRADE_PROXY")
    details = [row for row in n_compact if row.get("row_type") == "SIGNAL_DETAIL"]
    cohort_overall = {
        row["cohort"]: row for row in n_compact
        if row.get("row_type") == "COHORT_SUMMARY" and row.get("time_slice") == "2020_2025"
    }
    compact_count = cohort_overall["N_COMPACT_ONLY"]["observations"]
    two_count = cohort_overall["TWO_STAGE_TOP5_ONLY"]["observations"]
    union_count = cohort_overall["N_COMPACT_UNION_TWO_STAGE_TOP5"]["observations"]
    regime_answer = {}
    for label in LATER:
        local = [row for row in regimes if row["time_slice"] == label and row["cohort"] == "TWO_STAGE_TOP5"]
        values = [row["net_mean"] for row in local if row["net_mean"] is not None]
        regime_answer[label] = {
            "minimum_net_mean": min(values), "maximum_net_mean": max(values),
            "sign_changes_across_slices": min(values) < 0 < max(values),
        }
    gap_answer = {
        label: [
            {"bucket": row["entry_gap_bucket"], "net_mean": row["net_mean"], "mfe10": row["mfe10_mean"], "mae10": row["mae10_mean"]}
            for row in gaps if row["time_slice"] == label and row["cohort"] == "TWO_STAGE_TOP5"
        ] for label in LATER
    }
    stage_a_diagnostics = {
        label: {
            "mfe10": stage[label]["mfe10_mean"],
            "universe_mfe10": stage[label]["universe_mfe10_mean"],
            "mfe_lift_difference": stage[label]["mfe10_lift_difference"],
            "winner_rate": stage[label]["winner_rate"],
            "winner_rate_lift": stage[label]["winner_rate_lift"],
            "plus10_rate": stage[label]["plus10_before_minus5_rate"],
            "plus10_universe": stage[label]["universe_plus10_rate"],
            "plus15_rate": stage[label]["plus15_before_minus5_rate"],
            "plus15_universe": stage[label]["universe_plus15_rate"],
            "mae10": stage[label]["mae10_mean"],
            "universe_mae10": stage[label]["universe_mae10_mean"],
            "volatility20": stage[label]["signal_volatility20_mean"],
            "universe_volatility20": stage[label]["universe_volatility20_mean"],
            "mean_daily_mfe_ic": stage[label]["mean_daily_mfe_ic"],
        } for label in LATER
    }
    two_stage_diagnostics = {
        label: {key: two[label][key] for key in (
            "winner_rate", "plus10_before_minus5_rate", "plus15_before_minus5_rate",
            "mfe10_mean", "mae10_mean", "gross_mean", "net_mean",
            "gross_profit_factor", "net_profit_factor", "top1_removed_net_mean",
            "top1_removed_net_pf", "top5_removed_net_mean", "top5_removed_net_pf",
        )} for label in LATER
    }
    return {
        "study_id": CFG.study_id,
        "status": "COMPLETE",
        "final_classification": classification,
        "selected_stage_a_alpha": selected_alpha,
        "stage_a_promotion_gate": stage_gate,
        "two_stage_promotion_gate": two_gate,
        "concentration_diagnostics": concentration,
        "stage_a_later_periods": stage_a_diagnostics,
        "two_stage_later_periods": two_stage_diagnostics,
        "answers": {
            "1_mfe10_stably_predictable": stage_pass,
            "2_stage_a_mfe10_lift": {label: stage[label]["mfe10_lift_difference"] for label in LATER},
            "3_stage_a_barrier_rate_lifts": {label: {
                "plus8_difference": stage[label]["winner_rate"] - stage[label]["universe_winner_rate"],
                "plus10_difference": stage[label]["plus10_before_minus5_rate"] - stage[label]["universe_plus10_rate"],
                "plus15_difference": stage[label]["plus15_before_minus5_rate"] - stage[label]["universe_plus15_rate"],
            } for label in LATER},
            "4_stage_a_volatility_context": {label: {"selected": stage[label]["signal_volatility20_mean"], "universe": stage[label]["universe_volatility20_mean"]} for label in LATER},
            "5_stage_a_mae_change": {label: stage[label]["mae10_mean"] - stage[label]["universe_mae10_mean"] for label in LATER},
            "6_score_correlations": {label: {"spearman": comp[label]["daily_score_spearman_mean"], "pearson": comp[label]["daily_score_pearson_mean"]} for label in LATER},
            "7_complementary_information": all(abs(comp[label]["daily_score_spearman_mean"]) < 0.5 for label in LATER),
            "8_high_high_quadrant": {label: {"is_best_net_quadrant": best_quadrant[label] == "HIGH_UPSIDE_HIGH_RISK_QUALITY", "net_mean": high_high[label]["net_mean"], "mfe10": high_high[label]["mfe10_mean"], "mae10": high_high[label]["mae10_mean"]} for label in LATER},
            "9_10_two_stage_payoff": two_stage_diagnostics,
            "11_same_payoff_direction": all(two[label]["net_mean"] > 0 for label in LATER),
            "12_tail_removal": {label: {"top1_net_mean": two[label]["top1_removed_net_mean"], "top1_net_pf": two[label]["top1_removed_net_pf"], "top5_net_mean": two[label]["top5_removed_net_mean"], "top5_net_pf": two[label]["top5_removed_net_pf"]} for label in LATER},
            "13_cooldown_opportunities_per_month": overall_frequency["opportunities_per_month"],
            "14_opportunity_increase_vs_n_compact": {"two_stage_cooldown_per_month": overall_frequency["opportunities_per_month"], "n_compact_observations_per_month_proxy": compact_count / 72.0},
            "15_n_compact_stage_a_rank": {"signals": len(details), "median_rank": finite_mean(np.asarray([r["stage_a_rank"] for r in details], dtype=float)) if not details else float(np.median([r["stage_a_rank"] for r in details])), "top30_share": float(np.mean([r["in_stage_a_top30"] for r in details])) if details else None},
            "16_two_stage_n_compact_overlap": {"two_stage_observations": two_count, "n_compact_observations": compact_count, "overlap": two_count + compact_count - union_count},
            "17_market_regime": regime_answer,
            "18_entry_gap": gap_answer,
            "19_build_prospective_two_stage_shadow": classification == "PROMISING_FOR_TWO_STAGE_PROSPECTIVE_SHADOW",
        },
        "later_period_refit_count": 0,
        "stage_b_refit_count": 0,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
