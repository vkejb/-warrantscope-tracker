from __future__ import annotations

import argparse, csv, hashlib, json
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

from cross_sectional_alpha_ranking_v01.data import load_market_regime
from .analysis import boundaries, bootstrap_rows, bucket_results, define_score, gate_rows, monotonic_rows, regime_rows, score_by_date
from .config import CFG, FEATURES, PERIODS
from .sources import acquire, build_context, sha256


ROOT=Path(__file__).resolve().parent
RUNTIME=ROOT/"runtime"
COND=ROOT.parent/"conditional_path_quality_ranking_v01"/"runtime"/"conditional_store.npz"
UP=ROOT.parent/"upside_opportunity_ranking_v01"/"runtime"/"ranking_store.npz"


def write_csv(path, rows):
    rows=list(rows); fields=[]
    for r in rows:
        for k in r:
            if k not in fields: fields.append(k)
    with path.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore",lineterminator="\n"); w.writeheader(); w.writerows(rows)


def load_arrays():
    if sha256(UP)!=CFG.expected_stage_a_store_sha256: raise RuntimeError("frozen Stage A store hash mismatch")
    if sha256(COND)!=CFG.expected_conditional_store_sha256: raise RuntimeError("frozen conditional store hash mismatch")
    z=np.load(COND,allow_pickle=False)
    arrays={k:z[k] for k in z.files}
    upstream=np.load(UP,allow_pickle=False)
    if not np.array_equal(arrays["meta"],upstream["meta"]): raise RuntimeError("frozen observation keys mismatch")
    if not np.array_equal(arrays["stage_a_ranks"],upstream["stage_a_ranks"]): raise RuntimeError("frozen Stage A ranks drifted")
    if not np.array_equal(arrays["stage_a_pool"],arrays["stage_a_ranks"]<=30): raise RuntimeError("Stage A Top30 mismatch")
    return arrays


def publish():
    guarded=[ROOT/x for x in ("validation_summary.json","run_manifest.json","feature_bucket_results.csv")]
    if any(x.exists() for x in guarded): raise RuntimeError("published outputs already exist; refusing overwrite")
    manifest=acquire(RUNTIME); context_rows=build_context(RUNTIME); context={int(r["date"]):r for r in context_rows}
    arrays=load_arrays(); cuts=boundaries(context)
    bucket_rows=bucket_results(arrays,context,cuts)
    definitions=define_score(arrays,context,cuts)
    scores=score_by_date(context,definitions)
    gate,cohorts,sv=gate_rows(arrays,scores)
    boots=bootstrap_rows(arrays,cohorts)
    monotonic=monotonic_rows(bucket_rows)
    regimes,regime_audit=load_market_regime(ROOT.parent,arrays["meta"]["signal_date"],ROOT.parent/"winner_coverage_taxonomy_v01"/"run_manifest.json")
    regime=regime_rows(arrays,cohorts,regimes)
    write_csv(ROOT/"feature_bucket_results.csv",bucket_rows)
    write_csv(ROOT/"context_gate_summary.csv",gate)
    write_csv(ROOT/"cluster_bootstrap_summary.csv",boots)
    write_csv(ROOT/"feature_monotonicity.csv",monotonic)
    write_csv(ROOT/"regime_diagnostics.csv",regime)
    (ROOT/"context_score_definition.json").write_text(json.dumps({"requires_two_features":True,"high_risk_min_points":CFG.high_risk_min_points,"definitions":definitions,"discovery_boundaries":cuts},ensure_ascii=False,indent=2)+"\n")
    # Phase-0 decisions are explicit and conservative.
    audit=[]
    for family, fields, source, decision, timing in [
        ("TX_TOTAL_OPEN_INTEREST",["total_open_interest"],"TAIFEX annual futures daily market ZIP","PIT_USABLE","official regular-session closing file; usable only for T+1 entry after T close"),
        ("TXO_PUT_CALL",["volume_pc","oi_pc"],"TAIFEX annual options daily market ZIP","PIT_USABLE","computed from official regular-session TXO closing rows; usable only for T+1 entry after T close"),
        ("TX_NEAR_MONTH_BASIS",["basis_pct","basis_changes"],"TAIFEX futures ZIP + TWSE TAIEX historical endpoint","NOT_TESTED_DATA_UNAVAILABLE","TWSE public endpoint did not yield a complete reproducible 2020-2025 archive; no third-party substitute used"),
        ("FOREIGN_TX_OI",["net_oi","normalized_net_oi","changes"],"TAIFEX major institutional trader report","NOT_TESTED_DATA_UNAVAILABLE","free official historical query retains only recent years; no complete 2020-2022 discovery coverage"),
    ]:
        source_url = "https://www.taifex.com.tw/cht/3/dlFutDailyMarketView" if family.startswith("TX_") else "https://www.taifex.com.tw/cht/3/dlOptDailyMarketView" if family=="TXO_PUT_CALL" else "https://www.taifex.com.tw/cht/3/futContractsDate"
        audit.append({"feature_family":family,"raw_fields":"|".join(fields),"source":source,"source_url":source_url,"official":True,"earliest_required":"2020-01-01","latest_required":"2025-12-31","publication_timing":timing,"usable_for_t_plus_1":decision=="PIT_USABLE","required_lag_sessions":0 if decision=="PIT_USABLE" else None,"decision":decision})
    write_csv(ROOT/"derivatives_data_availability_audit.csv",audit)
    dates={int(x) for x in arrays["meta"]["signal_date"][arrays["stage_a_pool"]]}
    available=dates&set(context); missing=sorted(dates-set(context))
    period_labels={x[0] for x in PERIODS}
    later=[r for r in gate if r["cohort"]=="GATE_RETAINED" and r["period"] in period_labels and r["period"]!="HISTORICAL_DISCOVERY"]
    high=[r for r in gate if r["cohort"]=="HIGH_RISK"]
    base=[r for r in gate if r["cohort"]=="STAGE_A_TOP30"]
    high_worse=[]
    for p in [x[0] for x in PERIODS]:
        h=next((x for x in high if x["period"]==p),None); b=next((x for x in base if x["period"]==p),None)
        high_worse.append(bool(h and b and (h["mae10_mean"]<b["mae10_mean"] or h["downside_first_rate"]>b["downside_first_rate"])))
    gate_down=all(r.get("mae_improvement",-1)>0 and r.get("delta_downside_first",1)<0 for r in later)
    tradeable=all(r.get("net_mean",-1)>0 and r.get("net_profit_factor",0)>1 and r.get("mfe_retention",0)>=.8 for r in later)
    ci_support=any(r["ci_high"] is not None and ((r["metric"]=="mae10" and r["ci_low"]>0) or (r["metric"]=="downside" and r["ci_high"]<0)) for r in boots if r["period"]!="HISTORICAL_DISCOVERY")
    if len(definitions)<2 or not all(high_worse): classification="NO_DERIVATIVES_CONTEXT_EDGE"
    elif gate_down and tradeable and ci_support: classification="DERIVATIVES_CONTEXT_DOWNSIDE_EDGE_FOUND"
    elif gate_down and ci_support: classification="DERIVATIVES_CONTEXT_NOT_TRADEABLE"
    else: classification="NO_DERIVATIVES_CONTEXT_EDGE"
    validation={"study_id":CFG.study_id,"status":"COMPLETE","classification":classification,"phase0_pit_audit":"PASS_WITH_UNAVAILABLE_FAMILIES","frozen_stage_a_exact_reuse":True,"stage_a_refit_count":0,"conditional_refit_count":0,"context_features":list(FEATURES),"context_score_feature_count":len(definitions),"stage_a_signal_dates":len(dates),"context_covered_signal_dates":len(available),"missing_signal_dates":missing,"coverage_pct":len(available)/len(dates),"actual_orders":0,"actual_fills":0,"broker_connections":0}
    (ROOT/"validation_summary.json").write_text(json.dumps(validation,ensure_ascii=False,indent=2)+"\n")
    published=["derivatives_data_availability_audit.csv","feature_bucket_results.csv","feature_monotonicity.csv","context_score_definition.json","context_gate_summary.csv","cluster_bootstrap_summary.csv","regime_diagnostics.csv","validation_summary.json"]
    run={"study_id":CFG.study_id,"published_at_utc":datetime.now(timezone.utc).isoformat(),"config_fingerprint":CFG.fingerprint(),"frozen_stage_a_store":{"path":str(UP),"sha256":sha256(UP)},"conditional_store":{"path":str(COND),"sha256":sha256(COND)},"source_manifest_sha256":sha256(RUNTIME/"source_manifest.json"),"context_store_sha256":sha256(RUNTIME/"derivatives_context_daily.csv"),"regime_audit":regime_audit,"artifacts":{p:sha256(ROOT/p) for p in published},"formal_publish_count":1,"safety":{"actual_orders":0,"actual_fills":0,"broker_connections":0}}
    (ROOT/"run_manifest.json").write_text(json.dumps(run,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(validation,ensure_ascii=False,indent=2))


if __name__=="__main__":
    p=argparse.ArgumentParser(); p.add_argument("command",choices=("publish",)); a=p.parse_args()
    publish()
