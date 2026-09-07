from __future__ import annotations

import hashlib
import math

import numpy as np

from .config import (
    CFG,
    COHORTS,
    CORE_EXTENSION_FEATURES,
    FEATURE_NAMES,
    GAP_BUCKETS,
    PERIODS,
    Config,
)
from .pipeline import COHORT_BIT, OUTCOME_FIELDS


FEATURE_INDEX = {name: index for index, name in enumerate(FEATURE_NAMES)}
OUTCOME_INDEX = {name: index for index, name in enumerate(OUTCOME_FIELDS)}


def _clean_number(value: float | int | np.number | None):
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _mean(values: np.ndarray) -> float | None:
    return _clean_number(np.mean(values)) if len(values) else None


def _median(values: np.ndarray) -> float | None:
    return _clean_number(np.median(values)) if len(values) else None


def _profit_factor(values: np.ndarray) -> float | None:
    if not len(values):
        return None
    positive = float(values[values > 0].sum())
    negative = float(-values[values < 0].sum())
    if negative <= 0:
        return None
    return positive / negative


def _difference(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return float(left) - float(right)


def _winner_removed_mask(gross: np.ndarray, fraction: float) -> tuple[np.ndarray, int]:
    keep = np.ones(len(gross), dtype=bool)
    winner_indices = np.flatnonzero(gross > 0)
    remove_count = int(math.ceil(len(winner_indices) * fraction)) if len(winner_indices) else 0
    if remove_count:
        order = winner_indices[np.argsort(-gross[winner_indices], kind="stable")]
        keep[order[:remove_count]] = False
    return keep, remove_count


def metric_summary(arrays: dict[str, np.ndarray], signal_mask: np.ndarray) -> dict:
    meta = arrays["meta"]
    valid = signal_mask & meta["outcome_evaluable"]
    outcomes = arrays["outcomes"][valid]
    signals = int(np.count_nonzero(signal_mask))
    evaluable = int(len(outcomes))
    if not evaluable:
        return {
            "signals": signals,
            "evaluable_signals": 0,
            "outcome_observation_rate": 0.0 if signals else None,
            "primary_success_rate": None,
            "gross_average_return": None,
            "net_average_return": None,
            "gross_median_return": None,
            "net_median_return": None,
            "gross_win_rate": None,
            "net_win_rate": None,
            "gross_profit_factor": None,
            "net_profit_factor": None,
            "average_day1_close_return": None,
            "average_day3_close_return": None,
            "average_day5_close_return": None,
            "average_day10_close_return": None,
            "average_mfe_5d": None,
            "average_mfe_10d": None,
            "average_mae_5d": None,
            "average_mae_10d": None,
            "average_trade_mfe_abs_mae": None,
            "aggregate_mfe_abs_mae": None,
            "top1_removed_winners": 0,
            "top1_removed_gross_average_return": None,
            "top1_removed_gross_profit_factor": None,
            "top1_removed_net_profit_factor": None,
        }
    gross = outcomes[:, OUTCOME_INDEX["gross_return"]]
    net = outcomes[:, OUTCOME_INDEX["net_return"]]
    mfe = outcomes[:, OUTCOME_INDEX["mfe_10d"]]
    mae = outcomes[:, OUTCOME_INDEX["mae_10d"]]
    ratio = outcomes[:, OUTCOME_INDEX["mfe_abs_mae"]]
    ratio = ratio[np.isfinite(ratio)]
    keep, removed = _winner_removed_mask(gross, 0.01)
    aggregate_ratio = float(np.mean(mfe)) / abs(float(np.mean(mae))) if abs(float(np.mean(mae))) > 1e-15 else None
    return {
        "signals": signals,
        "evaluable_signals": evaluable,
        "outcome_observation_rate": evaluable / signals if signals else None,
        "primary_success_rate": float(np.mean(outcomes[:, OUTCOME_INDEX["primary_success"]])),
        "gross_average_return": float(np.mean(gross)),
        "net_average_return": float(np.mean(net)),
        "gross_median_return": float(np.median(gross)),
        "net_median_return": float(np.median(net)),
        "gross_win_rate": float(np.mean(gross > 0)),
        "net_win_rate": float(np.mean(net > 0)),
        "gross_profit_factor": _profit_factor(gross),
        "net_profit_factor": _profit_factor(net),
        "average_day1_close_return": float(np.mean(outcomes[:, OUTCOME_INDEX["day1_close_return"]])),
        "average_day3_close_return": float(np.mean(outcomes[:, OUTCOME_INDEX["day3_close_return"]])),
        "average_day5_close_return": float(np.mean(outcomes[:, OUTCOME_INDEX["day5_close_return"]])),
        "average_day10_close_return": float(np.mean(outcomes[:, OUTCOME_INDEX["day10_close_return"]])),
        "average_mfe_5d": float(np.mean(outcomes[:, OUTCOME_INDEX["mfe_5d"]])),
        "average_mfe_10d": float(np.mean(mfe)),
        "average_mae_5d": float(np.mean(outcomes[:, OUTCOME_INDEX["mae_5d"]])),
        "average_mae_10d": float(np.mean(mae)),
        "average_trade_mfe_abs_mae": _mean(ratio),
        "aggregate_mfe_abs_mae": aggregate_ratio,
        "top1_removed_winners": removed,
        "top1_removed_gross_average_return": _mean(gross[keep]),
        "top1_removed_gross_profit_factor": _profit_factor(gross[keep]),
        "top1_removed_net_profit_factor": _profit_factor(net[keep]),
    }


def _date_mask(meta: np.ndarray, start: str, end: str) -> np.ndarray:
    dates = meta["signal_date"]
    return (dates >= int(start)) & (dates <= int(end))


def _cohort_mask(meta: np.ndarray, cohort: str) -> np.ndarray:
    return (meta["cohort_mask"] & COHORT_BIT[cohort]) != 0


def _feature_set(cohort: str) -> tuple[str, ...]:
    return FEATURE_NAMES if cohort == "ALL_ELIGIBLE" else CORE_EXTENSION_FEATURES


def boundary_rows(
    deciles: dict[str, tuple[float, ...]],
    quintiles: dict[str, tuple[float, ...]],
    boundary_audit: dict,
) -> list[dict]:
    rows = []
    for resolution, values_by_feature in (("DECILE", deciles), ("QUINTILE", quintiles)):
        for feature in FEATURE_NAMES:
            edges = values_by_feature[feature]
            for index, value in enumerate(edges, 1):
                rows.append(
                    {
                        "boundary_source": "POOLED_2020_2022_ALL_ELIGIBLE",
                        "resolution": resolution,
                        "feature": feature,
                        "boundary_number": index,
                        "probability": index / (10 if resolution == "DECILE" else 5),
                        "boundary_value": value,
                        "finite_discovery_observations": boundary_audit["finite_counts"][feature],
                        "entry_gap_denominator": (
                            "T_PLUS_1_ENTRY_OBSERVED_ONLY" if feature == "entry_gap" else "T_CLOSE_ELIGIBLE"
                        ),
                        "boundary_sha256": boundary_audit["boundary_sha256"],
                    }
                )
    return rows


def _bucket_ids(
    values: np.ndarray, boundaries: tuple[float, ...]
) -> np.ndarray:
    result = np.zeros(len(values), dtype=np.uint8)
    finite = np.isfinite(values)
    result[finite] = np.searchsorted(
        np.asarray(boundaries, dtype=np.float64), values[finite], side="left"
    ).astype(np.uint8) + 1
    return result


def _bucket_bounds(edges: tuple[float, ...], bucket: int) -> tuple[float | None, float | None]:
    lower = None if bucket == 1 else edges[bucket - 2]
    upper = None if bucket == len(edges) + 1 else edges[bucket - 1]
    return lower, upper


def bucket_analysis_rows(
    arrays: dict[str, np.ndarray],
    deciles: dict[str, tuple[float, ...]],
    quintiles: dict[str, tuple[float, ...]],
    *,
    yearly: bool = False,
) -> list[dict]:
    meta = arrays["meta"]
    slices: list[tuple[str, str, str]]
    if yearly:
        slices = [(year, year + "0101", year + "1231") for year in map(str, range(2020, 2026))]
    else:
        slices = list(PERIODS)
    rows: list[dict] = []
    for label, start, end in slices:
        time_mask = _date_mask(meta, start, end)
        for cohort in COHORTS:
            base = time_mask & _cohort_mask(meta, cohort)
            for feature in _feature_set(cohort):
                values = arrays["features"][:, FEATURE_INDEX[feature]]
                for resolution, boundaries in (
                    ("DECILE", deciles[feature]),
                    ("QUINTILE", quintiles[feature]),
                ):
                    ids = _bucket_ids(values, boundaries)
                    for bucket in range(1, len(boundaries) + 2):
                        lower, upper = _bucket_bounds(boundaries, bucket)
                        selected = base & (ids == bucket)
                        rows.append(
                            {
                                ("year" if yearly else "period"): label,
                                "cohort": cohort,
                                "feature": feature,
                                "resolution": resolution,
                                "bucket": bucket,
                                "lower_bound_exclusive": lower,
                                "upper_bound_inclusive": upper,
                                "equality_rule": "BOUNDARY_EQUALITY_STAYS_IN_LOWER_BUCKET",
                                **metric_summary(arrays, selected),
                            }
                        )
    return rows


def cohort_summary_rows(arrays: dict[str, np.ndarray]) -> list[dict]:
    meta = arrays["meta"]
    rows = []
    for period, start, end in PERIODS:
        time_mask = _date_mask(meta, start, end)
        for cohort in COHORTS:
            selected = time_mask & _cohort_mask(meta, cohort)
            rows.append({"period": period, "cohort": cohort, **metric_summary(arrays, selected)})
    return rows


def interaction_rows(
    arrays: dict[str, np.ndarray],
    quintiles: dict[str, tuple[float, ...]],
) -> tuple[list[dict], list[dict]]:
    meta = arrays["meta"]
    bias20_ids = _bucket_ids(
        arrays["features"][:, FEATURE_INDEX["bias_20"]], quintiles["bias_20"]
    )
    time_slices = list(PERIODS) + [
        (year, year + "0101", year + "1231") for year in map(str, range(2020, 2026))
    ]
    momentum_rows: list[dict] = []
    gap_rows: list[dict] = []
    for label, start, end in time_slices:
        time_mask = _date_mask(meta, start, end)
        slice_type = "PERIOD" if not label.isdigit() else "YEAR"
        for momentum_q in range(1, 6):
            for bias_q in range(1, 6):
                selected = (
                    time_mask
                    & (meta["momentum_strength_quintile"] == momentum_q)
                    & (bias20_ids == bias_q)
                )
                momentum_rows.append(
                    {
                        "time_slice_type": slice_type,
                        "time_slice": label,
                        "momentum_strength_quintile": momentum_q,
                        "bias20_quintile": bias_q,
                        **metric_summary(arrays, selected),
                    }
                )
        for bias_q in range(1, 6):
            for gap_bucket in range(1, 7):
                selected = (
                    time_mask
                    & (bias20_ids == bias_q)
                    & (meta["entry_gap_bucket"] == gap_bucket)
                )
                gap_rows.append(
                    {
                        "time_slice_type": slice_type,
                        "time_slice": label,
                        "bias20_quintile": bias_q,
                        "entry_gap_bucket_number": gap_bucket,
                        "entry_gap_bucket": GAP_BUCKETS[gap_bucket - 1],
                        **metric_summary(arrays, selected),
                    }
                )
    return momentum_rows, gap_rows


def _stable_seed(base: int, *parts: str) -> int:
    payload = "|".join([str(base), *parts]).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _ci(values: np.ndarray) -> tuple[float | None, float | None]:
    finite = values[np.isfinite(values)]
    if not len(finite):
        return None, None
    low, high = np.quantile(finite, (0.025, 0.975), method="linear")
    return float(low), float(high)


def _region_number(decile_ids: np.ndarray) -> np.ndarray:
    result = np.zeros(len(decile_ids), dtype=np.uint8)
    result[(decile_ids >= 1) & (decile_ids <= 3)] = 1
    result[(decile_ids >= 4) & (decile_ids <= 7)] = 2
    result[(decile_ids >= 8) & (decile_ids <= 10)] = 3
    return result


REGION_NAMES = ("LOW_D1_D3", "MID_D4_D7", "HIGH_D8_D10")


def _cluster_design(
    arrays: dict[str, np.ndarray], selected: np.ndarray, features: tuple[str, ...], cluster_unit: str
) -> tuple[np.ndarray, dict[str, np.ndarray], int]:
    meta = arrays["meta"]
    valid = selected & meta["outcome_evaluable"]
    row_indices = np.flatnonzero(valid)
    if not len(row_indices):
        return np.empty((0, 0)), {}, 0
    dates = meta["signal_date"][row_indices]
    labels = dates if cluster_unit == "signal_date" else dates // 100
    unique, cluster_ids = np.unique(labels, return_inverse=True)
    cluster_count = len(unique)
    width = len(features) * 3
    stats = {
        name: np.zeros((cluster_count, width), dtype=np.float64)
        for name in ("count", "gross", "net", "success", "mfe", "mae", "gross_positive", "gross_negative")
    }
    outcomes = arrays["outcomes"][row_indices]
    gross = outcomes[:, OUTCOME_INDEX["gross_return"]]
    metric_values = {
        "count": np.ones(len(row_indices)),
        "gross": gross,
        "net": outcomes[:, OUTCOME_INDEX["net_return"]],
        "success": outcomes[:, OUTCOME_INDEX["primary_success"]],
        "mfe": outcomes[:, OUTCOME_INDEX["mfe_10d"]],
        "mae": outcomes[:, OUTCOME_INDEX["mae_10d"]],
        "gross_positive": np.maximum(gross, 0),
        "gross_negative": np.maximum(-gross, 0),
    }
    for feature_number, feature in enumerate(features):
        deciles = arrays["feature_bins"][row_indices, FEATURE_INDEX[feature]]
        regions = _region_number(deciles)
        for region in range(1, 4):
            column = feature_number * 3 + region - 1
            in_region = regions == region
            composite = cluster_ids[in_region]
            for name, values in metric_values.items():
                stats[name][:, column] = np.bincount(
                    composite, weights=values[in_region], minlength=cluster_count
                )
    return np.asarray(unique), stats, cluster_count


def cluster_bootstrap_rows(
    arrays: dict[str, np.ndarray], cfg: Config = CFG
) -> list[dict]:
    """Whole-cluster bootstrap for preregistered L/M/H shape diagnostics."""

    meta = arrays["meta"]
    output: list[dict] = []
    for period, start, end in PERIODS:
        time_mask = _date_mask(meta, start, end)
        for cohort in COHORTS:
            features = _feature_set(cohort)
            selected = time_mask & _cohort_mask(meta, cohort)
            for unit in ("signal_date", "calendar_month"):
                unit_key = "signal_date" if unit == "signal_date" else "month"
                _, stats, clusters = _cluster_design(arrays, selected, features, unit_key)
                if not clusters:
                    continue
                rng = np.random.default_rng(_stable_seed(cfg.bootstrap_seed, period, cohort, unit))
                weights = rng.multinomial(
                    clusters,
                    np.full(clusters, 1.0 / clusters),
                    size=cfg.bootstrap_iterations,
                ).astype(np.float64)
                totals = {name: weights @ values for name, values in stats.items()}
                with np.errstate(divide="ignore", invalid="ignore"):
                    estimates = {
                        "gross_average_return": totals["gross"] / totals["count"],
                        "net_average_return": totals["net"] / totals["count"],
                        "primary_success_rate": totals["success"] / totals["count"],
                        "average_mfe_10d": totals["mfe"] / totals["count"],
                        "average_mae_10d": totals["mae"] / totals["count"],
                        "gross_profit_factor": totals["gross_positive"] / totals["gross_negative"],
                    }
                for feature_number, feature in enumerate(features):
                    columns = [feature_number * 3 + region for region in range(3)]
                    for region, column in enumerate(columns):
                        for metric, matrix in estimates.items():
                            low, high = _ci(matrix[:, column])
                            point_count = stats["count"][:, column].sum()
                            if metric == "gross_average_return":
                                point = stats["gross"][:, column].sum() / point_count if point_count else math.nan
                            elif metric == "net_average_return":
                                point = stats["net"][:, column].sum() / point_count if point_count else math.nan
                            elif metric == "primary_success_rate":
                                point = stats["success"][:, column].sum() / point_count if point_count else math.nan
                            elif metric == "average_mfe_10d":
                                point = stats["mfe"][:, column].sum() / point_count if point_count else math.nan
                            elif metric == "average_mae_10d":
                                point = stats["mae"][:, column].sum() / point_count if point_count else math.nan
                            else:
                                denominator = stats["gross_negative"][:, column].sum()
                                point = stats["gross_positive"][:, column].sum() / denominator if denominator > 0 else math.nan
                            output.append(
                                {
                                    "period": period,
                                    "cohort": cohort,
                                    "feature": feature,
                                    "cluster_unit": unit,
                                    "clusters": clusters,
                                    "bootstrap_reps": cfg.bootstrap_iterations,
                                    "comparison": REGION_NAMES[region],
                                    "metric": metric,
                                    "point_estimate": _clean_number(point),
                                    "ci_low": low,
                                    "ci_high": high,
                                }
                            )
                    contrasts = {
                        "MID_MINUS_LOW": (columns[1], columns[0]),
                        "MID_MINUS_HIGH": (columns[1], columns[2]),
                        "HIGH_MINUS_MID": (columns[2], columns[1]),
                        "HIGH_MINUS_LOW": (columns[2], columns[0]),
                    }
                    for comparison, (left, right) in contrasts.items():
                        for metric in (
                            "gross_average_return",
                            "net_average_return",
                            "primary_success_rate",
                            "average_mfe_10d",
                            "average_mae_10d",
                        ):
                            differences = estimates[metric][:, left] - estimates[metric][:, right]
                            low, high = _ci(differences)
                            left_name = {
                                "gross_average_return": "gross",
                                "net_average_return": "net",
                                "primary_success_rate": "success",
                                "average_mfe_10d": "mfe",
                                "average_mae_10d": "mae",
                            }[metric]
                            left_count = stats["count"][:, left].sum()
                            right_count = stats["count"][:, right].sum()
                            point = (
                                stats[left_name][:, left].sum() / left_count
                                - stats[left_name][:, right].sum() / right_count
                                if left_count and right_count
                                else math.nan
                            )
                            output.append(
                                {
                                    "period": period,
                                    "cohort": cohort,
                                    "feature": feature,
                                    "cluster_unit": unit,
                                    "clusters": clusters,
                                    "bootstrap_reps": cfg.bootstrap_iterations,
                                    "comparison": comparison,
                                    "metric": metric,
                                    "point_estimate": _clean_number(point),
                                    "ci_low": low,
                                    "ci_high": high,
                                }
                            )
                del weights, totals, estimates
    return output


def _rankdata(values: list[float]) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    order = np.argsort(array, kind="stable")
    ranks = np.empty(len(array), dtype=np.float64)
    cursor = 0
    while cursor < len(array):
        end = cursor + 1
        while end < len(array) and array[order[end]] == array[order[cursor]]:
            end += 1
        ranks[order[cursor:end]] = ((cursor + 1) + end) / 2.0
        cursor = end
    return ranks


def _spearman(left: list[float | None], right: list[float | None]) -> float | None:
    pairs = [
        (float(a), float(b))
        for a, b in zip(left, right)
        if a is not None and b is not None and math.isfinite(float(a)) and math.isfinite(float(b))
    ]
    if len(pairs) < 3:
        return None
    left_rank = _rankdata([pair[0] for pair in pairs])
    right_rank = _rankdata([pair[1] for pair in pairs])
    if np.std(left_rank) <= 0 or np.std(right_rank) <= 0:
        return None
    return float(np.corrcoef(left_rank, right_rank)[0, 1])


def _eta_squared(arrays: dict[str, np.ndarray], mask: np.ndarray, feature: str) -> float | None:
    valid = mask & arrays["meta"]["outcome_evaluable"]
    bins = arrays["feature_bins"][valid, FEATURE_INDEX[feature]]
    gross = arrays["outcomes"][valid, OUTCOME_INDEX["gross_return"]]
    finite = bins > 0
    bins, gross = bins[finite], gross[finite]
    if len(gross) < 2:
        return None
    total = float(np.sum((gross - np.mean(gross)) ** 2))
    if total <= 0:
        return 0.0
    between = 0.0
    grand = float(np.mean(gross))
    for bucket in range(1, 11):
        values = gross[bins == bucket]
        if len(values):
            between += len(values) * (float(np.mean(values)) - grand) ** 2
    return between / total


def _profile_rows(
    bucket_rows: list[dict], period: str, cohort: str, feature: str
) -> list[dict]:
    return sorted(
        (
            row
            for row in bucket_rows
            if row.get("period") == period
            and row["cohort"] == cohort
            and row["feature"] == feature
            and row["resolution"] == "DECILE"
        ),
        key=lambda row: row["bucket"],
    )


def _region_metric(
    arrays: dict[str, np.ndarray], base: np.ndarray, feature: str, region: int
) -> dict:
    bins = arrays["feature_bins"][:, FEATURE_INDEX[feature]]
    if region == 1:
        mask = (bins >= 1) & (bins <= 3)
    elif region == 2:
        mask = (bins >= 4) & (bins <= 7)
    elif region == 3:
        mask = (bins >= 8) & (bins <= 10)
    else:
        raise ValueError("region must be 1, 2, or 3")
    return metric_summary(arrays, base & mask)


def _bootstrap_lookup(
    rows: list[dict], period: str, cohort: str, feature: str, comparison: str, metric: str
) -> list[dict]:
    return [
        row
        for row in rows
        if row["period"] == period
        and row["cohort"] == cohort
        and row["feature"] == feature
        and row["comparison"] == comparison
        and row["metric"] == metric
    ]


def _all_ci(rows: list[dict], direction: str) -> bool:
    if len(rows) != 2:
        return False
    if direction == "positive":
        return all(row["ci_low"] is not None and row["ci_low"] > 0 for row in rows)
    if direction == "negative":
        return all(row["ci_high"] is not None and row["ci_high"] < 0 for row in rows)
    raise ValueError("direction must be positive or negative")


def shape_diagnostic_rows(
    arrays: dict[str, np.ndarray], bucket_rows: list[dict], bootstrap_rows: list[dict], cfg: Config = CFG
) -> list[dict]:
    meta = arrays["meta"]
    output = []
    for cohort in COHORTS:
        for feature in _feature_set(cohort):
            profiles: dict[str, list[float | None]] = {}
            regions: dict[str, dict[int, dict]] = {}
            eta: dict[str, float | None] = {}
            for period, start, end in PERIODS:
                rows = _profile_rows(bucket_rows, period, cohort, feature)
                profiles[period] = [row["gross_average_return"] for row in rows]
                base = _date_mask(meta, start, end) & _cohort_mask(meta, cohort)
                regions[period] = {
                    region: _region_metric(arrays, base, feature, region)
                    for region in (1, 2, 3)
                }
                eta[period] = _eta_squared(arrays, base, feature)

            discovery = PERIODS[0][0]
            confirmation = PERIODS[1][0]
            stress = PERIODS[2][0]
            discovery_profile = profiles[discovery]
            finite_profile = [value for value in discovery_profile if value is not None]
            peak_bucket = (
                int(np.nanargmax(np.asarray([math.nan if value is None else value for value in discovery_profile]))) + 1
                if finite_profile
                else None
            )
            index_rho = _spearman(list(range(1, 11)), discovery_profile)
            adjacent = [
                float(discovery_profile[index + 1]) - float(discovery_profile[index])
                for index in range(9)
                if discovery_profile[index] is not None and discovery_profile[index + 1] is not None
            ]
            increasing = sum(delta > 0 for delta in adjacent)
            decreasing = sum(delta < 0 for delta in adjacent)
            d = regions[discovery]
            mid_low = (
                d[2]["gross_average_return"] - d[1]["gross_average_return"]
                if d[2]["gross_average_return"] is not None and d[1]["gross_average_return"] is not None
                else None
            )
            mid_high = (
                d[2]["gross_average_return"] - d[3]["gross_average_return"]
                if d[2]["gross_average_return"] is not None and d[3]["gross_average_return"] is not None
                else None
            )
            high_mid = -mid_high if mid_high is not None else None
            high_low = (
                d[3]["gross_average_return"] - d[1]["gross_average_return"]
                if d[3]["gross_average_return"] is not None and d[1]["gross_average_return"] is not None
                else None
            )
            inv_supported = bool(
                mid_low is not None
                and mid_high is not None
                and mid_low > 0
                and mid_high > 0
                and peak_bucket is not None
                and 4 <= peak_bucket <= 7
                and _all_ci(
                    _bootstrap_lookup(bootstrap_rows, discovery, cohort, feature, "MID_MINUS_LOW", "gross_average_return"),
                    "positive",
                )
                and _all_ci(
                    _bootstrap_lookup(bootstrap_rows, discovery, cohort, feature, "MID_MINUS_HIGH", "gross_average_return"),
                    "positive",
                )
            )
            threshold_supported = bool(
                not inv_supported
                and high_mid is not None
                and high_mid < 0
                and mid_low is not None
                and mid_low >= 0
                and _all_ci(
                    _bootstrap_lookup(bootstrap_rows, discovery, cohort, feature, "HIGH_MINUS_MID", "gross_average_return"),
                    "negative",
                )
            )
            monotonic_up = bool(
                not inv_supported
                and not threshold_supported
                and index_rho is not None
                and index_rho >= cfg.monotonic_min_abs_spearman
                and increasing >= cfg.monotonic_min_adjacent_agreement
                and _all_ci(
                    _bootstrap_lookup(bootstrap_rows, discovery, cohort, feature, "HIGH_MINUS_LOW", "gross_average_return"),
                    "positive",
                )
            )
            monotonic_down = bool(
                not inv_supported
                and not threshold_supported
                and index_rho is not None
                and index_rho <= -cfg.monotonic_min_abs_spearman
                and decreasing >= cfg.monotonic_min_adjacent_agreement
                and _all_ci(
                    _bootstrap_lookup(bootstrap_rows, discovery, cohort, feature, "HIGH_MINUS_LOW", "gross_average_return"),
                    "negative",
                )
            )
            if inv_supported:
                shape = "INVERTED_U"
            elif threshold_supported:
                shape = "HIGH_TAIL_THRESHOLD_LIKE"
            elif monotonic_up:
                shape = "MONOTONIC_UP"
            elif monotonic_down:
                shape = "MONOTONIC_DOWN"
            else:
                shape = "NO_STABLE_RELATION"

            later_direction_ok = []
            for period in (confirmation, stress):
                values = regions[period]
                later_mid_low = _difference(
                    values[2]["gross_average_return"], values[1]["gross_average_return"]
                )
                later_mid_high = _difference(
                    values[2]["gross_average_return"], values[3]["gross_average_return"]
                )
                later_high_low = _difference(
                    values[3]["gross_average_return"], values[1]["gross_average_return"]
                )
                if shape == "INVERTED_U":
                    later_direction_ok.append(
                        later_mid_low is not None
                        and later_mid_high is not None
                        and later_mid_low > 0
                        and later_mid_high > 0
                    )
                elif shape == "HIGH_TAIL_THRESHOLD_LIKE":
                    later_direction_ok.append(
                        later_mid_high is not None and later_mid_high > 0
                    )
                elif shape == "MONOTONIC_UP":
                    later_direction_ok.append(
                        later_high_low is not None and later_high_low > 0
                    )
                elif shape == "MONOTONIC_DOWN":
                    later_direction_ok.append(
                        later_high_low is not None and later_high_low < 0
                    )
                else:
                    later_direction_ok.append(False)

            corr_confirmation = _spearman(discovery_profile, profiles[confirmation])
            corr_stress = _spearman(discovery_profile, profiles[stress])
            correlation_values = [
                value for value in (corr_confirmation, corr_stress) if value is not None
            ]
            profile_stability = min(correlation_values) if len(correlation_values) == 2 else None
            eta_values = [value for value in eta.values() if value is not None]
            stable_information_score = (
                min(eta_values) * max(0.0, profile_stability)
                if len(eta_values) == 3 and profile_stability is not None
                else None
            )

            mfe_delta = {}
            mae_delta = {}
            for period in (discovery, confirmation, stress):
                mfe_delta[period] = _difference(
                    regions[period][3]["average_mfe_10d"],
                    regions[period][2]["average_mfe_10d"],
                )
                mae_delta[period] = _difference(
                    regions[period][3]["average_mae_10d"],
                    regions[period][2]["average_mae_10d"],
                )
            mae_ci_negative = _all_ci(
                _bootstrap_lookup(bootstrap_rows, discovery, cohort, feature, "HIGH_MINUS_MID", "average_mae_10d"),
                "negative",
            )
            mfe_ci_negative = _all_ci(
                _bootstrap_lookup(bootstrap_rows, discovery, cohort, feature, "HIGH_MINUS_MID", "average_mfe_10d"),
                "negative",
            )
            mae_complete = all(value is not None for value in mae_delta.values())
            mfe_complete = all(value is not None for value in mfe_delta.values())
            if (
                mae_complete
                and all(float(value) < 0 for value in mae_delta.values())
                and mae_ci_negative
            ):
                if (
                    mfe_complete
                    and all(float(value) < 0 for value in mfe_delta.values())
                    and mfe_ci_negative
                ):
                    mechanism = "BOTH_MFE_LOWER_AND_MAE_WORSE"
                else:
                    mechanism = "MAE_WORSE_WITHOUT_STABLE_MFE_REDUCTION"
            else:
                mechanism = "NOT_STABLY_MAE_DRIVEN"

            output.append(
                {
                    "cohort": cohort,
                    "feature": feature,
                    "discovery_shape": shape,
                    "later_period_same_direction": all(later_direction_ok),
                    "stable_inverted_u_descriptive": shape == "INVERTED_U" and all(later_direction_ok),
                    "multiplicity_status": "UNADJUSTED_EXPLORATORY_NO_FEATURE_SELECTION",
                    "discovery_peak_decile": peak_bucket,
                    "discovery_spearman_bucket_vs_gross": index_rho,
                    "discovery_adjacent_increases": increasing,
                    "discovery_adjacent_decreases": decreasing,
                    "discovery_mid_minus_low_gross": mid_low,
                    "discovery_mid_minus_high_gross": mid_high,
                    "discovery_high_minus_mid_gross": high_mid,
                    "discovery_high_minus_low_gross": high_low,
                    "profile_correlation_discovery_confirmation": corr_confirmation,
                    "profile_correlation_discovery_stress": corr_stress,
                    "profile_stability_score": profile_stability,
                    "eta_squared_discovery": eta[discovery],
                    "eta_squared_confirmation": eta[confirmation],
                    "eta_squared_stress": eta[stress],
                    "stable_information_score": stable_information_score,
                    "high_minus_mid_mfe_discovery": mfe_delta[discovery],
                    "high_minus_mid_mfe_confirmation": mfe_delta[confirmation],
                    "high_minus_mid_mfe_stress": mfe_delta[stress],
                    "high_minus_mid_mae_discovery": mae_delta[discovery],
                    "high_minus_mid_mae_confirmation": mae_delta[confirmation],
                    "high_minus_mid_mae_stress": mae_delta[stress],
                    "high_extension_mechanism": mechanism,
                }
            )
    return output


def year_direction_consistency_rows(
    arrays: dict[str, np.ndarray]
) -> list[dict]:
    meta = arrays["meta"]
    output = []
    for cohort in COHORTS:
        for feature in _feature_set(cohort):
            gross_deltas: list[float] = []
            mfe_deltas: list[float] = []
            mae_deltas: list[float] = []
            row: dict = {"cohort": cohort, "feature": feature}
            for year in range(2020, 2026):
                base = _date_mask(meta, f"{year}0101", f"{year}1231") & _cohort_mask(meta, cohort)
                mid = _region_metric(arrays, base, feature, 2)
                high = _region_metric(arrays, base, feature, 3)
                gross = _difference(high["gross_average_return"], mid["gross_average_return"])
                mfe = _difference(high["average_mfe_10d"], mid["average_mfe_10d"])
                mae = _difference(high["average_mae_10d"], mid["average_mae_10d"])
                gross_deltas.append(gross)
                mfe_deltas.append(mfe)
                mae_deltas.append(mae)
                row[f"gross_high_minus_mid_{year}"] = gross
                row[f"mfe_high_minus_mid_{year}"] = mfe
                row[f"mae_high_minus_mid_{year}"] = mae
            row.update(
                {
                    "gross_all_years_positive": len(gross_deltas) == 6 and all(value is not None and value > 0 for value in gross_deltas),
                    "gross_all_years_negative": len(gross_deltas) == 6 and all(value is not None and value < 0 for value in gross_deltas),
                    "mfe_all_years_positive": len(mfe_deltas) == 6 and all(value is not None and value > 0 for value in mfe_deltas),
                    "mfe_all_years_negative": len(mfe_deltas) == 6 and all(value is not None and value < 0 for value in mfe_deltas),
                    "mae_all_years_positive": len(mae_deltas) == 6 and all(value is not None and value > 0 for value in mae_deltas),
                    "mae_all_years_negative": len(mae_deltas) == 6 and all(value is not None and value < 0 for value in mae_deltas),
                }
            )
            row["gross_direction_consistent_2020_2025"] = bool(
                row["gross_all_years_positive"] or row["gross_all_years_negative"]
            )
            row["mfe_direction_consistent_2020_2025"] = bool(
                row["mfe_all_years_positive"] or row["mfe_all_years_negative"]
            )
            row["mae_direction_consistent_2020_2025"] = bool(
                row["mae_all_years_positive"] or row["mae_all_years_negative"]
            )
            output.append(row)
    return output


def key_interaction_bootstrap_rows(
    arrays: dict[str, np.ndarray], quintiles: dict[str, tuple[float, ...]], cfg: Config = CFG
) -> list[dict]:
    """Cluster CIs for the two preregistered interaction questions, not cell selection."""

    meta = arrays["meta"]
    bias_q = _bucket_ids(arrays["features"][:, FEATURE_INDEX["bias_20"]], quintiles["bias_20"])
    output: list[dict] = []
    metrics = {
        "gross_average_return": OUTCOME_INDEX["gross_return"],
        "net_average_return": OUTCOME_INDEX["net_return"],
        "primary_success_rate": OUTCOME_INDEX["primary_success"],
        "average_mfe_10d": OUTCOME_INDEX["mfe_10d"],
        "average_mae_10d": OUTCOME_INDEX["mae_10d"],
    }
    for period, start, end in PERIODS:
        time_mask = _date_mask(meta, start, end) & meta["outcome_evaluable"]
        definitions = {
            "MOMENTUM_Q5_BIAS_Q5_MINUS_Q3": [
                time_mask & (meta["momentum_strength_quintile"] == 5) & (bias_q == 5),
                time_mask & (meta["momentum_strength_quintile"] == 5) & (bias_q == 3),
            ],
            "HIGH_BIAS_LARGE_GAP_MINUS_ZERO_TO_ONE_GAP": [
                time_mask & (bias_q == 5) & (meta["entry_gap_bucket"] >= 5),
                time_mask & (bias_q == 5) & (meta["entry_gap_bucket"] == 3),
            ],
            "GAP_PENALTY_DID_Q5_MINUS_Q3": [
                time_mask & (bias_q == 5) & (meta["entry_gap_bucket"] >= 5),
                time_mask & (bias_q == 5) & (meta["entry_gap_bucket"] == 3),
                time_mask & (bias_q == 3) & (meta["entry_gap_bucket"] >= 5),
                time_mask & (bias_q == 3) & (meta["entry_gap_bucket"] == 3),
            ],
        }
        for comparison, masks in definitions.items():
            union = np.logical_or.reduce(masks)
            indices = np.flatnonzero(union)
            for cluster_unit in ("signal_date", "calendar_month"):
                labels = meta["signal_date"][indices]
                if cluster_unit == "calendar_month":
                    labels = labels // 100
                unique, cluster_ids = np.unique(labels, return_inverse=True)
                clusters = len(unique)
                if not clusters:
                    continue
                group_for_row = np.full(len(meta), -1, dtype=np.int8)
                for group, mask in enumerate(masks):
                    group_for_row[mask] = group
                groups = group_for_row[indices]
                width = len(masks)
                count = np.zeros((clusters, width), dtype=np.float64)
                sums = {name: np.zeros((clusters, width), dtype=np.float64) for name in metrics}
                for group in range(width):
                    selected_rows = groups == group
                    count[:, group] = np.bincount(cluster_ids[selected_rows], minlength=clusters)
                    outcome_rows = arrays["outcomes"][indices[selected_rows]]
                    for name, column in metrics.items():
                        sums[name][:, group] = np.bincount(
                            cluster_ids[selected_rows],
                            weights=outcome_rows[:, column],
                            minlength=clusters,
                        )
                rng = np.random.default_rng(
                    _stable_seed(cfg.bootstrap_seed, period, comparison, cluster_unit)
                )
                weights = rng.multinomial(
                    clusters,
                    np.full(clusters, 1.0 / clusters),
                    size=cfg.bootstrap_iterations,
                ).astype(np.float64)
                denominator = weights @ count
                for metric, values in sums.items():
                    with np.errstate(divide="ignore", invalid="ignore"):
                        means = (weights @ values) / denominator
                    if width == 2:
                        estimates = means[:, 0] - means[:, 1]
                        point = values[:, 0].sum() / count[:, 0].sum() - values[:, 1].sum() / count[:, 1].sum()
                    else:
                        estimates = (means[:, 0] - means[:, 1]) - (means[:, 2] - means[:, 3])
                        point = (
                            values[:, 0].sum() / count[:, 0].sum()
                            - values[:, 1].sum() / count[:, 1].sum()
                            - values[:, 2].sum() / count[:, 2].sum()
                            + values[:, 3].sum() / count[:, 3].sum()
                        )
                    low, high = _ci(estimates)
                    output.append(
                        {
                            "period": period,
                            "cohort": "ALL_ELIGIBLE_INTERACTION",
                            "feature": "bias_20",
                            "cluster_unit": cluster_unit,
                            "clusters": clusters,
                            "bootstrap_reps": cfg.bootstrap_iterations,
                            "comparison": comparison,
                            "metric": metric,
                            "point_estimate": _clean_number(point),
                            "ci_low": low,
                            "ci_high": high,
                        }
                    )
                del group_for_row, weights
    return output
