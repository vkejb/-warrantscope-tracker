from __future__ import annotations

import math

import numpy as np

from conditional_path_quality_ranking_v01.analysis import metric_summary
from extension_entry_study_v01.pipeline import OUTCOME_FIELDS

from .config import CFG, COMPOSITE_CANDIDATES, FEATURES, LEVEL_FEATURES, PERIODS


OI = {name: index for index, name in enumerate(OUTCOME_FIELDS)}


def period_mask(meta, start, end):
    return (meta["signal_date"] >= start) & (meta["signal_date"] <= end)


def _bucket(values, cuts):
    result = np.zeros(len(values), dtype=np.uint8)
    finite = np.isfinite(values)
    result[finite] = np.searchsorted(np.asarray(cuts), values[finite], side="right") + 1
    return result


def frozen_boundaries(arrays, store):
    discovery = arrays["stage_a_pool"] & store["available"] & period_mask(arrays["meta"], 20200101, 20221231)
    return {
        feature: [float(x) for x in np.quantile(store["features"][discovery, index], CFG.percentile_boundaries)]
        for index, feature in enumerate(FEATURES)
    }


def feature_bucket_rows(arrays, store, cuts):
    rows = []
    pool = arrays["stage_a_pool"] & store["available"]
    for index, feature in enumerate(FEATURES):
        buckets = _bucket(store["features"][:, index], cuts[feature])
        for period, start, end in PERIODS:
            pmask = period_mask(arrays["meta"], start, end)
            for bucket in range(1, 6):
                rows.append({
                    "period": period, "feature": feature,
                    "feature_family": CFG.family_by_feature[feature], "bucket": bucket,
                    "lower": None if bucket == 1 else cuts[feature][bucket - 2],
                    "upper": None if bucket == 5 else cuts[feature][bucket - 1],
                    **metric_summary(arrays, pool & pmask & (buckets == bucket)),
                })
    return rows


def acceleration_diagnostics(bucket_rows):
    rows = []
    for period, _, _ in PERIODS:
        for feature in FEATURES:
            group = sorted(
                [r for r in bucket_rows if r["period"] == period and r["feature"] == feature],
                key=lambda row: row["bucket"],
            )
            row = {"period": period, "feature": feature, "feature_family": CFG.family_by_feature[feature]}
            for field in ("path_success_rate", "downside_first_rate", "mae10_mean", "mfe10_mean", "net_mean"):
                values = np.asarray([np.nan if r.get(field) is None else r[field] for r in group], dtype=float)
                finite = np.isfinite(values)
                row[f"ordered_bucket_correlation_{field}"] = float(np.corrcoef(np.arange(1, 6)[finite], values[finite])[0, 1]) if finite.sum() > 1 else None
                row[f"q5_minus_q1_{field}"] = values[-1] - values[0] if np.all(np.isfinite(values[[0, -1]])) else None
            rows.append(row)
    return rows


def _empirical_percentile(values, reference):
    reference = np.sort(np.asarray(reference, dtype=float))
    result = np.full(np.shape(values), np.nan, dtype=float)
    finite = np.isfinite(values)
    result[finite] = np.searchsorted(reference, np.asarray(values)[finite], side="right") / len(reference)
    return result


def build_level_acceleration_indices(arrays, store):
    discovery = arrays["stage_a_pool"] & store["available"] & period_mask(arrays["meta"], 20200101, 20221231)
    level_refs = {}
    level_parts = []
    for index, feature in enumerate(LEVEL_FEATURES):
        reference = store["levels"][discovery, index]
        level_refs[feature] = np.sort(reference[np.isfinite(reference)])
        level_parts.append(_empirical_percentile(store["levels"][:, index], level_refs[feature]))
    acceleration_refs = {}
    acceleration_parts = []
    for feature in COMPOSITE_CANDIDATES:
        index = FEATURES.index(feature)
        reference = store["features"][discovery, index]
        acceleration_refs[feature] = np.sort(reference[np.isfinite(reference)])
        acceleration_parts.append(_empirical_percentile(store["features"][:, index], acceleration_refs[feature]))
    level_stack = np.vstack(level_parts); level_count = np.sum(np.isfinite(level_stack), axis=0)
    acceleration_stack = np.vstack(acceleration_parts); acceleration_count = np.sum(np.isfinite(acceleration_stack), axis=0)
    level_index = np.divide(np.nansum(level_stack, axis=0), level_count, out=np.full(level_stack.shape[1], np.nan), where=level_count > 0)
    acceleration_index = np.divide(np.nansum(acceleration_stack, axis=0), acceleration_count, out=np.full(acceleration_stack.shape[1], np.nan), where=acceleration_count > 0)
    level_cuts = [float(x) for x in np.quantile(level_index[discovery], CFG.level_acceleration_boundaries)]
    acceleration_cuts = [float(x) for x in np.quantile(acceleration_index[discovery], CFG.level_acceleration_boundaries)]
    level_band = _bucket(level_index, level_cuts)
    acceleration_band = _bucket(acceleration_index, acceleration_cuts)
    return {
        "level_index": level_index, "acceleration_index": acceleration_index,
        "level_band": level_band, "acceleration_band": acceleration_band,
        "level_cuts": level_cuts, "acceleration_cuts": acceleration_cuts,
        "level_refs": level_refs, "acceleration_refs": acceleration_refs,
    }


def level_acceleration_rows(arrays, store, indices):
    rows = []
    base = arrays["stage_a_pool"] & store["available"]
    labels = {1: "LOW", 2: "MID", 3: "HIGH"}
    for period, start, end in PERIODS:
        pmask = period_mask(arrays["meta"], start, end)
        for level in range(1, 4):
            for acceleration in range(1, 4):
                role = "OTHER"
                if level <= 2 and acceleration == 3: role = "EARLY_ROTATION"
                elif level == 3 and acceleration >= 2: role = "MATURE_ROTATION"
                elif level == 3 and acceleration == 1: role = "LATE_EXHAUSTION"
                elif level == 1 and acceleration == 1: role = "NO_ROTATION"
                rows.append({
                    "period": period, "level_band": labels[level],
                    "acceleration_band": labels[acceleration], "rotation_role": role,
                    **metric_summary(arrays, base & pmask & (indices["level_band"] == level) & (indices["acceleration_band"] == acceleration)),
                })
    return rows


def select_composite_families(bucket_rows, cuts):
    candidates = []
    for feature in COMPOSITE_CANDIDATES:
        group = sorted(
            [r for r in bucket_rows if r["period"] == "HISTORICAL_DISCOVERY" and r["feature"] == feature],
            key=lambda row: row["bucket"],
        )
        if len(group) != 5 or any(group[i].get("path_success_rate") is None for i in (0, 4)):
            continue
        low, high = group[0], group[-1]
        if high["path_success_rate"] > low["path_success_rate"] and high["downside_first_rate"] < low["downside_first_rate"]:
            candidates.append({
                "feature": feature, "family": CFG.family_by_feature[feature],
                "threshold": cuts[feature][2],
                "discovery_success_delta_q5_minus_q1": high["path_success_rate"] - low["path_success_rate"],
                "discovery_downside_delta_q5_minus_q1": high["downside_first_rate"] - low["downside_first_rate"],
                "strength": high["path_success_rate"] - low["path_success_rate"] + low["downside_first_rate"] - high["downside_first_rate"],
            })
    selected = []
    for family in sorted({row["family"] for row in candidates}):
        options = sorted([row for row in candidates if row["family"] == family], key=lambda row: (-row["strength"], row["feature"]))
        selected.append(options[0])
    eligible = len(selected) >= CFG.minimum_composite_families
    return selected if eligible else [], {
        "eligible": eligible, "selected_families": len(selected) if eligible else 0,
        "discovery_supporting_family_count": len(selected),
        "discovery_supporting_candidates": selected,
        "required_families": CFG.minimum_composite_families,
        "direction_rule": "only naturally HIGH acceleration may qualify; opposite-direction features are not flipped",
    }


def make_cohorts(arrays, store, indices, definitions):
    base = arrays["stage_a_pool"] & store["available"]
    early = base & (indices["level_band"] <= 2) & (indices["acceleration_band"] == 3)
    mature = base & (indices["level_band"] == 3) & (indices["acceleration_band"] >= 2)
    late = base & (indices["level_band"] == 3) & (indices["acceleration_band"] == 1)
    no_rotation = base & (indices["level_band"] == 1) & (indices["acceleration_band"] == 1)
    points = np.zeros(len(base), dtype=np.uint8)
    for definition in definitions:
        values = store["features"][:, FEATURES.index(definition["feature"])]
        points += (values >= definition["threshold"]).astype(np.uint8)
    early_score = points.astype(float) + (indices["level_band"] <= 2).astype(float)
    ranks = np.zeros(len(base), dtype=np.uint8)
    for day in np.unique(arrays["meta"]["signal_date"][base]):
        rows = np.flatnonzero(base & (arrays["meta"]["signal_date"] == day))
        order = np.lexsort((arrays["meta"]["stock_code"][rows], indices["level_index"][rows], -indices["acceleration_index"][rows], -early_score[rows]))
        ranks[rows[order]] = np.arange(1, len(rows) + 1, dtype=np.uint8)
    cohorts = {
        "STAGE_A_TOP30": base, "EARLY_ROTATION": early,
        "MATURE_ROTATION": mature, "LATE_EXHAUSTION": late, "NO_ROTATION": no_rotation,
    }
    for k in CFG.pool_sizes:
        cohorts[f"EARLY_SCORE_TOP{k}"] = base & (ranks > 0) & (ranks <= k)
    return cohorts, early_score, ranks


def pool_rows(arrays, cohorts, composite_eligible=True):
    rows = []
    for period, start, end in PERIODS:
        pmask = period_mask(arrays["meta"], start, end)
        baseline = metric_summary(arrays, pmask & cohorts["STAGE_A_TOP30"])
        for name, mask in cohorts.items():
            metrics = metric_summary(arrays, pmask & mask)
            rows.append({
                "period": period, "cohort": name,
                "interpretation_status": (
                    "DESCRIPTIVE_ONLY_COMPOSITE_GATE_FAILED"
                    if name.startswith("EARLY_SCORE_TOP") and not composite_eligible
                    else "PRIMARY_2D_DEFINITION" if name in {"EARLY_ROTATION", "MATURE_ROTATION", "LATE_EXHAUSTION", "NO_ROTATION"}
                    else "BASELINE"
                ),
                "retained_pct": metrics["observations"] / baseline["observations"] if baseline["observations"] else None,
                "average_stocks_per_day": metrics["observations"] / metrics["unique_dates"] if metrics["unique_dates"] else None,
                "mfe_retention_vs_stage_a": metrics.get("mfe10_mean") / baseline.get("mfe10_mean") if metrics.get("mfe10_mean") is not None and baseline.get("mfe10_mean") else None,
                **metrics,
            })
    return rows


def early_vs_mature_rows(arrays, cohorts):
    rows = []
    for period, start, end in PERIODS:
        pmask = period_mask(arrays["meta"], start, end)
        early = metric_summary(arrays, pmask & cohorts["EARLY_ROTATION"])
        mature = metric_summary(arrays, pmask & cohorts["MATURE_ROTATION"])
        for name, metrics in (("EARLY_ROTATION", early), ("MATURE_ROTATION", mature)):
            rows.append({"period": period, "cohort": name, **metrics})
        rows.append({
            "period": period, "cohort": "EARLY_MINUS_MATURE",
            "path_success_rate": early["path_success_rate"] - mature["path_success_rate"],
            "downside_first_rate": early["downside_first_rate"] - mature["downside_first_rate"],
            "mae10_mean": early["mae10_mean"] - mature["mae10_mean"],
            "mfe10_mean": early["mfe10_mean"] - mature["mfe10_mean"],
            "net_mean": early["net_mean"] - mature["net_mean"],
            "net_profit_factor": early["net_profit_factor"] - mature["net_profit_factor"],
        })
    return rows


def _metric(arrays, name):
    if name == "success": return (arrays["path_class"] == 1).astype(float)
    if name == "downside_first": return (arrays["path_class"] == 2).astype(float)
    if name == "mae10": return arrays["outcomes"][:, OI["mae_10d"]]
    if name == "mfe10": return arrays["outcomes"][:, OI["mfe_10d"]]
    return arrays["outcomes"][:, OI["net_return"]]


def _cluster_parts(values, mask, clusters, unique):
    sums=[]; counts=[]; gains=[]; losses=[]
    for key in unique:
        local=values[mask & (clusters==key)]; local=local[np.isfinite(local)]
        sums.append(local.sum()); counts.append(len(local)); gains.append(local[local>0].sum()); losses.append(-local[local<0].sum())
    return tuple(np.asarray(x,dtype=float) for x in (sums,counts,gains,losses))


def bootstrap_rows(arrays, cohorts):
    rows=[]; meta=arrays["meta"]; valid=meta["outcome_evaluable"]
    for period,start,end in PERIODS:
        pmask=period_mask(meta,start,end); early=cohorts["EARLY_ROTATION"]&pmask&valid; mature=cohorts["MATURE_ROTATION"]&pmask&valid
        for cluster_name,clusters in (("signal_date",meta["signal_date"]),("calendar_month",meta["signal_date"]//100)):
            unique=np.unique(clusters[early|mature])
            for metric in ("success","downside_first","mae10","mfe10","net","net_pf"):
                values=_metric(arrays,metric); ep=_cluster_parts(values,early,clusters,unique); mp=_cluster_parts(values,mature,clusters,unique)
                rng=np.random.default_rng(CFG.bootstrap_seed+len(unique)+len(metric)); sims=[]
                for offset in range(0,CFG.bootstrap_reps,250):
                    size=min(250,CFG.bootstrap_reps-offset); ix=rng.integers(0,len(unique),(size,len(unique)))
                    if metric=="net_pf":
                        e=np.divide(ep[2][ix].sum(1),ep[3][ix].sum(1),out=np.full(size,np.nan),where=ep[3][ix].sum(1)>0)
                        m=np.divide(mp[2][ix].sum(1),mp[3][ix].sum(1),out=np.full(size,np.nan),where=mp[3][ix].sum(1)>0)
                    else:
                        e=np.divide(ep[0][ix].sum(1),ep[1][ix].sum(1),out=np.full(size,np.nan),where=ep[1][ix].sum(1)>0)
                        m=np.divide(mp[0][ix].sum(1),mp[1][ix].sum(1),out=np.full(size,np.nan),where=mp[1][ix].sum(1)>0)
                    sims.extend((e-m).tolist())
                sims=np.asarray(sims); sims=sims[np.isfinite(sims)]
                observed=(ep[2].sum()/ep[3].sum()-mp[2].sum()/mp[3].sum()) if metric=="net_pf" else (ep[0].sum()/ep[1].sum()-mp[0].sum()/mp[1].sum())
                rows.append({"period":period,"comparison":"EARLY_MINUS_MATURE","metric":metric,"cluster":cluster_name,"reps":CFG.bootstrap_reps,"observed_difference":observed,"ci_low":float(np.quantile(sims,.025)),"ci_high":float(np.quantile(sims,.975))})
    return rows


def tail_rows(arrays, cohorts):
    rows=[]; meta=arrays["meta"]; net=arrays["outcomes"][:,OI["net_return"]]
    for period,start,end in PERIODS:
        pmask=period_mask(meta,start,end)
        for cohort in ("EARLY_ROTATION","MATURE_ROTATION","EARLY_SCORE_TOP15","EARLY_SCORE_TOP10","EARLY_SCORE_TOP5"):
            base=pmask&cohorts[cohort]; positive=np.flatnonzero(base&meta["outcome_evaluable"]&np.isfinite(net)&(net>0)); ranked=positive[np.argsort(-net[positive],kind="stable")]
            for label,fraction in (("ORIGINAL",0),("REMOVE_TOP1_PERCENT_POSITIVE_WINNERS",.01),("REMOVE_TOP5_PERCENT_POSITIVE_WINNERS",.05)):
                selected=base.copy(); removed=int(math.ceil(len(positive)*fraction))
                if removed:selected[ranked[:removed]]=False
                rows.append({"period":period,"cohort":cohort,"tail_treatment":label,"removed_trades":removed,**metric_summary(arrays,selected)})
    return rows


def lead_time_rows(arrays, store, indices, cohorts):
    level_future=np.zeros((len(arrays["meta"]),CFG.lead_time_sessions),dtype=float)
    for feature_index,feature in enumerate(LEVEL_FEATURES):
        ref=indices["level_refs"][feature]
        level_future += np.nan_to_num(_empirical_percentile(store["future_levels"][:,:,feature_index],ref),nan=0.0)
    finite_counts=np.sum(np.isfinite(store["future_levels"]),axis=2)
    level_future/=len(LEVEL_FEATURES); level_future[finite_counts<len(LEVEL_FEATURES)]=np.nan
    transition=np.zeros(len(arrays["meta"]),dtype=np.uint8)
    high_cut=indices["level_cuts"][1]
    for row in np.flatnonzero(cohorts["EARLY_ROTATION"]):
        hits=np.flatnonzero(level_future[row]>=high_cut)
        if len(hits):transition[row]=int(hits[0]+1)
    rows=[]
    for period,start,end in PERIODS:
        pmask=period_mask(arrays["meta"],start,end)&cohorts["EARLY_ROTATION"]
        valid_transitions=transition[pmask]; reached=valid_transitions>0
        for label,mask in (("ALL_EARLY",pmask),("TRANSITION_1_TO_3D",pmask&(transition>=1)&(transition<=3)),("TRANSITION_4_TO_10D",pmask&(transition>=4)),("NO_HIGH_LEVEL_TRANSITION_WITHIN_10D",pmask&(transition==0))):
            metrics=metric_summary(arrays,mask)
            rows.append({"period":period,"transition_group":label,"transition_within_10d_rate":float(np.mean(reached)) if label=="ALL_EARLY" and len(reached) else None,"median_transition_days":float(np.median(valid_transitions[reached])) if label=="ALL_EARLY" and np.any(reached) else None,**metrics})
    return rows


def regime_rows(arrays, cohorts, regimes):
    groups={"0050_CLOSE_VS_MA20":(("ABOVE_OR_EQUAL",regimes["close_vs_ma20"]>=0),("BELOW",regimes["close_vs_ma20"]<0)),"0050_MA20_SLOPE5":(("RISING_OR_FLAT",regimes["ma20_slope5"]>=0),("FALLING",regimes["ma20_slope5"]<0)),"0050_VOLATILITY20":(("HIGH",regimes["volatility20"]>=regimes["volatility_discovery_median"]),("LOW",regimes["volatility20"]<regimes["volatility_discovery_median"]))}
    rows=[]
    for period,start,end in PERIODS:
        pmask=period_mask(arrays["meta"],start,end)
        for dimension,parts in groups.items():
            for group,mask in parts:
                for cohort in ("EARLY_ROTATION","MATURE_ROTATION"):
                    rows.append({"period":period,"dimension":dimension,"group":group,"cohort":cohort,**metric_summary(arrays,pmask&mask&cohorts[cohort])})
    return rows


def classify(summary, composite_eligible):
    if not composite_eligible:return "NO_EARLY_ROTATION_EDGE"
    differences={r["period"]:r for r in summary if r["cohort"]=="EARLY_MINUS_MATURE"}
    later=[differences["RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS"],differences["STRESS_PREVALENCE_SEEN_NOT_BLIND"]]
    path=[r["downside_first_rate"]<0 or r["mae10_mean"]>0 for r in later]
    success=[r["path_success_rate"]>=0 for r in later]
    if all(path) and all(success):
        early_rows={r["period"]:r for r in summary if r["cohort"]=="EARLY_ROTATION"}
        tradeable=all(early_rows[p]["net_mean"]>0 and early_rows[p]["net_profit_factor"]>1 for p in ("RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS","STRESS_PREVALENCE_SEEN_NOT_BLIND"))
        return "EARLY_ROTATION_EDGE_FOUND" if tradeable else "EARLY_ROTATION_DIRECTION_ONLY"
    if any(path):return "EARLY_ROTATION_REGIME_DEPENDENT"
    return "NO_EARLY_ROTATION_EDGE"
