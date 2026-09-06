from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
import hashlib
import math
import random
import statistics

from .config import CFG, Config
from .study import PATTERNS


def quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def benjamini_hochberg(pvalues: dict[str, float]) -> dict[str, float]:
    ordered = sorted(pvalues.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    adjusted: dict[str, float] = {}
    running = 1.0
    for reverse_index in range(count - 1, -1, -1):
        name, pvalue = ordered[reverse_index]
        rank = reverse_index + 1
        running = min(running, pvalue * count / rank)
        adjusted[name] = min(1.0, running)
    return adjusted


def _evaluable(rows: list[dict]) -> list[dict]:
    return [
        row
        for row in rows
        if row.get("cooldown_included") and row.get("outcome_status") == "EVALUABLE"
    ]


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses == 0:
        return None
    return gains / losses


def metric_summary(rows: list[dict]) -> dict:
    causal = [row for row in rows if row.get("cooldown_included")]
    evaluable = [row for row in causal if row.get("outcome_status") == "EVALUABLE"]
    successes = sum(bool(row["primary_success"]) for row in evaluable)
    day10 = [float(row["day10_close_return"]) for row in evaluable]
    mfe = [float(row["mfe_close_10"]) for row in evaluable]
    mae = [float(row["mae_close_10"]) for row in evaluable]
    rule_returns = [float(row["gross_close_rule_return"]) for row in evaluable]
    winning_indices = [
        index for index, value in enumerate(rule_returns) if value > 0.0
    ]
    remove_count = math.ceil(len(winning_indices) * 0.01) if winning_indices else 0
    removed = set(
        sorted(
            winning_indices,
            key=lambda index: (rule_returns[index], index),
            reverse=True,
        )[:remove_count]
    )
    trimmed = [
        value for index, value in enumerate(rule_returns) if index not in removed
    ]
    path_counts: dict[str, int] = defaultdict(int)
    for row in evaluable:
        path_counts[str(row["path_result"])] += 1
    return {
        "raw_signal_count": len(rows),
        "causal_cooldown_signal_count": len(causal),
        "evaluable_count": len(evaluable),
        "outcome_observation_rate": len(evaluable) / len(causal) if causal else None,
        "success_count": successes,
        "success_rate": successes / len(evaluable) if evaluable else None,
        "average_day10_close_return": statistics.fmean(day10) if day10 else None,
        "median_day10_close_return": statistics.median(day10) if day10 else None,
        "positive_day10_rate": sum(value > 0 for value in day10) / len(day10)
        if day10
        else None,
        "average_mfe_close_10": statistics.fmean(mfe) if mfe else None,
        "average_mae_close_10": statistics.fmean(mae) if mae else None,
        "average_gross_close_rule_return": statistics.fmean(rule_returns)
        if rule_returns
        else None,
        "median_gross_close_rule_return": statistics.median(rule_returns)
        if rule_returns
        else None,
        "gross_close_rule_profit_factor": _profit_factor(rule_returns),
        "top1_percent_removed_count": remove_count,
        "top1_percent_removed_average_gross_close_rule_return": statistics.fmean(trimmed)
        if trimmed
        else None,
        "top1_percent_removed_profit_factor": _profit_factor(trimmed),
        "distinct_signal_dates": len({row["signal_date"] for row in causal}),
        "path_counts": dict(sorted(path_counts.items())),
    }


def summary_table(rows: list[dict], period: str) -> tuple[list[dict], dict]:
    output: list[dict] = []
    lookup: dict[str, dict] = {}
    years = sorted({row["signal_date"][:4] for row in rows})
    for pattern in (*PATTERNS, "ALL_UNIQUE"):
        pattern_rows = rows if pattern == "ALL_UNIQUE" else [
            row for row in rows if row["pattern"] == pattern
        ]
        for year in (*years, "ALL"):
            subset = pattern_rows if year == "ALL" else [
                row for row in pattern_rows if row["signal_date"].startswith(year)
            ]
            metrics = metric_summary(subset)
            record = {"period": period, "pattern": pattern, "year": year, **metrics}
            output.append(record)
            if year == "ALL":
                lookup[pattern] = metrics
    return output, lookup


def _bootstrap_sample_by_month(rows: list[dict], generator: random.Random) -> list[dict]:
    by_year_month: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        by_year_month[row["signal_date"][:4]][row["signal_date"][:6]].append(row)
    sample: list[dict] = []
    for year in sorted(by_year_month):
        month_map = by_year_month[year]
        months = sorted(month_map)
        for _ in months:
            sample.extend(month_map[generator.choice(months)])
    return sample


def cluster_bootstrap(
    rows: list[dict], pattern: str, cfg: Config = CFG, iterations: int | None = None
) -> dict:
    causal = [
        row
        for row in rows
        if row.get("cooldown_included")
        and row.get("outcome_status") == "EVALUABLE"
        and (pattern == "ALL_UNIQUE" or row["pattern"] == pattern)
    ]
    count = iterations or cfg.bootstrap_iterations
    seed = cfg.bootstrap_seed + int(
        hashlib.sha256(pattern.encode("utf-8")).hexdigest()[:8], 16
    )
    generator = random.Random(seed)
    success_rates: list[float] = []
    day10_means: list[float] = []
    rule_means: list[float] = []
    profit_factors: list[float] = []
    for _ in range(count):
        sample = _bootstrap_sample_by_month(causal, generator)
        if not sample:
            continue
        success_rates.append(
            sum(bool(row["primary_success"]) for row in sample) / len(sample)
        )
        day10_means.append(
            statistics.fmean(float(row["day10_close_return"]) for row in sample)
        )
        values = [float(row["gross_close_rule_return"]) for row in sample]
        rule_means.append(statistics.fmean(values))
        pf = _profit_factor(values)
        if pf is not None and math.isfinite(pf):
            profit_factors.append(pf)
    lower_probability = cfg.validation_ci_alpha / 2.0
    upper_probability = 1.0 - lower_probability
    return {
        "pattern": pattern,
        "iterations_requested": count,
        "cluster": "calendar month resampled within year",
        "confidence_level": 1.0 - cfg.validation_ci_alpha,
        "evaluable_count": len(causal),
        "success_rate_ci": [
            quantile(success_rates, lower_probability),
            quantile(success_rates, upper_probability),
        ],
        "average_day10_return_ci": [
            quantile(day10_means, lower_probability),
            quantile(day10_means, upper_probability),
        ],
        "average_gross_close_rule_return_ci": [
            quantile(rule_means, lower_probability),
            quantile(rule_means, upper_probability),
        ],
        "gross_close_rule_profit_factor_ci": [
            quantile(profit_factors, lower_probability),
            quantile(profit_factors, upper_probability),
        ],
    }


def bootstrap_table(rows: list[dict], cfg: Config = CFG) -> tuple[list[dict], dict]:
    records = [cluster_bootstrap(rows, pattern, cfg) for pattern in (*PATTERNS, "ALL_UNIQUE")]
    return records, {row["pattern"]: row for row in records}


def _quintile_boundaries(values: list[float], cfg: Config) -> list[float]:
    return [
        float(quantile(values, bucket / cfg.quintiles))
        for bucket in range(1, cfg.quintiles)
    ]


def _quintile(value: float, boundaries: list[float], cfg: Config) -> int:
    return min(cfg.quintiles, bisect_right(boundaries, value) + 1)


def _bucket_metric(rows: list[dict]) -> dict:
    if not rows:
        return {
            "sample_size": 0,
            "success_count": 0,
            "success_rate": None,
            "average_day10_return": None,
            "median_day10_return": None,
            "average_mfe": None,
            "average_mae": None,
        }
    returns = [float(row["day10_close_return"]) for row in rows]
    return {
        "sample_size": len(rows),
        "success_count": sum(bool(row["primary_success"]) for row in rows),
        "success_rate": sum(bool(row["primary_success"]) for row in rows) / len(rows),
        "average_day10_return": statistics.fmean(returns),
        "median_day10_return": statistics.median(returns),
        "average_mfe": statistics.fmean(float(row["mfe_close_10"]) for row in rows),
        "average_mae": statistics.fmean(float(row["mae_close_10"]) for row in rows),
    }


def _risk_ratio(favorable: list[dict], opposite: list[dict]) -> float | None:
    if not favorable or not opposite:
        return None
    favorable_rate = sum(bool(row["primary_success"]) for row in favorable) / len(favorable)
    opposite_rate = sum(bool(row["primary_success"]) for row in opposite) / len(opposite)
    if opposite_rate == 0:
        return None
    return favorable_rate / opposite_rate


def _month_sign_flip_pvalue(differences: list[float], seed: int, iterations: int) -> float:
    if not differences:
        return 1.0
    observed = abs(statistics.fmean(differences))
    if observed == 0:
        return 1.0
    generator = random.Random(seed)
    extreme = 0
    for _ in range(iterations):
        simulated = statistics.fmean(
            value if generator.random() < 0.5 else -value for value in differences
        )
        extreme += int(abs(simulated) >= observed - 1e-15)
    return (extreme + 1) / (iterations + 1)


def feature_diagnostic(
    discovery_rows: list[dict], validation_rows: list[dict], cfg: Config = CFG
) -> dict:
    """Discovery-only commonality study; it never filters V0.1 trades."""

    discovery = _evaluable(discovery_rows)
    validation = _evaluable(validation_rows)
    boundaries: dict[tuple[str, str], list[float]] = {}
    bucket_rows: list[dict] = []
    preliminary: dict[str, dict] = {}
    pvalues: dict[str, float] = {}

    for pattern in PATTERNS:
        pattern_discovery = [row for row in discovery if row["pattern"] == pattern]
        for feature in cfg.feature_names:
            values = [float(row[feature]) for row in pattern_discovery]
            if not values:
                continue
            cuts = _quintile_boundaries(values, cfg)
            boundaries[(pattern, feature)] = cuts
            for period, source in (("discovery", discovery), ("validation_diagnostic", validation)):
                pattern_source = [row for row in source if row["pattern"] == pattern]
                years = sorted({row["signal_date"][:4] for row in pattern_source})
                for year in (*years, "ALL"):
                    year_source = pattern_source if year == "ALL" else [
                        row for row in pattern_source if row["signal_date"].startswith(year)
                    ]
                    for bucket in range(1, cfg.quintiles + 1):
                        selected = [
                            row
                            for row in year_source
                            if _quintile(float(row[feature]), cuts, cfg) == bucket
                        ]
                        bucket_rows.append(
                            {
                                "period": period,
                                "pattern": pattern,
                                "feature": feature,
                                "family": cfg.family_by_feature[feature],
                                "year": year,
                                "quintile": bucket,
                                "discovery_boundaries": cuts,
                                **_bucket_metric(selected),
                            }
                        )

            tagged = [
                (row, _quintile(float(row[feature]), cuts, cfg))
                for row in pattern_discovery
            ]
            low = [row for row, bucket in tagged if bucket == 1]
            high = [row for row, bucket in tagged if bucket == 5]
            low_rate = sum(bool(row["primary_success"]) for row in low) / len(low) if low else 0.0
            high_rate = sum(bool(row["primary_success"]) for row in high) / len(high) if high else 0.0
            direction = "HIGH" if high_rate >= low_rate else "LOW"
            favorable_bucket, opposite_bucket = ((5, 1) if direction == "HIGH" else (1, 5))
            favorable = [row for row, bucket in tagged if bucket == favorable_bucket]
            opposite = [row for row, bucket in tagged if bucket == opposite_bucket]
            yearly_lifts: dict[str, float | None] = {}
            yearly_counts: dict[str, int] = {}
            yearly_dates: dict[str, int] = {}
            yearly_positive_tails: dict[str, dict[str, int]] = {}
            for year in ("2020", "2021", "2022"):
                yf = [row for row in favorable if row["signal_date"].startswith(year)]
                yo = [row for row in opposite if row["signal_date"].startswith(year)]
                yp = [row for row in pattern_discovery if row["signal_date"].startswith(year)]
                yearly_lifts[year] = _risk_ratio(yf, yo)
                yearly_counts[year] = len(yp)
                yearly_dates[year] = len({row["signal_date"] for row in yp})
                yearly_positive_tails[year] = {
                    "favorable": sum(bool(row["primary_success"]) for row in yf),
                    "opposite": sum(bool(row["primary_success"]) for row in yo),
                }
            month_differences: list[float] = []
            for month in sorted({row["signal_date"][:6] for row in pattern_discovery}):
                month_f = [row for row in favorable if row["signal_date"].startswith(month)]
                month_o = [row for row in opposite if row["signal_date"].startswith(month)]
                if month_f and month_o:
                    month_differences.append(
                        sum(bool(row["primary_success"]) for row in month_f) / len(month_f)
                        - sum(bool(row["primary_success"]) for row in month_o) / len(month_o)
                    )
            key = f"{pattern}:{feature}"
            seed = cfg.bootstrap_seed + int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
            pvalue = _month_sign_flip_pvalue(
                month_differences, seed, cfg.bootstrap_iterations
            )
            pvalues[key] = pvalue
            preliminary[key] = {
                "pattern": pattern,
                "feature": feature,
                "family": cfg.family_by_feature[feature],
                "direction": direction,
                "favorable_quintile": favorable_bucket,
                "opposite_quintile": opposite_bucket,
                "pooled_lift": _risk_ratio(favorable, opposite),
                "yearly_lifts": yearly_lifts,
                "yearly_evaluable_counts": yearly_counts,
                "yearly_signal_date_counts": yearly_dates,
                "yearly_positive_tail_counts": yearly_positive_tails,
                "month_cluster_sign_flip_pvalue": pvalue,
                "months_in_test": len(month_differences),
                "discovery_boundaries": cuts,
            }

    qvalues = benjamini_hochberg(pvalues)
    robust: list[dict] = []
    for key, row in preliminary.items():
        pooled_lift = row["pooled_lift"]
        criteria = {
            "bh_q_at_most_0_05": qvalues[key] <= cfg.fdr_alpha,
            "pooled_lift_at_least_1_25": pooled_lift is not None
            and pooled_lift >= cfg.discovery_minimum_pooled_lift,
            "yearly_direction_consistent": all(
                value is not None and value >= cfg.discovery_minimum_yearly_lift
                for value in row["yearly_lifts"].values()
            ),
            "yearly_evaluable_at_least_1000": all(
                value >= cfg.discovery_minimum_evaluable_per_year
                for value in row["yearly_evaluable_counts"].values()
            ),
            "yearly_dates_at_least_100": all(
                value >= cfg.discovery_minimum_dates_per_year
                for value in row["yearly_signal_date_counts"].values()
            ),
            "yearly_tail_positives_at_least_30": all(
                counts["favorable"] >= cfg.discovery_minimum_positive_per_tail_year
                and counts["opposite"] >= cfg.discovery_minimum_positive_per_tail_year
                for counts in row["yearly_positive_tail_counts"].values()
            ),
        }
        row["bh_qvalue"] = qvalues[key]
        row["criteria"] = criteria
        row["passes_robust_commonality_gate"] = all(criteria.values())
        row["selected_for_v01_trading"] = False
        if row["passes_robust_commonality_gate"]:
            robust.append(row)

    # Mechanically identify at most one per family and three per pattern as
    # future research candidates only. They do not change V0.1 signals.
    research_candidates: list[dict] = []
    for pattern in PATTERNS:
        winners: list[dict] = []
        for family in sorted(set(cfg.family_by_feature.values())):
            choices = [
                row for row in robust if row["pattern"] == pattern and row["family"] == family
            ]
            if not choices:
                continue
            choices.sort(
                key=lambda row: (
                    -min(float(value) for value in row["yearly_lifts"].values()),
                    -float(row["pooled_lift"]),
                    cfg.feature_names.index(row["feature"]),
                )
            )
            winners.append(choices[0])
        winners.sort(
            key=lambda row: (
                -min(float(value) for value in row["yearly_lifts"].values()),
                -float(row["pooled_lift"]),
                cfg.feature_names.index(row["feature"]),
            )
        )
        for row in winners[: cfg.maximum_selected_features_per_pattern]:
            research_candidates.append(
                {
                    "pattern": pattern,
                    "feature": row["feature"],
                    "family": row["family"],
                    "direction": row["direction"],
                    "warning": "diagnostic only; not used to filter V0.1 trades",
                }
            )
    selection_rows = [preliminary[key] for key in sorted(preliminary)]
    return {
        "quintile_rows": bucket_rows,
        "selection_rows": selection_rows,
        "research_candidates": research_candidates,
        "policy": "No diagnostic feature filters or reranks V0.1 trades.",
    }


def validation_decision(
    rows: list[dict],
    summary_lookup: dict,
    bootstrap_lookup: dict,
    portfolios: dict[str, dict],
    cfg: Config = CFG,
) -> dict:
    decisions: dict[str, dict] = {}
    for pattern in PATTERNS:
        pooled = summary_lookup[pattern]
        yearly = {
            year: metric_summary(
                [
                    row
                    for row in rows
                    if row["pattern"] == pattern and row["signal_date"].startswith(year)
                ]
            )
            for year in ("2023", "2024")
        }
        boot = bootstrap_lookup[pattern]
        baseline = portfolios[pattern]["baseline"]["summary"]
        stress = portfolios[pattern]["stress"]["summary"]
        gates = {
            "sample_size": pooled["evaluable_count"] >= cfg.validation_minimum_evaluable
            and all(
                yearly[year]["evaluable_count"] >= cfg.validation_minimum_evaluable_per_year
                and yearly[year]["distinct_signal_dates"] >= cfg.validation_minimum_dates_per_year
                for year in yearly
            ),
            "outcome_observation_rate": pooled["outcome_observation_rate"] is not None
            and pooled["outcome_observation_rate"] >= 1.0 - cfg.validation_maximum_attrition,
            "success_rate": pooled["success_rate"] is not None
            and pooled["success_rate"] >= cfg.validation_minimum_event_rate,
            "success_rate_ci_above_zero_cost_break_even": boot["success_rate_ci"][0]
            is not None
            and boot["success_rate_ci"][0] > cfg.validation_break_even_event_rate,
            "day10_mean": pooled["average_day10_close_return"] is not None
            and pooled["average_day10_close_return"] >= cfg.validation_minimum_day10_return,
            "day10_mean_ci_positive": boot["average_day10_return_ci"][0] is not None
            and boot["average_day10_return_ci"][0] > 0.0,
            "day10_median_positive": pooled["median_day10_close_return"] is not None
            and pooled["median_day10_close_return"] > 0.0,
            "both_years_day10_positive": all(
                yearly[year]["average_day10_close_return"] is not None
                and yearly[year]["average_day10_close_return"] > 0.0
                for year in yearly
            ),
            "top1_winner_removed_positive": pooled[
                "top1_percent_removed_average_gross_close_rule_return"
            ]
            is not None
            and pooled["top1_percent_removed_average_gross_close_rule_return"] > 0.0,
            "portfolio_positive": baseline["total_return_mark_to_market"] > 0.0,
            "portfolio_profit_factor": baseline["profit_factor"] is not None
            and baseline["profit_factor"] >= cfg.validation_minimum_profit_factor,
            "portfolio_both_years_positive": all(
                baseline["yearly_returns"].get(year) is not None
                and baseline["yearly_returns"][year] > 0.0
                for year in ("2023", "2024")
            ),
            "portfolio_top1_robust": baseline[
                "top1_percent_removed_average_net_trade_return"
            ]
            is not None
            and baseline["top1_percent_removed_average_net_trade_return"] > 0.0
            and baseline["top1_percent_removed_profit_factor"] is not None
            and baseline["top1_percent_removed_profit_factor"] > 1.0,
            "portfolio_maximum_drawdown": baseline["maximum_drawdown"]
            <= cfg.validation_maximum_drawdown,
            "no_corporate_action_censored_trades": baseline[
                "corporate_action_censored_trades"
            ]
            == 0
            and stress["corporate_action_censored_trades"] == 0,
            "stress_nonnegative": stress["total_return_mark_to_market"] >= 0.0,
            "no_unresolved_positions": baseline["unresolved_positions"] == 0
            and stress["unresolved_positions"] == 0,
        }
        decisions[pattern] = {
            "status": "PASS" if all(gates.values()) else "FAIL",
            "all_gates_passed": all(gates.values()),
            "gates": gates,
            "pooled": pooled,
            "yearly": yearly,
            "bootstrap": boot,
            "baseline_portfolio": baseline,
            "stress_portfolio": stress,
        }
    passing = [pattern for pattern in PATTERNS if decisions[pattern]["status"] == "PASS"]
    return {
        "status": "PASS" if passing else "FAIL",
        "pattern_decisions": decisions,
        "patterns_allowed_into_2025": passing,
        "feature_oos_2025_opened": bool(passing),
        "multiplicity_policy": "Each of two patterns uses a central 97.5% month-cluster interval.",
        "oos_policy": "Only an individually passing immutable pattern may be evaluated in 2025.",
    }
