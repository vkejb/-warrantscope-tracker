from __future__ import annotations

import math
import numpy as np

from conditional_path_quality_ranking_v01.analysis import metric_summary
from extension_entry_study_v01.pipeline import OUTCOME_FIELDS

from .config import CFG, FEATURES, PERIODS


OI = {name: i for i, name in enumerate(OUTCOME_FIELDS)}


def boundaries(context: dict[int, dict]) -> dict[str, list[float]]:
    dates = [d for d in context if CFG.discovery_start <= d <= CFG.discovery_end]
    result = {}
    for feature in FEATURES:
        values = np.asarray([context[d].get(feature, np.nan) for d in dates], dtype=float)
        values = values[np.isfinite(values)]
        result[feature] = [float(x) for x in np.quantile(values, CFG.percentile_boundaries)]
    return result


def bucket(value: float, cuts: list[float]) -> int:
    return int(np.searchsorted(np.asarray(cuts), value, side="right")) + 1


def period_mask(meta, start, end):
    return (meta["signal_date"] >= start) & (meta["signal_date"] <= end)


def bucket_results(arrays, context, cuts):
    rows = []
    pool = arrays["stage_a_pool"]
    dates = arrays["meta"]["signal_date"]
    for feature in FEATURES:
        vals = np.asarray([
            np.nan if context.get(int(d), {}).get(feature) is None else context.get(int(d), {}).get(feature, np.nan)
            for d in dates
        ], dtype=float)
        bs = np.asarray([bucket(v, cuts[feature]) if np.isfinite(v) else 0 for v in vals], dtype=np.int8)
        for label, start, end in PERIODS:
            pm = period_mask(arrays["meta"], start, end)
            for b in range(1, 7):
                selected = pool & pm & (bs == b)
                m = metric_summary(arrays, selected)
                rows.append({"period": label, "feature": feature, "bucket": b, "lower": None if b == 1 else cuts[feature][b-2], "upper": None if b == 6 else cuts[feature][b-1], **m})
    return rows


def _daily_feature_outcomes(arrays, context, feature):
    meta = arrays["meta"]; pool = arrays["stage_a_pool"] & meta["outcome_evaluable"]
    rows = []
    for date in np.unique(meta["signal_date"][pool]):
        mask = pool & (meta["signal_date"] == date)
        if int(date) not in context or not np.any(mask):
            continue
        out = arrays["outcomes"][mask]
        paths = arrays["path_class"][mask]
        rows.append((int(date), context[int(date)].get(feature, np.nan), float(np.mean(paths == 2)), float(np.mean(out[:, OI["mae_10d"]]))))
    return rows


def define_score(arrays, context, cuts):
    definitions = []
    used_family = set()
    family = {f: "basis" if f.startswith("tx_basis") else "oi" if f.startswith("tx_total") else "pc_volume" if "volume_pc" in f else "pc_oi" for f in FEATURES}
    for feature in FEATURES:
        rows = [r for r in _daily_feature_outcomes(arrays, context, feature) if CFG.discovery_start <= r[0] <= CFG.discovery_end and r[1] is not None and np.isfinite(r[1])]
        low = [r for r in rows if r[1] < cuts[feature][2]]
        high = [r for r in rows if r[1] >= cuts[feature][2]]
        if not low or not high:
            continue
        low_down, high_down = np.mean([r[2] for r in low]), np.mean([r[2] for r in high])
        low_mae, high_mae = np.mean([r[3] for r in low]), np.mean([r[3] for r in high])
        adverse_high = high_down > low_down and high_mae < low_mae
        adverse_low = low_down > high_down and low_mae < high_mae
        fam = family[feature]
        if (adverse_high or adverse_low) and fam not in used_family:
            definitions.append({"feature": feature, "family": fam, "adverse_side": "HIGH" if adverse_high else "LOW", "median": cuts[feature][2], "discovery_downside_delta_high_minus_low": high_down-low_down, "discovery_mae_delta_high_minus_low": high_mae-low_mae})
            used_family.add(fam)
    return definitions


def score_by_date(context, definitions):
    result = {}
    for date, values in context.items():
        score = 0
        for d in definitions:
            value = values.get(d["feature"], np.nan)
            if value is not None and np.isfinite(value):
                score += int(value >= d["median"]) if d["adverse_side"] == "HIGH" else int(value < d["median"])
        result[date] = score
    return result


def gate_rows(arrays, scores):
    meta=arrays["meta"]; pool=arrays["stage_a_pool"]
    sv=np.asarray([scores.get(int(d),-1) for d in meta["signal_date"]])
    cohorts={"STAGE_A_TOP30":pool&(sv>=0),"LOW_RISK":pool&(sv==0),"MEDIUM_RISK":pool&(sv==1),"HIGH_RISK":pool&(sv>=CFG.high_risk_min_points),"GATE_RETAINED":pool&(sv>=0)&(sv<CFG.high_risk_min_points),"GATE_SKIPPED":pool&(sv>=CFG.high_risk_min_points)}
    rows=[]
    slices=[("PERIOD",label,start,end) for label,start,end in PERIODS]
    slices += [("YEAR",str(year),year*10000+101,year*10000+1231) for year in range(2020,2026)]
    for slice_type,label,start,end in slices:
        pm=period_mask(meta,start,end)
        base=metric_summary(arrays,pm&cohorts["STAGE_A_TOP30"])
        for name,mask in cohorts.items():
            m=metric_summary(arrays,pm&mask)
            m.update({"time_slice_type":slice_type,"period":label,"cohort":name,"retained_pct":m["observations"]/base["observations"] if base["observations"] else None})
            if name=="GATE_RETAINED":
                m.update({"delta_success":m["path_success_rate"]-base["path_success_rate"],"delta_downside_first":m["downside_first_rate"]-base["downside_first_rate"],"mae_improvement":m["mae10_mean"]-base["mae10_mean"],"mfe_retention":m["mfe10_mean"]/base["mfe10_mean"],"delta_net":m["net_mean"]-base["net_mean"],"delta_net_pf":m["net_profit_factor"]-base["net_profit_factor"]})
            rows.append(m)
    return rows, cohorts, sv


def _cluster_delta(arrays, selected, baseline, metric, cluster_kind, reps=5000):
    meta=arrays["meta"]; valid=meta["outcome_evaluable"]
    dates=meta["signal_date"]
    clusters=dates if cluster_kind=="signal_date" else dates//100
    if metric=="mae10": values=arrays["outcomes"][:,OI["mae_10d"]]
    elif metric=="net": values=arrays["outcomes"][:,OI["net_return"]]
    elif metric=="downside": values=(arrays["path_class"]==2).astype(float)
    else: values=(arrays["path_class"]==1).astype(float)
    usable=np.unique(clusters[baseline&valid])
    base_sum=[]; base_n=[]; selected_sum=[]; selected_n=[]
    for c in usable:
        b=values[baseline&valid&(clusters==c)]; a=values[selected&valid&(clusters==c)]
        base_sum.append(float(np.sum(b))); base_n.append(len(b))
        selected_sum.append(float(np.sum(a))); selected_n.append(len(a))
    if len(usable) == 0:return None,None
    base_sum=np.asarray(base_sum); base_n=np.asarray(base_n)
    selected_sum=np.asarray(selected_sum); selected_n=np.asarray(selected_n)
    rng=np.random.default_rng(CFG.bootstrap_seed + len(usable) + len(metric))
    sims=[]
    for _ in range(reps):
        ix=rng.integers(0,len(usable),len(usable))
        sn=int(np.sum(selected_n[ix])); bn=int(np.sum(base_n[ix]))
        if sn and bn:
            sims.append(float(np.sum(selected_sum[ix])/sn-np.sum(base_sum[ix])/bn))
    if not sims:return None,None
    sims=np.asarray(sims)
    return float(np.quantile(sims,.025)),float(np.quantile(sims,.975))


def bootstrap_rows(arrays, cohorts):
    rows=[]; meta=arrays["meta"]
    for label,start,end in PERIODS:
        pm=period_mask(meta,start,end); base=pm&cohorts["STAGE_A_TOP30"]; sel=pm&cohorts["GATE_RETAINED"]
        for ck in ("signal_date","calendar_month"):
            for metric in ("mae10","downside","success","net"):
                lo,hi=_cluster_delta(arrays,sel,base,metric,ck)
                rows.append({"period":label,"comparison":"GATE_RETAINED_MINUS_BASELINE","metric":metric,"cluster":ck,"reps":CFG.bootstrap_reps,"ci_low":lo,"ci_high":hi})
    return rows


def monotonic_rows(bucket_rows):
    rows=[]
    for period,_,_ in PERIODS:
        for feature in FEATURES:
            group=[r for r in bucket_rows if r["period"]==period and r["feature"]==feature and r["evaluable_observations"]]
            row={"period":period,"feature":feature}
            for field in ("path_success_rate","downside_first_rate","mae10_mean","mfe10_mean","net_mean"):
                x=np.asarray([r["bucket"] for r in group],dtype=float); y=np.asarray([r[field] for r in group],dtype=float)
                row[f"bucket_correlation_{field}"]=float(np.corrcoef(x,y)[0,1]) if len(x)>1 else None
            rows.append(row)
    return rows


def regime_rows(arrays, cohorts, regimes):
    groups={
        "0050_CLOSE_VS_MA20":(("ABOVE_OR_EQUAL",regimes["close_vs_ma20"]>=0),("BELOW",regimes["close_vs_ma20"]<0)),
        "0050_MA20_SLOPE5":(("RISING_OR_FLAT",regimes["ma20_slope5"]>=0),("FALLING",regimes["ma20_slope5"]<0)),
        "0050_VOLATILITY20":(("HIGH",regimes["volatility20"]>=regimes["volatility_discovery_median"]),("LOW",regimes["volatility20"]<regimes["volatility_discovery_median"])),
    }
    rows=[]
    for period,start,end in PERIODS:
        pm=period_mask(arrays["meta"],start,end)
        for dimension,values in groups.items():
            for group,mask in values:
                for cohort in ("STAGE_A_TOP30","GATE_RETAINED"):
                    rows.append({"period":period,"market_proxy":"0050","regime_dimension":dimension,"regime_group":group,"cohort":cohort,**metric_summary(arrays,pm&mask&cohorts[cohort])})
    return rows
