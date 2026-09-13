from __future__ import annotations

from collections import Counter
import math

import numpy as np

from conditional_path_quality_ranking_v01.analysis import metric_summary
from extension_entry_study_v01.pipeline import OUTCOME_FIELDS

from .config import CFG, FEATURES, PERIODS, Config


OI = {name: index for index, name in enumerate(OUTCOME_FIELDS)}


def period_mask(meta: np.ndarray, start: int, end: int) -> np.ndarray:
    return (meta["signal_date"] >= start) & (meta["signal_date"] <= end)


def discovery_boundaries(store: dict[str, np.ndarray], arrays, cfg: Config = CFG):
    discovery = (
        arrays["stage_a_pool"]
        & store["available"]
        & period_mask(arrays["meta"], 20200101, 20221231)
    )
    result = {}
    for index, feature in enumerate(FEATURES):
        values = store["features"][discovery, index]
        values = values[np.isfinite(values)]
        if not len(values):
            raise RuntimeError(f"no discovery values for {feature}")
        result[feature] = [float(value) for value in np.quantile(values, cfg.percentile_boundaries)]
    return result


def bucket_ids(values: np.ndarray, cuts: list[float]) -> np.ndarray:
    result = np.zeros(len(values), dtype=np.uint8)
    finite = np.isfinite(values)
    result[finite] = np.searchsorted(np.asarray(cuts), values[finite], side="right") + 1
    return result


def feature_bucket_rows(arrays, store, cuts):
    rows = []
    pool = arrays["stage_a_pool"] & store["available"]
    for feature_index, feature in enumerate(FEATURES):
        buckets = bucket_ids(store["features"][:, feature_index], cuts[feature])
        for period, start, end in PERIODS:
            pmask = period_mask(arrays["meta"], start, end)
            for bucket in range(1, 6):
                metrics = metric_summary(arrays, pool & pmask & (buckets == bucket))
                rows.append({
                    "context_type": "DYNAMIC_MARKET_PEERS",
                    "period": period,
                    "feature": feature,
                    "feature_family": CFG.family_by_feature[feature],
                    "bucket": bucket,
                    "lower": None if bucket == 1 else cuts[feature][bucket - 2],
                    "upper": None if bucket == 5 else cuts[feature][bucket - 1],
                    **metrics,
                })
    return rows


def dynamic_feature_rows(bucket_rows):
    rows = []
    for period, _, _ in PERIODS:
        for feature in FEATURES:
            group = [
                row for row in bucket_rows
                if row["period"] == period and row["feature"] == feature
                and row["evaluable_observations"]
            ]
            result = {
                "period": period,
                "feature": feature,
                "feature_family": CFG.family_by_feature[feature],
            }
            for field in (
                "path_success_rate", "downside_first_rate", "mae10_mean",
                "mfe10_mean", "net_mean",
            ):
                x = np.asarray([row["bucket"] for row in group], dtype=float)
                y = np.asarray([row[field] for row in group], dtype=float)
                result[f"bucket_spearman_{field}"] = (
                    float(np.corrcoef(x, np.argsort(np.argsort(y)))[0, 1])
                    if len(x) > 1 else None
                )
                result[f"q5_minus_q1_{field}"] = (
                    group[-1][field] - group[0][field] if len(group) == 5 else None
                )
            rows.append(result)
    return rows


def define_composite(bucket_rows, cuts, cfg: Config = CFG):
    candidates = []
    for feature in FEATURES:
        group = [
            row for row in bucket_rows
            if row["period"] == "HISTORICAL_DISCOVERY"
            and row["feature"] == feature
        ]
        group.sort(key=lambda row: row["bucket"])
        if len(group) != 5:
            continue
        low, high = group[0], group[-1]
        required = ("path_success_rate", "downside_first_rate")
        if any(low.get(field) is None or high.get(field) is None for field in required):
            continue
        high_favorable = (
            high["path_success_rate"] > low["path_success_rate"]
            and high["downside_first_rate"] < low["downside_first_rate"]
        )
        low_favorable = (
            low["path_success_rate"] > high["path_success_rate"]
            and low["downside_first_rate"] < high["downside_first_rate"]
        )
        if not (high_favorable or low_favorable):
            continue
        side = "HIGH" if high_favorable else "LOW"
        favorable, adverse = (high, low) if high_favorable else (low, high)
        strength = (
            favorable["path_success_rate"] - adverse["path_success_rate"]
            + adverse["downside_first_rate"] - favorable["downside_first_rate"]
        )
        candidates.append({
            "feature": feature,
            "feature_family": cfg.family_by_feature[feature],
            "favorable_side": side,
            "threshold": cuts[feature][2] if side == "HIGH" else cuts[feature][1],
            "discovery_extreme_success_delta": (
                favorable["path_success_rate"] - adverse["path_success_rate"]
            ),
            "discovery_extreme_downside_delta": (
                favorable["downside_first_rate"] - adverse["downside_first_rate"]
            ),
            "selection_strength": strength,
        })
    definitions = []
    for family in sorted(set(row["feature_family"] for row in candidates)):
        family_rows = [row for row in candidates if row["feature_family"] == family]
        family_rows.sort(key=lambda row: (-row["selection_strength"], row["feature"]))
        definitions.append(family_rows[0])
    eligible = len(definitions) >= cfg.minimum_composite_families
    return definitions if eligible else [], {
        "eligible": eligible,
        "required_distinct_feature_families": cfg.minimum_composite_families,
        "selected_feature_families": len(definitions) if eligible else 0,
        "rule": "one strongest discovery-consistent feature per economic family; HIGH uses frozen Q60, LOW uses frozen Q40",
    }


def composite_scores(store, definitions):
    scores = np.zeros(len(store["features"]), dtype=np.int8)
    for definition in definitions:
        index = FEATURES.index(definition["feature"])
        values = store["features"][:, index]
        if definition["favorable_side"] == "HIGH":
            scores += (values >= definition["threshold"]).astype(np.int8)
        else:
            scores += (values <= definition["threshold"]).astype(np.int8)
    scores[~store["available"]] = -1
    return scores


def daily_context_ranks(arrays, store, scores):
    ranks = np.zeros(len(scores), dtype=np.uint8)
    meta = arrays["meta"]
    pool = arrays["stage_a_pool"] & store["available"]
    for day in np.unique(meta["signal_date"][pool]):
        indices = np.flatnonzero(pool & (meta["signal_date"] == day))
        # Higher composite is stronger; mean peer correlation and stock id are frozen tie-breakers.
        mean_corr = np.nanmean(store["peer_correlations"][indices], axis=1)
        order = np.lexsort((meta["stock_code"][indices], -mean_corr, -scores[indices]))
        ranks[indices[order]] = np.arange(1, len(indices) + 1, dtype=np.uint8)
    return ranks


def pool_rows(arrays, store, scores, ranks):
    available_pool = arrays["stage_a_pool"] & store["available"]
    maximum = max(int(scores.max()), 0)
    strong_min = max(2, maximum - 1)
    cohorts = {
        "STAGE_A_TOP30": available_pool,
        "STRONG_GROUP_CONTEXT": available_pool & (scores >= strong_min),
        "GROUP_CONTEXT_TOP15": available_pool & (ranks > 0) & (ranks <= 15),
        "GROUP_CONTEXT_TOP10": available_pool & (ranks > 0) & (ranks <= 10),
        "GROUP_CONTEXT_TOP5": available_pool & (ranks > 0) & (ranks <= 5),
    }
    rows = []
    for period, start, end in PERIODS:
        pmask = period_mask(arrays["meta"], start, end)
        baseline = metric_summary(arrays, pmask & cohorts["STAGE_A_TOP30"])
        for name, cohort in cohorts.items():
            metrics = metric_summary(arrays, pmask & cohort)
            rows.append({
                "period": period,
                "cohort": name,
                "pre_registered_k": int(name.rsplit("TOP", 1)[-1]) if "TOP" in name and name != "STAGE_A_TOP30" else None,
                "strong_context_min_points": strong_min if name == "STRONG_GROUP_CONTEXT" else None,
                "retained_pct": metrics["observations"] / baseline["observations"] if baseline["observations"] else None,
                "delta_success_vs_top30": metrics.get("path_success_rate") - baseline.get("path_success_rate") if metrics.get("path_success_rate") is not None else None,
                "delta_downside_vs_top30": metrics.get("downside_first_rate") - baseline.get("downside_first_rate") if metrics.get("downside_first_rate") is not None else None,
                "mae_improvement_vs_top30": metrics.get("mae10_mean") - baseline.get("mae10_mean") if metrics.get("mae10_mean") is not None else None,
                "mfe_retention_vs_top30": metrics.get("mfe10_mean") / baseline.get("mfe10_mean") if metrics.get("mfe10_mean") is not None and baseline.get("mfe10_mean") else None,
                "delta_net_vs_top30": metrics.get("net_mean") - baseline.get("net_mean") if metrics.get("net_mean") is not None else None,
                **metrics,
            })
    return rows, cohorts, strong_min


def _metric_values(arrays, metric):
    if metric == "success": return (arrays["path_class"] == 1).astype(float)
    if metric == "downside_first": return (arrays["path_class"] == 2).astype(float)
    if metric == "mae10": return arrays["outcomes"][:, OI["mae_10d"]]
    if metric == "mfe10": return arrays["outcomes"][:, OI["mfe_10d"]]
    if metric == "net": return arrays["outcomes"][:, OI["net_return"]]
    if metric == "net_pf": return arrays["outcomes"][:, OI["net_return"]]
    raise KeyError(metric)


def _aggregate(values, mask, pf=False):
    local = values[mask & np.isfinite(values)]
    if not len(local): return np.nan
    if not pf: return float(np.mean(local))
    loss = -float(local[local < 0].sum())
    return float(local[local > 0].sum()) / loss if loss > 0 else np.nan


def cluster_bootstrap_rows(arrays, cohorts, cfg: Config = CFG):
    rows = []
    meta = arrays["meta"]
    valid = meta["outcome_evaluable"]
    for period, start, end in PERIODS:
        pmask = period_mask(meta, start, end)
        baseline = cohorts["STAGE_A_TOP30"] & pmask & valid
        for cohort_name in ("STRONG_GROUP_CONTEXT", "GROUP_CONTEXT_TOP15", "GROUP_CONTEXT_TOP10", "GROUP_CONTEXT_TOP5"):
            selected = cohorts[cohort_name] & pmask & valid
            for cluster_name, clusters in (("signal_date", meta["signal_date"]), ("calendar_month", meta["signal_date"] // 100)):
                unique = np.unique(clusters[baseline | selected])
                for metric in ("success", "downside_first", "mae10", "mfe10", "net", "net_pf"):
                    values = _metric_values(arrays, metric)
                    selected_sum = []
                    selected_count = []
                    baseline_sum = []
                    baseline_count = []
                    selected_gain = []
                    selected_loss = []
                    baseline_gain = []
                    baseline_loss = []
                    for cluster in unique:
                        sv = values[selected & (clusters == cluster)]
                        bv = values[baseline & (clusters == cluster)]
                        sv = sv[np.isfinite(sv)]; bv = bv[np.isfinite(bv)]
                        selected_sum.append(float(sv.sum())); selected_count.append(len(sv))
                        baseline_sum.append(float(bv.sum())); baseline_count.append(len(bv))
                        selected_gain.append(float(sv[sv > 0].sum())); selected_loss.append(float(-sv[sv < 0].sum()))
                        baseline_gain.append(float(bv[bv > 0].sum())); baseline_loss.append(float(-bv[bv < 0].sum()))
                    selected_sum = np.asarray(selected_sum); selected_count = np.asarray(selected_count)
                    baseline_sum = np.asarray(baseline_sum); baseline_count = np.asarray(baseline_count)
                    selected_gain = np.asarray(selected_gain); selected_loss = np.asarray(selected_loss)
                    baseline_gain = np.asarray(baseline_gain); baseline_loss = np.asarray(baseline_loss)
                    rng = np.random.default_rng(cfg.bootstrap_seed + len(unique) + len(metric) + len(cohort_name))
                    simulations = []
                    for offset in range(0, cfg.bootstrap_reps, 250):
                        batch = min(250, cfg.bootstrap_reps - offset)
                        ix = rng.integers(0, len(unique), size=(batch, len(unique)))
                        if metric == "net_pf":
                            sg = selected_gain[ix].sum(axis=1); sl = selected_loss[ix].sum(axis=1)
                            bg = baseline_gain[ix].sum(axis=1); bl = baseline_loss[ix].sum(axis=1)
                            local = np.divide(sg, sl, out=np.full(batch, np.nan), where=sl > 0) - np.divide(bg, bl, out=np.full(batch, np.nan), where=bl > 0)
                        else:
                            ss = selected_sum[ix].sum(axis=1); sn = selected_count[ix].sum(axis=1)
                            bs = baseline_sum[ix].sum(axis=1); bn = baseline_count[ix].sum(axis=1)
                            local = np.divide(ss, sn, out=np.full(batch, np.nan), where=sn > 0) - np.divide(bs, bn, out=np.full(batch, np.nan), where=bn > 0)
                        simulations.extend(local.tolist())
                    finite = np.asarray(simulations, dtype=float)
                    finite = finite[np.isfinite(finite)]
                    rows.append({
                        "period": period,
                        "comparison": f"{cohort_name}_MINUS_STAGE_A_TOP30",
                        "metric": metric,
                        "cluster": cluster_name,
                        "reps": cfg.bootstrap_reps,
                        "observed_difference": _aggregate(values, selected, metric == "net_pf") - _aggregate(values, baseline, metric == "net_pf"),
                        "ci_low": float(np.quantile(finite, 0.025)) if len(finite) else None,
                        "ci_high": float(np.quantile(finite, 0.975)) if len(finite) else None,
                    })
    return rows


def tail_rows(arrays, cohorts):
    rows = []
    meta = arrays["meta"]
    net = arrays["outcomes"][:, OI["net_return"]]
    for period, start, end in PERIODS:
        pmask = period_mask(meta, start, end)
        for cohort_name, cohort in cohorts.items():
            base = pmask & cohort & meta["outcome_evaluable"]
            positive = np.flatnonzero(base & np.isfinite(net) & (net > 0))
            ranked = positive[np.argsort(-net[positive], kind="stable")]
            for label, fraction in (("ORIGINAL", 0.0), ("REMOVE_TOP1_PERCENT_POSITIVE_WINNERS", 0.01), ("REMOVE_TOP5_PERCENT_POSITIVE_WINNERS", 0.05)):
                selected = pmask & cohort
                removed = int(math.ceil(len(positive) * fraction)) if positive.size else 0
                if removed:
                    selected = selected.copy(); selected[ranked[:removed]] = False
                rows.append({
                    "period": period, "cohort": cohort_name, "tail_treatment": label,
                    "removed_trades": removed, **metric_summary(arrays, selected),
                })
    return rows


def concentration_rows(arrays, store, cohorts):
    rows = []
    meta = arrays["meta"]
    for cohort_name, cohort in cohorts.items():
        for period, start, end in PERIODS:
            selected = cohort & period_mask(meta, start, end)
            total = int(np.count_nonzero(selected))
            for dimension, values in (
                ("stock", meta["stock_code"]),
                ("signal_date", meta["signal_date"]),
                ("calendar_month", meta["signal_date"] // 100),
            ):
                keys, counts = np.unique(values[selected], return_counts=True)
                order = np.argsort(-counts, kind="stable")
                rows.append({
                    "period": period, "cohort": cohort_name, "dimension": dimension,
                    "observations": total,
                    "largest_key": int(keys[order[0]]) if len(keys) else None,
                    "largest_share": float(counts[order[0]] / total) if total else None,
                    "top5_share": float(counts[order[:5]].sum() / total) if total else None,
                    "unique_groups": int(len(keys)),
                })
    available = arrays["stage_a_pool"] & store["available"]
    signatures = [
        "-".join(map(str, sorted(store["peer_codes"][index].tolist())))
        for index in np.flatnonzero(available)
    ]
    signature_counts = Counter(signatures)
    rows.append({
        "period": "2020_2025", "cohort": "STAGE_A_TOP30", "dimension": "dynamic_peer_basket_signature",
        "observations": len(signatures),
        "largest_key": signature_counts.most_common(1)[0][0] if signatures else None,
        "largest_share": signature_counts.most_common(1)[0][1] / len(signatures) if signatures else None,
        "top5_share": sum(value for _, value in signature_counts.most_common(5)) / len(signatures) if signatures else None,
        "unique_groups": len(signature_counts),
    })
    for period, start, end in PERIODS:
        selected = available & period_mask(meta, start, end)
        values = store["stage_a_peer_count"][selected].astype(float)
        rows.append({
            "period": period, "cohort": "STAGE_A_TOP30", "dimension": "stage_a_peer_count",
            "observations": len(values), "largest_key": None,
            "largest_share": None, "top5_share": None, "unique_groups": None,
            "mean": float(np.mean(values)) if len(values) else None,
            "median": float(np.median(values)) if len(values) else None,
            "p90": float(np.quantile(values, 0.9)) if len(values) else None,
            "maximum": float(np.max(values)) if len(values) else None,
        })
    return rows


def regime_rows(arrays, cohorts, regimes):
    groups = {
        "0050_CLOSE_VS_MA20": (("ABOVE_OR_EQUAL", regimes["close_vs_ma20"] >= 0), ("BELOW", regimes["close_vs_ma20"] < 0)),
        "0050_MA20_SLOPE5": (("RISING_OR_FLAT", regimes["ma20_slope5"] >= 0), ("FALLING", regimes["ma20_slope5"] < 0)),
        "0050_VOLATILITY20": (("HIGH", regimes["volatility20"] >= regimes["volatility_discovery_median"]), ("LOW", regimes["volatility20"] < regimes["volatility_discovery_median"])),
    }
    rows = []
    for period, start, end in PERIODS:
        pmask = period_mask(arrays["meta"], start, end)
        for dimension, parts in groups.items():
            for group, mask in parts:
                for cohort in ("STAGE_A_TOP30", "GROUP_CONTEXT_TOP10", "GROUP_CONTEXT_TOP5"):
                    rows.append({"period": period, "dimension": dimension, "group": group, "cohort": cohort, **metric_summary(arrays, pmask & mask & cohorts[cohort])})
    return rows


def classify(pool_rows, composite_available):
    if not composite_available:
        return "NO_SECTOR_ROTATION_EDGE"
    chosen = {
        row["period"]: row for row in pool_rows if row["cohort"] == "GROUP_CONTEXT_TOP10"
    }
    later = [
        chosen["RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS"],
        chosen["STRESS_PREVALENCE_SEEN_NOT_BLIND"],
    ]
    downside = all(
        row["delta_downside_vs_top30"] < 0 or row["mae_improvement_vs_top30"] > 0
        for row in later
    )
    retention = all(row["mfe_retention_vs_top30"] >= 0.8 for row in later)
    tradeable = all(row["net_mean"] > 0 and row["net_profit_factor"] > 1 for row in later)
    if downside and retention and tradeable:
        return "DYNAMIC_PEER_EDGE_FOUND"
    if downside and retention:
        return "SECTOR_ROTATION_DIRECTION_ONLY"
    discovery = chosen["HISTORICAL_DISCOVERY"]
    if discovery["mae_improvement_vs_top30"] > 0 or discovery["delta_downside_vs_top30"] < 0:
        return "SECTOR_ROTATION_REGIME_DEPENDENT"
    return "NO_SECTOR_ROTATION_EDGE"
