"""Combine completed evidence/arithmetic audits; never calculates new trades."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
from .isolation import HERE,ROOT,MODULE,R2,digest,dump
from .forward import verify_profile

def load(path):return json.loads(Path(path).read_text())

def derived_once(path,value):
    # Resume an interrupted presentation assembly only when its already-written
    # derived bytes decode to exactly the same value; never replace a difference.
    if path.exists():
        if load(path)!=value:raise ValueError('DIFFERENT_DERIVED_ARTIFACT_ALREADY_EXISTS:'+path.name)
        return
    dump(path,value)

def unchanged(snapshot):
    mappings=[snapshot['protected_old_artifact_hashes'],snapshot['old_specs_and_freezes'],snapshot['dirty_file_sha256']]
    failures=[n for m in mappings for n,h in m.items() if not (ROOT/n).is_file() or digest(ROOT/n)!=h]
    if failures:raise ValueError('PRESERVED_INPUT_CHANGED:'+','.join(failures))
    return sum(map(len,mappings))

def requirements():
    fields=[
      ('CANDIDATES','Historical preopen Stage A Top30, rank, created_at, model/config/input/seal SHA','REQUIRED','Post-close reconstruction forbidden'),
      ('TICKS','Raw DealPrice/DealVol/InOutFlag/SerialNo, exchange and receipt clocks, raw unit/semantic','REQUIRED_FOR_FLOW','Native units remain UNKNOWN until version-matched official proof'),
      ('BOOK_BID','All five bid prices/volumes and original native values','REQUIRED_FOR_ENTRY','Do not reuse stale or already consumed capacity'),
      ('BOOK_ASK','All five ask prices/volumes and original native values','REQUIRED_FOR_COVER','No quote means no invented cover fill'),
      ('CLOCKS','Exchange/receipt/write clocks, stream and cross-channel sequence, reconnect/halt events','PARTIAL_HISTORICAL','Receipt cannot repair absent exchange clock'),
      ('0050','Synchronous raw ticks, full opening warmup, received VWAP and 300s endpoint','REQUIRED_FOR_R2_R5','Missing means UNAVAILABLE; own/common scopes kept separate'),
      ('ELIGIBILITY','Effective-dated short-first/suspension/disposal/halt and availability-at-decision evidence','UNKNOWN_REMAINS_CONDITIONAL','Exchange report is not broker account entitlement'),
      ('FEES','Actual account commission, minimum/per-order granularity, rounding and taxes','ACTUAL_UNKNOWN','Proxy unchanged; evidence correction requires a new common data version'),
      ('LIMITS','Reference/open/upper/lower/tick size and applicable session rules','NOT_FULLY_CERTIFIED','Inherited conditional model is not strict execution'),
      ('EXECUTION_DELAY','Signal/submit/ack/fill clocks, queue and capacity evidence','ACTUAL_UNKNOWN','Quote latency is not order latency'),
      ('PROVENANCE','Canonical run/stream hashes, complete coverage and callback error logs','REQUIRED','No latest aliases or mixed checkouts'),
      ('UNSEEN','New day after local freeze, explicit unseen-outcome attestation body+SHA','REQUIRED','Program cannot independently prove what humans saw elsewhere'),
    ]
    return [dict(requirement_id=i,fields=f,status=s,limitation=l) for i,f,s,l in fields]

def strict_gates():
    unknowns=[('DEALVOL_UNIT','UNKNOWN'),('BID_DEPTH_UNIT','UNKNOWN'),('ASK_DEPTH_UNIT','UNKNOWN'),
      ('DEALVOL_SINGLE_MATCH_SEMANTIC','PARTIAL_NOT_CERTIFIED'),('HISTORICAL_SDK_SOURCE_BINDING','UNKNOWN'),
      ('BOOK_EXCHANGE_TIME','HISTORICALLY_ABSENT_NOT_RECOVERABLE'),('CROSS_CHANNEL_SEQUENCE','HISTORICALLY_ABSENT'),
      ('ACTUAL_COMMISSION_MINIMUM_ROUNDING','UNKNOWN'),('ACTUAL_ORDER_LATENCY_QUEUE','UNKNOWN'),
      ('HISTORICAL_RESTRICTION_PIT','RETROSPECTIVE_NEGATIVE_EVIDENCE_ONLY'),('BROKER_ENTITLEMENT','UNKNOWN'),
      ('STATUTORY_DAYTRADE_TAX','CONDITIONAL_RATE_SUPPORTED_NOT_ACCOUNT_APPLICATION')]
    return [dict(gate=g,status=s,strict_pass=False,strict_executable=None,
      interpretation='Individual evidence is not aggregate executable certification') for g,s in unknowns]

def assemble(output,isolated):
    output=Path(output);snapshot=load(output/'session_snapshot.json')
    preserved=unchanged(snapshot)
    profile,iso=verify_profile(HERE/'forward_freeze.json',isolated)
    evidence=load(output/'evidence_tables.json');elig=load(output/'eligibility_tables.json');account=load(output/'account_tables.json')
    tables={**account['tables'],**elig['tables']}
    for name in ('evidence_audit','raw_stream_schema_audit','source_manifest_schema_audit','evidence_daily_coverage','provenance_schema_examples'):
        tables[name]=evidence[name]
    tables['evidence_sources']=[{'source_id':k,**v} for k,v in evidence['sources'].items()]
    tables['strict_gate_status']=strict_gates();tables['forward_data_requirements']=requirements()
    tables['forward_versions']=[{'variant':v,'version':profile['version'],'variant_hash':h,
      'profile_hash':profile['profile_hash'],'frozen_at':profile['frozen_at'],'exit_model':'E0',
      'status':'PENDING_NO_NEW_UNSEEN_ARCHIVED_DATE','actual_orders':0} for v,h in profile['variant_hashes'].items()]
    # Append D as a separate evidence item; retain detailed candidate/date tables.
    revision=elig['restriction_data_revision']
    tables['evidence_audit'].append({'item_id':'D01_HISTORICAL_RESTRICTIONS','category':'QUALIFICATION',
      'existing_assumption':'Incomplete original historical restrictions; unknown not legal.',
      'actual_evidence':f"New negative candidate-days {revision['new_negative_candidate_stock_days']}; recorded signal/trade intersections {revision['all_observed_round1_round2_candidate_and_trade_intersections']}.",
      'confirmation_level':'RETROSPECTIVE_NEGATIVE_EVENTS_VERIFIED_NOT_ORIGINAL_PIT_OR_BROKER_CERTIFICATION',
      'affected_scope':'All recorded Round 1/2 branches joined; see restriction_impact_checks.',
      'correction_or_next_evidence':'Retain negative-mask evidence version; do not rerun identical zero-intersection performance. Original acquisition time and broker entitlement remain UNKNOWN.',
      'source_ids':['eligibility_tables.json','public_evidence_inventory'],'model_parameter_change_supported':False})
    comparisons=[]
    for version,path in [('ROUND1',MODULE/'results_round1_v2/tables.json'),('ROUND2',R2/'results_round2_v1/tables.json')]:
        old=load(path)
        for source_index,r in enumerate(old['experiments']):
            if version=='ROUND2' and r.get('statistics_scope')!='FULL_PERIOD':continue
            comparisons.append({'research_version':version,'source_experiment_row_index':source_index,
              'source_array':'experiments','row_index_basis':'ZERO_BASED_ORIGINAL_JSON_ARRAY',
              **{k:r.get(k) for k in ('experiment','scenario','phase','scope','variant','study_status',
                                     'statistics_scope','data_splits','split','exit_model','short_entry','selection_status')},
              'original_net_pnl':r.get('net_pnl'),'reused_net_pnl':r.get('net_pnl'),
              'numerical_model_change':False,'new_restriction_signal_intersections':0,
              'method':'ORIGINAL_RESULT_REUSE_ZERO_RESTRICTION_INTERSECTION_NOT_NEW_BACKTEST',
              'strict_executable':None,'source_tables_sha256':digest(path)})
    tables['correction_impact_comparison']=comparisons
    pending={'status':'PENDING_NO_NEW_DATE_ARCHIVE','forward_freeze_at':profile['frozen_at'],
      'latest_archived_day':elig['new_qualifying_date_validation']['last_archive_day'],
      'archive_run_count':len(tables['archive_manifest_inventory']),'new_qualifying_date_count':0,
      'new_date_signals_or_performance_read':False,'evaluate_invocations':0,
      'old_dates_are_all_known':True,'automatic_schedules':0,'actual_orders':0,
      'batch_limitation':'One manifest is one continuous 190000-TWD account cohort. No cross-batch resume; do not merge independent batches into one continuous paper account.'}
    tables['forward_status']=[pending]
    derived_once(output/'tables.json',tables);derived_once(output/'forward_status.json',pending)
    dump(output/'run_manifest.json',{'research_id':'CASH_SHORT_EVIDENCE_ACCOUNT_FORWARD_FOLLOWUP_V01',
      'completed_at':datetime.now(timezone.utc).isoformat(),'initial_HEAD':snapshot['HEAD'],
      'old_research_source_commit':profile['original_source_commit'],'forward_profile_hash':profile['profile_hash'],
      'forward_variant_hashes':profile['variant_hashes'],'forward_freeze_sha256':digest(HERE/'forward_freeze.json'),
      'tables_sha256':digest(output/'tables.json'),'input_audit_hashes':{n:digest(output/n) for n in
         ['evidence_tables.json','eligibility_tables.json','account_tables.json','session_snapshot.json','isolation_receipt.json','forward_review.json']},
      'old_artifact_and_user_file_hashes_checked':preserved,'first_and_second_round_unchanged':True,
      'numerical_model_correction_required':False,'new_negative_restriction_mask':revision,
      'strategy_replay_count':0,'prospective_validation':'PENDING','STRICT_EXECUTABLE':None,
      'research_scope':'EVIDENCE_AND_ACCOUNT_ARITHMETIC_NOT_PARAMETER_SEARCH',
      'actual_orders':0,'broker_connections':0,'market_subscriptions':0,
      'authoring_note':'Audit source privacy/presentation changes and explicit original row/split context in reused-result references do not change original JSON, market scans or account arithmetic. Pre-label followup draft retained in authoring_history. Published helper is the final authoring version.'})
    report(output,tables,evidence,elig,account,profile,preserved)
    return len(tables)

def report(output,tables,evidence,elig,account,profile,preserved):
    ticks=sum(r['tick_rows'] for r in evidence['evidence_daily_coverage'])
    books=sum(r['book_rows'] for r in evidence['evidence_daily_coverage'])
    revision=elig['restriction_data_revision'];gaps=tables['account_gap_summary']
    base=[r for r in gaps if r['scenario']=='BASE_ASSUMED_LOTS']
    lines=['# 做空研究：資料證據、出場帳戶對帳與前瞻準備','',
      '## 結論','',
      f"本輪沒有新增策略條件／參數、沒有重跑相同績效。只讀掃描{len(evidence['evidence_daily_coverage'])}日、{len(evidence['raw_stream_schema_audit'])}個凍結串流、{ticks+books:,}筆（ticks {ticks:,}、books {books:,}）。查證提高可追溯性，不足以認證可成交績效。STRICT_EXECUTABLE仍NA。",'',
      f"歷史限制新增{revision['new_negative_candidate_stock_days']}候選股日負面證據；全部舊訊號／交易交集為{revision['all_observed_round1_round2_candidate_and_trade_intersections']}，故提供全組既有結果重用與零影響證明，而不是製造新的修正版回測。詳細每股／日及官方receipt見HISTORICAL_ELIGIBILITY.md與eligibility_candidates。",'',
      'E1/E2出場差距已完整對回：不是用「資金路徑不同」概括。未見這次差額的費稅、股數守恆或損益加總缺陷；有註解／provenance標籤可讀性缺陷，詳下。','',
      '|出場|固定進場改善|完整帳戶改善|帳戶減配對|未解釋殘差|',
      '|---|---:|---:|---:|---:|']
    lines.extend(f"|{r['variant']}|{r['fixed_increment']}|{r['account_increment']}|{r['account_minus_fixed_increment']}|{r['unexplained_residual']}|" for r in base)
    lines+=['','差額來自早退改善前期權益後，完整帳戶多做一筆原E0無法負擔的交易。第一分歧、資金不足與風險不足的分離、requested與filled差異、所有後續事件及費用，見ACCOUNT_DIVERGENCE_REPORT.md及account_*表。預算／限價／風險是原碼＋因果五檔重建，不冒充原始sizing ledger。','',
      '## 資料可信度提高了什麼','',
      '- 已追溯SDK原欄位、collector原值、normalizer float及成交層的情境乘數；新sidecar保留raw value/unit/semantic/basis，未知不自動換算，也防重複轉換。台股DealVol及五檔買／賣量股張仍UNKNOWN。',
      '- SDK目前版本可辨識，但歷史manifest未綁SDK/DLL／collector source SHA；不宣稱歷史SDK環境已恢復。可觀測量下降只排除整日單調累積，不足認證每callback單一撮合。',
      '- 實際費率補查有明確專案LaunchAgent→runtime證據，只讀含WAL帳本且不輸出帳號或payload；沒有可驗證實際費用欄，最低費／計費粒度／取整仍UNKNOWN。法定合格當沖稅率情境有官方支持且與原設定同值，不代表每筆合格或帳戶代徵已證。',
      '- 歷史五檔交易所時間、獨立寫檔時鐘及跨通道序號未封存，不能補回或用receipt冒充；callback空窗不是斷線證明，行情延遲不是委託延遲。',
      '- 新查到的歷史限制有事件生效期間與本次取得時間，僅負面排除；當時系統是否已取得、未知日是否合法與券商授權仍UNKNOWN。重新查同一歷史日期的HTTP內容也可能修訂，差異已保留。','',
      '以上沒有主觀可信度百分比；strict_gate_status逐項列尚未過關的證據，不能因稅率或任一項確認就整體升級。','',
      '## 缺陷與重跑決策','',
      '- DOC_COMMENT_MISMATCH：舊sizing註解說另預留來回費及風險額度，實際是本金＋進場費受cap約束、結構風險另受3500約束。本輪準確描述實作，不改公式、不把註解落差包裝成優化。',
      '- CACHED_METADATA_LABEL_AMBIGUITY：部分帳戶交易帶固定cohort快取來源標籤，原ledger未改。本次用phase、逐候選決策、requested parity及逐列fills對帳釐清，不誤認強制數量。',
      '- 未發現需修正交易數值的已證實模型缺陷。新增資格負面遮罩對所有已記錄分支零交集；correction_impact_comparison明示原結果重用，不冒稱重跑。若未來證實量單位或費率改變，需新資料版本並共同重跑全部受影響組。','',
      '## 固定後續版本與新日期','',
      f"forward freeze={profile['frozen_at']}；profile hash={profile['profile_hash']}。只保留R0/R2/R5＋E0及空手，不新增與E1/E2交叉組合。原10筆／5交易日／正淨利門檻不降低，達標仍非可上線證據。",'',
      '|版本|固定版本hash|','|---|---|']
    lines.extend(f'|{v}|{h}|' for v,h in profile['variant_hashes'].items())
    lines+=['',f"封存盤點{len(tables['archive_manifest_inventory'])}個run，最後日期{tables['forward_status'][0]['latest_archived_day']}，無符合本輪凍結後的新日期；前瞻PENDING，未讀新日期訊號／損益、未執行evaluate、未新增排程。",'',
      '原13日全部已知探索資料，不能更名樣本外。後續只接受當地凍結日之後的新日及未看結果attestation；程式無法獨立證明人在別處未看過。0050缺資料保持UNAVAILABLE、COMMON只取R0/R2/R5可評估交集，保留無訊號及虧損日。整批資料不足先阻擋、不填0。',
      '同一manifest內跨日資金及剩倉carry；每次evaluate為一個新190000元cohort，未做跨batch resume。不同批次不可合併冒稱連續paper帳戶，尤其前批有剩倉時。需在未查看結果前預先劃定一段完整連續日期的manifest。',
      'R2/R5目前僅保留第二輪已知樣本現象：成本敏感、交易少、單日集中，沒有新增獲利證據。舊結果及失敗分支保留，不把跨版本重複交易算成多份獨立證據。新LONG改版不在本研究，不套用舊A/C/D；共用資金比較需另凍結同版本。','',
      '## 保存與交付','',
      f'已保存初始HEAD與4個未提交使用者檔案hash；舊結果／SPEC／freeze及使用者檔案共{preserved}項核對一致。隔離副本恢復實際source bodies並匹配258pins，不是只找到hash；歷史SDK程序／OS／網路環境未完整重建。',
      '完整重現命令、資料需求、無覆寫CLI及版本見README.md／FORWARD_SPEC.md。新CSV保留精度、時區／微秒、NA與0差別，並逐欄核對JSON。私下ZIP只含白名單原碼／證據／結果，不含帳戶DB、raw行情、憑證、帳號、Git或SDK二進位；GitHub只推研究程式。','']
    dump_path=output/'FOLLOWUP_REPORT.md'
    if dump_path.exists():raise FileExistsError('Preserve completed report')
    dump_path.write_text('\n'.join(lines))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--isolated-root',type=Path,required=True);a=p.parse_args()
    print(json.dumps({'csv_tables_expected':assemble(a.output,a.isolated_root)}))
