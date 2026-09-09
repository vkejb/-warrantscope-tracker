from __future__ import annotations

from collections import Counter
import math

import numpy as np

from cross_sectional_alpha_ranking_v01.config import PERIODS
from cross_sectional_alpha_ranking_v01.preprocessing import iter_date_slices, rank_percentile, spearman
from extension_entry_study_v01.pipeline import OUTCOME_FIELDS

from .config import CONDITIONAL_TOP_K, GAP_BUCKETS


OI = {name: index for index, name in enumerate(OUTCOME_FIELDS)}
PATH_NAMES = {1: "UPSIDE_FIRST", 2: "DOWNSIDE_FIRST", 3: "TIMEOUT"}


def date_mask(meta: np.ndarray, start: str, end: str) -> np.ndarray:
    return (meta["signal_date"] >= int(start)) & (meta["signal_date"] <= int(end))


def time_slices() -> list[tuple[str, str, str, str]]:
    rows = [("PERIOD", label, start, end) for label, start, end in PERIODS]
    rows.extend(("YEAR", str(year), f"{year}0101", f"{year}1231") for year in range(2020, 2026))
    rows.append(("OVERALL", "2020_2025", "20200101", "20251231"))
    return rows


def finite_mean(values: np.ndarray) -> float | None:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    return float(np.mean(values)) if len(values) else None


def finite_median(values: np.ndarray) -> float | None:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    return float(np.median(values)) if len(values) else None


def profit_factor(values: np.ndarray) -> float | None:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    gain = float(values[values > 0].sum())
    loss = float(-values[values < 0].sum())
    return gain / loss if loss > 0 else None


def binary_auc(target: np.ndarray, score: np.ndarray) -> float | None:
    finite = np.isfinite(target) & np.isfinite(score)
    target = np.asarray(target[finite], dtype=np.float64)
    score = np.asarray(score[finite], dtype=np.float64)
    positive = target >= 0.5
    negative = ~positive
    if not np.any(positive) or not np.any(negative):
        return None
    ranks = rank_percentile(score) * (len(score) - 1) + 1
    rank_sum = float(np.sum(ranks[positive]))
    n_pos = int(np.count_nonzero(positive))
    n_neg = int(np.count_nonzero(negative))
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def probability_metrics(target: np.ndarray, probability: np.ndarray) -> dict:
    finite = np.isfinite(target) & np.isfinite(probability)
    y = np.asarray(target[finite], dtype=np.float64)
    p = np.clip(np.asarray(probability[finite], dtype=np.float64), 1e-12, 1.0 - 1e-12)
    return {
        "observations": len(y),
        "event_rate": finite_mean(y),
        "predicted_probability_mean": finite_mean(p),
        "log_loss": float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))) if len(y) else None,
        "brier_score": float(np.mean((p - y) ** 2)) if len(y) else None,
        "auc": binary_auc(y, p),
    }


def _tail_keep(values: np.ndarray, fraction: float) -> tuple[np.ndarray, int]:
    candidates = np.flatnonzero(np.isfinite(values) & (values > 0))
    remove = int(math.ceil(len(candidates) * fraction)) if len(candidates) else 0
    keep = np.ones(len(values), dtype=bool)
    if remove:
        ranked = candidates[np.argsort(-values[candidates], kind="stable")]
        keep[ranked[:remove]] = False
    return keep, remove


def _without_best_cluster(
    values: np.ndarray, clusters: np.ndarray
) -> tuple[np.ndarray, int | None]:
    unique = np.unique(clusters)
    if len(unique) < 2:
        return np.ones(len(values), dtype=bool), None
    means = []
    for cluster in unique:
        local = values[clusters == cluster]
        means.append(-np.inf if finite_mean(local) is None else finite_mean(local))
    best = int(unique[int(np.argmax(means))])
    return clusters != best, best


def metric_summary(arrays: dict[str, np.ndarray], selected: np.ndarray) -> dict:
    meta = arrays["meta"]
    valid = selected & meta["outcome_evaluable"]
    indices = np.flatnonzero(valid)
    base = {
        "observations": int(np.count_nonzero(selected)),
        "evaluable_observations": len(indices),
        "unique_dates": int(len(np.unique(meta["signal_date"][selected]))),
        "unique_months": int(len(np.unique(meta["signal_date"][selected] // 100))),
        "unique_stocks": int(len(np.unique(meta["stock_code"][selected]))),
    }
    if not len(indices):
        return {**base, "path_success_rate": None, "net_mean": None, "net_profit_factor": None}
    outcomes = arrays["outcomes"][indices]
    desc = arrays["descriptive_outcomes"][indices]
    paths = arrays["path_class"][indices]
    gross = outcomes[:, OI["gross_return"]]
    net = outcomes[:, OI["net_return"]]
    mfe10 = outcomes[:, OI["mfe_10d"]]
    mae10 = outcomes[:, OI["mae_10d"]]
    result = {
        **base,
        "path_success_rate": float(np.mean(paths == 1)),
        "downside_first_rate": float(np.mean(paths == 2)),
        "timeout_rate": float(np.mean(paths == 3)),
        "plus10_before_minus5_rate": finite_mean(desc[:, 0]),
        "plus15_before_minus5_rate": finite_mean(desc[:, 1]),
        "day1_return": finite_mean(outcomes[:, OI["day1_close_return"]]),
        "day3_return": finite_mean(outcomes[:, OI["day3_close_return"]]),
        "day5_return": finite_mean(outcomes[:, OI["day5_close_return"]]),
        "day10_return": finite_mean(outcomes[:, OI["day10_close_return"]]),
        "mfe5_mean": finite_mean(outcomes[:, OI["mfe_5d"]]),
        "mfe10_mean": finite_mean(mfe10),
        "mae5_mean": finite_mean(outcomes[:, OI["mae_5d"]]),
        "mae10_mean": finite_mean(mae10),
        "mfe_abs_mae": finite_mean(mfe10) / abs(finite_mean(mae10)) if finite_mean(mae10) not in (None, 0.0) else None,
        "gross_mean": finite_mean(gross),
        "net_mean": finite_mean(net),
        "median_net": finite_median(net),
        "gross_profit_factor": profit_factor(gross),
        "net_profit_factor": profit_factor(net),
    }
    for label, fraction in (("top1", 0.01), ("top5", 0.05)):
        keep, removed = _tail_keep(net, fraction)
        result[f"{label}_positive_winners_removed"] = removed
        result[f"{label}_removed_net_mean"] = finite_mean(net[keep])
        result[f"{label}_removed_net_pf"] = profit_factor(net[keep])
        result[f"{label}_removed_gross_mean"] = finite_mean(gross[keep])
        result[f"{label}_removed_gross_pf"] = profit_factor(gross[keep])
    dates = meta["signal_date"][indices]
    months = dates // 100
    for label, clusters in (("best_signal_date", dates), ("best_calendar_month", months)):
        keep, removed_cluster = _without_best_cluster(net, clusters)
        result[f"removed_{label}"] = removed_cluster
        result[f"remove_{label}_net_mean"] = finite_mean(net[keep])
        result[f"remove_{label}_net_pf"] = profit_factor(net[keep])
    return result


def daily_path_rows(
    model: str,
    scores: np.ndarray,
    pool: np.ndarray,
    arrays: dict[str, np.ndarray],
) -> list[dict]:
    rows = []
    meta = arrays["meta"]
    for day, region in iter_date_slices(meta["signal_date"]):
        local = pool[region] & meta["outcome_evaluable"][region]
        rows.append({
            "signal_date": day,
            "year": day // 10000,
            "month": day // 100,
            "model": model,
            "pool_evaluable": int(np.count_nonzero(local)),
            "daily_spearman_ic_path_success": spearman(scores[region][local], arrays["path_success"][region][local]),
        })
    return rows


def _daily_quality(rows: list[dict]) -> dict:
    values = np.asarray([
        np.nan if row["daily_spearman_ic_path_success"] is None else row["daily_spearman_ic_path_success"]
        for row in rows
    ], dtype=np.float64)
    finite = values[np.isfinite(values)]
    return {
        "mean_daily_path_ic": finite_mean(finite),
        "median_daily_path_ic": finite_median(finite),
        "path_ic_positive_day_rate": float(np.mean(finite > 0)) if len(finite) else None,
        "path_ic_days": len(finite),
    }


def conditional_topk_rows(
    arrays: dict[str, np.ndarray],
    rankings: dict[str, dict[str, np.ndarray]],
    daily_rows: list[dict],
) -> list[dict]:
    rows = []
    for model, ranking in rankings.items():
        for kind, label, start, end in time_slices():
            period = date_mask(arrays["meta"], start, end)
            local_daily = [
                row for row in daily_rows
                if row["model"] == model and int(start) <= row["signal_date"] <= int(end)
            ]
            for k in CONDITIONAL_TOP_K:
                row = {
                    "time_slice_type": kind,
                    "time_slice": label,
                    "model": model,
                    "top_k": k,
                    "primary_selection": model == "LOGISTIC_RIDGE_PATH_SUCCESS" and k == 5,
                    **metric_summary(arrays, period & (ranking["rank"] > 0) & (ranking["rank"] <= k)),
                }
                if k == 5:
                    row.update(_daily_quality(local_daily))
                rows.append(row)
    return rows


def comparison_rows(
    arrays: dict[str, np.ndarray], conditional_rank: np.ndarray
) -> list[dict]:
    cohorts = {
        "FULL_MARKET": np.ones(len(conditional_rank), dtype=bool),
        "STAGE_A_TOP30": arrays["stage_a_ranks"] <= 30,
        "CONDITIONAL_TOP10": (conditional_rank > 0) & (conditional_rank <= 10),
        "CONDITIONAL_TOP5": (conditional_rank > 0) & (conditional_rank <= 5),
        "PREVIOUS_TWO_STAGE_TOP5": (arrays["previous_two_stage_ranks"] > 0) & (arrays["previous_two_stage_ranks"] <= 5),
    }
    rows = []
    for kind, label, start, end in time_slices():
        period = date_mask(arrays["meta"], start, end)
        metrics = {name: metric_summary(arrays, period & mask) for name, mask in cohorts.items()}
        for name, values in metrics.items():
            row = {
                "time_slice_type": kind,
                "time_slice": label,
                "cohort": name,
                "primary_selection": name == "CONDITIONAL_TOP5",
                **values,
            }
            if name in {"CONDITIONAL_TOP10", "CONDITIONAL_TOP5"}:
                stage = metrics["STAGE_A_TOP30"]
                row.update({
                    "delta_path_success_vs_stage_a": values["path_success_rate"] - stage["path_success_rate"],
                    "delta_plus10_vs_stage_a": values["plus10_before_minus5_rate"] - stage["plus10_before_minus5_rate"],
                    "delta_plus15_vs_stage_a": values["plus15_before_minus5_rate"] - stage["plus15_before_minus5_rate"],
                    "delta_downside_first_vs_stage_a": values["downside_first_rate"] - stage["downside_first_rate"],
                    "delta_mfe10_vs_stage_a": values["mfe10_mean"] - stage["mfe10_mean"],
                    "delta_mae10_vs_stage_a": values["mae10_mean"] - stage["mae10_mean"],
                    "delta_net_vs_stage_a": values["net_mean"] - stage["net_mean"],
                    "delta_net_pf_vs_stage_a": values["net_profit_factor"] - stage["net_profit_factor"],
                })
            rows.append(row)
    return rows


def retention_rows(comparison: list[dict]) -> tuple[list[dict], list[dict]]:
    lookup = {(row["time_slice_type"], row["time_slice"], row["cohort"]): row for row in comparison}
    mfe_rows = []
    mae_rows = []
    for kind, label, _start, _end in time_slices():
        stage = lookup[(kind, label, "STAGE_A_TOP30")]
        top = lookup[(kind, label, "CONDITIONAL_TOP5")]
        retention = top["mfe10_mean"] / stage["mfe10_mean"] if stage["mfe10_mean"] else None
        improvement = top["mae10_mean"] - stage["mae10_mean"]
        mfe_rows.append({
            "time_slice_type": kind,
            "time_slice": label,
            "stage_a_top30_mfe10": stage["mfe10_mean"],
            "conditional_top5_mfe10": top["mfe10_mean"],
            "mfe_retention_ratio": retention,
            "mfe_retention_percent": retention * 100 if retention is not None else None,
        })
        mae_rows.append({
            "time_slice_type": kind,
            "time_slice": label,
            "stage_a_top30_mae10": stage["mae10_mean"],
            "conditional_top5_mae10": top["mae10_mean"],
            "mae_absolute_improvement": improvement,
            "mae_improvement_percentage": improvement / abs(stage["mae10_mean"]) if stage["mae10_mean"] else None,
        })
    return mfe_rows, mae_rows


def calibration_rows(
    arrays: dict[str, np.ndarray], ranking: dict[str, np.ndarray]
) -> list[dict]:
    rows = []
    pool = ranking["rank"] > 0
    for kind, label, start, end in time_slices():
        if kind not in {"PERIOD", "OVERALL"}:
            continue
        period = date_mask(arrays["meta"], start, end)
        decile_results = []
        for decile in range(1, 11):
            selection = period & pool & (ranking["probability_decile"] == decile) & arrays["meta"]["outcome_evaluable"]
            metrics = probability_metrics(arrays["path_success"][selection], ranking["scores"][selection])
            decile_results.append((decile, metrics))
        success_by_decile = np.asarray([entry[1]["event_rate"] for entry in decile_results], dtype=np.float64)
        calibration_spearman = spearman(np.arange(1, 11, dtype=float), success_by_decile)
        monotonic = bool(np.all(np.diff(success_by_decile) >= -1e-12))
        total = sum(entry[1]["observations"] for entry in decile_results)
        ece = sum(
            entry[1]["observations"]
            * abs(entry[1]["predicted_probability_mean"] - entry[1]["event_rate"])
            for entry in decile_results
            if entry[1]["observations"]
        ) / total if total else None
        for decile, metrics in decile_results:
            rows.append({
                "time_slice_type": kind,
                "time_slice": label,
                "row_type": "PROBABILITY_DECILE",
                "band": "BOTTOM" if decile == 1 else "MIDDLE" if decile in {5, 6} else "TOP" if decile == 10 else "OTHER",
                "probability_decile": decile,
                **metrics,
                "decile_success_spearman": calibration_spearman,
                "strictly_nondecreasing_realized_success": monotonic,
                "expected_calibration_error": ece,
            })
        for band, deciles in (("BOTTOM", {1}), ("MIDDLE", {5, 6}), ("TOP", {10})):
            selection = period & pool & np.isin(ranking["probability_decile"], list(deciles)) & arrays["meta"]["outcome_evaluable"]
            rows.append({
                "time_slice_type": kind,
                "time_slice": label,
                "row_type": "SUMMARY_BAND",
                "band": band,
                "probability_decile": None,
                **probability_metrics(arrays["path_success"][selection], ranking["scores"][selection]),
                "decile_success_spearman": calibration_spearman,
                "strictly_nondecreasing_realized_success": monotonic,
                "expected_calibration_error": ece,
            })
    return rows


def top_bottom_rows(
    arrays: dict[str, np.ndarray], conditional_rank: np.ndarray
) -> list[dict]:
    rows = []
    top = (conditional_rank > 0) & (conditional_rank <= 5)
    bottom = conditional_rank >= 26
    for kind, label, start, end in time_slices():
        period = date_mask(arrays["meta"], start, end)
        top_metrics = metric_summary(arrays, period & top)
        bottom_metrics = metric_summary(arrays, period & bottom)
        row = {"time_slice_type": kind, "time_slice": label}
        for prefix, metrics in (("top5", top_metrics), ("bottom5", bottom_metrics)):
            for key in (
                "observations", "path_success_rate", "downside_first_rate", "timeout_rate",
                "mfe10_mean", "mae10_mean", "net_mean", "net_profit_factor",
            ):
                row[f"{prefix}_{key}"] = metrics[key]
        for key in ("path_success_rate", "downside_first_rate", "mfe10_mean", "mae10_mean", "net_mean"):
            row[f"top_minus_bottom_{key}"] = top_metrics[key] - bottom_metrics[key]
        rows.append(row)
    return rows


def previous_stage_b_comparison_rows(comparison: list[dict]) -> list[dict]:
    lookup = {(row["time_slice_type"], row["time_slice"], row["cohort"]): row for row in comparison}
    rows = []
    for kind, label, _start, _end in time_slices():
        conditional = lookup[(kind, label, "CONDITIONAL_TOP5")]
        previous = lookup[(kind, label, "PREVIOUS_TWO_STAGE_TOP5")]
        row = {"time_slice_type": kind, "time_slice": label}
        for key in (
            "path_success_rate", "downside_first_rate", "timeout_rate", "mfe10_mean",
            "mae10_mean", "gross_mean", "net_mean", "gross_profit_factor", "net_profit_factor",
        ):
            row[f"conditional_{key}"] = conditional[key]
            row[f"previous_{key}"] = previous[key]
            row[f"difference_{key}"] = conditional[key] - previous[key]
        rows.append(row)
    return rows


def cooldown_rows(arrays: dict[str, np.ndarray], cooldown: np.ndarray) -> list[dict]:
    return [
        {
            "time_slice_type": kind,
            "time_slice": label,
            "cohort": "CONDITIONAL_TOP5_10D_COOLDOWN_TRADE_PROXY",
            **metric_summary(arrays, date_mask(arrays["meta"], start, end) & cooldown),
        }
        for kind, label, start, end in time_slices()
    ]


def frequency_rows(
    arrays: dict[str, np.ndarray], raw: np.ndarray, cooldown: np.ndarray
) -> list[dict]:
    meta = arrays["meta"]
    rows = []
    for mode, selection in (("RAW_CONDITIONAL_TOP5", raw), ("10D_COOLDOWN_TRADE_PROXY", cooldown)):
        for kind, label, start, end in time_slices():
            period = date_mask(meta, start, end)
            calendar = np.unique(meta["signal_date"][period])
            months = np.unique(calendar // 100)
            counts = Counter(map(int, meta["signal_date"][period & selection]))
            daily = np.asarray([counts.get(int(day), 0) for day in calendar], dtype=np.float64)
            monthly = np.asarray([
                np.count_nonzero(period & selection & (meta["signal_date"] // 100 == month))
                for month in months
            ], dtype=np.float64)
            active_month = np.asarray([
                len(np.unique(meta["signal_date"][period & selection & (meta["signal_date"] // 100 == month)]))
                for month in months
            ], dtype=np.float64)
            unique_stocks_month = np.asarray([
                len(np.unique(meta["stock_code"][period & selection & (meta["signal_date"] // 100 == month)]))
                for month in months
            ], dtype=np.float64)
            rows.append({
                "time_slice_type": kind,
                "time_slice": label,
                "ranking_mode": mode,
                "opportunities": int(np.count_nonzero(period & selection)),
                "raw_candidates_per_month": finite_mean(monthly) if mode == "RAW_CONDITIONAL_TOP5" else None,
                "cooldown_opportunities_per_month": finite_mean(monthly) if mode == "10D_COOLDOWN_TRADE_PROXY" else None,
                "active_dates": int(np.count_nonzero(daily > 0)),
                "active_dates_per_month": finite_mean(active_month),
                "median_opportunities_per_day": finite_median(daily),
                "p90_opportunities_per_day": float(np.quantile(daily, 0.9, method="linear")),
                "maximum_opportunities_per_day": int(np.max(daily)),
                "unique_stocks_per_month": finite_mean(unique_stocks_month),
            })
    return rows


def regime_rows(
    arrays: dict[str, np.ndarray], conditional_rank: np.ndarray, regimes: dict[str, np.ndarray]
) -> list[dict]:
    groups = {
        "0050_CLOSE_VS_MA20": (("ABOVE_OR_EQUAL", regimes["close_vs_ma20"] >= 0), ("BELOW", regimes["close_vs_ma20"] < 0)),
        "0050_MA20_SLOPE5": (("RISING_OR_FLAT", regimes["ma20_slope5"] >= 0), ("FALLING", regimes["ma20_slope5"] < 0)),
        "0050_VOLATILITY20": (("HIGH", regimes["volatility20"] >= regimes["volatility_discovery_median"]), ("LOW", regimes["volatility20"] < regimes["volatility_discovery_median"])),
    }
    top = (conditional_rank > 0) & (conditional_rank <= 5)
    rows = []
    for label, start, end in PERIODS:
        period = date_mask(arrays["meta"], start, end)
        for dimension, values in groups.items():
            for group, mask in values:
                rows.append({
                    "time_slice": label,
                    "market_proxy": "0050",
                    "regime_dimension": dimension,
                    "regime_group": group,
                    **metric_summary(arrays, period & mask & top),
                })
    return rows


def gap_rows(arrays: dict[str, np.ndarray], conditional_rank: np.ndarray) -> list[dict]:
    top = (conditional_rank > 0) & (conditional_rank <= 5)
    gap = arrays["entry_gap"]
    rows = []
    for label, start, end in PERIODS:
        period = date_mask(arrays["meta"], start, end)
        for bucket, lower, upper in GAP_BUCKETS:
            local = np.isfinite(gap)
            if lower is not None:
                local &= gap >= lower
            if upper is not None:
                local &= gap < upper
            rows.append({
                "time_slice": label,
                "entry_gap_bucket": bucket,
                "lower_inclusive": lower,
                "upper_exclusive": upper,
                **metric_summary(arrays, period & top & local),
            })
    return rows


def n_compact_rows(arrays: dict[str, np.ndarray], conditional_rank: np.ndarray) -> list[dict]:
    meta = arrays["meta"]
    rows = []
    for index in np.flatnonzero(arrays["n_compact"]):
        rows.append({
            "row_type": "SIGNAL_DETAIL",
            "signal_date": int(meta["signal_date"][index]),
            "stock_code": int(meta["stock_code"][index]),
            "stage_a_rank": int(arrays["stage_a_ranks"][index]),
            "in_stage_a_top30": bool(arrays["stage_a_ranks"][index] <= 30),
            "conditional_path_rank": int(conditional_rank[index]) if conditional_rank[index] else None,
            "in_conditional_top5": bool(0 < conditional_rank[index] <= 5),
        })
    compact = arrays["n_compact"]
    conditional = (conditional_rank > 0) & (conditional_rank <= 5)
    for kind, label, start, end in time_slices():
        period = date_mask(meta, start, end)
        for cohort, selection in (
            ("N_COMPACT_ONLY", compact),
            ("CONDITIONAL_TOP5_ONLY", conditional),
            ("N_COMPACT_UNION_CONDITIONAL_TOP5", compact | conditional),
        ):
            rows.append({
                "row_type": "COHORT_SUMMARY",
                "time_slice_type": kind,
                "time_slice": label,
                "cohort": cohort,
                **metric_summary(arrays, period & selection),
            })
    return rows
