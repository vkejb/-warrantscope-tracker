from __future__ import annotations

from collections import defaultdict
import math

import numpy as np

from conditional_path_quality_ranking_v01.analysis import (
    binary_auc,
    date_mask,
    finite_mean,
    metric_summary,
    time_slices,
)
from conditional_path_quality_ranking_v01.ranking import conditional_ranks, cooldown_proxy
from cross_sectional_alpha_ranking_v01.preprocessing import iter_date_slices, spearman
from extension_entry_study_v01.pipeline import OUTCOME_FIELDS


OI = {name: index for index, name in enumerate(OUTCOME_FIELDS)}


def complete_pool_dates(meta: np.ndarray, valid: np.ndarray, pool: np.ndarray, expected: int = 30) -> np.ndarray:
    keep = np.zeros(len(meta), dtype=bool)
    for _date, region in iter_date_slices(meta["signal_date"]):
        if np.count_nonzero(pool[region]) == expected and np.count_nonzero(valid[region] & pool[region]) == expected:
            keep[region] = True
    return keep & pool & valid


def rank_scores(scores: np.ndarray, pool: np.ndarray, meta: np.ndarray) -> dict[str, np.ndarray]:
    return conditional_ranks(scores, pool, meta["signal_date"], meta["stock_code"])[0]


def daily_ic(scores: np.ndarray, target: np.ndarray, pool: np.ndarray, meta: np.ndarray, start: str, end: str) -> dict:
    values = []
    for day, region in iter_date_slices(meta["signal_date"]):
        if not (int(start) <= day <= int(end)):
            continue
        local = pool[region] & meta["outcome_evaluable"][region]
        value = spearman(scores[region][local], target[region][local]) if np.count_nonzero(local) >= 3 else None
        if value is not None:
            values.append(value)
    return {
        "mean_daily_path_ic": float(np.mean(values)) if values else None,
        "median_daily_path_ic": float(np.median(values)) if values else None,
        "positive_ic_day_rate": float(np.mean(np.asarray(values) > 0)) if values else None,
        "ic_days": len(values),
    }


def comparison_rows(arrays: dict[str, np.ndarray], pools: dict[str, np.ndarray], scores: dict[str, np.ndarray]) -> list[dict]:
    rows = []
    for kind, label, start, end in time_slices():
        period = date_mask(arrays["meta"], start, end)
        for name, mask in pools.items():
            row = {
                "time_slice_type": kind, "time_slice": label, "cohort": name,
                "primary_selection": name == "CHIP_TOP5",
                **metric_summary(arrays, period & mask),
            }
            if name in scores:
                local = period & pools["COMMON_STAGE_A_TOP30"] & arrays["meta"]["outcome_evaluable"]
                row.update({
                    "auc_on_common_stage_a_pool": binary_auc(arrays["path_success"][local], scores[name][local]),
                    **daily_ic(scores[name], arrays["path_success"], pools["COMMON_STAGE_A_TOP30"], arrays["meta"], start, end),
                })
            rows.append(row)
    return rows


def incremental_rows(model_rows: list[dict]) -> list[dict]:
    groups = defaultdict(dict)
    for row in model_rows:
        groups[(row["time_slice_type"], row["time_slice"])][row["cohort"]] = row
    output = []
    for key, items in groups.items():
        if "CHIP_TOP5" not in items or "OHLCV_TOP5_COMMON_DATES" not in items:
            continue
        chip, base, stage = items["CHIP_TOP5"], items["OHLCV_TOP5_COMMON_DATES"], items["COMMON_STAGE_A_TOP30"]
        output.append({
            "time_slice_type": key[0], "time_slice": key[1],
            "delta_success_rate": chip["path_success_rate"] - base["path_success_rate"],
            "delta_downside_first_rate": chip["downside_first_rate"] - base["downside_first_rate"],
            "delta_mae10": chip["mae10_mean"] - base["mae10_mean"],
            "delta_mfe10": chip["mfe10_mean"] - base["mfe10_mean"],
            "delta_net_mean": chip["net_mean"] - base["net_mean"],
            "delta_net_pf": chip["net_profit_factor"] - base["net_profit_factor"],
            "ohlcv_auc": base.get("auc_on_common_stage_a_pool"),
            "chip_auc": chip.get("auc_on_common_stage_a_pool"),
            "delta_auc": None if base.get("auc_on_common_stage_a_pool") is None else chip.get("auc_on_common_stage_a_pool") - base.get("auc_on_common_stage_a_pool"),
            "ohlcv_mean_daily_ic": base.get("mean_daily_path_ic"),
            "chip_mean_daily_ic": chip.get("mean_daily_path_ic"),
            "delta_daily_ic": None if base.get("mean_daily_path_ic") is None else chip.get("mean_daily_path_ic") - base.get("mean_daily_path_ic"),
            "mfe_retention_vs_stage_a": chip["mfe10_mean"] / stage["mfe10_mean"] if stage["mfe10_mean"] else None,
            "mae_improvement_vs_stage_a": chip["mae10_mean"] - stage["mae10_mean"],
        })
    return output


def _bh_adjust(pvalues: list[float]) -> list[float]:
    count = len(pvalues)
    order = np.argsort(pvalues)
    adjusted = np.ones(count)
    running = 1.0
    for reverse_rank in range(count - 1, -1, -1):
        index = int(order[reverse_rank])
        rank = reverse_rank + 1
        running = min(running, pvalues[index] * count / rank)
        adjusted[index] = running
    return adjusted.tolist()


def discovery_diagnostics(
    raw: np.ndarray,
    transformed: np.ndarray,
    names: tuple[str, ...],
    arrays: dict[str, np.ndarray],
    pool: np.ndarray,
    bootstrap_reps: int = 1000,
) -> list[dict]:
    discovery = date_mask(arrays["meta"], "20200101", "20221231") & pool & arrays["meta"]["outcome_evaluable"]
    outcomes = {
        "path_success": arrays["path_success"],
        "downside_first": (arrays["path_class"] == 2).astype(float),
        "mae10": arrays["outcomes"][:, OI["mae_10d"]],
        "mfe10": arrays["outcomes"][:, OI["mfe_10d"]],
        "net_return": arrays["outcomes"][:, OI["net_return"]],
    }
    rng = np.random.default_rng(20260909)
    unique_dates = np.unique(arrays["meta"]["signal_date"][discovery])
    rows = []
    pvalues = []
    for column, name in enumerate(names):
        x = transformed[:, column]
        for outcome_name, y in outcomes.items():
            daily_values = []
            for day in unique_dates:
                selected = discovery & (arrays["meta"]["signal_date"] == day)
                value = spearman(x[selected], y[selected])
                if value is not None:
                    daily_values.append(value)
            point = float(np.mean(daily_values)) if daily_values else None
            boot = []
            if daily_values:
                daily_array = np.asarray(daily_values)
                for _ in range(bootstrap_reps):
                    boot.append(float(np.mean(rng.choice(daily_array, len(daily_array), replace=True))))
            if point is None or not boot:
                pvalue = 1.0
                low = high = None
            else:
                values = np.asarray(boot)
                pvalue = float(2 * min(np.mean(values <= 0), np.mean(values >= 0)))
                low, high = map(float, np.quantile(values, [0.025, 0.975]))
            finite = discovery & np.isfinite(raw[:, column])
            ranks = transformed[:, column]
            top = finite & (ranks >= 0.3)
            bottom = finite & (ranks <= -0.3)
            top_mean = finite_mean(y[top])
            bottom_mean = finite_mean(y[bottom])
            rows.append({
                "feature": name, "outcome": outcome_name,
                "discovery_observations": int(np.count_nonzero(finite)),
                "same_day_spearman": point,
                "cluster_bootstrap_ci_low": low, "cluster_bootstrap_ci_high": high,
                "raw_p_value": pvalue,
                "top_quintile_mean": top_mean,
                "bottom_quintile_mean": bottom_mean,
                "top_minus_bottom_effect": (
                    None if top_mean is None or bottom_mean is None
                    else top_mean - bottom_mean
                ),
            })
            pvalues.append(pvalue)
    adjusted = _bh_adjust(pvalues)
    for row, value in zip(rows, adjusted):
        row["bh_fdr_q_value"] = value
    return rows


def ablation_rows(arrays: dict[str, np.ndarray], rankings: dict[str, np.ndarray], common_pool: np.ndarray) -> list[dict]:
    rows = []
    for kind, label, start, end in time_slices():
        period = date_mask(arrays["meta"], start, end)
        for family, rank in rankings.items():
            top5 = common_pool & (rank > 0) & (rank <= 5)
            rows.append({"time_slice_type": kind, "time_slice": label, "chip_family_model": family, **metric_summary(arrays, period & top5)})
    return rows


def lag_rows(arrays: dict[str, np.ndarray], normal_rank: np.ndarray, extra_rank: np.ndarray, common_normal: np.ndarray, common_extra: np.ndarray) -> list[dict]:
    rows = []
    for kind, label, start, end in time_slices():
        period = date_mask(arrays["meta"], start, end)
        for lag, rank, pool in ((1, normal_rank, common_normal), (2, extra_rank, common_extra)):
            rows.append({"time_slice_type": kind, "time_slice": label, "lag_sessions": lag, **metric_summary(arrays, period & pool & (rank > 0) & (rank <= 5))})
    return rows


def cluster_bootstrap_rows(
    arrays: dict[str, np.ndarray],
    chip_top5: np.ndarray,
    ohlcv_top5: np.ndarray,
    reps: int = 5000,
) -> list[dict]:
    """Cluster-bootstrap primary Top5-minus-frozen-OHLCV Top5 effects."""
    meta = arrays["meta"]
    outcome = arrays["outcomes"]
    metrics = {
        "success_rate_delta": (arrays["path_success"].astype(float), "MEAN"),
        "downside_first_rate_delta": ((arrays["path_class"] == 2).astype(float), "MEAN"),
        "mfe10_delta": (outcome[:, OI["mfe_10d"]], "MEAN"),
        "mae10_delta": (outcome[:, OI["mae_10d"]], "MEAN"),
        "gross_mean_delta": (outcome[:, OI["gross_return"]], "MEAN"),
        "net_mean_delta": (outcome[:, OI["net_return"]], "MEAN"),
        "net_pf_delta": (outcome[:, OI["net_return"]], "PROFIT_FACTOR"),
    }
    periods = (
        ("2020_2022_HISTORICAL_DISCOVERY", "20200101", "20221231"),
        ("2023_2024_RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", "20230101", "20241231"),
        ("2025_STRESS_PREVALENCE_SEEN_NOT_BLIND", "20250101", "20251231"),
    )
    rng = np.random.default_rng(20260911)
    rows = []
    for period_label, start, end in periods:
        period = date_mask(meta, start, end) & meta["outcome_evaluable"]
        for cluster_type, cluster_values in (
            ("SIGNAL_DATE", meta["signal_date"]),
            ("CALENDAR_MONTH", meta["signal_date"] // 100),
        ):
            union = period & (chip_top5 | ohlcv_top5)
            clusters = np.unique(cluster_values[union])
            if len(clusters) < 2:
                continue
            for metric_name, (values, kind) in metrics.items():
                aggregates = []
                for cluster in clusters:
                    local = period & (cluster_values == cluster)
                    chip_values = values[local & chip_top5]
                    base_values = values[local & ohlcv_top5]
                    if kind == "MEAN":
                        aggregates.append((
                            float(np.nansum(chip_values)), int(np.count_nonzero(np.isfinite(chip_values))),
                            float(np.nansum(base_values)), int(np.count_nonzero(np.isfinite(base_values))),
                        ))
                    else:
                        aggregates.append((
                            float(np.nansum(chip_values[chip_values > 0])),
                            float(-np.nansum(chip_values[chip_values < 0])),
                            float(np.nansum(base_values[base_values > 0])),
                            float(-np.nansum(base_values[base_values < 0])),
                        ))
                aggregate = np.asarray(aggregates, dtype=np.float64)
                draws = rng.integers(0, len(clusters), size=(reps, len(clusters)))
                sampled = np.sum(aggregate[draws], axis=1)
                if kind == "MEAN":
                    valid = (sampled[:, 1] > 0) & (sampled[:, 3] > 0)
                    boot = sampled[valid, 0] / sampled[valid, 1] - sampled[valid, 2] / sampled[valid, 3]
                    chip_all = values[period & chip_top5]
                    base_all = values[period & ohlcv_top5]
                    point = float(np.nanmean(chip_all) - np.nanmean(base_all))
                else:
                    valid = (sampled[:, 1] > 0) & (sampled[:, 3] > 0)
                    boot = sampled[valid, 0] / sampled[valid, 1] - sampled[valid, 2] / sampled[valid, 3]
                    chip_all = values[period & chip_top5]
                    base_all = values[period & ohlcv_top5]
                    point = float(
                        np.sum(chip_all[chip_all > 0]) / -np.sum(chip_all[chip_all < 0])
                        - np.sum(base_all[base_all > 0]) / -np.sum(base_all[base_all < 0])
                    )
                low, high = map(float, np.quantile(boot, (0.025, 0.975)))
                rows.append({
                    "period": period_label,
                    "cluster_type": cluster_type,
                    "metric": metric_name,
                    "point_estimate": point,
                    "ci_95_low": low,
                    "ci_95_high": high,
                    "ci_crosses_zero": low <= 0 <= high,
                    "bootstrap_reps": reps,
                    "cluster_count": len(clusters),
                })
    return rows


def frequency_rows(arrays: dict[str, np.ndarray], raw_top5: np.ndarray, cooldown: np.ndarray) -> list[dict]:
    rows = []
    for label, start, end in (("2020_2022", "20200101", "20221231"), ("2023_2024", "20230101", "20241231"), ("2025", "20250101", "20251231")):
        period = date_mask(arrays["meta"], start, end)
        months = np.unique(arrays["meta"]["signal_date"][period] // 100)
        rows.append({
            "period": label,
            "raw_candidates_per_month": float(np.count_nonzero(period & raw_top5) / len(months)),
            "cooldown_opportunities_per_month": float(np.count_nonzero(period & cooldown) / len(months)),
            "active_dates": int(len(np.unique(arrays["meta"]["signal_date"][period & cooldown]))),
            "unique_stocks_per_month": float(sum(len(np.unique(arrays["meta"]["stock_code"][period & cooldown & (arrays["meta"]["signal_date"] // 100 == month)])) for month in months) / len(months)),
        })
    return rows


__all__ = [
    "complete_pool_dates", "rank_scores", "comparison_rows", "incremental_rows",
    "discovery_diagnostics", "ablation_rows", "lag_rows", "frequency_rows",
    "cluster_bootstrap_rows", "cooldown_proxy",
]
