from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from cross_sectional_alpha_ranking_v01.data import load_market_regime
from sector_rotation_incremental_study_v01.data import store_digest

from .analysis import (
    acceleration_diagnostics, bootstrap_rows, build_level_acceleration_indices,
    classify, early_vs_mature_rows, feature_bucket_rows, frozen_boundaries,
    lead_time_rows, level_acceleration_rows, make_cohorts, pool_rows, regime_rows,
    select_composite_families, tail_rows,
)
from .config import CFG
from .data import (
    build_acceleration_store, load_acceleration_store, load_frozen_arrays,
    load_ohlcv, load_store, prepare_stocks, protected_hashes, save_store, sha256_file,
)


ROOT=Path(__file__).resolve().parent
STOCK=ROOT.parent
RUNTIME=ROOT/"runtime"
STAGE=STOCK/"upside_opportunity_ranking_v01"/"runtime"/"ranking_store.npz"
COND=STOCK/"conditional_path_quality_ranking_v01"/"runtime"/"conditional_store.npz"
PEER=STOCK/"sector_rotation_incremental_study_v01"/"runtime"/"dynamic_peer_store.npz"
SECTOR_MANIFEST=STOCK/"sector_rotation_incremental_study_v01"/"run_manifest.json"
WINNER_MANIFEST=STOCK/"winner_coverage_taxonomy_v01"/"run_manifest.json"
OUTPUTS=("validation_summary.json","feature_bucket_results.csv","acceleration_feature_summary.csv","level_acceleration_matrix.csv","early_vs_mature_summary.csv","pool_reduction_summary.csv","cluster_bootstrap_summary.csv","tail_removal_summary.csv","lead_time_diagnostics.csv","regime_diagnostics.csv","run_manifest.json")


def write_csv(path,rows):
    rows=list(rows)
    if not rows:raise RuntimeError(f"refusing empty output: {path.name}")
    fields=[]
    for row in rows:
        for key in row:
            if key not in fields:fields.append(key)
    with path.open("w",encoding="utf-8-sig",newline="") as handle:
        writer=csv.DictWriter(handle,fieldnames=fields,lineterminator="\n");writer.writeheader();writer.writerows(rows)


def write_json(path,payload):
    path.write_text(json.dumps(payload,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+"\n",encoding="utf-8")


def tree_hash(root):
    digest=hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and "runtime" not in path.parts:
            digest.update(str(path.relative_to(root)).encode());digest.update(path.read_bytes())
    return digest.hexdigest()


def input_sources():
    manifest=json.loads(WINNER_MANIFEST.read_text());archives=[];supplements=[];hashes=[]
    for item in manifest["input_hashes"]:
        raw=Path(item["path"]);path=raw if raw.is_absolute() else STOCK/raw
        if not (path.name.startswith("yearly_20") or path.name=="twse_price_supplement.csv"):continue
        if path.name.startswith("yearly_2026"):continue
        actual=sha256_file(path)
        if actual!=item["sha256"]:raise RuntimeError(f"OHLCV source drifted: {path}")
        (archives if path.suffix==".zip" else supplements).append(path)
        hashes.append({"path":str(path.relative_to(STOCK)),"sha256":actual,"bytes":path.stat().st_size})
    if len(archives)!=7:raise RuntimeError("expected exact yearly 2019-2025 sources")
    return archives,supplements,hashes


def publish():
    existing=[name for name in OUTPUTS if (ROOT/name).exists()]
    if existing:raise RuntimeError("published outputs already exist; refusing overwrite: "+", ".join(existing))
    if sha256_file(PEER)!=CFG.expected_peer_store_sha256:raise RuntimeError("frozen dynamic peer store hash drifted")
    sector_manifest=json.loads(SECTOR_MANIFEST.read_text())
    if sector_manifest["peer_store_content_digest"]!=CFG.expected_peer_content_digest:raise RuntimeError("frozen peer content digest drifted")
    before=protected_hashes(STOCK); sector_before=tree_hash(STOCK/"sector_rotation_incremental_study_v01")
    arrays,frozen_audit=load_frozen_arrays(STAGE,COND);peer_store,peer_audit=load_store(PEER)
    if store_digest(peer_store)!=CFG.expected_peer_content_digest:raise RuntimeError("peer store content verification failed")
    archives,supplements,input_hashes=input_sources()
    stocks,benchmark_bars,load_audit=load_ohlcv(archives,supplement_paths=supplements)
    prepared,benchmark,prepare_audit=prepare_stocks(stocks,benchmark_bars)
    RUNTIME.mkdir(exist_ok=True); local=RUNTIME/"acceleration_store.npz"
    if local.exists():
        store,accel_audit=load_acceleration_store(local)
    else:
        store,accel_audit=build_acceleration_store(arrays,peer_store,prepared,benchmark);save_store(local,store,accel_audit)
    cuts=frozen_boundaries(arrays,store);buckets=feature_bucket_rows(arrays,store,cuts);feature_summary=acceleration_diagnostics(buckets)
    indices=build_level_acceleration_indices(arrays,store);matrix=level_acceleration_rows(arrays,store,indices)
    definitions,composite_audit=select_composite_families(buckets,cuts);cohorts,early_score,early_ranks=make_cohorts(arrays,store,indices,definitions)
    pools=pool_rows(arrays,cohorts,composite_audit["eligible"]);comparison=early_vs_mature_rows(arrays,cohorts);boots=bootstrap_rows(arrays,cohorts);tails=tail_rows(arrays,cohorts);lead=lead_time_rows(arrays,store,indices,cohorts)
    regimes,regime_audit=load_market_regime(STOCK,arrays["meta"]["signal_date"],WINNER_MANIFEST);regime=regime_rows(arrays,cohorts,regimes)
    classification=classify(comparison,composite_audit["eligible"])
    write_csv(ROOT/"feature_bucket_results.csv",buckets);write_csv(ROOT/"acceleration_feature_summary.csv",feature_summary);write_csv(ROOT/"level_acceleration_matrix.csv",matrix);write_csv(ROOT/"early_vs_mature_summary.csv",comparison);write_csv(ROOT/"pool_reduction_summary.csv",pools);write_csv(ROOT/"cluster_bootstrap_summary.csv",boots);write_csv(ROOT/"tail_removal_summary.csv",tails);write_csv(ROOT/"lead_time_diagnostics.csv",lead);write_csv(ROOT/"regime_diagnostics.csv",regime)
    after=protected_hashes(STOCK);sector_after=tree_hash(STOCK/"sector_rotation_incremental_study_v01")
    failures=[]
    if before!=after:failures.append("protected_artifact_changed")
    if sector_before!=sector_after:failures.append("frozen_sector_study_changed")
    if accel_audit["stage_a_refit_count"]!=0:failures.append("stage_a_refit")
    validation={"study_id":CFG.study_id,"status":"COMPLETE" if not failures else "FAILED","classification":classification if not failures else "NO_EARLY_ROTATION_EDGE","failures":failures,"official_sector_status":"OFFICIAL_SECTOR_PIT_UNSAFE_INHERITED_NOT_RETESTED","stage_a_refit_count":0,"later_period_refit_count":0,"dynamic_peer_refit_count":0,"dynamic_peer_coverage":accel_audit["coverage_pct"],"early_rotation_composite_eligible":composite_audit["eligible"],"selected_acceleration_families":len(definitions),"discovery_supporting_acceleration_families":composite_audit["discovery_supporting_family_count"],"checks":{"frozen_stage_a_exact_reuse":True,"frozen_dynamic_peer_exact_reuse":True,"all_primary_features_t_or_earlier":True,"future_data_only_in_lead_time_diagnostic":True,"leave_one_out":True,"later_period_threshold_selection":False,"protected_artifacts_unchanged":before==after and sector_before==sector_after,"actual_orders":0,"actual_fills":0,"broker_connections":0}}
    write_json(ROOT/"validation_summary.json",validation)
    if failures:raise RuntimeError("validation failed: "+", ".join(failures))
    published=[name for name in OUTPUTS if name not in ("run_manifest.json",)]
    manifest={"study_id":CFG.study_id,"published_at_utc":datetime.now(timezone.utc).isoformat(),"formal_publish_count":1,"config":CFG.snapshot(),"config_fingerprint":CFG.fingerprint(),"frozen_inputs":frozen_audit,"frozen_peer_store":{"path":str(PEER.relative_to(STOCK)),"sha256":sha256_file(PEER),"content_digest":store_digest(peer_store),"audit":peer_audit},"input_hashes":input_hashes,"load_audit":load_audit,"prepare_audit":prepare_audit,"acceleration_audit":accel_audit,"acceleration_store_sha256":sha256_file(local),"discovery_feature_boundaries":cuts,"level_boundaries":indices["level_cuts"],"acceleration_boundaries":indices["acceleration_cuts"],"early_rotation_definition":{"level":"LOW_OR_MID","acceleration":"HIGH","level_components":list(indices["level_refs"]),"acceleration_components":list(indices["acceleration_refs"])},"composite_audit":composite_audit,"composite_definitions":definitions,"regime_audit":regime_audit,"artifacts":{name:sha256_file(ROOT/name) for name in published},"source_hashes":{name:sha256_file(ROOT/name) for name in ("config.py","data.py","analysis.py","main.py")},"documentation_sha256":sha256_file(ROOT/"README.md"),"safety":{"actual_orders":0,"actual_fills":0,"broker_connections":0}}
    write_json(ROOT/"run_manifest.json",manifest);print(json.dumps(validation,ensure_ascii=False,indent=2))


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("command",choices=("publish",));parser.parse_args();publish()
