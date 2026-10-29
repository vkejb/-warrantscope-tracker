"""Reproducible local research entrypoint. Never imports any broker/runtime main."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

from . import RESEARCH_ID, SAFETY
from .data import audit_all, load_day
from .freeze import MODULE, sha, verify_freeze
from .replay import index_day, run_portfolio, scan_day, stamp, summarize


def serial(value):
    if isinstance(value,datetime): return value.isoformat()
    if isinstance(value,set): return sorted(value)
    raise TypeError(type(value).__name__)


def dump(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=serial,allow_nan=False)+'\n',encoding='utf-8')


def partition(day,config):
    if day in config['split']['audit_only']: return 'AUDIT_ONLY'
    for key in ['train','validation','retrospective_holdout']:
        if config['split'][key]['start']<=day<=config['split'][key]['end']: return key
    return 'AUDIT_ONLY'


def classify(row,config):
    reasons=[]
    if not row.get('source_hash_verified'): reasons.append('SOURCE_HASH_OR_SCHEMA_FAILED')
    if not row.get('point_in_time_pass'): reasons.append('CANDIDATE_POINT_IN_TIME_FAILED')
    if not row.get('run_start_before_open'): reasons.append('LATE_START_NO_FULL_0900_WINDOW')
    if not row.get('run_end_after_close'): reasons.append('ENDED_BEFORE_CLOSE')
    if row.get('source_status')!='COMPLETE': reasons.append('SOURCE_NOT_COMPLETE')
    if row.get('recorded_coverage_status')!='FULL_SESSION' or row.get('recorded_stream_coverage_pass') is not True:
        reasons.append('RECORDED_FULL_SESSION_GATE_FAILED')
    if row.get('callback_errors',0): reasons.append('RECORDED_CALLBACK_ERRORS')
    if not row.get('context_0050_complete'): reasons.append('NO_ARCHIVED_0050_TICK_AND_BOOK')
    if partition(row['day'],config)=='AUDIT_ONLY': reasons.append('PREDECLARED_AUDIT_ONLY_DATE')
    row['split']=partition(row['day'],config)
    row['evaluation_grade']='NOT_EVALUABLE' if reasons else 'SIMPLIFIED_CONDITIONAL'
    row['exclusion_reasons']=reasons
    row['strict_executable_status']='UNAVAILABLE_UNITS_HISTORICAL_QUALIFICATION_AND_BOOK_CLOCK_UNPROVEN'
    row['index_taiex_available']=False
    row['actual_account_fee_verified']=False
    return row


def empty_state():
    return {'trades':[],'daily':[],'issues':[],'dispositions':[],'ending_equity':190000.0,
            'peak_equity':190000.0,'max_drawdown':0.0,'residual':False}


def advance(state,day,data,signals,config,mode,short_model='S1',market_filter='NONE',exit_model='E0',cache=None):
    result=run_portfolio({day:data},{day:signals},config,mode,short_model,market_filter,exit_model,
                         cache=cache,initial_equity=state['ending_equity'],prior_peak=state['peak_equity'],
                         prior_max_dd=state['max_drawdown'],prior_residual=state['residual'])
    for key in ['trades','daily','issues','dispositions']: state[key].extend(result[key])
    for key in ['ending_equity','peak_equity','max_drawdown','residual']: state[key]=result[key]


def choose_entry(states,base_scenario,config):
    choices=[]
    diagnostics=[]
    for name in config['selection']['eligible_entries']:
        result=states[(base_scenario,'ENTRY_'+name)]
        metrics=summarize(result['trades'],result['daily'],result['dispositions'])
        enough=metrics['closed_trades']>=config['selection']['minimum_closed_train_validation'] and len({t['day'] for t in result['trades'] if t['net_pnl'] is not None})>=config['selection']['minimum_distinct_train_validation_days']
        positive=metrics['net_pnl'] is not None and metrics['net_pnl']>0
        diagnostics.append({'entry':name,'enough_sample':enough,'positive_combined_net':positive,'metrics':metrics})
        if enough and positive and metrics['account_complete']:
            choices.append((metrics['max_drawdown_twd'],-metrics['net_pnl'],name))
    selected=min(choices)[2] if choices else 'S1_NONE'
    return {'entry':selected,'status':'SELECTED_CONDITIONAL_TRAIN_VALIDATION_ONLY' if choices else 'NO_WINNER_FIXED_REFERENCE_NOT_SELECTED',
            'selection_time':datetime.now(timezone.utc).isoformat(),'holdout_not_loaded_yet':True,
            'diagnostics':diagnostics,'exit':'E0','exit_selection_status':'FIXED_BASELINE_NO_EXIT_OPTIMIZATION',
            'execution_evidence':'UNIT_UNVERIFIED_QUALIFICATION_UNPROVEN_NO_LIVE_ACTIVATION'}


def build_experiment_rows(states,config,selected):
    rows=[]
    baseline={scenario:state for (scenario,label),state in states.items() if label=='A'}
    for (scenario,label),state in states.items():
        metrics=summarize(state['trades'],state['daily'],state['dispositions'])
        base=baseline.get(scenario)
        base_by_day={x['day']:x for x in base['daily']} if base else {}
        common=[x for x in state['daily'] if x['day'] in base_by_day]
        delta=sum(x['net_pnl']-base_by_day[x['day']]['net_pnl'] for x in common) if common and all(x['net_pnl'] is not None and base_by_day[x['day']]['net_pnl'] is not None for x in common) else None
        rows.append({'experiment':label,'scenario':scenario,'study_status':'EXPLORATORY_CONDITIONAL',
                     'short_entry':selected['entry'] if label in ('B','C','D','EXIT_E1','EXIT_E2') else label.removeprefix('ENTRY_'),
                     'selection_status':selected['status'],'exit_model':'E1' if label=='EXIT_E1' else 'E2' if label=='EXIT_E2' else 'E0',
                     'data_splits':sorted({partition(x['day'],config) for x in state['daily']}),
                     'common_day_incremental_net_vs_A':delta,
                     'same_day_count_compared_to_A':len(common),
                     'capital_twd':190000,'risk_limit_twd':3500,'maximum_trades_daily':1,
                     **metrics})
        for split in ['train','validation','retrospective_holdout']:
            ds=[x for x in state['daily'] if partition(x['day'],config)==split]
            ts=[x for x in state['trades'] if partition(x['day'],config)==split]
            ps=[x for x in state['dispositions'] if partition(x['day'],config)==split]
            if ds:
                split_metrics=summarize(ts,ds,ps)
                peak=float(ds[0]['starting_equity']); drawdown=0.0
                for d in ds:
                    for t in ts:
                        if t['day']!=d['day']:continue
                        for point in sorted(t.get('path',[]),key=lambda x:stamp(x['time'])):
                            value=d['starting_equity']+point['net']
                            peak=max(peak,value);drawdown=max(drawdown,peak-value)
                    if d['equity'] is not None:
                        peak=max(peak,d['equity']);drawdown=max(drawdown,peak-d['equity'])
                split_metrics['max_drawdown_twd']=drawdown if split_metrics['account_complete'] else None
                split_metrics['observed_partial_max_drawdown_twd']=drawdown
                split_metrics['max_drawdown_fraction']=drawdown/ds[0]['starting_equity'] if split_metrics['account_complete'] else None
                rows.append({'experiment':label,'scenario':scenario,
                             'study_status':'SPLIT_DIAGNOSTIC','data_splits':[split],
                             'short_entry':selected['entry'] if label in ('B','C','D','EXIT_E1','EXIT_E2') else label.removeprefix('ENTRY_'),
                             'exit_model':'E1' if label=='EXIT_E1' else 'E2' if label=='EXIT_E2' else 'E0',
                             **split_metrics})
    for label in ['A','B','C','D']:
        rows.append({'experiment':label,'scenario':'STRICT_EXECUTABLE','status':'UNAVAILABLE_EVIDENCE_INSUFFICIENT',
                     'effective_days':0,'entries':None,'closed_trades':None,'net_pnl':None,'gross_pnl':None,
                     'max_drawdown_twd':None,'reason':'UNKNOWN_UNITS_AND_HISTORICAL_BROKER_QUALIFICATION_NOT_A_ZERO_RETURN'})
    return rows


def _public_trade(trade):
    return {k:v for k,v in trade.items() if k not in ('path','entry_fills','exit_fills')}


def flatten(states,signals_by_day,audit,selected,config):
    experiments=build_experiment_rows(states,config,selected)
    trades=[]; daily=[]; issues=[]; signals=[]; grouped=[]; fills=[]; missed=[]
    for (scenario,label),state in states.items():
        for row in state['trades']:
            trades.append({'experiment':label,'scenario':scenario,**_public_trade(row)})
            for phase,key in [('ENTRY','entry_fills'),('EXIT','exit_fills')]:
                fills.extend({'experiment':label,'scenario':scenario,'signal_id':row['signal_id'],'day':row['day'],
                              'symbol':row['symbol'],'phase':phase,**x} for x in row[key])
        daily.extend({'experiment':label,'scenario':scenario,'split':partition(x['day'],config),**x} for x in state['daily'])
        issues.extend({'experiment':label,'scenario':scenario,**x} for x in state['issues'])
        for x in state['dispositions']:
            original=next((s for s in signals_by_day[x['day']] if s['signal_id']==x['signal_id']),{})
            features=original.get('features',{})
            signals.append({'experiment':label,'scenario':scenario,**x,
                            'entry_model':original.get('entry_model'), 'score':original.get('score'),
                            'structure_high':original.get('structure_high'),
                            'market_filter_pass':features.get('market_filter_pass'),
                            'features':features,'record_type':'PORTFOLIO_DISPOSITION'})
            if x['reason']=='SHORT_USED_DAILY_BUDGET_MISSED_LONG': missed.append({'experiment':label,'scenario':scenario,**x})
        for key in ['day','symbol','market_regime','timing_group']:
            buckets=defaultdict(list)
            for trade in state['trades']: buckets[str(trade.get(key,'UNKNOWN'))].append(trade)
            for group,ts in buckets.items():
                vals=[t['net_pnl'] for t in ts if t['net_pnl'] is not None]
                grouped.append({'experiment':label,'scenario':scenario,'group_by':key,'group':group,
                                'entries':len(ts),'completed':len(vals),'completed_subset_net':sum(vals),
                                'net_pnl':sum(vals) if len(vals)==len(ts) else None,
                                'wins':sum(x>0 for x in vals),'losses':sum(x<0 for x in vals)})
    for day,raw in signals_by_day.items():
        for row in raw:
            signals.append({'experiment':'ALL_QUALIFIED_DIAGNOSTIC','scenario':'NO_EXECUTION',
                            'record_type':'QUALIFIED_SIGNAL_NOT_A_TRADE','day':day,**row,
                            'reason':'SIGNAL_ONLY_SUBJECT_TO_PORTFOLIO_EXECUTION_AND_EVIDENCE_GATES'})
    base={(x['scenario'],x['day']):x for x in daily if x['experiment']=='A'}
    for row in daily:
        a=base.get((row['scenario'],row['day']))
        row['incremental_net_vs_A']=row['net_pnl']-a['net_pnl'] if a and a['net_pnl'] is not None and row['net_pnl'] is not None else None
        row['drawdown_change_vs_A']=row['running_max_drawdown_twd']-a['running_max_drawdown_twd'] if a and row['account_complete'] and a['account_complete'] else None
    return {'DATA_AUDIT':audit,'experiments':experiments,'trades':trades,'signals':signals,
            'daily_results':daily,'execution_issues':issues,'grouped_diagnostics':grouped,
            'fills':fills,'missed_longs':missed}


def archive_baseline(root):
    """Read dated paper evidence, not latest and never an actual broker ledger."""
    rows=[]
    for path in sorted((root/'stock-strategy/paper_shadow_v01/runtime/days').glob('*/*/manifest.json')):
        try:
            doc=json.loads(path.read_text())
            unsigned={k:v for k,v in doc.items() if k!='manifest_hash'}
            canonical=json.dumps(unsigned,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
            verified=hashlib.sha256(canonical).hexdigest()==doc.get('manifest_hash')
            artifact=path.parent/'paper_trades.jsonl'
            verified=verified and artifact.is_file() and sha(artifact)==doc.get('artifacts',{}).get('paper_trades.jsonl')
            variants=doc.get('variant_results',{}).get('PRODUCTION_ANTI_CHASE',{})
            rows.append({'day':doc.get('session_date'),'source_path':str(path),'source_sha256':sha(path),
                         'source_run_id':doc.get('source_run_id'),'hash_verified':verified,
                         'net_pnl':variants.get('net_pnl',doc.get('net_pnl')),
                         'trade_count':variants.get('trade_count'), 'comparison_status':'ORIGINAL_FULL_FILL_PROXY_NOT_COMPARABLE_EXECUTION_MODEL',
                         'direction':'LONG_ONLY','actual_orders':doc.get('actual_orders'),
                         'execution_model':'IMMEDIATE_FULL_FILL_AT_LIVE_ADVERSE_ONE_TICK_PROXY'})
        except (ValueError,OSError):
            rows.append({'source_path':str(path),'hash_verified':False,'net_pnl':None})
    return rows


def add_missing_days(audit,root,config):
    existing={x['day'] for x in audit}
    calendar_path=root/'stock-strategy/shadow_daily_runner/runtime/trading_calendar.csv'
    calendar={x.strip().replace('-','') for x in calendar_path.read_text().splitlines() if len(x.strip())==10}
    automatic=root/'stock-strategy/yuanta_intraday_shadow_v01/runtime/automatic_runs.jsonl'
    receipts=defaultdict(list)
    if automatic.is_file():
        for line in automatic.read_text().splitlines():
            try:
                x=json.loads(line);d=str(x.get('session_date') or x.get('date') or '')
                if len(d)==8:receipts[d].append({k:x.get(k) for k in ['status','reason','session_date','started_at','completed_at']})
            except ValueError: pass
    cursor=datetime(2026,9,22)
    while cursor<=datetime(2026,10,29):
        day=cursor.strftime('%Y%m%d')
        if cursor.weekday()<5 and day not in existing:
            audit.append({'day':day,'evaluation_grade':'NOT_EVALUABLE','split':partition(day,config),
                          'calendar_open':day in calendar,'source_status':'NO_ARCHIVED_COLLECTOR_RUN',
                          'calendar_interpretation':'SCHEDULED_TRADING_DATE_NO_ARCHIVE' if day in calendar else 'LOCAL_CALENDAR_CLOSED_NOT_INDEPENDENT_HOLIDAY_PROOF',
                          'automatic_receipts':receipts.get(day,[]),'exclusion_reasons':['NO_SOURCE_ARCHIVE_NOT_NO_SIGNAL'],
                          'strict_executable_status':'UNAVAILABLE','candidate_count':None})
        cursor+=timedelta(days=1)
    return sorted(audit,key=lambda x:x['day'])


def historical_eligibility(root):
    """Conservative exclusion from existing dated tables, not PIT certification."""
    mapping={};sources=[]
    for path in sorted((root/'stock-strategy/short_side_study_v01/sources').glob('*_twse_eligibility.json')):
        doc=json.loads(path.read_text());day=str(doc.get('session_date',''))
        raw=doc.get('raw_response',{})
        if raw.get('date')!=day or raw.get('stat')!='OK':raise ValueError('HISTORICAL_ELIGIBILITY_DATE_MISMATCH')
        table=next((t for t in raw.get('tables',[]) if '暫停現股賣出後現款買進當沖註記' in t.get('fields',[])),None)
        if table is None:raise ValueError('HISTORICAL_ELIGIBILITY_TABLE_MISSING')
        column=table['fields'].index('暫停現股賣出後現款買進當沖註記')
        mapping[day]={str(row[0]):str(row[column]).strip() for row in table['data']}
        sources.append({'day':day,'path':str(path),'sha256':sha(path),'role':'HISTORICAL_ELIGIBILITY_CACHE',
                        'point_in_time_verified':False,'original_http_bytes_verified':False})
    return mapping,sources


def run(args):
    root=args.root.resolve()
    frozen=verify_freeze(root,MODULE/'freeze.json')
    config=json.loads((MODULE/'config.json').read_text())
    output=args.output.resolve()
    if output.exists(): raise FileExistsError('Output exists; choose a fresh output path, never overwrite a research run.')
    if MODULE.resolve() not in output.parents: raise ValueError('Research output must stay inside the isolated module.')
    output.mkdir(parents=True)
    dump(output/'implementation_freeze.json',{'frozen_at':datetime.now(timezone.utc).isoformat(),
          'phase':'BEFORE_REAL_PERFORMANCE','notes_sha256':sha(MODULE/'IMPLEMENTATION_NOTES.md'),
          'code_hashes':{str(p.relative_to(MODULE)):sha(p) for p in sorted(MODULE.rglob('*.py')) if '__pycache__' not in p.parts},
          'thresholds_changed_after_performance':False})
    if args.audit:
        a=json.loads(args.audit.read_text()); audit,sources=a['rows'],a['sources']
        if a.get('reader_sha256')!=sha(MODULE/'data.py'):raise ValueError('AUDIT_READER_VERSION_MISMATCH')
    else:
        audit,sources=audit_all(root)
    audit=add_missing_days([classify(x,config) for x in audit],root,config)
    qualifications,qualification_sources=historical_eligibility(root)
    sources.extend(qualification_sources)
    qualification_hashes={x['path']:x['sha256'] for x in qualification_sources}
    dump(output/'eligibility_input_freeze.json',{'frozen_at':datetime.now(timezone.utc).isoformat(),'sources':qualification_sources,
          'interpretation':'KNOWN_SUSPENSION_EXCLUDED_UNKNOWN_NOT_PROMOTED_TO_CERTIFIED'})
    for row in audit:
        row['historical_eligibility_table_present']=row['day'] in qualifications
        row['historical_eligibility_point_in_time_verified']=False
    dump(output/'data_audit.json',{'rows':audit,'sources':sources})
    good={x['day']:x for x in audit if x['evaluation_grade']=='SIMPLIFIED_CONDITIONAL'}
    trainval={k:v for k,v in good.items() if v['split'] in ('train','validation')}
    print('AUDIT usable conditional dates:',','.join(good),flush=True)
    scenarios={x['name']:{**config,**x} for x in config['stress_scenarios']}
    states={}
    for scenario in scenarios:
        for label in ['A','ENTRY_M0_NONE','ENTRY_S1_NONE','ENTRY_S1_WEAK','ENTRY_S2_NONE','ENTRY_S2_WEAK']:
            states[(scenario,label)]=empty_state()
    signals_by_day={}; transitions={}; diagnostic_counts={}; code_hashes={p.name:sha(p) for p in MODULE.glob('*.py')}
    def get_data(day):
        source=root/'stock-strategy/yuanta_intraday_shadow_v01/runtime/runs'/good[day]['canonical_run_id']
        relative=str((source/'run_manifest.json').relative_to(root))
        if frozen['source_manifest_hashes'].get(relative)!=sha(source/'run_manifest.json'):
            raise ValueError('SOURCE_NOT_IN_PRE_PERFORMANCE_FREEZE:'+day)
        data=load_day([source])
        if not data['point_in_time_pass'] or not data['source_hash_verified'] or data['day']!=day or data['source_status']!='COMPLETE' or data['recorded_coverage_status']!='FULL_SESSION' or data['recorded_stream_coverage_pass'] is not True or data['callback_errors']!=0:
            raise ValueError('LOADED_SOURCE_SAFETY_GATE_FAILED:'+day)
        data['metadata']={k:v['name'] if isinstance(v,dict) else v for k,v in data['metadata'].items()}
        data['known_short_ineligible']={code:True for code,flag in qualifications.get(day,{}).items() if flag}
        previous={};decreases=0;valid=0;transport=[]
        for event in data['events']:
            if event['kind']!='tick' or event.get('flag') not in ('0','1'):continue
            valid+=1;symbol=event['symbol'];volume=event['volume']
            if symbol in previous and volume<previous[symbol]:decreases+=1
            previous[symbol]=volume
            if event.get('exchange_time') is not None:transport.append((event['received_at']-event['exchange_time']).total_seconds())
        transport.sort()
        good[day]['volume_semantics_diagnostic']={'normal_tick_messages':valid,'native_volume_decreases':decreases,
              'interpretation':'OBSERVED_NONMONOTONE_NOT_CONTINUOUS_CUMULATIVE_VOLUME' if decreases else 'INSUFFICIENT_EMPIRICAL_DISTINCTION',
              'share_or_lot_unit':'UNVERIFIED','official_single_trade_semantics':'FIELD_DEALVOL_NOT_INDEPENDENTLY_ATTESTED'}
        good[day]['tick_transport_seconds']={p:transport[min(len(transport)-1,int((len(transport)-1)*q))] if transport else None for p,q in [('min',0),('p50',.5),('p95',.95),('max',1)]}
        good[day]['order_latency_evidence']='NO_MEASURED_SIGNAL_TO_ORDER_DISTRIBUTION_FIXED_ASSUMPTION'
        data['index']=index_day(data)
        return data
    for day in sorted(trainval):
        print('TRAIN_VALIDATION',day,'loading',flush=True)
        data=get_data(day)
        raw,trace=scan_day(data,config); signals_by_day[day]=raw; transitions[day]=trace
        diagnostic_counts[day]=dict(Counter(x['entry_model'] for x in raw))
        cache={}
        for scenario,cfg in scenarios.items():
            advance(states[(scenario,'A')],day,data,raw,cfg,'A',cache=cache)
            for model in ['M0','S1','S2']:
                for gate in (['NONE'] if model=='M0' else ['NONE','WEAK']):
                    advance(states[(scenario,'ENTRY_'+model+'_'+gate)],day,data,raw,cfg,'B',model,gate,cache=cache)
        dump(output/('signals_'+day+'.json'),raw)
        print('TRAIN_VALIDATION',day,'signals',diagnostic_counts[day],flush=True)
        del data,cache
    selected=choose_entry(states,'BASE_ASSUMED_LOTS',config)
    dump(output/'selection_before_holdout.json',selected)
    chosen,gate=selected['entry'].split('_')
    print('FROZEN SELECTION',selected['entry'],selected['status'],flush=True)
    for scenario in scenarios:
        for label in ['B','C','D','EXIT_E1','EXIT_E2']: states[(scenario,label)]=empty_state()
    examples=[];post_exit=[];issue_cases=[]
    for day in sorted(good):
        print('PORTFOLIO',day,'loading',flush=True)
        data=get_data(day)
        if day not in signals_by_day:
            raw,trace=scan_day(data,config); signals_by_day[day]=raw; transitions[day]=trace
            diagnostic_counts[day]=dict(Counter(x['entry_model'] for x in raw))
            dump(output/('signals_'+day+'.json'),raw)
        raw=signals_by_day[day];cache={}
        for scenario,cfg in scenarios.items():
            if day not in trainval: advance(states[(scenario,'A')],day,data,raw,cfg,'A',cache=cache)
            for mode in ['B','C','D']: advance(states[(scenario,mode)],day,data,raw,cfg,mode,chosen,gate,cache=cache)
            if day in trainval:
                for exit_model in ['E1','E2']:
                    advance(states[(scenario,'EXIT_'+exit_model)],day,data,raw,cfg,'B',chosen,gate,exit_model,cache=cache)
        # Compact quote fragments only; never package original account/raw payloads.
        for key in [(s,m) for s in scenarios for m in ['A','B','C','D']]:
            state=states[key]
            for t in state['trades']:
                if t['day']!=day: continue
                signal_at=stamp(t['signal_time'])
                fragment=[{k:v for k,v in e.items() if k in ['kind','symbol','received_at','exchange_time','sequence','price','volume','flag','serial','bids','asks','quote_valid','order_flags']}
                          for e in data['index']['events'][t['symbol']] if signal_at-timedelta(seconds=10)<=e['received_at']<=signal_at+timedelta(seconds=10)]
                examples.append({'experiment':key[1],'scenario':key[0],'trade':_public_trade(t),'market_fragment':fragment[:60]})
                if t['exit_time'] is not None and key[0]=='BASE_ASSUMED_LOTS':
                    after=[b for b in data['index']['books'][t['symbol']] if b['received_at']>stamp(t['exit_time']) and b['received_at'].strftime('%H:%M')<'13:25']
                    marks=[]
                    from .execution import cost,shift_price
                    for book in after:
                        levels=book.get('bids' if t['side']=='LONG' else 'asks',[])
                        if levels and 0<levels[0][0]<99999:
                            try:
                                price=shift_price(levels[0][0],1,-1 if t['side']=='LONG' else 1)
                                net=cost(t['side'],t['entry_price'],price,t['quantity'],config)['net']
                                marks.append((book['received_at'],price,net))
                            except ValueError:pass
                    post_exit.append({'experiment':key[1],'scenario':key[0],'day':day,'symbol':t['symbol'],'signal_id':t['signal_id'],
                                      'exit_time':t['exit_time'],'exit_reason':t['exit_reason'],'actual_modeled_net':t['net_pnl'],
                                      'best_post_exit_quote_net':max((x[2] for x in marks),default=None),
                                      'worst_post_exit_quote_net':min((x[2] for x in marks),default=None),
                                      'post_exit_opportunity_not_fill':max(0,max(x[2] for x in marks)-t['net_pnl']) if marks else None,
                                      'stopped_short_later_lower_ask':any(x[1]<t['exit_price'] for x in marks) if marks and t['side']=='SHORT' and t['exit_reason']=='STOP_LOSS' else None,
                                      'status':'POST_HOC_DIAGNOSTIC_NOT_TRADEABLE_RETURN' if marks else 'PENDING_NO_POST_EXIT_QUOTES'})
            for issue in state['issues']:
                if issue.get('day')!=day or issue.get('code') not in ['UNFILLED_ENTRY_IOC','PARTIAL_ENTRY_REMAINDER_CANCELED_MODEL','COVER_INCOMPLETE_EXPOSURE_RETAINED','ENTRY_CAP_BREACH_EXPOSURE_RETAINED']:
                    continue
                issue_at=stamp(issue['time']);symbol=issue['symbol']
                issue_cases.append({'experiment':key[1],'scenario':key[0],'issue':issue,
                                    'market_fragment':[{k:v for k,v in e.items() if k in ['kind','symbol','received_at','exchange_time','sequence','price','volume','flag','bids','asks','quote_valid','order_flags']}
                                                       for e in data['index']['events'][symbol] if issue_at-timedelta(seconds=5)<=e['received_at']<=issue_at+timedelta(seconds=8)][:50]})
        print('PORTFOLIO',day,'completed',flush=True)
        del data,cache
    tables=flatten(states,signals_by_day,audit,selected,config)
    for day,trace in transitions.items():
        for row in trace:
            if row.get('record_type')=='LONG_APPROVAL_DIAGNOSTIC' and row.get('selection_status')=='DIAGNOSTIC_NOT_TOP_RANK':
                tables['signals'].append({'day':day,'experiment':'ALL_QUALIFIED_DIAGNOSTIC','scenario':'NO_EXECUTION',**row,
                    'reason':'ORIGINAL_LONG_SELECTOR_LOWER_RANK_NOT_EXECUTABLE_ACCOUNT_RETURN'})
    tables['baseline_reference']=archive_baseline(root)
    # Diagnostic path after the real modeled exit is never fed back to signals.
    tables['post_exit_diagnostics']=post_exit
    tables['per_symbol_data_audit']=[{'day':row['day'],'symbol':symbol,**coverage} for row in audit for symbol,coverage in row.get('per_symbol_coverage',{}).items()]
    tables['eligibility_audit']=[{'day':row['day'],'symbol':symbol,'historical_flag':qualifications.get(row['day'],{}).get(symbol),
          'status':'KNOWN_SUSPENSION_EXCLUDED' if qualifications.get(row['day'],{}).get(symbol) else
                   'TABLE_LISTED_NO_SUSPENSION_PIT_UNVERIFIED' if symbol in qualifications.get(row['day'],{}) else 'UNKNOWN_NOT_ASSUMED_CERTIFIED',
          'broker_entitlement':'UNPROVEN','entry_time_restrictions':'UNPROVEN','table_available_before_entry':'UNPROVEN'}
          for row in audit for symbol in row.get('per_symbol_coverage',{}) if symbol not in ('0050','2330')]
    dump(output/'tables.json',tables)
    dump(output/'signal_counts.json',diagnostic_counts)
    dump(output/'state_transitions.json',transitions)
    completed=[x for x in examples if x['trade']['net_pnl'] is not None and x['scenario']=='BASE_ASSUMED_LOTS']
    cases={}
    if completed:
        cases['maximum_profit']=max(completed,key=lambda x:x['trade']['net_pnl'])
        cases['maximum_loss']=min(completed,key=lambda x:x['trade']['net_pnl'])
    first_issue=min(issue_cases,key=lambda x:str(x['issue'].get('time','')),default=None)
    cases['first_execution_issue']=first_issue
    for name,case in cases.items():
        if case and case.get('trade'):
            t=case['trade'];case['state_transitions']=[x for x in transitions.get(t['day'],[]) if x.get('record_type')=='STATE_TRANSITION' and x.get('symbol')==t['symbol'] and str(x.get('time',''))<=str(t['entry_time'])][-15:]
    dump(output/'event_cases.json',cases)
    verify_freeze(root,MODULE/'freeze.json')
    if any(sha(Path(path))!=expected for path,expected in qualification_hashes.items()):raise ValueError('ELIGIBILITY_SOURCE_CHANGED_DURING_RUN')
    if code_hashes!={p.name:sha(p) for p in MODULE.glob('*.py')}: raise ValueError('RESEARCH_CODE_CHANGED_DURING_RUN')
    manifest={'research_id':RESEARCH_ID,**SAFETY,'NO_LIVE_ORDER':True,'completed_at':datetime.now(timezone.utc).isoformat(),
              'source_commit':frozen['commit'],'source_root':str(root),'freeze_sha256':sha(MODULE/'freeze.json'),
              'protocol_hashes':frozen['protocol_hashes'],'research_code_hashes':code_hashes,'data_range':[min(good),max(good)] if good else [],
              'conditional_days':sorted(good),'source_audit':sources,'selection':selected,
              'reproduce':f'cd stock-strategy && python3 -B -m cash_short_intraday_research_v01.run --output cash_short_intraday_research_v01/results_reproduction',
              'strict_executable_performance':'UNAVAILABLE_NOT_ZERO','actual_simulated_orders_are_not_broker_orders':True,
              'limits':['UNITS_UNVERIFIED','HISTORICAL_QUALIFICATION_INCOMPLETE','BOOK_EXCHANGE_TIME_UNAVAILABLE','LEGACY_RECEIPT_ORDER_PARTLY_UNKNOWN','ACTUAL_ACCOUNT_FEE_UNVERIFIED','RETROSPECTIVE_HOLDOUT_LONG_RESULTS_PREVIOUSLY_SEEN']}
    dump(output/'run_manifest.json',manifest)
    print('COMPLETE',output,'actual_orders=0',flush=True)
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=MODULE.parents[1])
    parser.add_argument('--audit',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    run(parser.parse_args())


if __name__=='__main__': main()
