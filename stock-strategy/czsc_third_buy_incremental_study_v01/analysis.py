from __future__ import annotations

import math

import numpy as np

from conditional_path_quality_ranking_v01.analysis import finite_mean, profit_factor
from extension_entry_study_v01.pipeline import OUTCOME_FIELDS

from .config import CFG, COHORTS, PERIODS, Config


OI = {name: index for index, name in enumerate(OUTCOME_FIELDS)}


def period_mask(meta, start: int, end: int):
    return (meta["signal_date"] >= start) & (meta["signal_date"] <= end)


def slices():
    rows = [("PERIOD", label, start, end) for label, start, end in PERIODS]
    rows += [("YEAR", str(year), year * 10000 + 101, year * 10000 + 1231) for year in range(2020, 2026)]
    rows.append(("OVERALL", "2020_2025", 20200101, 20251231))
    return rows


def cohort_masks(arrays, czsc_mask):
    stage = arrays["stage_a_pool"]
    return {
        "CZSC_ONLY_ALL": czsc_mask,
        "STAGE_A_TOP30": stage,
        "STAGE_A_AND_CZSC": stage & czsc_mask,
        "STAGE_A_WITHOUT_CZSC": stage & ~czsc_mask,
    }


def metrics(arrays, selected):
    meta = arrays["meta"]
    valid = selected & meta["outcome_evaluable"]
    idx = np.flatnonzero(valid)
    base = {
        "observations": int(np.count_nonzero(selected)),
        "evaluable_observations": len(idx),
        "signal_dates": int(len(np.unique(meta["signal_date"][selected]))),
        "unique_stocks": int(len(np.unique(meta["stock_code"][selected]))),
    }
    if not len(idx):
        return {**base, "success_rate": None, "net_mean": None, "net_pf": None}
    out = arrays["outcomes"][idx]
    path = arrays["path_class"][idx]
    gross, net = out[:, OI["gross_return"]], out[:, OI["net_return"]]
    return {
        **base,
        "success_rate": float(np.mean(path == 1)),
        "downside_first_rate": float(np.mean(path == 2)),
        "timeout_rate": float(np.mean(path == 3)),
        "day5_positive_rate": float(np.mean(out[:, OI["day5_close_return"]] > 0)),
        "day10_positive_rate": float(np.mean(out[:, OI["day10_close_return"]] > 0)),
        "mfe5_mean": finite_mean(out[:, OI["mfe_5d"]]),
        "mfe10_mean": finite_mean(out[:, OI["mfe_10d"]]),
        "mae5_mean": finite_mean(out[:, OI["mae_5d"]]),
        "mae10_mean": finite_mean(out[:, OI["mae_10d"]]),
        "gross_mean": finite_mean(gross),
        "net_mean": finite_mean(net),
        "gross_pf": profit_factor(gross),
        "net_pf": profit_factor(net),
    }


def comparison_rows(arrays, cohorts):
    rows = []
    comparisons = (
        ("CZSC_ONLY_ALL", "STAGE_A_TOP30"),
        ("STAGE_A_AND_CZSC", "STAGE_A_TOP30"),
        ("STAGE_A_AND_CZSC", "STAGE_A_WITHOUT_CZSC"),
    )
    for kind, label, start, end in slices():
        pmask = period_mask(arrays["meta"], start, end)
        values = {name: metrics(arrays, pmask & mask) for name, mask in cohorts.items()}
        for name in COHORTS:
            rows.append({"slice_type": kind, "slice": label, "row_type": "COHORT", "cohort": name, **values[name]})
        for left, right in comparisons:
            row = {"slice_type": kind, "slice": label, "row_type": "DELTA", "cohort": f"{left}_MINUS_{right}"}
            for field in ("success_rate", "downside_first_rate", "mae10_mean", "mfe10_mean", "net_mean", "net_pf"):
                a, b = values[left].get(field), values[right].get(field)
                row[f"delta_{field}"] = None if a is None or b is None else a - b
            rows.append(row)
    return rows


def annual_rows(arrays, cohorts):
    return [
        {"year": year, "cohort": name, **metrics(arrays, period_mask(arrays["meta"], year * 10000 + 101, year * 10000 + 1231) & mask)}
        for year in range(2020, 2026) for name, mask in cohorts.items()
    ]


def bi_count_rows(arrays, signals, czsc_mask):
    bi_by_key = {(row["signal_date"], row["stock_id"]): row["bi_count"] for row in signals}
    bi = np.zeros(len(czsc_mask), dtype=np.uint8)
    for index in np.flatnonzero(czsc_mask):
        bi[index] = bi_by_key[(int(arrays["meta"]["signal_date"][index]), int(arrays["meta"]["stock_code"][index]))]
    rows = []
    for _, label, start, end in slices():
        pmask = period_mask(arrays["meta"], start, end)
        for count in sorted(set(int(x) for x in bi[czsc_mask])):
            rows.append({"slice": label, "bi_count": count, **metrics(arrays, pmask & czsc_mask & (bi == count))})
    return rows


def coverage_rows(arrays, cohorts):
    meta = arrays["meta"]
    all_dates = np.unique(meta["signal_date"])
    rows = []
    for _, label, start, end in slices():
        pmask = period_mask(meta, start, end)
        period_dates = all_dates[(all_dates >= start) & (all_dates <= end)]
        months = np.unique(period_dates // 100)
        stage_n = int(np.count_nonzero(pmask & cohorts["STAGE_A_TOP30"]))
        for name in COHORTS:
            selected = pmask & cohorts[name]
            counts = np.asarray([np.count_nonzero(selected & (meta["signal_date"] == day)) for day in period_dates])
            rows.append({
                "slice": label, "cohort": name,
                "signals": int(np.count_nonzero(selected)),
                "signals_per_year": float(np.count_nonzero(selected) / max(1, len(np.unique(period_dates // 10000)))),
                "signals_per_month": float(np.count_nonzero(selected) / max(1, len(months))),
                "average_signals_per_day": float(np.mean(counts)) if len(counts) else None,
                "median_signals_per_day": float(np.median(counts)) if len(counts) else None,
                "zero_signal_days_pct": float(np.mean(counts == 0)) if len(counts) else None,
                "signal_dates": int(np.count_nonzero(counts)),
                "unique_stocks": int(len(np.unique(meta["stock_code"][selected]))),
                "retained_pct_of_stage_a": float(np.count_nonzero(selected) / stage_n) if stage_n and name == "STAGE_A_AND_CZSC" else None,
            })
    return rows


def tail_rows(arrays, cohorts):
    rows = []
    meta = arrays["meta"]
    for _, label, start, end in slices():
        pmask = period_mask(meta, start, end)
        for name, mask in cohorts.items():
            selected = pmask & mask & meta["outcome_evaluable"]
            idx = np.flatnonzero(selected)
            net = arrays["outcomes"][idx, OI["net_return"]]
            for removal, fraction in (("ORIGINAL", 0.0), ("REMOVE_TOP1_PERCENT_POSITIVE_NET", 0.01), ("REMOVE_TOP5_PERCENT_POSITIVE_NET", 0.05)):
                keep = np.ones(len(idx), dtype=bool)
                candidates = np.flatnonzero(np.isfinite(net) & (net > 0))
                remove = int(math.ceil(len(candidates) * fraction)) if candidates.size else 0
                if remove:
                    keep[candidates[np.argsort(-net[candidates], kind="stable")[:remove]]] = False
                chosen = np.zeros(len(meta), dtype=bool); chosen[idx[keep]] = True
                row = metrics(arrays, chosen)
                rows.append({"slice": label, "cohort": name, "removal": removal, "removed": remove,
                             "success_rate": row.get("success_rate"), "mfe10_mean": row.get("mfe10_mean"),
                             "net_mean": row.get("net_mean"), "net_pf": row.get("net_pf")})
    return rows


def _cluster_stats(arrays, mask, clusters):
    valid = mask & arrays["meta"]["outcome_evaluable"]
    rows = []
    for cluster in np.unique(clusters[valid]):
        idx = np.flatnonzero(valid & (clusters == cluster))
        out = arrays["outcomes"][idx]
        path = arrays["path_class"][idx]
        gross = out[:, OI["net_return"]]
        rows.append((len(idx), np.sum(path == 1), np.sum(path == 2), np.sum(out[:, OI["mae_10d"]]),
                     np.sum(out[:, OI["mfe_10d"]]), np.sum(gross), np.sum(gross[gross > 0]), -np.sum(gross[gross < 0])))
    return np.asarray(rows, dtype=float)


def _metric_from_aggregates(values, metric):
    total = values[:, 0].sum()
    if not total: return np.nan
    pos = {"success": 1, "downside_first": 2, "mae10": 3, "mfe10": 4, "net": 5}
    if metric in pos: return values[:, pos[metric]].sum() / total
    loss = values[:, 7].sum()
    return values[:, 6].sum() / loss if loss > 0 else np.nan


def bootstrap_rows(arrays, cohorts, cfg: Config = CFG):
    rows = []
    rng = np.random.default_rng(cfg.bootstrap_seed)
    for label, start, end in PERIODS:
        pmask = period_mask(arrays["meta"], start, end)
        for cluster_kind, clusters in (("SIGNAL_DATE", arrays["meta"]["signal_date"]), ("CALENDAR_MONTH", arrays["meta"]["signal_date"] // 100)):
            unique = np.unique(clusters[pmask])
            left = _cluster_stats(arrays, pmask & cohorts["STAGE_A_AND_CZSC"], clusters)
            right = _cluster_stats(arrays, pmask & cohorts["STAGE_A_TOP30"], clusters)
            # Reindex aggregates onto the same cluster universe, including zero-selection clusters.
            def aligned(mask):
                result = np.zeros((len(unique), 8), dtype=float)
                valid = mask & arrays["meta"]["outcome_evaluable"]
                for i, cluster in enumerate(unique):
                    idx = np.flatnonzero(valid & (clusters == cluster))
                    if not len(idx): continue
                    out = arrays["outcomes"][idx]; path = arrays["path_class"][idx]; net = out[:, OI["net_return"]]
                    result[i] = (len(idx), np.sum(path == 1), np.sum(path == 2), np.sum(out[:, OI["mae_10d"]]),
                                 np.sum(out[:, OI["mfe_10d"]]), np.sum(net), np.sum(net[net > 0]), -np.sum(net[net < 0]))
                return result
            left, right = aligned(pmask & cohorts["STAGE_A_AND_CZSC"]), aligned(pmask & cohorts["STAGE_A_TOP30"])
            for metric in ("success", "downside_first", "mae10", "mfe10", "net", "pf"):
                sims = np.empty(cfg.bootstrap_reps, dtype=float)
                for rep in range(cfg.bootstrap_reps):
                    sample = rng.integers(0, len(unique), len(unique))
                    sims[rep] = _metric_from_aggregates(left[sample], metric) - _metric_from_aggregates(right[sample], metric)
                sims = sims[np.isfinite(sims)]
                point = _metric_from_aggregates(left, metric) - _metric_from_aggregates(right, metric)
                rows.append({"period": label, "cluster": cluster_kind, "comparison": "STAGE_A_AND_CZSC_MINUS_STAGE_A_TOP30",
                             "metric": metric, "point_difference": point, "ci_low": float(np.quantile(sims, .025)),
                             "ci_high": float(np.quantile(sims, .975)), "reps": len(sims), "ci_crosses_zero": bool(np.quantile(sims,.025) <= 0 <= np.quantile(sims,.975))})
    return rows


def classify(arrays, cohorts, coverage, cfg: Config = CFG):
    overall = next(row for row in coverage if row["slice"] == "2020_2025" and row["cohort"] == "STAGE_A_AND_CZSC")
    later = []
    for label, start, end in PERIODS[1:]:
        pmask = period_mask(arrays["meta"], start, end)
        stage = metrics(arrays, pmask & cohorts["STAGE_A_TOP30"])
        both = metrics(arrays, pmask & cohorts["STAGE_A_AND_CZSC"])
        later.append((both, stage))
    if (overall["signal_dates"] < cfg.minimum_intersection_signal_dates or
            overall["signals_per_month"] < cfg.minimum_intersection_signals_per_month or
            any(x[0]["evaluable_observations"] < cfg.minimum_later_period_intersection for x in later)):
        return "CZSC_THIRD_BUY_TOO_SPARSE"
    success = [a["success_rate"] > b["success_rate"] for a, b in later]
    downside = [a["downside_first_rate"] < b["downside_first_rate"] for a, b in later]
    mae = [a["mae10_mean"] > b["mae10_mean"] for a, b in later]
    retention = [a["mfe10_mean"] / b["mfe10_mean"] >= cfg.minimum_mfe_retention for a, b in later]
    tradeable = [a["net_mean"] > 0 and a["net_pf"] > 1 for a, _ in later]
    if all(success + downside + mae + retention + tradeable): return "CZSC_THIRD_BUY_INCREMENTAL_EDGE_FOUND"
    if all(success + downside + mae + retention): return "CZSC_THIRD_BUY_NOT_TRADEABLE"
    if all(success + downside): return "CZSC_THIRD_BUY_DIRECTION_ONLY"
    if all(downside + mae + retention): return "CZSC_THIRD_BUY_RISK_PATH_ONLY"
    signs = [(a["success_rate"] - b["success_rate"], a["mae10_mean"] - b["mae10_mean"]) for a, b in later]
    if signs[0][0] * signs[1][0] < 0 or signs[0][1] * signs[1][1] < 0: return "CZSC_THIRD_BUY_REGIME_DEPENDENT"
    return "NO_CZSC_THIRD_BUY_EDGE"
