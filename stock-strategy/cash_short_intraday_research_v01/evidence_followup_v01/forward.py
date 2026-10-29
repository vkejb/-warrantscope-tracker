"""Frozen, offline-only forward validation. No SDK, network or live entrypoint.

The worker imports only an independently restored original research tree, never
the active checkout's changed LONG. Outputs must be fresh. Missing evidence is
reported and blocks the batch rather than being turned into a no-signal day.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
R2=HERE.parent/'round2'
TZ=ZoneInfo('Asia/Taipei')
VARIANTS=('R0','R2','R5')
SAFETY={'mode':'SHADOW_ONLY','NO_LIVE_ORDER':True,'actual_orders':0,
        'broker_connections':0,'market_subscriptions':0}

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for b in iter(lambda:stream.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def canonical(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def load(path):return json.loads(Path(path).read_text())

def dump(path,value):
    path=Path(path)
    if path.exists():raise FileExistsError('Never overwrite a preserved artifact')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False,
        default=lambda v:v.isoformat() if isinstance(v,datetime) else str(v))+'\n')

def verify_isolated(profile,isolated):
    isolated=Path(isolated).resolve()
    if isolated==ROOT:raise ValueError('ACTIVE_CHECKOUT_NOT_AN_ISOLATED_COPY')
    old=isolated/'stock-strategy/cash_short_intraday_research_v01/round2/freeze.json'
    if not old.is_file() or sha(old)!=profile['original_freeze_sha256']:raise ValueError('ORIGINAL_FREEZE_BODY_MISSING')
    original=load(old)
    if original['files']!=profile['original_pins']:raise ValueError('ORIGINAL_PIN_SET_INCOMPLETE_OR_CHANGED')
    if original.get('source_commit')!=profile.get('original_source_commit'):raise ValueError('ORIGINAL_SOURCE_COMMIT_CHANGED')
    failures=[n for n,h in profile['original_pins'].items()
              if not (isolated/n).is_file() or sha(isolated/n)!=h]
    if failures:raise ValueError('PINNED_SOURCE_BODY_OR_INPUT_MISSING_OR_CHANGED:'+','.join(failures))
    return isolated

def config_contract(cfg,original,round2):
    pairs=[('capital_twd','capital_twd'),('risk_per_trade_twd','risk_per_trade_twd'),
           ('maximum_trades_per_day','maximum_trades_per_day'),('maximum_positions','maximum_positions'),
           ('board_lot_shares','board_lot_shares'),('market_filter','market_filter')]
    for new,old in pairs:
        if cfg.get(new)!=original.get(old):raise ValueError('DECLARED_AND_EXECUTED_CONFIG_DIFFER:'+new)
    for key in ('rs_300_max','selection_minimum_closed','selection_minimum_distinct_days','selection_requires_positive_net'):
        if cfg.get(key)!=round2.get(key):raise ValueError('DECLARED_AND_EXECUTED_CONFIG_DIFFER:'+key)
    if cfg['rs_300_max']!=-0.002:raise ValueError('FROZEN_RS_CONSTANT_MISMATCH')
    if cfg['scenarios']!=[s['name'] for s in original['stress_scenarios']]:raise ValueError('SCENARIOS_CHANGED')
    if cfg['variants']!=list(VARIANTS) or cfg['exit_model']!='E0':raise ValueError('FORBIDDEN_BRANCH')

def freeze(isolated,output):
    cfg=load(HERE/'forward_config.json');old=load(R2/'freeze.json')
    config_contract(cfg,load(HERE.parent/'config.json'),load(R2/'config.json'))
    profile={**SAFETY,'version':cfg['version'],'frozen_at':datetime.now(timezone.utc).isoformat(),
      'config':cfg,'original_pins':old['files'],'original_freeze_sha256':sha(R2/'freeze.json'),
      'original_source_commit':old['source_commit'],'research_scope':'SHORT_ONLY_NOT_NEW_LONG',
      'authored_files':{str(p.relative_to(ROOT)):sha(p) for p in
        [HERE/'forward.py',HERE/'forward_config.json',HERE/'FORWARD_SPEC.md']},
      'known_exploratory_dates':load(R2/'config.json')['conditional_dates'],
      'unseen_status':'REQUIRES_NEW_DATE_AND_EXTERNAL_ATTESTATION_NOT_AUTOMATIC_PROOF',
      'STRICT_EXECUTABLE':None}
    verify_isolated(profile,isolated)
    deps={n:h for n,h in old['files'].items() if n.endswith('.py') or n.endswith('config.json')}
    profile['variant_hashes']={v:canonical({'variant':v,'config':cfg,'dependencies':deps}) for v in VARIANTS}
    profile['profile_hash']=canonical(profile)
    dump(output,profile);return profile

def verify_profile(path,isolated):
    p=load(path);expected=p.get('profile_hash');bare={k:v for k,v in p.items() if k!='profile_hash'}
    if canonical(bare)!=expected:raise ValueError('FORWARD_PROFILE_HASH_MISMATCH')
    if p['config']['variants']!=list(VARIANTS) or p['config']['exit_model']!='E0':raise ValueError('FORBIDDEN_BRANCH')
    if any(p.get(k)!=v for k,v in SAFETY.items()):raise ValueError('SAFETY_PROFILE_CHANGED')
    required={str(p.relative_to(ROOT)) for p in [HERE/'forward.py',HERE/'forward_config.json',HERE/'FORWARD_SPEC.md']}
    if set(p['authored_files'])!=required:raise ValueError('AUTHORED_PIN_SET_INCOMPLETE_OR_CHANGED')
    if p['config']!=load(HERE/'forward_config.json'):raise ValueError('PROFILE_CONFIG_BODY_DIFFERS')
    if p['original_freeze_sha256']!=sha(R2/'freeze.json'):raise ValueError('WRONG_ORIGINAL_FREEZE')
    for n,h in p['authored_files'].items():
        if not (ROOT/n).is_file() or sha(ROOT/n)!=h:raise ValueError('FORWARD_ENTRY_CHANGED_AFTER_FREEZE:'+n)
    isolated=verify_isolated(p,isolated)
    config_contract(p['config'],load(isolated/'stock-strategy/cash_short_intraday_research_v01/config.json'),
                    load(isolated/'stock-strategy/cash_short_intraday_research_v01/round2/config.json'))
    deps={n:h for n,h in p['original_pins'].items() if n.endswith('.py') or n.endswith('config.json')}
    hashes={v:canonical({'variant':v,'config':p['config'],'dependencies':deps}) for v in VARIANTS}
    if hashes!=p['variant_hashes']:raise ValueError('VARIANT_IDENTITY_HASH_MISMATCH')
    return p,isolated

def validate_manifest(manifest,profile,base):
    """Validate chronology before reading any potential new date's outcomes."""
    errors=[];runs=manifest.get('runs');base=Path(base)
    if manifest.get('schema')!='CASH_SHORT_FORWARD_MANIFEST_V01':errors.append('MANIFEST_SCHEMA')
    a=manifest.get('unseen_attestation',{})
    if a.get('outcomes_inspected') is not False or not a.get('basis'):errors.append('UNSEEN_ATTESTATION_REQUIRED')
    evidence=base/str(a.get('evidence_path','__MISSING__'))
    if not evidence.is_file() or sha(evidence)!=a.get('evidence_sha256'):errors.append('ATTESTATION_BODY_OR_HASH_MISSING')
    if not isinstance(runs,list) or not runs:errors.append('NO_NEW_ARCHIVED_DATES');runs=[]
    cutoff=datetime.fromisoformat(profile['frozen_at']).astimezone(TZ).strftime('%Y%m%d')
    seen=set()
    for row in runs:
        day=str(row.get('day',''));reason=[]
        try:datetime.strptime(day,'%Y%m%d')
        except ValueError:reason.append('INVALID_DAY')
        if day<=cutoff:reason.append('DAY_NOT_STRICTLY_AFTER_FREEZE')
        if day in profile['known_exploratory_dates']:reason.append('KNOWN_EXPLORATORY_DATE')
        if day in seen:reason.append('DUPLICATE_DATE')
        seen.add(day)
        path=Path(row.get('path',''))
        if not path.is_absolute():path=base/path
        if 'latest' in path.parts:reason.append('LATEST_ALIAS_FORBIDDEN')
        if not path.is_dir():reason.append('ARCHIVE_BODY_MISSING')
        for item in row.get('eligibility',[]):
            if item.get('known_restriction') is True:
                source=base/str(item.get('evidence_path','__MISSING__'))
                if not source.is_file() or sha(source)!=item.get('evidence_sha256'):reason.append('RESTRICTION_EVIDENCE_BODY_OR_HASH_MISSING')
                if not item.get('effective_from') or str(item['effective_from'])>day:reason.append('RESTRICTION_EFFECTIVE_DATE_INVALID')
                if item.get('effective_to') and str(item['effective_to'])<day:reason.append('RESTRICTION_ALREADY_ENDED')
        errors.extend(day+':'+r for r in reason)
    return errors

def original_modules(isolated):
    """Only called in a fresh standalone worker, avoiding parent-package cache."""
    tree=Path(isolated)/'stock-strategy';sys.path.insert(0,str(tree))
    from cash_short_intraday_research_v01 import data,replay
    from cash_short_intraday_research_v01.round2 import features,run
    import yuanta_live_runtime_v01.strategy as strategy
    for module in (data,replay,features,run,strategy):
        if tree not in Path(module.__file__).resolve().parents:raise ValueError('ACTIVE_CODE_IMPORT_REJECTED')
    return data,replay,features,run

def source_gate(data,day):
    return [name for name,ok in {
      'ARCHIVE_DAY_MISMATCH':data['day']==day,'POINT_IN_TIME_FAIL':data['point_in_time_pass'],
      'SOURCE_HASH_FAIL':data['source_hash_verified'],'CALLBACK_ERRORS':not data['callback_errors'],
      'SOURCE_INCOMPLETE':data['source_status']=='COMPLETE',
      'COVERAGE_NOT_FULL':data['recorded_coverage_status']=='FULL_SESSION',
      'STREAM_COVERAGE_FAIL':data['recorded_stream_coverage_pass'] is True}.items() if not ok]

def worker(args):
    profile,isolated=verify_profile(args.freeze_file,args.isolated_root)
    input_sha=sha(args.manifest);manifest=load(args.manifest)
    if sha(args.manifest)!=input_sha:raise ValueError('MANIFEST_CHANGED_DURING_READ')
    errors=validate_manifest(manifest,profile,Path(args.manifest).resolve().parent)
    output=Path(args.output).resolve()
    if output.exists():raise FileExistsError('Fresh output required')
    if ROOT in output.parents and HERE not in output.parents:raise ValueError('OUTPUT_OUTSIDE_FOLLOWUP_IN_ACTIVE_TREE')
    # No output may be inside original results or source trees.
    if any(p==output or p in output.parents for p in (R2/'results_round2_v1',HERE.parent/'results_round1_v2',isolated)):
        raise ValueError('PROTECTED_OUTPUT_PATH')
    output.mkdir(parents=True)
    if errors:
        dump(output/'validation_status.json',{'status':'PENDING_OR_BLOCKED_MANIFEST','errors':errors,**SAFETY})
        return 2
    data_mod,replay,features,r2run=original_modules(isolated)
    audit=[];paths={};source_fingerprints={};base=Path(args.manifest).resolve().parent
    # Preflight all supplied days before any signal/performance work.
    for row in sorted(manifest['runs'],key=lambda r:r['day']):
        path=Path(row['path']);path=path if path.is_absolute() else base/path
        try:
            daydata=data_mod.load_day([path]);reasons=source_gate(daydata,row['day'])
            source_fingerprints[row['day']]=canonical(daydata.get('sources',[]))
        except Exception as exc:
            reasons=['ARCHIVE_REJECTED:'+type(exc).__name__]
        audit.append({'day':row['day'],'path':str(path),'data_evaluable':not reasons,'reasons':reasons})
        paths[row['day']]=path
    dump(output/'data_validation.json',audit)
    if any(not r['data_evaluable'] for r in audit):
        dump(output/'validation_status.json',{'status':'BLOCKED_DATA_INCOMPLETE_NO_PNL_CALCULATED','dates':audit,**SAFETY})
        return 2
    if args.check:
        dump(output/'validation_status.json',{'status':'DATA_PREFLIGHT_PASS_NOT_PERFORMANCE','dates':audit,**SAFETY})
        return 0
    cfg=load(isolated/'stock-strategy/cash_short_intraday_research_v01/config.json')
    scenarios={s['name']:{**cfg,**s} for s in cfg['stress_scenarios']}
    def empty():return {'trades':[],'daily':[],'issues':[],'dispositions':[],
                       'ending_equity':190000.0,'peak_equity':190000.0,'max_drawdown':0.0,'residual':False}
    states={(s,scope,v):empty() for s in scenarios for scope in ('OWN_EVALUABLE','COMMON_EVALUABLE') for v in VARIANTS}
    all_decisions=[];signal_rows=[];cash_days=[]
    for row in sorted(manifest['runs'],key=lambda r:r['day']):
        data=data_mod.load_day([paths[row['day']]])
        if source_gate(data,row['day']) or canonical(data.get('sources',[]))!=source_fingerprints[row['day']]:
            raise ValueError('SOURCE_CHANGED_AFTER_PREFLIGHT:'+row['day'])
        if sha(args.manifest)!=input_sha:raise ValueError('MANIFEST_CHANGED_AFTER_PREFLIGHT')
        data['metadata']={k:v['name'] if isinstance(v,dict) else v for k,v in data['metadata'].items()}
        data['known_short_ineligible']={str(i['symbol']):True for i in row.get('eligibility',[]) if i.get('known_restriction') is True}
        data['index']=replay.index_day(data)
        generated,_=replay.scan_day(data,cfg)
        s1=[s for s in generated if s['entry_model']=='S1']
        enriched=features.build_feature_context(data,s1,cfg)['signals'];signal_rows.extend(enriched)
        evaluations={s['signal_id']:{v:features.evaluate_variant(s,v) for v in VARIANTS} for s in enriched}
        common={sid:all(e['evaluable'] for e in es.values()) for sid,es in evaluations.items()}
        for (scenario,scope,variant),state in states.items():
            selected=[]
            for s in enriched:
                e=evaluations[s['signal_id']][variant];included=scope=='OWN_EVALUABLE' or common[s['signal_id']]
                decision={'scenario':scenario,'phase':'ENTRY','scope':scope,'variant':variant,'day':row['day'],
                    'signal_id':s['signal_id'],'symbol':s['symbol'],'time':s['time'],
                    'historical_qualification_certified':False,
                    'eligibility_basis':'KNOWN_NEGATIVE_EXCLUSION_ONLY_UNKNOWN_NOT_CERTIFIED',
                    'scope_included':included,**e}
                all_decisions.append(decision)
                if included and e['evaluable'] and e['pass'] is True:selected.append(s)
            result=replay.run_portfolio({row['day']:data},{row['day']:selected},scenarios[scenario],
                'B','S1','NONE','E0',initial_equity=state['ending_equity'],
                prior_peak=state['peak_equity'],prior_max_dd=state['max_drawdown'],prior_residual=state['residual'])
            for trade in result['trades']:r2run.annotate_path_profit(trade)
            r2run.append_state(state,result)
            state['daily'][-1].update(r2run.day_extrema(result['trades']))
        cash_days.append({'day':row['day'],'net_pnl':0,'equity':190000,'basis':'NO_POSITION_ON_PREFLIGHT_VALIDATED_DATE'})
    tables={'experiments':[],'trades':[],'daily_results':[],'execution_issues':[],
            'portfolio_dispositions':[],'signals':all_decisions,'raw_signals':signal_rows,'cash_daily':cash_days}
    for (scenario,scope,variant),state in states.items():
        dims={'scenario':scenario,'phase':'ENTRY','scope':scope,'variant':variant}
        sr=[r for r in all_decisions if all(r[k]==v for k,v in dims.items())]
        m=r2run.metrics(state,sr,cfg)
        m.update(sample_status='NEW_AFTER_FREEZE_UNSEEN_ATTESTED_NOT_INDEPENDENTLY_CERTIFIED',
                 prospective_validation='CONDITIONAL_UNSEEN_ATTESTED',strict_executable=None,
                 original_s1_candidate_count=len(signal_rows))
        tables['experiments'].append({**dims,**m})
        for name,key in [('trades','trades'),('daily_results','daily'),('execution_issues','issues'),('portfolio_dispositions','dispositions')]:
            tables[name].extend({**x,**dims,**({'execution_phase':x.get('phase')} if key=='issues' else {})} for x in state[key])
    for r in tables['experiments']:
        b=next(x for x in tables['experiments'] if x['scenario']==r['scenario'] and x['scope']==r['scope'] and x['variant']=='R0')
        r['incremental_net_vs_R0']=r['net_pnl']-b['net_pnl'] if r['net_pnl'] is not None and b['net_pnl'] is not None else None
        r['drawdown_change_vs_R0']=r['max_drawdown_twd']-b['max_drawdown_twd'] if r['max_drawdown_twd'] is not None and b['max_drawdown_twd'] is not None else None
        r['incremental_vs_cash']=r['net_pnl']
    if sha(args.manifest)!=input_sha:raise ValueError('MANIFEST_CHANGED_DURING_EVALUATION')
    # Ensure source bodies/code did not change while the offline worker ran.
    verify_profile(args.freeze_file,args.isolated_root)
    dump(output/'tables.json',tables)
    dump(output/'run_manifest.json',{'profile_hash':profile['profile_hash'],'variant_hashes':profile['variant_hashes'],
      'input_manifest_sha256':input_sha,'tables_sha256':sha(output/'tables.json'),
      'completed_at':datetime.now(timezone.utc).isoformat(),'dates':[x['day'] for x in audit],
      'original_source_commit':profile['original_source_commit'],'STRICT_EXECUTABLE':None,
      'source_quality':'CONDITIONAL_UNIT_FEE_ELIGIBILITY_CLOCK_ASSUMPTIONS_UNCHANGED',**SAFETY})
    return 0

def main():
    p=argparse.ArgumentParser(description=__doc__)
    action=p.add_mutually_exclusive_group(required=True)
    action.add_argument('--freeze',action='store_true');action.add_argument('--check',action='store_true')
    action.add_argument('--evaluate',action='store_true')
    p.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    p.add_argument('--isolated-root',type=Path,required=True);p.add_argument('--freeze-file',type=Path,required=True)
    p.add_argument('--manifest',type=Path);p.add_argument('--output',type=Path)
    a=p.parse_args()
    if a.freeze:
        result=freeze(a.isolated_root,a.freeze_file)
        print(json.dumps({'frozen_at':result['frozen_at'],'profile_hash':result['profile_hash'],
                          'variant_hashes':result['variant_hashes'],**SAFETY}));return 0
    if not a.manifest or not a.output:p.error('Manifest and fresh output required')
    if a.worker:return worker(a)
    # Fresh process prevents current package __path__ caching from importing new LONG.
    result=subprocess.run([sys.executable,'-B',str(Path(__file__).resolve()),
      '--check' if a.check else '--evaluate','--worker','--isolated-root',str(a.isolated_root.resolve()),
      '--freeze-file',str(a.freeze_file.resolve()),'--manifest',str(a.manifest.resolve()),
      '--output',str(a.output.resolve())],cwd=a.isolated_root.resolve()/'stock-strategy')
    return result.returncode

if __name__=='__main__':raise SystemExit(main())
