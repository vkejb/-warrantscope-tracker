from __future__ import annotations

from collections import defaultdict
import hashlib
import math
import random
import statistics

from .config import CFG, Config
from .features import evaluate_outcome, iter_signal_dates
from .models import Outcome, PreparedBenchmark, PreparedStock


def percentile_ranks(values: list[float]) -> list[float]:
    """Average-rank empirical percentiles in (0, 1), deterministic for ties."""

    if not values:
        return []
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    result = [0.0] * len(values)
    position = 0
    size = len(values)
    while position < size:
        end = position + 1
        value = values[order[position]]
        while end < size and values[order[end]] == value:
            end += 1
        average_one_based_rank = ((position + 1) + end) / 2.0
        percentile = (average_one_based_rank - 0.5) / size
        for cursor in range(position, end):
            result[order[cursor]] = percentile
        position = end
    return result


def percentile_quintile(percentile: float, cfg: Config = CFG) -> int:
    return min(cfg.quintiles, max(1, int(percentile * cfg.quintiles) + 1))


def _empty_bucket() -> dict:
    return {
        "signal_count": 0,
        "evaluable_count": 0,
        "event_count": 0,
        "clean_event_count": 0,
        "high_touch_count": 0,
        "return_sum": 0.0,
        "mfe_sum": 0.0,
        "mae_sum": 0.0,
    }


def _update_bucket(bucket: dict, outcome: Outcome) -> None:
    bucket["signal_count"] += 1
    if outcome.status != "EVALUABLE":
        return
    bucket["evaluable_count"] += 1
    bucket["event_count"] += int(bool(outcome.primary_event))
    bucket["clean_event_count"] += int(bool(outcome.clean_primary_event))
    bucket["high_touch_count"] += int(bool(outcome.high_touch_event_10))
    bucket["return_sum"] += float(outcome.close_return_10)
    bucket["mfe_sum"] += float(outcome.mfe_close_10)
    bucket["mae_sum"] += float(outcome.mae_close_10)


def _bucket_row(feature: str, family: str, period: str, quintile: int, bucket: dict) -> dict:
    n = bucket["evaluable_count"]
    signals = bucket["signal_count"]
    return {
        "feature": feature,
        "family": family,
        "period": period,
        "quintile": quintile,
        **bucket,
        "outcome_observation_rate": n / signals if signals else None,
        "event_rate": bucket["event_count"] / n if n else None,
        "clean_event_rate": bucket["clean_event_count"] / n if n else None,
        "high_touch_rate": bucket["high_touch_count"] / n if n else None,
        "average_day10_close_return": bucket["return_sum"] / n if n else None,
        "average_mfe_close_10": bucket["mfe_sum"] / n if n else None,
        "average_mae_close_10": bucket["mae_sum"] / n if n else None,
    }


def _risk_ratio(favorable: dict, opposite: dict) -> float | None:
    if not favorable["evaluable_count"] or not opposite["evaluable_count"]:
        return None
    favorable_rate = favorable["event_count"] / favorable["evaluable_count"]
    opposite_rate = opposite["event_count"] / opposite["evaluable_count"]
    if opposite_rate == 0:
        return math.inf if favorable_rate > 0 else None
    return favorable_rate / opposite_rate


def _cluster_sign_flip_pvalue(monthly_differences: list[float], seed: int, iterations: int) -> float:
    if not monthly_differences:
        return 1.0
    observed = abs(statistics.fmean(monthly_differences))
    if observed == 0:
        return 1.0
    generator = random.Random(seed)
    extreme = 0
    for _ in range(iterations):
        simulated = statistics.fmean(
            value if generator.random() < 0.5 else -value
            for value in monthly_differences
        )
        if abs(simulated) >= observed - 1e-15:
            extreme += 1
    return (extreme + 1) / (iterations + 1)


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


def _threshold_key(period: str, year: str, horizon: int, threshold: float) -> tuple:
    return period, year, horizon, threshold


def _update_thresholds(
    aggregates: dict,
    period: str,
    year: str,
    outcome: Outcome,
    cfg: Config,
) -> None:
    for horizon in cfg.sensitivity_horizons:
        close_return = outcome.max_close_return(horizon)
        high_return = outcome.max_high_return(horizon)
        for threshold in cfg.sensitivity_thresholds:
            for label_year in (year, "ALL"):
                key = _threshold_key(period, label_year, horizon, threshold)
                bucket = aggregates.setdefault(
                    key,
                    {
                        "signal_count": 0,
                        "evaluable_count": 0,
                        "close_event_count": 0,
                        "high_touch_event_count": 0,
                    },
                )
                bucket["signal_count"] += 1
                if close_return is not None:
                    bucket["evaluable_count"] += 1
                    bucket["close_event_count"] += int(close_return >= threshold - 1e-12)
                    bucket["high_touch_event_count"] += int(
                        high_return is not None and high_return >= threshold - 1e-12
                    )


def threshold_rows(aggregates: dict) -> list[dict]:
    rows = []
    for (period, year, horizon, threshold), bucket in sorted(aggregates.items()):
        n = bucket["evaluable_count"]
        rows.append(
            {
                "period": period,
                "year": year,
                "horizon_sessions": horizon,
                "threshold": threshold,
                **bucket,
                "outcome_observation_rate": n / bucket["signal_count"]
                if bucket["signal_count"]
                else None,
                "close_event_rate": bucket["close_event_count"] / n if n else None,
                "high_touch_event_rate": bucket["high_touch_event_count"] / n
                if n
                else None,
            }
        )
    return rows


def discover_features(
    stocks: list[PreparedStock], benchmark: PreparedBenchmark, cfg: Config = CFG
) -> dict:
    buckets: dict[tuple[str, str, int], dict] = defaultdict(_empty_bucket)
    monthly: dict[tuple[str, str, int], dict] = defaultdict(_empty_bucket)
    dates_seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    threshold_aggregates: dict = {}
    outcome_reasons: dict[str, int] = defaultdict(int)
    daily_signal_counts: list[dict] = []
    descriptive_events: list[dict] = []
    last_descriptive_event: dict[str, int] = {}
    maximum_date_read: str | None = None

    for day, calendar_index, contexts in iter_signal_dates(
        stocks, benchmark, cfg.discovery_start, cfg.discovery_end, cfg
    ):
        maximum_date_read = day
        year = day[:4]
        month = day[:6]
        outcomes = [evaluate_outcome(stock, local_index, cfg) for _, stock, local_index in contexts]
        daily_signal_counts.append(
            {
                "signal_date": day,
                "signal_eligible_count": len(contexts),
                "primary_outcome_evaluable_count": sum(
                    outcome.status == "EVALUABLE" for outcome in outcomes
                ),
            }
        )
        for outcome in outcomes:
            outcome_reasons[outcome.reason] += 1
            _update_thresholds(threshold_aggregates, "discovery", year, outcome, cfg)

        for feature in cfg.feature_names:
            available = [
                (index, context[0].features[feature])
                for index, context in enumerate(contexts)
                if feature in context[0].features
            ]
            if not available:
                continue
            percentiles = percentile_ranks([value for _, value in available])
            dates_seen[(feature, year)].add(day)
            for (context_index, _), percentile in zip(available, percentiles):
                quintile = percentile_quintile(percentile, cfg)
                outcome = outcomes[context_index]
                for period in (year, "ALL"):
                    _update_bucket(buckets[(feature, period, quintile)], outcome)
                _update_bucket(monthly[(feature, month, quintile)], outcome)

        for (observation, _, _), outcome in zip(contexts, outcomes):
            if not outcome.primary_event:
                continue
            previous = last_descriptive_event.get(observation.code)
            if previous is not None and calendar_index - previous < cfg.causal_cooldown_sessions:
                continue
            last_descriptive_event[observation.code] = calendar_index
            descriptive_events.append(
                {
                    "signal_date": day,
                    "code": observation.code,
                    "name": observation.name,
                    "entry_date": outcome.entry_date,
                    "entry_open": outcome.entry_open,
                    "first_hit_day": outcome.first_primary_hit_day,
                    "day10_close_return": outcome.close_return_10,
                    "mfe_close_10": outcome.mfe_close_10,
                    "mae_close_10": outcome.mae_close_10,
                    "description_only_outcome_selected": True,
                    **observation.features,
                }
            )

    quintile_rows = []
    for (feature, period, quintile), bucket in sorted(buckets.items()):
        quintile_rows.append(
            _bucket_row(
                feature, cfg.family_by_feature[feature], period, quintile, bucket
            )
        )

    preliminary: dict[str, dict] = {}
    pvalues: dict[str, float] = {}
    for feature in cfg.feature_names:
        low = buckets[(feature, "ALL", 1)]
        high = buckets[(feature, "ALL", 5)]
        low_rate = low["event_count"] / low["evaluable_count"] if low["evaluable_count"] else 0.0
        high_rate = high["event_count"] / high["evaluable_count"] if high["evaluable_count"] else 0.0
        direction = "HIGH" if high_rate >= low_rate else "LOW"
        favorable_quintile, opposite_quintile = (5, 1) if direction == "HIGH" else (1, 5)
        favorable = buckets[(feature, "ALL", favorable_quintile)]
        opposite = buckets[(feature, "ALL", opposite_quintile)]
        pooled_lift = _risk_ratio(favorable, opposite)
        yearly_lifts = {}
        yearly_tail_counts = {}
        yearly_date_counts = {}
        for year in ("2020", "2021", "2022"):
            yearly_favorable = buckets[(feature, year, favorable_quintile)]
            yearly_opposite = buckets[(feature, year, opposite_quintile)]
            yearly_lifts[year] = _risk_ratio(yearly_favorable, yearly_opposite)
            yearly_tail_counts[year] = {
                "favorable": yearly_favorable["evaluable_count"],
                "opposite": yearly_opposite["evaluable_count"],
            }
            yearly_date_counts[year] = len(dates_seen[(feature, year)])
        monthly_differences = []
        for month in sorted({key[1] for key in monthly if key[0] == feature}):
            month_low = monthly[(feature, month, 1)]
            month_high = monthly[(feature, month, 5)]
            if month_low["evaluable_count"] and month_high["evaluable_count"]:
                monthly_differences.append(
                    month_high["event_count"] / month_high["evaluable_count"]
                    - month_low["event_count"] / month_low["evaluable_count"]
                )
        stable_seed = cfg.bootstrap_seed + int(
            hashlib.sha256(feature.encode("utf-8")).hexdigest()[:8], 16
        )
        pvalue = _cluster_sign_flip_pvalue(
            monthly_differences, stable_seed, cfg.bootstrap_iterations
        )
        pvalues[feature] = pvalue
        finite_yearly = [value for value in yearly_lifts.values() if value is not None]
        preliminary[feature] = {
            "feature": feature,
            "family": cfg.family_by_feature[feature],
            "direction": direction,
            "favorable_quintile": favorable_quintile,
            "opposite_quintile": opposite_quintile,
            "pooled_lift": pooled_lift,
            "yearly_lifts": yearly_lifts,
            "minimum_yearly_lift": min(finite_yearly) if len(finite_yearly) == 3 else None,
            "yearly_tail_counts": yearly_tail_counts,
            "yearly_date_counts": yearly_date_counts,
            "cluster_sign_flip_pvalue": pvalue,
            "months_in_test": len(monthly_differences),
        }

    qvalues = benjamini_hochberg(pvalues)
    eligible = []
    for feature in cfg.feature_names:
        row = preliminary[feature]
        criteria = {
            "bh_q_at_most_alpha": qvalues[feature] <= cfg.fdr_alpha,
            "pooled_lift": row["pooled_lift"] is not None
            and row["pooled_lift"] >= cfg.discovery_minimum_pooled_lift,
            "all_year_lifts": all(
                value is not None and value >= cfg.discovery_minimum_yearly_lift
                for value in row["yearly_lifts"].values()
            ),
            "all_year_date_counts": all(
                value >= cfg.discovery_minimum_dates_per_year
                for value in row["yearly_date_counts"].values()
            ),
            "all_year_tail_counts": all(
                counts["favorable"] >= cfg.discovery_minimum_tail_evaluable_per_year
                and counts["opposite"] >= cfg.discovery_minimum_tail_evaluable_per_year
                for counts in row["yearly_tail_counts"].values()
            ),
        }
        row["bh_qvalue"] = qvalues[feature]
        row["passes_statistical_gate"] = all(criteria.values())
        row["criteria"] = criteria
        row["selected"] = False
        if row["passes_statistical_gate"]:
            eligible.append(row)

    family_winners = []
    for family in sorted(set(cfg.family_by_feature.values())):
        choices = [row for row in eligible if row["family"] == family]
        if not choices:
            continue
        choices.sort(
            key=lambda row: (
                -(row["minimum_yearly_lift"] or -math.inf),
                -(row["pooled_lift"] or -math.inf),
                row["feature"],
            )
        )
        family_winners.append(choices[0])
    family_winners.sort(
        key=lambda row: (
            -(row["minimum_yearly_lift"] or -math.inf),
            -(row["pooled_lift"] or -math.inf),
            row["feature"],
        )
    )
    selected = family_winners[: cfg.maximum_selected_features]
    selected_names = {row["feature"] for row in selected}
    for row in preliminary.values():
        row["selected"] = row["feature"] in selected_names
    selection_rows = [preliminary[name] for name in cfg.feature_names]
    selected_rule = {
        "status": "FEATURE_RULE_FROZEN" if selected else "NO_ROBUST_FEATURES",
        "selection_count": len(selected),
        "features": [
            {
                "feature": row["feature"],
                "family": row["family"],
                "direction": row["direction"],
            }
            for row in selected
        ],
        "score": "equal-weight mean of direction-aligned same-day percentiles",
        "daily_selection": f"Top {cfg.daily_selection_count}; score descending then code ascending",
        "discovery_maximum_date_read": maximum_date_read,
        "does_not_use_validation_or_oos": maximum_date_read is not None
        and maximum_date_read <= cfg.discovery_end,
    }
    return {
        "quintile_rows": quintile_rows,
        "selection_rows": selection_rows,
        "selected_rule": selected_rule,
        "threshold_rows": threshold_rows(threshold_aggregates),
        "descriptive_events": descriptive_events,
        "daily_signal_counts": daily_signal_counts,
        "outcome_reasons": dict(sorted(outcome_reasons.items())),
    }


def _outcome_values(outcomes: list[Outcome]) -> tuple[int, int, float, int]:
    evaluable = [outcome for outcome in outcomes if outcome.status == "EVALUABLE"]
    return (
        len(evaluable),
        sum(bool(outcome.primary_event) for outcome in evaluable),
        sum(float(outcome.close_return_10) for outcome in evaluable),
        sum(bool(outcome.clean_primary_event) for outcome in evaluable),
    )


def score_period(
    stocks: list[PreparedStock],
    benchmark: PreparedBenchmark,
    selected_rule: dict,
    start_date: str,
    end_date: str,
    period: str,
    cfg: Config = CFG,
) -> dict:
    selected_features = selected_rule.get("features", [])
    if not selected_features:
        return {
            "period": period,
            "daily_rows": [],
            "signal_rows": [],
            "threshold_rows": [],
            "outcome_reasons": {},
        }
    directions = {row["feature"]: row["direction"] for row in selected_features}
    feature_names = [row["feature"] for row in selected_features]
    daily_rows = []
    signal_rows = []
    threshold_aggregates: dict = {}
    outcome_reasons: dict[str, int] = defaultdict(int)
    last_cooldown_selection: dict[str, int] = {}

    for day, calendar_index, contexts in iter_signal_dates(
        stocks, benchmark, start_date, end_date, cfg
    ):
        scoreable_indices = [
            index
            for index, (observation, _, _) in enumerate(contexts)
            if all(feature in observation.features for feature in feature_names)
        ]
        percentile_by_feature: dict[str, dict[int, float]] = {}
        for feature in feature_names:
            available = [
                (index, contexts[index][0].features[feature])
                for index in range(len(contexts))
                if feature in contexts[index][0].features
            ]
            ranks = percentile_ranks([value for _, value in available])
            percentile_by_feature[feature] = {
                index: percentile for (index, _), percentile in zip(available, ranks)
            }
        scored = []
        for context_index in scoreable_indices:
            observation = contexts[context_index][0]
            aligned = [
                percentile_by_feature[feature][context_index]
                if directions[feature] == "HIGH"
                else 1.0 - percentile_by_feature[feature][context_index]
                for feature in feature_names
            ]
            scored.append((statistics.fmean(aligned), observation.code, context_index, aligned))
        scored.sort(key=lambda item: (-item[0], item[1]))
        selected_scored = scored[: cfg.daily_selection_count]
        selected_indices = {item[2] for item in selected_scored}
        control_indices = [item[2] for item in scored[cfg.daily_selection_count :]]
        outcomes_by_index: dict[int, Outcome] = {}
        for _, _, context_index, _ in scored:
            _, stock, local_index = contexts[context_index]
            outcome = evaluate_outcome(stock, local_index, cfg)
            outcomes_by_index[context_index] = outcome
            outcome_reasons[outcome.reason] += 1
            _update_thresholds(
                threshold_aggregates, period, day[:4], outcome, cfg
            )

        selected_outcomes = [outcomes_by_index[index] for index in selected_indices]
        control_outcomes = [outcomes_by_index[index] for index in control_indices]
        selected_evaluable, selected_events, selected_return_sum, selected_clean = _outcome_values(
            selected_outcomes
        )
        control_evaluable, control_events, control_return_sum, control_clean = _outcome_values(
            control_outcomes
        )
        cooldown_indices = []
        for score, _, context_index, aligned in selected_scored:
            observation = contexts[context_index][0]
            previous = last_cooldown_selection.get(observation.code)
            cooldown_included = previous is None or (
                calendar_index - previous >= cfg.causal_cooldown_sessions
            )
            if cooldown_included:
                last_cooldown_selection[observation.code] = calendar_index
                cooldown_indices.append(context_index)
            outcome = outcomes_by_index[context_index]
            row = {
                "period": period,
                "signal_date": day,
                "calendar_index": calendar_index,
                "rank": len(signal_rows),
                "daily_rank": selected_scored.index((score, observation.code, context_index, aligned)) + 1,
                "code": observation.code,
                "name": observation.name,
                "score": score,
                "signal_close": observation.signal_close,
                "average_volume_20": observation.average_volume_20,
                "average_turnover_proxy_20": observation.average_turnover_proxy_20,
                "cooldown_included": cooldown_included,
                "outcome_status": outcome.status,
                "outcome_reason": outcome.reason,
                "entry_date": outcome.entry_date,
                "entry_open_proxy": outcome.entry_open,
                "entry_gap": outcome.entry_gap,
                "primary_event": outcome.primary_event,
                "clean_primary_event": outcome.clean_primary_event,
                "first_primary_hit_day": outcome.first_primary_hit_day,
                "day10_close_return": outcome.close_return_10,
                "mfe_close_10": outcome.mfe_close_10,
                "mae_close_10": outcome.mae_close_10,
                "is_actual_order": False,
                "is_actual_fill": False,
            }
            for feature, aligned_value in zip(feature_names, aligned):
                raw_percentile = percentile_by_feature[feature][context_index]
                row[f"{feature}_percentile"] = raw_percentile
                row[f"{feature}_aligned_percentile"] = aligned_value
            signal_rows.append(row)
        cooldown_outcomes = [outcomes_by_index[index] for index in cooldown_indices]
        cooldown_evaluable, cooldown_events, cooldown_return_sum, _ = _outcome_values(
            cooldown_outcomes
        )
        daily_rows.append(
            {
                "period": period,
                "signal_date": day,
                "year": day[:4],
                "month": day[:6],
                "calendar_index": calendar_index,
                "scoreable_count": len(scored),
                "selected_signal_count": len(selected_indices),
                "selected_evaluable_count": selected_evaluable,
                "selected_event_count": selected_events,
                "selected_clean_event_count": selected_clean,
                "selected_return_sum": selected_return_sum,
                "control_signal_count": len(control_indices),
                "control_evaluable_count": control_evaluable,
                "control_event_count": control_events,
                "control_clean_event_count": control_clean,
                "control_return_sum": control_return_sum,
                "cooldown_signal_count": len(cooldown_indices),
                "cooldown_evaluable_count": cooldown_evaluable,
                "cooldown_event_count": cooldown_events,
                "cooldown_return_sum": cooldown_return_sum,
            }
        )
    return {
        "period": period,
        "daily_rows": daily_rows,
        "signal_rows": signal_rows,
        "threshold_rows": threshold_rows(threshold_aggregates),
        "outcome_reasons": dict(sorted(outcome_reasons.items())),
    }


def _metric_from_daily(rows: list[dict], prefix: str = "selected") -> dict:
    signals = sum(row[f"{prefix}_signal_count"] for row in rows)
    evaluable = sum(row[f"{prefix}_evaluable_count"] for row in rows)
    events = sum(row[f"{prefix}_event_count"] for row in rows)
    returns = sum(row[f"{prefix}_return_sum"] for row in rows)
    return {
        "signal_count": signals,
        "evaluable_count": evaluable,
        "outcome_observation_rate": evaluable / signals if signals else None,
        "event_count": events,
        "event_rate": events / evaluable if evaluable else None,
        "average_day10_close_return": returns / evaluable if evaluable else None,
    }


def summarize_edge(daily_rows: list[dict], signal_rows: list[dict]) -> dict:
    selected = _metric_from_daily(daily_rows, "selected")
    controls = _metric_from_daily(daily_rows, "control")
    cooldown = _metric_from_daily(daily_rows, "cooldown")
    hit_lift = (
        selected["event_rate"] / controls["event_rate"]
        if selected["event_rate"] is not None and controls["event_rate"]
        else None
    )
    return_increment = (
        selected["average_day10_close_return"]
        - controls["average_day10_close_return"]
        if selected["average_day10_close_return"] is not None
        and controls["average_day10_close_return"] is not None
        else None
    )
    cooldown_lift = (
        cooldown["event_rate"] / controls["event_rate"]
        if cooldown["event_rate"] is not None and controls["event_rate"]
        else None
    )
    cooldown_increment = (
        cooldown["average_day10_close_return"]
        - controls["average_day10_close_return"]
        if cooldown["average_day10_close_return"] is not None
        and controls["average_day10_close_return"] is not None
        else None
    )
    selected_returns = sorted(
        float(row["day10_close_return"])
        for row in signal_rows
        if row.get("outcome_status") == "EVALUABLE"
    )
    remove_count = math.ceil(len(selected_returns) * 0.01) if selected_returns else 0
    trimmed = selected_returns[:-remove_count] if remove_count else selected_returns
    trimmed_average = statistics.fmean(trimmed) if trimmed else None
    top1_removed_increment = (
        trimmed_average - controls["average_day10_close_return"]
        if trimmed_average is not None
        and controls["average_day10_close_return"] is not None
        else None
    )
    return {
        "selected": selected,
        "controls_excluding_top30": controls,
        "causal_cooldown_selected": cooldown,
        "hit_rate_lift": hit_lift,
        "day10_return_increment": return_increment,
        "cooldown_hit_rate_lift": cooldown_lift,
        "cooldown_day10_return_increment": cooldown_increment,
        "top1_percent_selected_winners_removed_count": remove_count,
        "top1_percent_removed_selected_average_return": trimmed_average,
        "top1_percent_removed_return_increment": top1_removed_increment,
        "outcome_attrition_rate_gap": (
            selected["outcome_observation_rate"] - controls["outcome_observation_rate"]
            if selected["outcome_observation_rate"] is not None
            and controls["outcome_observation_rate"] is not None
            else None
        ),
    }


def _bootstrap_stat(rows: list[dict]) -> tuple[float | None, float | None]:
    selected = _metric_from_daily(rows, "selected")
    controls = _metric_from_daily(rows, "control")
    lift = (
        selected["event_rate"] / controls["event_rate"]
        if selected["event_rate"] is not None and controls["event_rate"]
        else None
    )
    difference = (
        selected["average_day10_close_return"]
        - controls["average_day10_close_return"]
        if selected["average_day10_close_return"] is not None
        and controls["average_day10_close_return"] is not None
        else None
    )
    return lift, difference


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def cluster_bootstrap(
    daily_rows: list[dict], cfg: Config = CFG, iterations: int | None = None
) -> dict:
    iteration_count = iterations or cfg.bootstrap_iterations
    months_by_year: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in daily_rows:
        months_by_year[row["year"]][row["month"]].append(row)
    generator = random.Random(cfg.bootstrap_seed)
    lifts: list[float] = []
    differences: list[float] = []
    for _ in range(iteration_count):
        sample: list[dict] = []
        for year in sorted(months_by_year):
            month_map = months_by_year[year]
            months = sorted(month_map)
            for _ in months:
                chosen = generator.choice(months)
                sample.extend(month_map[chosen])
        lift, difference = _bootstrap_stat(sample)
        if lift is not None and math.isfinite(lift):
            lifts.append(lift)
        if difference is not None and math.isfinite(difference):
            differences.append(difference)
    return {
        "iterations_requested": iteration_count,
        "seed": cfg.bootstrap_seed,
        "cluster": "calendar month, resampled within year",
        "hit_rate_lift_samples": len(lifts),
        "hit_rate_lift_ci95": [_quantile(lifts, 0.025), _quantile(lifts, 0.975)],
        "return_increment_samples": len(differences),
        "day10_return_increment_ci95": [
            _quantile(differences, 0.025),
            _quantile(differences, 0.975),
        ],
    }


def validation_decision(
    daily_rows: list[dict], signal_rows: list[dict], benchmark: PreparedBenchmark, cfg: Config = CFG
) -> dict:
    pooled = summarize_edge(daily_rows, signal_rows)
    bootstrap = cluster_bootstrap(daily_rows, cfg)
    yearly = {}
    coverage = {}
    for year in ("2023", "2024"):
        rows = [row for row in daily_rows if row["year"] == year]
        year_signals = [row for row in signal_rows if row["signal_date"].startswith(year)]
        yearly[year] = summarize_edge(rows, year_signals)
        calendar_dates = sum(day.startswith(year) for day in benchmark.calendar)
        usable_dates = sum(
            row["selected_evaluable_count"] > 0 and row["control_evaluable_count"] > 0
            for row in rows
        )
        coverage[year] = {
            "calendar_dates": calendar_dates,
            "usable_dates": usable_dates,
            "coverage": usable_dates / calendar_dates if calendar_dates else 0.0,
        }
    gates = {
        "coverage": all(
            item["usable_dates"] >= cfg.validation_minimum_usable_dates_per_year
            and item["coverage"] >= cfg.validation_minimum_coverage
            for item in coverage.values()
        ),
        "pooled_hit_lift": pooled["hit_rate_lift"] is not None
        and pooled["hit_rate_lift"] >= cfg.validation_minimum_hit_lift,
        "hit_lift_ci_lower": bootstrap["hit_rate_lift_ci95"][0] is not None
        and bootstrap["hit_rate_lift_ci95"][0] > 1.0,
        "yearly_hit_lifts": all(
            yearly[year]["hit_rate_lift"] is not None
            and yearly[year]["hit_rate_lift"] >= cfg.validation_minimum_yearly_hit_lift
            for year in yearly
        ),
        "pooled_return_increment": pooled["day10_return_increment"] is not None
        and pooled["day10_return_increment"] >= cfg.validation_minimum_return_increment,
        "return_increment_ci_lower": bootstrap["day10_return_increment_ci95"][0]
        is not None
        and bootstrap["day10_return_increment_ci95"][0] > 0.0,
        "yearly_return_increments_positive": all(
            yearly[year]["day10_return_increment"] is not None
            and yearly[year]["day10_return_increment"] > 0.0
            for year in yearly
        ),
        "top1_percent_winner_sensitivity": pooled[
            "top1_percent_removed_return_increment"
        ]
        is not None
        and pooled["top1_percent_removed_return_increment"] > 0.0,
        "causal_cooldown_sensitivity": pooled["cooldown_hit_rate_lift"] is not None
        and pooled["cooldown_hit_rate_lift"] > 1.0
        and pooled["cooldown_day10_return_increment"] is not None
        and pooled["cooldown_day10_return_increment"] > 0.0,
        "outcome_attrition_balance": pooled["outcome_attrition_rate_gap"] is not None
        and abs(pooled["outcome_attrition_rate_gap"])
        <= cfg.validation_maximum_attrition_gap,
    }
    return {
        "status": "PASS" if all(gates.values()) else "FAIL",
        "all_gates_passed": all(gates.values()),
        "gates": gates,
        "pooled": pooled,
        "yearly": yearly,
        "coverage": coverage,
        "bootstrap": bootstrap,
        "oos_policy": "2025 may be opened only when every validation gate passes",
    }


def oos_decision(
    daily_rows: list[dict], signal_rows: list[dict], benchmark: PreparedBenchmark, cfg: Config = CFG
) -> dict:
    pooled = summarize_edge(daily_rows, signal_rows)
    bootstrap = cluster_bootstrap(daily_rows, cfg)
    calendar_dates = sum(day.startswith("2025") for day in benchmark.calendar)
    usable_dates = sum(
        row["selected_evaluable_count"] > 0 and row["control_evaluable_count"] > 0
        for row in daily_rows
    )
    coverage = usable_dates / calendar_dates if calendar_dates else 0.0
    gates = {
        "coverage": usable_dates >= cfg.validation_minimum_usable_dates_per_year
        and coverage >= cfg.validation_minimum_coverage,
        "hit_lift": pooled["hit_rate_lift"] is not None
        and pooled["hit_rate_lift"] >= cfg.validation_minimum_hit_lift,
        "hit_lift_ci_lower": bootstrap["hit_rate_lift_ci95"][0] is not None
        and bootstrap["hit_rate_lift_ci95"][0] > 1.0,
        "return_increment": pooled["day10_return_increment"] is not None
        and pooled["day10_return_increment"] >= cfg.validation_minimum_return_increment,
        "return_increment_ci_lower": bootstrap["day10_return_increment_ci95"][0]
        is not None
        and bootstrap["day10_return_increment_ci95"][0] > 0.0,
        "outcome_attrition_balance": pooled["outcome_attrition_rate_gap"] is not None
        and abs(pooled["outcome_attrition_rate_gap"])
        <= cfg.validation_maximum_attrition_gap,
    }
    return {
        "status": "PASS" if all(gates.values()) else "FAIL",
        "all_gates_passed": all(gates.values()),
        "gates": gates,
        "pooled": pooled,
        "bootstrap": bootstrap,
        "coverage": {
            "calendar_dates": calendar_dates,
            "usable_dates": usable_dates,
            "coverage": coverage,
        },
        "holdout_disclosure": cfg.oos_disclosure,
    }
