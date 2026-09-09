from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import math

import numpy as np

from cross_sectional_alpha_ranking_v01.config import PERIODS
from cross_sectional_alpha_ranking_v01.preprocessing import iter_date_slices, spearman
from extension_entry_study_v01.pipeline import OUTCOME_FIELDS

from .config import CFG, GAP_BUCKETS, STAGE_A_TOP_K, TWO_STAGE_TOP_K, Config


OI = {name: index for index, name in enumerate(OUTCOME_FIELDS)}
QUADRANTS = {
    1: "HIGH_UPSIDE_HIGH_RISK_QUALITY",
    2: "HIGH_UPSIDE_LOW_RISK_QUALITY",
    3: "LOW_UPSIDE_HIGH_RISK_QUALITY",
    4: "LOW_UPSIDE_LOW_RISK_QUALITY",
}


def date_mask(meta: np.ndarray, start: str, end: str) -> np.ndarray:
    return (meta["signal_date"] >= int(start)) & (meta["signal_date"] <= int(end))


def time_slices() -> list[tuple[str, str, str, str]]:
    rows = [("PERIOD", label, start, end) for label, start, end in PERIODS]
    rows.extend(("YEAR", str(year), f"{year}0101", f"{year}1231") for year in range(2020, 2026))
    rows.append(("OVERALL", "2020_2025", "20200101", "20251231"))
    return rows


def finite_mean(values: np.ndarray) -> float | None:
    values = values[np.isfinite(values)]
    return float(np.mean(values)) if len(values) else None


def finite_median(values: np.ndarray) -> float | None:
    values = values[np.isfinite(values)]
    return float(np.median(values)) if len(values) else None


def profit_factor(values: np.ndarray) -> float | None:
    values = values[np.isfinite(values)]
    gain = float(values[values > 0].sum())
    loss = float(-values[values < 0].sum())
    return gain / loss if loss > 0 else None


def _tail_keep(values: np.ndarray, fraction: float, *, positive_only: bool) -> tuple[np.ndarray, int]:
    candidates = np.flatnonzero(values > 0) if positive_only else np.flatnonzero(np.isfinite(values))
    remove = int(math.ceil(len(candidates) * fraction)) if len(candidates) else 0
    keep = np.ones(len(values), dtype=bool)
    if remove:
        ranked = candidates[np.argsort(-values[candidates], kind="stable")]
        keep[ranked[:remove]] = False
    return keep, remove


def metric_summary(arrays: dict[str, np.ndarray], selected: np.ndarray, reference: np.ndarray | None = None) -> dict:
    meta = arrays["meta"]
    evaluable = selected & meta["outcome_evaluable"]
    indices = np.flatnonzero(evaluable)
    if reference is None:
        dates = np.unique(meta["signal_date"][selected])
        reference = meta["outcome_evaluable"] & np.isin(meta["signal_date"], dates)
    else:
        reference = reference & meta["outcome_evaluable"]
    base = {
        "observations": int(np.count_nonzero(selected)),
        "evaluable_observations": len(indices),
        "unique_dates": int(len(np.unique(meta["signal_date"][selected]))),
        "unique_months": int(len(np.unique(meta["signal_date"][selected] // 100))),
        "unique_stocks": int(len(np.unique(meta["stock_code"][selected]))),
    }
    base["candidates_per_day"] = base["observations"] / base["unique_dates"] if base["unique_dates"] else None
    base["candidates_per_month"] = base["observations"] / base["unique_months"] if base["unique_months"] else None
    if not len(indices):
        return {**base, "mfe10_mean": None, "net_mean": None, "net_profit_factor": None}
    outcome = arrays["outcomes"][indices]
    desc = arrays["descriptive_outcomes"][indices]
    ref_outcome = arrays["outcomes"][reference]
    ref_desc = arrays["descriptive_outcomes"][reference]
    mfe10 = outcome[:, OI["mfe_10d"]]
    mae10 = outcome[:, OI["mae_10d"]]
    gross = outcome[:, OI["gross_return"]]
    net = outcome[:, OI["net_return"]]
    winner = outcome[:, OI["primary_success"]]
    ref_mfe = ref_outcome[:, OI["mfe_10d"]]
    ref_winner = ref_outcome[:, OI["primary_success"]]
    feature_names = [str(value) for value in arrays["feature_names"]]
    volatility20 = arrays["raw_features"][:, feature_names.index("volatility20")]
    atr20 = arrays["raw_features"][:, feature_names.index("atr20")]
    result = {
        **base,
        "winner_rate": finite_mean(winner),
        "plus10_before_minus5_rate": finite_mean(desc[:, 0]),
        "plus15_before_minus5_rate": finite_mean(desc[:, 1]),
        "mfe5_mean": finite_mean(outcome[:, OI["mfe_5d"]]),
        "mfe10_mean": finite_mean(mfe10),
        "mfe10_median": finite_median(mfe10),
        "mae5_mean": finite_mean(outcome[:, OI["mae_5d"]]),
        "mae10_mean": finite_mean(mae10),
        "mfe_abs_mae": (
            finite_mean(mfe10) / abs(finite_mean(mae10))
            if finite_mean(mae10) not in (None, 0.0) else None
        ),
        "day1_return": finite_mean(outcome[:, OI["day1_close_return"]]),
        "day3_return": finite_mean(outcome[:, OI["day3_close_return"]]),
        "day5_return": finite_mean(outcome[:, OI["day5_close_return"]]),
        "day10_return": finite_mean(outcome[:, OI["day10_close_return"]]),
        "gross_mean": finite_mean(gross),
        "net_mean": finite_mean(net),
        "median_net_return": finite_median(net),
        "gross_profit_factor": profit_factor(gross),
        "net_profit_factor": profit_factor(net),
        "universe_winner_rate": finite_mean(ref_winner),
        "universe_plus10_rate": finite_mean(ref_desc[:, 0]),
        "universe_plus15_rate": finite_mean(ref_desc[:, 1]),
        "universe_mfe10_mean": finite_mean(ref_mfe),
        "universe_mae10_mean": finite_mean(ref_outcome[:, OI["mae_10d"]]),
        "signal_volatility20_mean": finite_mean(volatility20[indices]),
        "universe_volatility20_mean": finite_mean(volatility20[reference]),
        "signal_atr20_mean": finite_mean(atr20[indices]),
        "universe_atr20_mean": finite_mean(atr20[reference]),
    }
    result["winner_rate_lift"] = result["winner_rate"] / result["universe_winner_rate"] if result["universe_winner_rate"] else None
    result["mfe10_lift_difference"] = result["mfe10_mean"] - result["universe_mfe10_mean"]
    result["mfe10_lift_multiple"] = result["mfe10_mean"] / result["universe_mfe10_mean"] if result["universe_mfe10_mean"] else None
    result["mae10_improvement_vs_universe"] = result["mae10_mean"] - result["universe_mae10_mean"]
    for label, fraction in (("top1", 0.01), ("top5", 0.05)):
        keep, removed = _tail_keep(net, fraction, positive_only=True)
        result[f"{label}_positive_net_removed_count"] = removed
        result[f"{label}_removed_net_mean"] = finite_mean(net[keep])
        result[f"{label}_removed_net_pf"] = profit_factor(net[keep])
        mfe_keep, mfe_removed = _tail_keep(mfe10, fraction, positive_only=False)
        result[f"{label}_extreme_mfe_removed_count"] = mfe_removed
        result[f"{label}_extreme_mfe_removed_mean"] = finite_mean(mfe10[mfe_keep])
        result[f"{label}_extreme_mfe_removed_lift"] = finite_mean(mfe10[mfe_keep]) - finite_mean(ref_mfe)
    return result


def daily_stage_a_rows(model: str, ranking: dict[str, np.ndarray], arrays: dict[str, np.ndarray]) -> list[dict]:
    meta = arrays["meta"]
    scores = ranking["scores"]
    outcomes = arrays["outcomes"]
    desc = arrays["descriptive_outcomes"]
    rows = []
    for day, region in iter_date_slices(meta["signal_date"]):
        local_eval = meta["outcome_evaluable"][region]
        all_idx = np.flatnonzero(local_eval) + region.start
        top_idx = np.flatnonzero((ranking["rank"][region] <= CFG.stage_a_primary_top_k) & local_eval) + region.start
        mfe_all = outcomes[all_idx, OI["mfe_10d"]]
        mfe_top = outcomes[top_idx, OI["mfe_10d"]]
        winner_all = outcomes[all_idx, OI["primary_success"]]
        winner_top = outcomes[top_idx, OI["primary_success"]]
        rows.append({
            "signal_date": day,
            "year": day // 10000,
            "month": day // 100,
            "model": model,
            "eligible_stocks": region.stop - region.start,
            "evaluable_stocks": len(all_idx),
            "top30_evaluable": len(top_idx),
            "daily_spearman_ic_score_mfe10": spearman(scores[region][local_eval], mfe_all),
            "daily_spearman_ic_score_plus8": spearman(scores[region][local_eval], winner_all),
            "daily_spearman_ic_score_plus10": spearman(scores[region][local_eval], desc[all_idx, 0]),
            "top30_mfe10_mean": finite_mean(mfe_top),
            "universe_mfe10_mean": finite_mean(mfe_all),
            "top30_mfe10_spread": finite_mean(mfe_top) - finite_mean(mfe_all) if len(top_idx) and len(all_idx) else None,
            "top30_plus8_rate": finite_mean(winner_top),
            "universe_plus8_rate": finite_mean(winner_all),
            "top30_plus8_rate_spread": finite_mean(winner_top) - finite_mean(winner_all) if len(top_idx) and len(all_idx) else None,
        })
    return rows


def _daily_quality(rows: list[dict]) -> dict:
    return {
        "mean_daily_mfe_ic": finite_mean(np.asarray([r["daily_spearman_ic_score_mfe10"] for r in rows], dtype=float)),
        "median_daily_mfe_ic": finite_median(np.asarray([r["daily_spearman_ic_score_mfe10"] for r in rows], dtype=float)),
        "mfe_ic_positive_day_rate": finite_mean(np.asarray([float(r["daily_spearman_ic_score_mfe10"] > 0) for r in rows if r["daily_spearman_ic_score_mfe10"] is not None])),
        "mean_daily_plus8_ic": finite_mean(np.asarray([r["daily_spearman_ic_score_plus8"] for r in rows], dtype=float)),
        "mean_daily_plus10_ic": finite_mean(np.asarray([r["daily_spearman_ic_score_plus10"] for r in rows], dtype=float)),
        "mean_daily_top30_mfe_spread": finite_mean(np.asarray([r["top30_mfe10_spread"] for r in rows], dtype=float)),
        "mean_daily_top30_plus8_spread": finite_mean(np.asarray([r["top30_plus8_rate_spread"] for r in rows], dtype=float)),
    }


def stage_a_topk_rows(arrays: dict[str, np.ndarray], rankings: dict[str, dict[str, np.ndarray]], daily: list[dict]) -> list[dict]:
    rows = []
    for model, ranking in rankings.items():
        for kind, label, start, end in time_slices():
            period = date_mask(arrays["meta"], start, end)
            reference = period
            local_daily = [r for r in daily if r["model"] == model and int(start) <= r["signal_date"] <= int(end)]
            for k in STAGE_A_TOP_K:
                row = {
                    "time_slice_type": kind, "time_slice": label, "model": model,
                    "top_k": k, "primary_opportunity_pool": k == CFG.stage_a_primary_top_k,
                    **metric_summary(arrays, period & (ranking["rank"] <= k), reference),
                }
                if k == CFG.stage_a_primary_top_k:
                    row.update(_daily_quality(local_daily))
                rows.append(row)
    return rows


def comparison_rows(arrays: dict[str, np.ndarray], stage_a_rank: np.ndarray, two_stage_rank: np.ndarray) -> list[dict]:
    cohorts = {
        "UNIVERSE": np.ones(len(stage_a_rank), dtype=bool),
        "STAGE_A_TOP30": stage_a_rank <= 30,
        "NET_RIDGE_TOP10": arrays["stage_b_ranks"] <= 10,
        "TWO_STAGE_TOP3": (two_stage_rank > 0) & (two_stage_rank <= 3),
        "TWO_STAGE_TOP5": (two_stage_rank > 0) & (two_stage_rank <= 5),
        "TWO_STAGE_TOP10": (two_stage_rank > 0) & (two_stage_rank <= 10),
    }
    rows = []
    for kind, label, start, end in time_slices():
        period = date_mask(arrays["meta"], start, end)
        for cohort, mask in cohorts.items():
            rows.append({
                "time_slice_type": kind, "time_slice": label, "cohort": cohort,
                "primary_final_candidate": cohort == "TWO_STAGE_TOP5",
                **metric_summary(arrays, period & mask, period),
            })
    return rows


def complementarity_rows(arrays: dict[str, np.ndarray], stage_a_ranking: dict[str, np.ndarray]) -> list[dict]:
    meta = arrays["meta"]
    output = []
    for kind, label, start, end in time_slices():
        if kind not in {"PERIOD", "OVERALL"}:
            continue
        daily = []
        for day, region in iter_date_slices(meta["signal_date"]):
            if not (int(start) <= day <= int(end)):
                continue
            a = stage_a_ranking["scores"][region]
            b = arrays["stage_b_scores"][region]
            pearson = float(np.corrcoef(a, b)[0, 1]) if np.std(a) > 0 and np.std(b) > 0 else None
            a_decile = stage_a_ranking["score_decile"][region] == 10
            b_rank = arrays["stage_b_ranks"][region]
            b_decile = b_rank <= int(math.ceil(len(b_rank) / 10))
            intersection = int(np.count_nonzero(a_decile & b_decile))
            union = int(np.count_nonzero(a_decile | b_decile))
            a30 = stage_a_ranking["rank"][region] <= 30
            b10 = b_rank <= 10
            daily.append({
                "spearman": spearman(a, b), "pearson": pearson,
                "top_decile_jaccard": intersection / union if union else None,
                "top_decile_overlap": intersection,
                "top30_top10_overlap": int(np.count_nonzero(a30 & b10)),
                "stage_b_top10_captured_share": float(np.count_nonzero(a30 & b10) / np.count_nonzero(b10)),
            })
        output.append({
            "time_slice_type": kind, "time_slice": label,
            "daily_score_spearman_mean": finite_mean(np.asarray([r["spearman"] for r in daily], dtype=float)),
            "daily_score_pearson_mean": finite_mean(np.asarray([r["pearson"] for r in daily], dtype=float)),
            "top_decile_jaccard_mean": finite_mean(np.asarray([r["top_decile_jaccard"] for r in daily], dtype=float)),
            "top_decile_overlap_mean_stocks": finite_mean(np.asarray([r["top_decile_overlap"] for r in daily], dtype=float)),
            "stage_a_top30_stage_b_top10_overlap_mean": finite_mean(np.asarray([r["top30_top10_overlap"] for r in daily], dtype=float)),
            "stage_b_top10_captured_share_mean": finite_mean(np.asarray([r["stage_b_top10_captured_share"] for r in daily], dtype=float)),
        })
    return output


def quadrant_rows(arrays: dict[str, np.ndarray], labels: np.ndarray) -> list[dict]:
    rows = []
    for kind, label, start, end in time_slices():
        if kind not in {"PERIOD", "OVERALL"}:
            continue
        period = date_mask(arrays["meta"], start, end)
        for value, name in QUADRANTS.items():
            rows.append({
                "time_slice_type": kind, "time_slice": label,
                "quadrant_id": value, "quadrant": name,
                **metric_summary(arrays, period & (labels == value), period),
            })
    return rows


def cooldown_rows(arrays: dict[str, np.ndarray], cooldown: np.ndarray) -> list[dict]:
    rows = []
    for kind, label, start, end in time_slices():
        period = date_mask(arrays["meta"], start, end)
        rows.append({
            "time_slice_type": kind, "time_slice": label,
            "cohort": "TWO_STAGE_TOP5_10D_COOLDOWN_TRADE_PROXY",
            **metric_summary(arrays, period & cooldown, period),
        })
    return rows


def frequency_rows(arrays: dict[str, np.ndarray], raw: np.ndarray, cooldown: np.ndarray) -> list[dict]:
    meta = arrays["meta"]
    output = []
    for mode, selection in (("RAW_TWO_STAGE_TOP5", raw), ("10D_COOLDOWN_TRADE_PROXY", cooldown)):
        for kind, label, start, end in time_slices():
            period = date_mask(meta, start, end)
            calendar = np.unique(meta["signal_date"][period])
            selected_dates = meta["signal_date"][period & selection]
            counts = Counter(map(int, selected_dates))
            daily = np.asarray([counts.get(int(day), 0) for day in calendar], dtype=float)
            months = np.unique(calendar // 100)
            monthly = [int(np.count_nonzero(period & selection & (meta["signal_date"] // 100 == month))) for month in months]
            active_month = [len(np.unique(meta["signal_date"][period & selection & (meta["signal_date"] // 100 == month)])) for month in months]
            output.append({
                "time_slice_type": kind, "time_slice": label, "ranking_mode": mode,
                "opportunities": int(np.count_nonzero(period & selection)),
                "opportunities_per_month": finite_mean(np.asarray(monthly, dtype=float)),
                "active_dates": int(np.count_nonzero(daily > 0)),
                "active_dates_per_month": finite_mean(np.asarray(active_month, dtype=float)),
                "median_opportunities_per_day": finite_median(daily),
                "p90_opportunities_per_day": float(np.quantile(daily, 0.9, method="linear")),
                "maximum_opportunities_per_day": int(np.max(daily)),
            })
    return output


def n_compact_rows(arrays: dict[str, np.ndarray], stage_a_rank: np.ndarray, two_stage_rank: np.ndarray) -> list[dict]:
    meta = arrays["meta"]
    rows = []
    compact_indices = np.flatnonzero(arrays["n_compact"])
    for index in compact_indices:
        rows.append({
            "row_type": "SIGNAL_DETAIL", "signal_date": int(meta["signal_date"][index]),
            "stock_code": int(meta["stock_code"][index]),
            "stage_a_rank": int(stage_a_rank[index]),
            "stage_a_percentile": 1.0 - (int(stage_a_rank[index]) - 1) / max(1, int(np.count_nonzero(meta["signal_date"] == meta["signal_date"][index])) - 1),
            "stage_b_original_rank": int(arrays["stage_b_ranks"][index]),
            "in_stage_a_top30": bool(stage_a_rank[index] <= 30),
            "in_two_stage_top5": bool(0 < two_stage_rank[index] <= 5),
        })
    compact = arrays["n_compact"]
    two = (two_stage_rank > 0) & (two_stage_rank <= 5)
    for kind, label, start, end in time_slices():
        period = date_mask(meta, start, end)
        for cohort, mask in (
            ("N_COMPACT_ONLY", compact),
            ("TWO_STAGE_TOP5_ONLY", two),
            ("N_COMPACT_UNION_TWO_STAGE_TOP5", compact | two),
        ):
            rows.append({
                "row_type": "COHORT_SUMMARY", "time_slice_type": kind,
                "time_slice": label, "cohort": cohort,
                **metric_summary(arrays, period & mask, period),
            })
    return rows


def regime_rows(arrays: dict[str, np.ndarray], stage_a_rank: np.ndarray, two_stage_rank: np.ndarray, regimes: dict[str, np.ndarray]) -> list[dict]:
    groups = {
        "0050_CLOSE_VS_MA20": (("ABOVE_OR_EQUAL", regimes["close_vs_ma20"] >= 0), ("BELOW", regimes["close_vs_ma20"] < 0)),
        "0050_MA20_SLOPE5": (("RISING_OR_FLAT", regimes["ma20_slope5"] >= 0), ("FALLING", regimes["ma20_slope5"] < 0)),
        "0050_VOLATILITY20": (("HIGH", regimes["volatility20"] >= regimes["volatility_discovery_median"]), ("LOW", regimes["volatility20"] < regimes["volatility_discovery_median"])),
    }
    cohorts = {"STAGE_A_TOP30": stage_a_rank <= 30, "TWO_STAGE_TOP5": (two_stage_rank > 0) & (two_stage_rank <= 5)}
    rows = []
    for label, start, end in PERIODS:
        period = date_mask(arrays["meta"], start, end)
        for dimension, values in groups.items():
            for group, regime in values:
                reference = period & regime
                for cohort, selection in cohorts.items():
                    rows.append({
                        "time_slice": label, "market_proxy": "0050", "regime_dimension": dimension,
                        "regime_group": group, "cohort": cohort,
                        **metric_summary(arrays, reference & selection, reference),
                    })
    return rows


def gap_rows(arrays: dict[str, np.ndarray], stage_a_rank: np.ndarray, two_stage_rank: np.ndarray) -> list[dict]:
    gap = arrays["entry_gap"]
    cohorts = {"STAGE_A_TOP30": stage_a_rank <= 30, "TWO_STAGE_TOP5": (two_stage_rank > 0) & (two_stage_rank <= 5)}
    rows = []
    for label, start, end in PERIODS:
        period = date_mask(arrays["meta"], start, end)
        for bucket, lower, upper in GAP_BUCKETS:
            local = np.isfinite(gap)
            if lower is not None:
                local &= gap >= lower
            if upper is not None:
                local &= gap < upper
            for cohort, selection in cohorts.items():
                rows.append({
                    "time_slice": label, "cohort": cohort, "entry_gap_bucket": bucket,
                    "lower_inclusive": lower, "upper_exclusive": upper,
                    **metric_summary(arrays, period & local & selection, period & local),
                })
    return rows


def two_stage_daily_rows(arrays: dict[str, np.ndarray], two_stage_rank: np.ndarray) -> list[dict]:
    meta = arrays["meta"]
    rows = []
    for day, region in iter_date_slices(meta["signal_date"]):
        ev = meta["outcome_evaluable"][region]
        universe = np.flatnonzero(ev) + region.start
        two = np.flatnonzero((two_stage_rank[region] > 0) & (two_stage_rank[region] <= 5) & ev) + region.start
        net_two = finite_mean(arrays["outcomes"][two, OI["net_return"]])
        net_universe = finite_mean(arrays["outcomes"][universe, OI["net_return"]])
        rows.append({
            "signal_date": day, "month": day // 100,
            "two_stage_net_mean": net_two, "universe_net_mean": net_universe,
            "two_stage_net_spread": net_two - net_universe if net_two is not None and net_universe is not None else None,
        })
    return rows


def _stable_seed(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big") % (2**32)


def _bootstrap(values: np.ndarray, clusters: np.ndarray, reps: int, seed: int) -> tuple[float | None, float | None, int]:
    finite = np.isfinite(values)
    values = values[finite]
    clusters = clusters[finite]
    unique = np.unique(clusters)
    if len(unique) < 2:
        return None, None, len(unique)
    sums = np.asarray([np.sum(values[clusters == key]) for key in unique])
    counts = np.asarray([np.count_nonzero(clusters == key) for key in unique])
    rng = np.random.default_rng(seed)
    sims = np.empty(reps)
    for offset in range(0, reps, 250):
        size = min(250, reps - offset)
        sample = rng.integers(0, len(unique), size=(size, len(unique)))
        sims[offset:offset + size] = np.sum(sums[sample], axis=1) / np.sum(counts[sample], axis=1)
    return (*map(float, np.quantile(sims, (0.025, 0.975), method="linear")), len(unique))


def bootstrap_rows(stage_a_daily: list[dict], two_stage_daily: list[dict], cfg: Config = CFG) -> list[dict]:
    rows = []
    for label, start, end in PERIODS:
        specs = (
            ("STAGE_A_TOP30", stage_a_daily, "top30_mfe10_spread"),
            ("STAGE_A_TOP30", stage_a_daily, "top30_plus8_rate_spread"),
            ("TWO_STAGE_TOP5", two_stage_daily, "two_stage_net_spread"),
        )
        for cohort, source, field in specs:
            selected = [r for r in source if int(start) <= r["signal_date"] <= int(end)]
            values = np.asarray([r[field] for r in selected], dtype=float)
            dates = np.asarray([r["signal_date"] for r in selected], dtype=int)
            for unit, clusters in (("signal_date", dates), ("calendar_month", dates // 100)):
                seed = _stable_seed(cfg.bootstrap_seed, label, cohort, field, unit)
                low, high, count = _bootstrap(values, clusters, cfg.bootstrap_iterations, seed)
                rows.append({
                    "time_slice": label, "cohort": cohort, "metric": field,
                    "cluster_unit": unit, "clusters": count, "bootstrap_reps": cfg.bootstrap_iterations,
                    "seed": seed, "point_estimate": finite_mean(values), "ci95_low": low, "ci95_high": high,
                    "iid_trade_bootstrap": False,
                })
    return rows


def remove_best_cluster_mean(rows: list[dict], field: str, cluster_field: str) -> float | None:
    grouped = defaultdict(list)
    for row in rows:
        if row[field] is not None:
            grouped[row[cluster_field]].append(float(row[field]))
    if len(grouped) < 2:
        return None
    best = max(grouped, key=lambda key: float(np.mean(grouped[key])))
    remaining = [value for key, values in grouped.items() if key != best for value in values]
    return float(np.mean(remaining)) if remaining else None
