from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import math

import numpy as np

from extension_entry_study_v01.pipeline import OUTCOME_FIELDS

from .config import CFG, PERIODS, TOP_K, Config
from .preprocessing import spearman


OUTCOME_INDEX = {name: index for index, name in enumerate(OUTCOME_FIELDS)}


def date_mask(meta: np.ndarray, start: str, end: str) -> np.ndarray:
    return (meta["signal_date"] >= int(start)) & (meta["signal_date"] <= int(end))


def time_slices() -> list[tuple[str, str, str, str]]:
    rows = [("PERIOD", label, start, end) for label, start, end in PERIODS]
    rows.extend(
        ("YEAR", str(year), f"{year}0101", f"{year}1231")
        for year in range(2020, 2026)
    )
    rows.append(("OVERALL", "2020_2025", "20200101", "20251231"))
    return rows


def profit_factor(values: np.ndarray) -> float | None:
    finite = values[np.isfinite(values)]
    positive = float(finite[finite > 0].sum())
    negative = float(-finite[finite < 0].sum())
    return positive / negative if negative > 0 else None


def finite_mean(values: np.ndarray) -> float | None:
    finite = values[np.isfinite(values)]
    return float(np.mean(finite)) if len(finite) else None


def finite_median(values: np.ndarray) -> float | None:
    finite = values[np.isfinite(values)]
    return float(np.median(finite)) if len(finite) else None


def _remove_top_positive(
    net: np.ndarray, fraction: float
) -> tuple[np.ndarray, int]:
    keep = np.ones(len(net), dtype=bool)
    positive = np.flatnonzero(net > 0)
    remove = int(math.ceil(len(positive) * fraction)) if len(positive) else 0
    if remove:
        ranked = positive[np.argsort(-net[positive], kind="stable")]
        keep[ranked[:remove]] = False
    return keep, remove


def metric_summary(
    arrays: dict[str, np.ndarray],
    selected: np.ndarray,
) -> dict:
    meta = arrays["meta"]
    evaluable = selected & meta["outcome_evaluable"]
    indices = np.flatnonzero(evaluable)
    outcome = arrays["outcomes"][indices]
    dates = meta["signal_date"][selected]
    stocks = meta["stock_code"][selected]
    base = {
        "observations": int(np.count_nonzero(selected)),
        "evaluable_observations": len(indices),
        "unique_dates": int(len(np.unique(dates))),
        "unique_stocks": int(len(np.unique(stocks))),
    }
    if not len(indices):
        return {
            **base,
            "winner_rate": None,
            "same_day_universe_winner_rate": None,
            "winner_rate_lift": None,
            "gross_mean": None,
            "net_mean": None,
            "median_net_return": None,
            "gross_profit_factor": None,
            "net_profit_factor": None,
            "mfe5": None,
            "mae5": None,
            "mfe10": None,
            "mae10": None,
            "mfe_abs_mae": None,
        }
    gross = outcome[:, OUTCOME_INDEX["gross_return"]]
    net = outcome[:, OUTCOME_INDEX["net_return"]]
    winner = outcome[:, OUTCOME_INDEX["primary_success"]]
    selected_dates = np.unique(meta["signal_date"][indices])
    universe = meta["outcome_evaluable"] & np.isin(meta["signal_date"], selected_dates)
    universe_winner = arrays["outcomes"][universe, OUTCOME_INDEX["primary_success"]]
    universe_rate = finite_mean(universe_winner)
    winner_rate = float(np.mean(winner))
    mfe10 = outcome[:, OUTCOME_INDEX["mfe_10d"]]
    mae10 = outcome[:, OUTCOME_INDEX["mae_10d"]]
    entry_gap = arrays["entry_gap"][indices]
    signal_close_proxy = (1.0 + entry_gap) * (1.0 + gross) - 1.0
    result = {
        **base,
        "winner_rate": winner_rate,
        "same_day_universe_winner_rate": universe_rate,
        "winner_rate_lift": (
            winner_rate / universe_rate if universe_rate and universe_rate > 0 else None
        ),
        "gross_mean": float(np.mean(gross)),
        "net_mean": float(np.mean(net)),
        "median_net_return": float(np.median(net)),
        "gross_profit_factor": profit_factor(gross),
        "net_profit_factor": profit_factor(net),
        "day1_return": float(np.mean(outcome[:, OUTCOME_INDEX["day1_close_return"]])),
        "day3_return": float(np.mean(outcome[:, OUTCOME_INDEX["day3_close_return"]])),
        "day5_return": float(np.mean(outcome[:, OUTCOME_INDEX["day5_close_return"]])),
        "day10_return": float(np.mean(outcome[:, OUTCOME_INDEX["day10_close_return"]])),
        "mfe5": float(np.mean(outcome[:, OUTCOME_INDEX["mfe_5d"]])),
        "mae5": float(np.mean(outcome[:, OUTCOME_INDEX["mae_5d"]])),
        "mfe10": float(np.mean(mfe10)),
        "mae10": float(np.mean(mae10)),
        "mfe_abs_mae": (
            float(np.mean(mfe10)) / abs(float(np.mean(mae10)))
            if abs(float(np.mean(mae10))) > 1e-15
            else None
        ),
        "entry_gap_mean": finite_mean(entry_gap),
        "signal_close_to_exit_proxy_mean": finite_mean(signal_close_proxy),
        "gap_payoff_erosion": finite_mean(signal_close_proxy) - float(np.mean(gross)),
    }
    for label, fraction in (("top1", 0.01), ("top5", 0.05)):
        keep, removed = _remove_top_positive(net, fraction)
        result[f"{label}_positive_removed_count"] = removed
        result[f"{label}_removed_gross_mean"] = finite_mean(gross[keep])
        result[f"{label}_removed_net_mean"] = finite_mean(net[keep])
        result[f"{label}_removed_gross_pf"] = profit_factor(gross[keep])
        result[f"{label}_removed_net_pf"] = profit_factor(net[keep])
    result["net_tail_dependent"] = bool(
        result["net_mean"] > 0
        and (
            result["top1_removed_net_mean"] is None
            or result["top1_removed_net_mean"] <= 0
            or result["top1_removed_net_pf"] is None
            or result["top1_removed_net_pf"] <= 1
        )
    )
    return result


def _daily_slice(
    daily_rows: list[dict], model: str, start: str, end: str
) -> list[dict]:
    return [
        row
        for row in daily_rows
        if row["model"] == model and int(start) <= row["signal_date"] <= int(end)
    ]


def daily_quality_summary(rows: list[dict]) -> dict:
    ic = np.asarray(
        [row["daily_spearman_ic_net_return"] for row in rows], dtype=float
    )
    finite_ic = ic[np.isfinite(ic)]
    monthly = defaultdict(list)
    for row in rows:
        value = row["daily_spearman_ic_net_return"]
        if value is not None and math.isfinite(value):
            monthly[row["month"]].append(value)
    monthly_ic = np.asarray(
        [np.mean(values) for values in monthly.values()], dtype=float
    )
    conviction = np.asarray(
        [row["top10_vs_universe_score_spread"] for row in rows], dtype=float
    )
    top_net = np.asarray([row["top10_net_mean"] for row in rows], dtype=float)
    return {
        "mean_daily_ic": finite_mean(finite_ic),
        "median_daily_ic": finite_median(finite_ic),
        "ic_positive_day_rate": (
            float(np.mean(finite_ic > 0)) if len(finite_ic) else None
        ),
        "mean_monthly_ic": finite_mean(monthly_ic),
        "median_monthly_ic": finite_median(monthly_ic),
        "top10_minus_universe_net_spread": finite_mean(
            np.asarray([row["top10_minus_universe_net_spread"] for row in rows], dtype=float)
        ),
        "top10_minus_bottom10_net_spread": finite_mean(
            np.asarray([row["top10_minus_bottom10_net_spread"] for row in rows], dtype=float)
        ),
        "top_decile_minus_bottom_decile_net_spread": finite_mean(
            np.asarray(
                [row["top_decile_minus_bottom_decile_net_spread"] for row in rows],
                dtype=float,
            )
        ),
        "conviction_score_spread_vs_top10_net_spearman": spearman(
            conviction, top_net
        ),
        "score_vs_entry_gap_spearman": finite_mean(
            np.asarray([row["score_vs_entry_gap_spearman"] for row in rows], dtype=float)
        ),
        "top10_entry_gap_vs_net_spearman": finite_mean(
            np.asarray([row["top10_entry_gap_vs_net_spearman"] for row in rows], dtype=float)
        ),
    }


def topk_summary_rows(
    arrays: dict[str, np.ndarray], rankings: dict[str, dict[str, np.ndarray]]
) -> list[dict]:
    rows = []
    for model, ranking in rankings.items():
        for kind, label, start, end in time_slices():
            period = date_mask(arrays["meta"], start, end)
            for k in TOP_K:
                selected = period & (ranking["rank"] <= k)
                rows.append(
                    {
                        "time_slice_type": kind,
                        "time_slice": label,
                        "model": model,
                        "ranking_mode": "RAW_RANKING",
                        "top_k": k,
                        "primary_evaluation": k == CFG.primary_top_k,
                        **metric_summary(arrays, selected),
                    }
                )
    return rows


def primary_summary_rows(
    arrays: dict[str, np.ndarray],
    rankings: dict[str, dict[str, np.ndarray]],
    daily_rows: list[dict],
    slice_type: str,
) -> list[dict]:
    rows = []
    for model, ranking in rankings.items():
        for kind, label, start, end in time_slices():
            if kind != slice_type:
                continue
            selected = date_mask(arrays["meta"], start, end) & (
                ranking["rank"] <= CFG.primary_top_k
            )
            rows.append(
                {
                    "time_slice_type": kind,
                    "time_slice": label,
                    "model": model,
                    "ranking_mode": "RAW_RANKING",
                    "top_k": CFG.primary_top_k,
                    **metric_summary(arrays, selected),
                    **daily_quality_summary(_daily_slice(daily_rows, model, start, end)),
                }
            )
    return rows


def score_decile_rows(
    arrays: dict[str, np.ndarray], rankings: dict[str, dict[str, np.ndarray]]
) -> list[dict]:
    rows = []
    for model, ranking in rankings.items():
        for kind, label, start, end in time_slices():
            period = date_mask(arrays["meta"], start, end)
            for decile in range(1, 11):
                selected = period & (ranking["score_decile"] == decile)
                rows.append(
                    {
                        "time_slice_type": kind,
                        "time_slice": label,
                        "model": model,
                        "score_decile": decile,
                        **metric_summary(arrays, selected),
                    }
                )
    return rows


def _remove_best_cluster(values: np.ndarray, clusters: np.ndarray) -> float | None:
    finite = np.isfinite(values)
    if np.count_nonzero(finite) < 2:
        return None
    values = values[finite]
    clusters = clusters[finite]
    unique = np.unique(clusters)
    cluster_means = {
        cluster: float(np.mean(values[clusters == cluster])) for cluster in unique
    }
    best = max(cluster_means, key=cluster_means.get)
    return finite_mean(values[clusters != best])


def spread_summary_rows(daily_rows: list[dict]) -> list[dict]:
    fields = (
        "top10_minus_universe_net_spread",
        "top10_minus_bottom10_net_spread",
        "top_decile_minus_bottom_decile_net_spread",
    )
    models = sorted({row["model"] for row in daily_rows})
    rows = []
    for model in models:
        for kind, label, start, end in time_slices():
            selected = _daily_slice(daily_rows, model, start, end)
            dates = np.asarray([row["signal_date"] for row in selected])
            months = dates // 100
            for field in fields:
                values = np.asarray([row[field] for row in selected], dtype=float)
                rows.append(
                    {
                        "time_slice_type": kind,
                        "time_slice": label,
                        "model": model,
                        "spread": field,
                        "daily_observations": int(np.count_nonzero(np.isfinite(values))),
                        "mean_daily_spread": finite_mean(values),
                        "median_daily_spread": finite_median(values),
                        "positive_day_rate": (
                            float(np.mean(values[np.isfinite(values)] > 0))
                            if np.any(np.isfinite(values))
                            else None
                        ),
                        "remove_best_signal_date_mean_spread": _remove_best_cluster(
                            values, dates
                        ),
                        "remove_best_calendar_month_mean_spread": _remove_best_cluster(
                            values, months
                        ),
                    }
                )
    return rows


def _stable_seed(*parts: object) -> int:
    digest = hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (2**32)


def _cluster_bootstrap(
    values: np.ndarray,
    clusters: np.ndarray,
    reps: int,
    seed: int,
) -> tuple[float | None, float | None, int]:
    finite = np.isfinite(values)
    values = values[finite]
    clusters = clusters[finite]
    unique = np.unique(clusters)
    if len(unique) < 2:
        return None, None, len(unique)
    sums = np.asarray([np.sum(values[clusters == key]) for key in unique], dtype=float)
    counts = np.asarray([np.count_nonzero(clusters == key) for key in unique], dtype=float)
    rng = np.random.default_rng(seed)
    simulations = np.empty(reps, dtype=float)
    for offset in range(0, reps, 250):
        size = min(250, reps - offset)
        sampled = rng.integers(0, len(unique), size=(size, len(unique)))
        simulations[offset : offset + size] = (
            np.sum(sums[sampled], axis=1) / np.sum(counts[sampled], axis=1)
        )
    low, high = np.quantile(simulations, (0.025, 0.975), method="linear")
    return float(low), float(high), len(unique)


def cluster_bootstrap_rows(
    daily_rows: list[dict], cfg: Config = CFG
) -> list[dict]:
    fields = (
        "top10_minus_universe_net_spread",
        "top10_minus_bottom10_net_spread",
        "top_decile_minus_bottom_decile_net_spread",
    )
    models = sorted({row["model"] for row in daily_rows})
    rows = []
    for model in models:
        for kind, label, start, end in time_slices():
            selected = _daily_slice(daily_rows, model, start, end)
            dates = np.asarray([row["signal_date"] for row in selected], dtype=int)
            for field in fields:
                values = np.asarray([row[field] for row in selected], dtype=float)
                for cluster_unit, clusters in (
                    ("signal_date", dates),
                    ("calendar_month", dates // 100),
                ):
                    seed = _stable_seed(
                        cfg.bootstrap_seed, model, label, field, cluster_unit
                    )
                    low, high, cluster_count = _cluster_bootstrap(
                        values, clusters, cfg.bootstrap_iterations, seed
                    )
                    rows.append(
                        {
                            "time_slice_type": kind,
                            "time_slice": label,
                            "model": model,
                            "spread": field,
                            "cluster_unit": cluster_unit,
                            "bootstrap_reps": cfg.bootstrap_iterations,
                            "seed": seed,
                            "cluster_count": cluster_count,
                            "point_estimate": finite_mean(values),
                            "ci95_low": low,
                            "ci95_high": high,
                            "iid_trade_bootstrap": False,
                        }
                    )
    return rows


def frequency_rows(
    arrays: dict[str, np.ndarray],
    rankings: dict[str, dict[str, np.ndarray]],
    cooldown_masks: dict[str, np.ndarray],
) -> list[dict]:
    meta = arrays["meta"]
    rows = []
    for model, ranking in rankings.items():
        modes = {
            "RAW_RANKING_TOP10": ranking["rank"] <= CFG.primary_top_k,
            "10D_COOLDOWN_TRADE_PROXY": cooldown_masks[model],
        }
        for mode, selected_all in modes.items():
            for kind, label, start, end in time_slices():
                period = date_mask(meta, start, end)
                selected = period & selected_all
                calendar_dates = np.unique(meta["signal_date"][period])
                selected_dates = meta["signal_date"][selected]
                selected_codes = meta["stock_code"][selected]
                daily_counts = Counter(int(value) for value in selected_dates)
                counts = np.asarray(
                    [daily_counts.get(int(day), 0) for day in calendar_dates], dtype=float
                )
                months = np.unique(calendar_dates // 100)
                monthly_opportunities = []
                monthly_active_dates = []
                monthly_unique_stocks = []
                for month in months:
                    local = selected & (meta["signal_date"] // 100 == month)
                    dates = np.unique(meta["signal_date"][local])
                    monthly_opportunities.append(int(np.count_nonzero(local)))
                    monthly_active_dates.append(len(dates))
                    monthly_unique_stocks.append(len(np.unique(meta["stock_code"][local])))
                rows.append(
                    {
                        "time_slice_type": kind,
                        "time_slice": label,
                        "model": model,
                        "ranking_mode": mode,
                        "calendar_dates": len(calendar_dates),
                        "opportunities": int(np.count_nonzero(selected)),
                        "candidates_per_day": finite_mean(counts),
                        "opportunities_per_month": finite_mean(
                            np.asarray(monthly_opportunities, dtype=float)
                        ),
                        "unique_stocks_per_month": finite_mean(
                            np.asarray(monthly_unique_stocks, dtype=float)
                        ),
                        "active_dates_per_month": finite_mean(
                            np.asarray(monthly_active_dates, dtype=float)
                        ),
                        "median_opportunities_per_day": finite_median(counts),
                        "p90_opportunities_per_day": (
                            float(np.quantile(counts, 0.90, method="linear"))
                            if len(counts)
                            else None
                        ),
                        "maximum_opportunities_day": int(np.max(counts)) if len(counts) else 0,
                        "maximum_opportunity_date": (
                            int(min(day for day in calendar_dates if daily_counts.get(int(day), 0) == np.max(counts)))
                            if len(counts) and np.max(counts) > 0
                            else None
                        ),
                        "active_dates": int(np.count_nonzero(counts > 0)),
                    }
                )
    return rows


def cooldown_summary_rows(
    arrays: dict[str, np.ndarray], cooldown_masks: dict[str, np.ndarray]
) -> list[dict]:
    rows = []
    for model, cooldown in cooldown_masks.items():
        for kind, label, start, end in time_slices():
            selected = cooldown & date_mask(arrays["meta"], start, end)
            rows.append(
                {
                    "time_slice_type": kind,
                    "time_slice": label,
                    "model": model,
                    "ranking_mode": "10D_COOLDOWN_TRADE_PROXY",
                    **metric_summary(arrays, selected),
                }
            )
    return rows


def n_compact_overlay_rows(
    arrays: dict[str, np.ndarray], rankings: dict[str, dict[str, np.ndarray]]
) -> list[dict]:
    meta = arrays["meta"]
    compact_indices = np.flatnonzero(arrays["n_compact"])
    rows = []
    for model, ranking in rankings.items():
        for signal_date in np.unique(meta["signal_date"]):
            local = arrays["n_compact"] & (meta["signal_date"] == signal_date)
            rows.append(
                {
                    "row_type": "DAILY_STATUS",
                    "model": model,
                    "signal_date": int(signal_date),
                    "period": next(
                        label for label, start, end in PERIODS
                        if int(start) <= signal_date <= int(end)
                    ),
                    "n_compact_appeared": bool(np.any(local)),
                    "n_compact_count": int(np.count_nonzero(local)),
                    "stock_code": None,
                    "score": None,
                    "rank": None,
                    "rank_percentile": None,
                    "in_top5": None,
                    "in_top10": None,
                    "in_top20": None,
                    "outcome_evaluable": None,
                    "winner": None,
                    "net_return": None,
                }
            )
        for index in compact_indices:
            rows.append(
                {
                    "row_type": "SIGNAL_DETAIL",
                    "model": model,
                    "signal_date": int(meta["signal_date"][index]),
                    "period": next(
                        label
                        for label, start, end in PERIODS
                        if int(start) <= meta["signal_date"][index] <= int(end)
                    ),
                    "stock_code": int(meta["stock_code"][index]),
                    "n_compact_appeared": True,
                    "n_compact_count": 1,
                    "score": float(ranking["scores"][index]),
                    "rank": int(ranking["rank"][index]),
                    "rank_percentile": float(ranking["rank_percentile"][index]),
                    "in_top5": bool(ranking["rank"][index] <= 5),
                    "in_top10": bool(ranking["rank"][index] <= 10),
                    "in_top20": bool(ranking["rank"][index] <= 20),
                    "outcome_evaluable": bool(meta["outcome_evaluable"][index]),
                    "winner": (
                        bool(arrays["outcomes"][index, OUTCOME_INDEX["primary_success"]])
                        if meta["outcome_evaluable"][index]
                        else None
                    ),
                    "net_return": (
                        float(arrays["outcomes"][index, OUTCOME_INDEX["net_return"]])
                        if meta["outcome_evaluable"][index]
                        else None
                    ),
                }
            )
    return rows


def n_compact_cohort_rows(
    arrays: dict[str, np.ndarray], rankings: dict[str, dict[str, np.ndarray]]
) -> list[dict]:
    rows = []
    for model, ranking in rankings.items():
        rank_top10 = ranking["rank"] <= CFG.primary_top_k
        cohorts = {
            "N_COMPACT_ONLY": arrays["n_compact"],
            "RANK_TOP10_ONLY": rank_top10,
            "N_COMPACT_UNION_RANK_TOP10": arrays["n_compact"] | rank_top10,
        }
        for kind, label, start, end in time_slices():
            period = date_mask(arrays["meta"], start, end)
            for cohort, selected in cohorts.items():
                rows.append(
                    {
                        "time_slice_type": kind,
                        "time_slice": label,
                        "model": model,
                        "cohort": cohort,
                        **metric_summary(arrays, period & selected),
                    }
                )
    return rows


def regime_rows(
    arrays: dict[str, np.ndarray],
    rankings: dict[str, dict[str, np.ndarray]],
    regimes: dict[str, np.ndarray],
) -> list[dict]:
    regime_groups = {
        "0050_CLOSE_VS_MA20": {
            "ABOVE_OR_EQUAL_MA20": regimes["close_vs_ma20"] >= 0,
            "BELOW_MA20": regimes["close_vs_ma20"] < 0,
        },
        "0050_MA20_SLOPE5": {
            "RISING_OR_FLAT": regimes["ma20_slope5"] >= 0,
            "FALLING": regimes["ma20_slope5"] < 0,
        },
        "0050_VOLATILITY20": {
            "HIGH": regimes["volatility20"] >= regimes["volatility_discovery_median"],
            "LOW": regimes["volatility20"] < regimes["volatility_discovery_median"],
        },
    }
    meta = arrays["meta"]
    rows = []
    for model, ranking in rankings.items():
        top10 = ranking["rank"] <= CFG.primary_top_k
        for label, start, end in PERIODS:
            period = date_mask(meta, start, end)
            for dimension, groups in regime_groups.items():
                for group, regime_mask in groups.items():
                    selected = period & regime_mask & top10
                    dates = np.unique(meta["signal_date"][period & regime_mask])
                    universe = meta["outcome_evaluable"] & np.isin(meta["signal_date"], dates)
                    summary = metric_summary(arrays, selected)
                    summary["top10_minus_regime_universe_net_spread"] = (
                        summary["net_mean"]
                        - finite_mean(
                            arrays["outcomes"][universe, OUTCOME_INDEX["net_return"]]
                        )
                        if summary["net_mean"] is not None and np.any(universe)
                        else None
                    )
                    rows.append(
                        {
                            "time_slice": label,
                            "model": model,
                            "market_proxy": "0050",
                            "regime_dimension": dimension,
                            "regime_group": group,
                            **summary,
                        }
                    )
    return rows


def entry_gap_rows(
    arrays: dict[str, np.ndarray], rankings: dict[str, dict[str, np.ndarray]]
) -> tuple[list[dict], dict]:
    discovery = date_mask(
        arrays["meta"], CFG.discovery_start, CFG.discovery_end
    ) & arrays["meta"]["outcome_evaluable"]
    values = arrays["entry_gap"][discovery]
    values = values[np.isfinite(values)]
    boundaries = np.quantile(values, (0.2, 0.4, 0.6, 0.8), method="linear")
    bucket = np.zeros(len(arrays["meta"]), dtype=np.uint8)
    finite = np.isfinite(arrays["entry_gap"])
    bucket[finite] = np.searchsorted(
        boundaries, arrays["entry_gap"][finite], side="left"
    ) + 1
    rows = []
    for model, ranking in rankings.items():
        top10 = ranking["rank"] <= CFG.primary_top_k
        for label, start, end in PERIODS:
            period = date_mask(arrays["meta"], start, end)
            for quintile in range(1, 6):
                selected = period & top10 & (bucket == quintile)
                rows.append(
                    {
                        "time_slice": label,
                        "model": model,
                        "entry_gap_quintile": quintile,
                        "boundaries_fit_period": "HISTORICAL_DISCOVERY_2020_2022_ONLY",
                        "entry_gap_lower": (
                            None if quintile == 1 else float(boundaries[quintile - 2])
                        ),
                        "entry_gap_upper": (
                            None if quintile == 5 else float(boundaries[quintile - 1])
                        ),
                        **metric_summary(arrays, selected),
                    }
                )
    return rows, {
        "fit_period": "HISTORICAL_DISCOVERY_2020_2022_ONLY",
        "boundaries": [float(value) for value in boundaries],
        "threshold_selection": "NONE_DESCRIPTIVE_QUINTILES_ONLY",
    }


def conviction_rows(daily_rows: list[dict]) -> tuple[list[dict], dict]:
    output = []
    boundaries_by_model = {}
    for model in sorted({row["model"] for row in daily_rows}):
        discovery = [
            row for row in daily_rows
            if row["model"] == model and row["period"] == "HISTORICAL_DISCOVERY"
        ]
        fit_values = np.asarray(
            [row["top10_vs_universe_score_spread"] for row in discovery], dtype=float
        )
        boundaries = np.quantile(fit_values, (0.2, 0.4, 0.6, 0.8), method="linear")
        boundaries_by_model[model] = [float(value) for value in boundaries]
        for label, start, end in PERIODS:
            selected = _daily_slice(daily_rows, model, start, end)
            values = np.asarray(
                [row["top10_vs_universe_score_spread"] for row in selected], dtype=float
            )
            buckets = np.searchsorted(boundaries, values, side="left") + 1
            for quintile in range(1, 6):
                local = [row for row, bucket in zip(selected, buckets) if bucket == quintile]
                nets = np.asarray([row["top10_net_mean"] for row in local], dtype=float)
                spreads = np.asarray(
                    [row["top10_minus_universe_net_spread"] for row in local], dtype=float
                )
                ics = np.asarray(
                    [row["daily_spearman_ic_net_return"] for row in local], dtype=float
                )
                output.append(
                    {
                        "time_slice": label,
                        "model": model,
                        "conviction_quintile": quintile,
                        "boundaries_fit_period": "HISTORICAL_DISCOVERY_2020_2022_ONLY",
                        "lower": None if quintile == 1 else float(boundaries[quintile - 2]),
                        "upper": None if quintile == 5 else float(boundaries[quintile - 1]),
                        "signal_dates": len(local),
                        "top10_net_mean_across_dates": finite_mean(nets),
                        "top10_universe_spread_mean": finite_mean(spreads),
                        "mean_daily_ic": finite_mean(ics),
                        "positive_spread_day_rate": (
                            float(np.mean(spreads[np.isfinite(spreads)] > 0))
                            if np.any(np.isfinite(spreads)) else None
                        ),
                    }
                )
    return output, {
        "variable": "TOP10_VS_UNIVERSE_SCORE_SPREAD",
        "fit_period": "HISTORICAL_DISCOVERY_2020_2022_ONLY",
        "boundaries_by_model": boundaries_by_model,
        "threshold_selection": "NONE_DESCRIPTIVE_QUINTILES_ONLY",
    }


def coefficient_rows(models: list, comparisons: list[dict]) -> list[dict]:
    comparison_by_period = {
        item["left_fit_period"]: item for item in comparisons
    }
    rows = []
    from .config import FEATURE_NAMES

    for model in models:
        comparison = comparison_by_period.get(model.fit_period, {})
        for feature, coefficient in zip(FEATURE_NAMES, model.coefficients):
            rows.append(
                {
                    "model": model.name,
                    "fit_period": model.fit_period,
                    "feature": feature,
                    "coefficient_or_weight": coefficient,
                    "direction": (
                        "POSITIVE" if coefficient > 0 else "NEGATIVE" if coefficient < 0 else "ZERO"
                    ),
                    "intercept": model.intercept,
                    "regularization_alpha": model.regularization_alpha,
                    "training_observations": model.training_observations,
                    "comparison_to_final_sign_agreement": comparison.get("sign_agreement"),
                    "comparison_to_final_cosine_similarity": comparison.get("cosine_similarity"),
                }
            )
    return rows


def _find(rows: list[dict], *, model: str, time_slice: str) -> dict:
    matches = [
        row
        for row in rows
        if row.get("model") == model and row.get("time_slice") == time_slice
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one row for {model}/{time_slice}, got {len(matches)}")
    return matches[0]


def build_validation_summary(
    period_rows: list[dict],
    spread_rows: list[dict],
    bootstrap_rows: list[dict],
    cooldown_rows: list[dict],
    frequency: list[dict],
    overlays: list[dict],
    n_compact_cohorts: list[dict],
    regimes: list[dict],
    model_comparisons: list[dict],
    selected_alpha: float,
) -> dict:
    primary = CFG.primary_model
    later_labels = (
        "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS",
        "STRESS_PREVALENCE_SEEN_NOT_BLIND",
    )
    later = {label: _find(period_rows, model=primary, time_slice=label) for label in later_labels}
    spread_lookup = {
        (row["model"], row["time_slice"], row["spread"]): row for row in spread_rows
    }
    primary_spread = "top10_minus_universe_net_spread"
    bottom_spread = "top10_minus_bottom10_net_spread"
    gate_checks: dict[str, bool] = {"later_period_refit_count_equals_zero": True}
    for label, row in later.items():
        short = "2023_2024" if label.startswith("RETROSPECTIVE") else "2025"
        gate_checks[f"{short}_net_mean_positive"] = bool(row["net_mean"] is not None and row["net_mean"] > 0)
        gate_checks[f"{short}_net_pf_above_one"] = bool(row["net_profit_factor"] is not None and row["net_profit_factor"] > 1)
        gate_checks[f"{short}_winner_lift_above_one"] = bool(row["winner_rate_lift"] is not None and row["winner_rate_lift"] > 1)
        gate_checks[f"{short}_top10_universe_spread_positive"] = bool(row[primary_spread] is not None and row[primary_spread] > 0)
        gate_checks[f"{short}_top10_bottom10_spread_positive"] = bool(row[bottom_spread] is not None and row[bottom_spread] > 0)
        gate_checks[f"{short}_top1_removed_edge_remains"] = bool(
            row.get("top1_removed_net_mean") is not None
            and row["top1_removed_net_mean"] > 0
            and row.get("top1_removed_net_pf") is not None
            and row["top1_removed_net_pf"] > 1
        )
        spread = spread_lookup[(primary, label, primary_spread)]
        gate_checks[f"{short}_not_single_date_supported"] = bool(
            spread["remove_best_signal_date_mean_spread"] is not None
            and spread["remove_best_signal_date_mean_spread"] > 0
        )
        gate_checks[f"{short}_not_single_month_supported"] = bool(
            spread["remove_best_calendar_month_mean_spread"] is not None
            and spread["remove_best_calendar_month_mean_spread"] > 0
        )

    if all(gate_checks.values()):
        classification = "PROMISING_FOR_PROSPECTIVE_SHADOW"
    elif any(
        row["net_mean"] is not None and row["net_mean"] > 0
        and row[primary_spread] is not None and row[primary_spread] > 0
        for row in later.values()
    ):
        classification = "DESCRIPTIVE_ONLY"
    else:
        classification = "NO_CROSS_SECTIONAL_EDGE"

    overall = _find(period_rows, model=primary, time_slice="HISTORICAL_DISCOVERY")
    discovery_base = overall["same_day_universe_winner_rate"]
    ridge_all = [row for row in period_rows if row["model"] == primary]
    benchmark_all = [row for row in period_rows if row["model"] == CFG.benchmark_model]
    model_scores = {}
    for name, rows in ((primary, ridge_all), (CFG.benchmark_model, benchmark_all)):
        targets = [row for row in rows if row["time_slice"] in later_labels]
        model_scores[name] = {
            "positive_ic_periods": sum(bool(row["mean_daily_ic"] is not None and row["mean_daily_ic"] > 0) for row in targets),
            "positive_spread_periods": sum(bool(row[primary_spread] is not None and row[primary_spread] > 0) for row in targets),
            "mean_later_ic": finite_mean(np.asarray([row["mean_daily_ic"] for row in targets], dtype=float)),
            "mean_later_spread": finite_mean(np.asarray([row[primary_spread] for row in targets], dtype=float)),
        }
    model_stability = {
        "conclusion": "NEITHER_STABLE_ACROSS_BOTH_LATER_PERIODS",
        "ridge_has_larger_mean_later_payoff_spread": (
            (model_scores[primary]["mean_later_spread"] or -999)
            > (model_scores[CFG.benchmark_model]["mean_later_spread"] or -999)
        ),
        "ic_weighted_has_larger_mean_later_ic": (
            (model_scores[CFG.benchmark_model]["mean_later_ic"] or -999)
            > (model_scores[primary]["mean_later_ic"] or -999)
        ),
        "details": model_scores,
    }
    compact = [
        row for row in overlays
        if row["model"] == primary and row.get("row_type") == "SIGNAL_DETAIL"
    ]
    compact_ranks = np.asarray([row["rank"] for row in compact], dtype=float)
    compact_pct = np.asarray([row["rank_percentile"] for row in compact], dtype=float)
    overall_frequency = next(
        row for row in frequency
        if row["model"] == primary
        and row["time_slice_type"] == "OVERALL"
        and row["ranking_mode"] == "10D_COOLDOWN_TRADE_PROXY"
    )
    overall_cooldown = next(
        row for row in cooldown_rows
        if row["model"] == primary and row["time_slice_type"] == "OVERALL"
    )
    overall_cohorts = {
        row["cohort"]: row
        for row in n_compact_cohorts
        if row["model"] == primary and row["time_slice_type"] == "OVERALL"
    }
    compact_count = overall_cohorts["N_COMPACT_ONLY"]["observations"]
    raw_top10_count = overall_cohorts["RANK_TOP10_ONLY"]["observations"]
    union_count = overall_cohorts["N_COMPACT_UNION_RANK_TOP10"]["observations"]
    ridge_regimes = [row for row in regimes if row["model"] == primary]
    regime_spreads = [row["top10_minus_regime_universe_net_spread"] for row in ridge_regimes if row["top10_minus_regime_universe_net_spread"] is not None]
    gap_damage = {label: later[label]["gap_payoff_erosion"] for label in later_labels}
    coefficient_stability = {
        item["left_fit_period"]: {
            "sign_agreement": item["sign_agreement"],
            "cosine_similarity": item["cosine_similarity"],
        }
        for item in model_comparisons
    }
    bootstrap_primary = [
        row for row in bootstrap_rows
        if row["model"] == primary
        and row["time_slice"] in later_labels
        and row["spread"] == primary_spread
    ]

    failure_diagnostics = {
        "ic_near_zero": all(abs(later[label]["mean_daily_ic"] or 0) < 0.01 for label in later_labels),
        "winner_prediction_without_payoff": any(
            later[label]["winner_rate_lift"] is not None
            and later[label]["winner_rate_lift"] > 1
            and (later[label]["net_mean"] is None or later[label]["net_mean"] <= 0)
            for label in later_labels
        ),
        "top_ranked_mfe_and_mae_both_high": any(
            later[label]["mfe10"] is not None
            and later[label]["mae10"] is not None
            and later[label]["mfe10"] > 0.08
            and later[label]["mae10"] < -0.05
            for label in later_labels
        ),
        "entry_gap_erodes_payoff": any(value is not None and value > 0 for value in gap_damage.values()),
        "gross_positive_costs_negative": any(
            later[label]["gross_mean"] is not None and later[label]["gross_mean"] > 0
            and (later[label]["net_mean"] is None or later[label]["net_mean"] <= 0)
            for label in later_labels
        ),
        "regime_sensitive": bool(regime_spreads and min(regime_spreads) < 0 < max(regime_spreads)),
        "tail_dependent": any(bool(later[label].get("net_tail_dependent")) for label in later_labels),
        "single_date_or_month_sensitive": any(
            (later[label][primary_spread] or 0) > 0
            and (
                (spread_lookup[(primary, label, primary_spread)]["remove_best_signal_date_mean_spread"] or 0) <= 0
                or (spread_lookup[(primary, label, primary_spread)]["remove_best_calendar_month_mean_spread"] or 0) <= 0
            )
            for label in later_labels
        ),
        "coefficient_direction_unstable": any(
            (item["sign_agreement"] or 0) < 0.6 or (item["cosine_similarity"] or 0) < 0.6
            for item in model_comparisons
            if item["left_model"] == primary
        ),
    }

    answers = {
        "1_score_has_stable_positive_ic": all((later[label]["mean_daily_ic"] or 0) > 0 for label in later_labels),
        "2_top10_winner_rate_lift": {
            label: {
                "top10_rate": later[label]["winner_rate"],
                "same_day_universe_rate": later[label]["same_day_universe_winner_rate"],
                "lift_multiple": later[label]["winner_rate_lift"],
                "lift_percentage_points": (
                    100 * (later[label]["winner_rate"] - later[label]["same_day_universe_winner_rate"])
                    if later[label]["winner_rate"] is not None and later[label]["same_day_universe_winner_rate"] is not None else None
                ),
            } for label in later_labels
        },
        "3_top10_gross_net_mean": {label: {"gross_mean": later[label]["gross_mean"], "net_mean": later[label]["net_mean"]} for label in later_labels},
        "4_top10_gross_net_pf": {label: {"gross_pf": later[label]["gross_profit_factor"], "net_pf": later[label]["net_profit_factor"]} for label in later_labels},
        "5_period_direction_consistency": all((row["net_mean"] or 0) > 0 and (row["mean_daily_ic"] or 0) > 0 for row in [overall, *later.values()]),
        "6_positive_excess_vs_universe": {label: later[label][primary_spread] for label in later_labels},
        "7_positive_spread_vs_bottom10": {label: later[label][bottom_spread] for label in later_labels},
        "8_tail_removal": {label: {"top1_removed_net_mean": later[label].get("top1_removed_net_mean"), "top1_removed_net_pf": later[label].get("top1_removed_net_pf"), "top5_removed_net_mean": later[label].get("top5_removed_net_mean"), "top5_removed_net_pf": later[label].get("top5_removed_net_pf")} for label in later_labels},
        "9_mfe_mae_effect": {label: {"mfe10": later[label]["mfe10"], "mae10": later[label]["mae10"], "ratio": later[label]["mfe_abs_mae"]} for label in later_labels},
        "10_cooldown_monthly_opportunities": overall_frequency["opportunities_per_month"],
        "11_ordering_information": {label: {"mean_daily_ic": later[label]["mean_daily_ic"], "top10_universe_spread": later[label][primary_spread]} for label in later_labels},
        "12_n_compact_rank": {"signals": len(compact), "median_rank": finite_median(compact_ranks), "median_rank_percentile": finite_median(compact_pct), "top10_share": float(np.mean(compact_ranks <= 10)) if len(compact_ranks) else None},
        "13_candidates_outside_n_compact": {
            "raw_top10_observations": raw_top10_count,
            "n_compact_observations": compact_count,
            "union_observations": union_count,
            "raw_top10_outside_n_compact": union_count - compact_count,
            "raw_top10_overlap_with_n_compact": raw_top10_count + compact_count - union_count,
            "definition": "Observation-level candidate counts; not portfolio trades",
        },
        "14_year_month_regime_concentration": {"regime_sensitive": failure_diagnostics["regime_sensitive"], "single_date_or_month_sensitive": failure_diagnostics["single_date_or_month_sensitive"]},
        "15_t_plus_1_gap_effect": gap_damage,
        "16_ridge_coefficient_stability": coefficient_stability,
        "17_more_stable_model": model_stability,
        "18_build_prospective_rank_shadow": classification == "PROMISING_FOR_PROSPECTIVE_SHADOW",
    }
    # Answer 13 is reconciled from the overall mother masks, not a portfolio assumption.
    answers["13_candidates_outside_n_compact"]["top10_proxy_opportunities"] = overall_cooldown["observations"]
    return {
        "study_id": CFG.study_id,
        "status": "COMPLETE",
        "final_classification": classification,
        "primary_model": primary,
        "selected_alpha": selected_alpha,
        "later_period_refit_count": 0,
        "primary_top_k": CFG.primary_top_k,
        "discovery_universe_winner_base_rate": discovery_base,
        "promotion_gate": gate_checks,
        "bootstrap_later_primary_spread": bootstrap_primary,
        "model_later_period_comparison": model_scores,
        "failure_diagnostics": failure_diagnostics,
        "answers": answers,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
