"""Frozen, offline second-round diagnostics; reads only pinned original archives."""
from __future__ import annotations
import argparse
from collections import Counter,defaultdict
from datetime import datetime,timezone
import gc
import gzip
import hashlib
import json
from pathlib import Path
import statistics

from ..data import load_day
from ..freeze import sha
from ..replay import index_day,run_portfolio,summarize,stamp,target_quantity,last_book
from ..run import empty_state,historical_eligibility,partition
from . import SAFETY
from .features import VARIANTS,build_feature_context,evaluate_variant
from .diagnostics import fixed_trade,diagnose
from .protocol import HERE,MODULE,ROOT,R1,dump,verify


def append_state(state,result):
    for k in ('trades','daily','issues','dispositions'):state[k].extend(result[k])
    for k in ('ending_equity','peak_equity','max_drawdown','residual'):state[k]=result[k]


def tag(key):
    scenario,phase,scope,variant=key
    return dict(scenario=scenario,phase=phase,scope=scope,variant=variant)


def day_extrema(trades):
    points=[{'net':0.0,'time':None}]
    for t in trades:points.extend(t.get('path',[]))
    peak=0.0;local_dd=0.0;peak_time=trough_time=None
    for p in points:
        if p['net']>peak:peak=p['net'];peak_time=p['time']
        dd=peak-p['net']
        if dd>local_dd:local_dd=dd;trough_time=p['time']
    return {'daily_path_max_net':max(p['net'] for p in points),'daily_path_min_net':min(p['net'] for p in points),
            'daily_path_local_drawdown_twd':local_dd,'daily_peak_time':peak_time,
            'daily_local_dd_trough_time':trough_time,'daily_valuation_points':len(points)-1}


def recompute_dd(daily):
    if not daily:return None
    peak=float(daily[0]['starting_equity']);dd=0.0
    for d in daily:
        start=float(d['starting_equity'])
        dd=max(dd,peak-(start+d['daily_path_min_net']),d['daily_path_local_drawdown_twd'])
        peak=max(peak,start+d['daily_path_max_net'])
        if d.get('equity') is not None:
            dd=max(dd,peak-d['equity']);peak=max(peak,d['equity'])
    return dd


def metrics(state,signal_rows,config,split=None):
    ds=[d for d in state['daily'] if split is None or d['split']==split]
    ts=[t for t in state['trades'] if split is None or partition(t['day'],config)==split]
    ps=[p for p in state['dispositions'] if split is None or partition(p['day'],config)==split]
    sr=[r for r in signal_rows if split is None or partition(r['day'],config)==split]
    m=summarize(ts,ds,ps)
    evals=[r for r in sr if r.get('scope_included') and r.get('evaluable')]
    cond=[r for r in evals if r.get('pass') is False]
    unavailable=[r for r in sr if r.get('evaluable') is False]
    vals=[t['net_pnl'] for t in ts if t.get('net_pnl') is not None]
    dd=recompute_dd(ds)
    m.update(archive_calendar_days=len(ds),effective_days=len({r['day'] for r in evals}),
             evaluable_signal_count=len(evals),unavailable_signal_count=len(unavailable),
             common_scope_excluded_count=sum(not r.get('scope_included',True) for r in sr),
             condition_rejected_count=len(cond),passed_condition_count=sum(r.get('pass') is True for r in evals),
             original_s1_candidate_count=len(sr),trade_days=len({t['day'] for t in ts}),
             effective_dates=sorted({r['day'] for r in evals}),trade_dates=sorted({t['day'] for t in ts}),
             max_drawdown_twd=dd if m['account_complete'] else None,
             observed_partial_max_drawdown_twd=dd,
             max_drawdown_fraction=dd/ds[0]['starting_equity'] if ds and m['account_complete'] else None,
             net_ex_best_trade=m['net_pnl']-max(vals) if m['net_pnl'] is not None and vals else m['net_pnl'],
             exit_reasons=dict(Counter(t['exit_reason'] for t in ts)),
             independent_signal_ids=len({t['signal_id'] for t in ts}),
             data_scope_status='PARTIAL_EVALUABILITY_CONDITIONAL_NOT_FULL_CERTIFIED_SESSION',
             evaluated_dates_intervals_basis='evaluation_mask.csv_AND_market_decision_mask.csv',
             selection_threshold_met=len(vals)>=10 and len({t['day'] for t in ts if t.get('net_pnl') is not None})>=5 and m['net_pnl'] is not None and m['net_pnl']>0,
             prospective_validation='PENDING',sample_status='ALL_DATES_SEEN_EXPLORATORY')
    m['mean_mae_twd']=statistics.mean(t['mae_net_twd'] for t in ts) if ts else None
    m['max_peak_to_final_drawdown_twd']=max((t.get('peak_to_final_drawdown_twd') for t in ts if t.get('peak_to_final_drawdown_twd') is not None),default=None)
    m['max_profit_giveback_twd']=max((t.get('positive_profit_surrendered_twd') for t in ts if t.get('positive_profit_surrendered_twd') is not None),default=None)
    m['profit_giveback_definition']='CAPPED_AT_OBSERVED_POSITIVE_PROFIT_NOT_ALL_LOSS_AFTER_PEAK'
    m['fees_and_tax']=m['commission']+m['sell_tax']
    return m


def public_trade(trade):
    return {k:v for k,v in trade.items() if k not in ('path','entry_fills','exit_fills')}


def annotate_path_profit(trade):
    points=[p for p in trade.get('path',[]) if p.get('net') is not None]
    peak=max((p['net'] for p in points),default=None)
    positive=max(0,peak) if peak is not None else None
    final=trade.get('marked_net_pnl')
    trade['observed_path_peak_net_twd']=peak
    trade['observed_path_trough_net_twd']=min((p['net'] for p in points),default=None)
    trade['positive_profit_surrendered_twd']=min(positive,max(0,positive-final)) if positive is not None and final is not None else None
    trade['peak_to_final_drawdown_twd']=positive-final if positive is not None and final is not None else None
    trade['mfe_mae_basis']='ORIGINAL_ROUND1_QUOTE_MARK_CONVENTION_SEPARATE_FULL_PATH_FIELDS'


def original_cohorts(tables):
    groups=defaultdict(dict)
    for raw in tables['trades']:
        if raw['experiment']!='B':continue
        t=dict(raw)
        for phase,name in [('ENTRY','entry_fills'),('EXIT','exit_fills')]:
            t[name]=[dict(f) for f in tables['fills'] if f['experiment']=='B' and f['scenario']==raw['scenario'] and f['signal_id']==raw['signal_id'] and f['phase']==phase]
        groups[t['scenario']][t['day']]=t
    return groups


def add_pair_diagnostics(enriched,cohorts):
    rows=[]
    for s in enriched:
        for scenario,byday in cohorts.items():
            old=byday.get(s['day'])
            if old is None or old['signal_id']!=s['signal_id']:continue
            for variant in VARIANTS:
                e=evaluate_variant(s,variant)
                if e['pass'] is True:continue
                value=old.get('net_pnl')
                rows.append({'day':s['day'],'symbol':s['symbol'],'signal_id':s['signal_id'],'scenario':scenario,
                  'variant':variant,'evaluable':e['evaluable'],'decision':'UNAVAILABLE' if not e['evaluable'] else 'BLOCKED_CONDITION',
                  'reason':e['reasons'],'round1_R0_original_net_pnl':value,
                  'round1_R0_original_quantity':old['quantity'],
                  'original_outcome':'UNKNOWN' if value is None else 'WIN' if value>0 else 'LOSS' if value<0 else 'FLAT',
                  'interpretation':'PAIRED_ORIGINAL_FILLED_SIGNAL_DIAGNOSTIC_NOT_SUBTRACTION_OR_COUNTERFACTUAL_ACCOUNT_RETURN'})
    return rows


def build_experiments(states,decisions,config,blocked):
    rows=[]
    for key,state in states.items():
        dims=tag(key);sr=[r for r in decisions if all(r[k]==v for k,v in dims.items())]
        for split in [None,'train','validation','retrospective_holdout']:
            m=metrics(state,sr,config,split)
            own=[p for p in blocked if p['scenario']==dims['scenario'] and p['variant']==dims['variant'] and (split is None or partition(p['day'],config)==split)]
            included_ids={x['signal_id'] for x in sr if x.get('scope_included') and (split is None or partition(x['day'],config)==split)}
            own=[p for p in own if p['signal_id'] in included_ids]
            rows.append({**dims,'statistics_scope':'FULL_PERIOD' if split is None else 'SPLIT_DIAGNOSTIC',
              'split':split or 'ALL_KNOWN_RESEARCH_DATES','exit_model':'E0' if dims['phase']=='ENTRY' else dims['variant'],
              'blocked_original_wins':sum(p['evaluable'] and p['original_outcome']=='WIN' for p in own),
              'blocked_original_losses':sum(p['evaluable'] and p['original_outcome']=='LOSS' for p in own),
              'unavailable_original_wins':sum(not p['evaluable'] and p['original_outcome']=='WIN' for p in own),
              'unavailable_original_losses':sum(not p['evaluable'] and p['original_outcome']=='LOSS' for p in own),**m})
    lookup={(r['scenario'],r['phase'],r['scope'],r['variant'],r['split']):r for r in rows}
    for r in rows:
        reference='R0' if r['phase']=='ENTRY' else 'E0'
        b=lookup[(r['scenario'],r['phase'],r['scope'],reference,r['split'])]
        r['incremental_net_vs_R0']=r['net_pnl']-b['net_pnl'] if r['net_pnl'] is not None and b['net_pnl'] is not None else None
        r['drawdown_change_vs_R0']=r['max_drawdown_twd']-b['max_drawdown_twd'] if r['max_drawdown_twd'] is not None and b['max_drawdown_twd'] is not None else None
        r['incremental_vs_cash']=r['net_pnl']
    return rows


def run(output):
    frozen=verify()
    cfg2=json.loads((HERE/'config.json').read_text());cfg1=json.loads((MODULE/'config.json').read_text())
    tables1=json.loads((R1/'tables.json').read_text());manifest1=json.loads((R1/'run_manifest.json').read_text())
    if list(manifest1['conditional_days'])!=cfg2['conditional_dates']:raise ValueError('UNDECLARED_DATA_RANGE')
    output=output.resolve()
    if HERE not in output.parents:raise ValueError('OUTPUT_MUST_BE_INSIDE_ROUND2')
    if output.exists():raise FileExistsError('Fresh output required; never overwrite')
    output.mkdir(parents=True)
    dump(output/'execution_start.json',{'started_at':datetime.now(timezone.utc).isoformat(),'freeze_sha256':sha(HERE/'freeze.json'),**SAFETY})
    dump(output/'precheck.json',json.loads((HERE/'precheck.json').read_text()))
    sources={r['day']:r for r in tables1['DATA_AUDIT'] if r['evaluation_grade']=='SIMPLIFIED_CONDITIONAL'}
    cohorts=original_cohorts(tables1)
    raw_byday=defaultdict(list)
    for s in tables1['signals']:
        if s['record_type']=='QUALIFIED_SIGNAL_NOT_A_TRADE' and s.get('entry_model')=='S1':raw_byday[s['day']].append(dict(s))
    eligibility,_=historical_eligibility(ROOT)
    scenarios={s['name']:{**cfg1,**s} for s in cfg1['stress_scenarios']}
    states={}
    for scenario in scenarios:
        for scope in cfg2['scope']:
            for variant in VARIANTS:states[(scenario,'ENTRY',scope,variant)]=empty_state()
        for e in cfg2['exit_variants']:states[(scenario,'ACCOUNT_EXIT','OWN_EVALUABLE',e)]=empty_state()
    fixed_states={(s,'FIXED_EXIT','FIXED_COHORT',e):empty_state() for s in scenarios for e in cfg2['exit_variants']}
    all_signals=[];decisions=[];masks=[];market_masks=[];diagnostics=[];paired=[];path_meta=[]
    fixed_issues=[];dedup_paths=set();reused=[]
    def register(trade,writer,scenario):
        path=trade.get('path',[])
        encoded=json.dumps(path,sort_keys=True,separators=(',',':'),default=lambda x:x.isoformat()).encode()
        pid=hashlib.sha256(encoded).hexdigest()
        trade['valuation_path_id']=pid
        if pid not in dedup_paths:
            dedup_paths.add(pid)
            record={'path_id':pid,'day':trade['day'],'symbol':trade['symbol'],'signal_id':trade['signal_id'],
                    'scenario':scenario,'exit_model':trade['exit_model'],'quantity':trade['quantity'],'path':path}
            writer.write(json.dumps(record,default=lambda x:x.isoformat(),separators=(',',':'))+'\n')
            path_meta.append({k:v for k,v in record.items() if k!='path'}|{'points':len(path)})
    with gzip.open(output/'valuation_paths.jsonl.gz','xt',encoding='utf-8') as path_writer:
      for day in cfg2['conditional_dates']:
        print('ROUND2',day,'loading',flush=True)
        source=ROOT/'stock-strategy/yuanta_intraday_shadow_v01/runtime/runs'/sources[day]['canonical_run_id']
        data=load_day([source])
        if (not data['point_in_time_pass'] or not data['source_hash_verified'] or data['callback_errors'] or data['day']!=day
            or data['source_status']!='COMPLETE' or data['recorded_coverage_status']!='FULL_SESSION'
            or data['recorded_stream_coverage_pass'] is not True):raise ValueError('SOURCE_GATE_FAILED:'+day)
        data['metadata']={k:v['name'] if isinstance(v,dict) else v for k,v in data['metadata'].items()}
        data['known_short_ineligible']={code:True for code,flag in eligibility.get(day,{}).items() if flag=='Y'}
        # Do not retain duplicate vendor payload strings in a model-only index.
        # All normalized fields and original on-disk bytes remain unchanged.
        for event in data['events']:event.pop('payload',None)
        data['index']=index_day(data)
        context=build_feature_context(data,raw_byday[day],cfg1)
        enriched=context['signals'];all_signals.extend(enriched)
        masks.extend({'day':day,**m} for m in context['common_mask'])
        market_masks.extend({'day':day,**m} for m in context['market_decision_mask'])
        common={m['signal_id']:m['common_evaluable'] for m in context['common_mask']}
        cache={};fixed_today={}
        # Reuse the identical original E0 entry contract, while reconstructing
        # quote paths and fixed-cohort exits needed for requested diagnostics.
        for scenario,cfg in scenarios.items():
            old=cohorts.get(scenario,{}).get(day)
            fixed_today[scenario]={}
            if old:
                signal=next(s for s in enriched if s['signal_id']==old['signal_id'])
                original_day=next(r for r in tables1['daily_results'] if r['experiment']=='B' and r['scenario']==scenario and r['day']==day)
                requested,_=target_quantity(signal,last_book(data['index'],signal['symbol'],stamp(signal['time'])),original_day['starting_equity'],cfg)
                if requested!=old['requested_quantity']:raise ValueError('ORIGINAL_SIZING_CHANGED')
                for exit_model in cfg2['exit_variants']:
                    trade,issues=fixed_trade(old,signal,data,data['index'],cfg,exit_model)
                    annotate_path_profit(trade)
                    register(trade,path_writer,scenario)
                    fixed_today[scenario][exit_model]=trade
                    fixed_issues.extend({**tag((scenario,'FIXED_EXIT','FIXED_COHORT',exit_model)),'day':day,'signal_id':signal['signal_id'],**x} for x in issues)
                    if exit_model=='E0':
                        fp=hashlib.sha256(json.dumps(cfg,sort_keys=True,default=str).encode()).hexdigest()
                        cache[(day,signal['signal_id'],'E0',round(original_day['starting_equity'],2),fp)]=(trade,issues)
                        reused.append({'day':day,'scenario':scenario,'signal_id':signal['signal_id'],'type':'ARCHIVED_R0_E0_FILL_PARITY_AND_PATH_RECONSTRUCTION'})
                        if scenario=='BASE_ASSUMED_LOTS':diagnostics.append(diagnose(old,trade,signal,data,data['index'],cfg))
                base=fixed_today[scenario]['E0']
                for e,t in fixed_today[scenario].items():
                    paired.append({'day':day,'symbol':signal['symbol'],'signal_id':signal['signal_id'],'scenario':scenario,'exit_model':e,
                        'quantity':t['quantity'],'entry_fill_parity':t['entry_fill_parity'],'e0_net_pnl':base['net_pnl'],
                        'net_pnl':t['net_pnl'],'incremental_vs_E0':t['net_pnl']-base['net_pnl'] if t['net_pnl'] is not None and base['net_pnl'] is not None else None,
                        'exit_reason':t['exit_reason'],'remaining_quantity':t['remaining_quantity'],'basis':'FIXED_ORIGINAL_ENTRY_NOT_EXECUTABLE_ACCOUNT_REPLAY'})
            for e in cfg2['exit_variants']:
                key=(scenario,'FIXED_EXIT','FIXED_COHORT',e);state=fixed_states[key]
                t=fixed_today[scenario].get(e);start=state['ending_equity']
                end=start+t['marked_net_pnl'] if t and t['marked_net_pnl'] is not None else start if not t else None
                prior_open=state.get('unresolved_remaining_quantity',0)
                open_quantity=prior_open+(t['remaining_quantity'] if t else 0)
                state['unresolved_remaining_quantity']=open_quantity
                if prior_open:end=None
                d={'day':day,'split':partition(day,cfg1),'starting_equity':start,'net_pnl':None if prior_open else t['marked_net_pnl'] if t else 0,
                   'current_fixed_trade_net_pnl':t['marked_net_pnl'] if t else 0,'prior_unresolved_quantity':prior_open,
                   'equity':end,'account_complete':open_quantity==0,'end_quantity':open_quantity,
                   'realized_pnl':t['realized_pnl'] if t else 0,'unrealized_pnl':t['unrealized_pnl'] if t else 0,
                   'running_max_drawdown_twd':0,'external_cashflow':0,'basis':'FIXED_COHORT_CUMULATIVE_DIAGNOSTIC_NOT_EXECUTABLE_ACCOUNT',**day_extrema([t] if t else [])}
                state['daily'].append(d)
                if t:state['trades'].append(dict(t))
                if end is not None:state['ending_equity']=end
                d['running_max_drawdown_twd']=recompute_dd(state['daily'])
        for key,state in states.items():
            dims=tag(key);variant=dims['variant'] if dims['phase']=='ENTRY' else 'R0'
            rows=[]
            for signal in enriched:
                evaluation=evaluate_variant(signal,variant)
                included=dims['scope']!='COMMON_EVALUABLE' or common[signal['signal_id']]
                decisions.append({**dims,'day':day,'signal_id':signal['signal_id'],'symbol':signal['symbol'],'time':signal['time'],
                   'scope_included':included,**{k:v for k,v in evaluation.items() if k not in ('variant','features')},
                   'filter_variant':variant,'known_eligibility_block':data['known_short_ineligible'].get(signal['symbol'],False),
                   'historical_qualification_certified':False,'round2_features':signal['round2_features']})
                if included and evaluation['pass'] is True:rows.append(signal)
            result=run_portfolio({day:data},{day:rows},scenarios[dims['scenario']],'B','S1','NONE',
                 dims['variant'] if dims['phase']=='ACCOUNT_EXIT' else 'E0',cache=cache,
                 initial_equity=state['ending_equity'],prior_peak=state['peak_equity'],prior_max_dd=state['max_drawdown'],prior_residual=state['residual'])
            d=result['daily'][0];d.update(split=partition(day,cfg1),**day_extrema(result['trades']))
            for t in result['trades']:
                annotate_path_profit(t)
                register(t,path_writer,dims['scenario'])
            append_state(state,result)
        # Original whole-period R0 and old train/validation exits must match,
        # not merely use the same strategy name.
        for scenario in scenarios:
            for phase,variant,oldlabel in [('ENTRY','R0','B'),('ACCOUNT_EXIT','E0','B'),('ACCOUNT_EXIT','E1','EXIT_E1'),('ACCOUNT_EXIT','E2','EXIT_E2')]:
                if oldlabel!='B' and partition(day,cfg1)=='retrospective_holdout':continue
                new=[t for t in states[(scenario,phase,'OWN_EVALUABLE',variant)]['trades'] if t['day']==day]
                old=[t for t in tables1['trades'] if t['experiment']==oldlabel and t['scenario']==scenario and t['day']==day]
                if [(t['signal_id'],t['quantity'],t['net_pnl'],t['exit_reason']) for t in new]!=[(t['signal_id'],t['quantity'],t['net_pnl'],t['exit_reason']) for t in old]:raise ValueError('EXISTING_EFFECTIVE_RESULT_CHANGED:'+scenario+oldlabel+day)
        # Paths are now immutably stored once; do not retain duplicate heavy
        # quote arrays across days just to author summaries.
        for state in [*states.values(),*fixed_states.values()]:
            for t in state['trades']:
                if t['day']==day:t.pop('path',None)
        dump(output/('day_'+day+'.json'),{'day':day,'source_run_id':data['selected_run_ids'],'signals':enriched,
             'common_mask':context['common_mask'],'market_decision_mask':context['market_decision_mask'],
             'completed_at':datetime.now(timezone.utc).isoformat(),**SAFETY})
        print('ROUND2',day,'completed','S1',len(enriched),'common',sum(common.values()),flush=True)
        del data,cache,context,fixed_today
        gc.collect()
    blocked=add_pair_diagnostics(all_signals,cohorts)
    experiments=build_experiments(states,decisions,cfg1,blocked)
    # Fixed-cohort summaries use only those fixed entries, not regenerated entry
    # opportunities or a claim of executable shared-capital performance.
    for key,state in fixed_states.items():
        dims=tag(key)
        for split in [None,'train','validation','retrospective_holdout']:
            ds=[d for d in state['daily'] if split is None or d['split']==split]
            ts=[t for t in state['trades'] if split is None or partition(t['day'],cfg1)==split]
            m=summarize(ts,ds,[]);dd=recompute_dd(ds)
            m.update(max_drawdown_twd=dd if m['account_complete'] else None,
               observed_partial_max_drawdown_twd=dd,
               max_drawdown_fraction=dd/ds[0]['starting_equity'] if ds and m['account_complete'] else None,
               mean_mae_twd=statistics.mean(t['mae_net_twd'] for t in ts) if ts else None,
               trade_days=len({t['day'] for t in ts}),effective_days=len({t['day'] for t in ts}),
               net_ex_best_trade=m['net_pnl']-max((t['net_pnl'] for t in ts if t['net_pnl'] is not None),default=0) if m['net_pnl'] is not None else None,
               evidence_scope='PAIRED_COHORT_NOT_EXECUTABLE_PORTFOLIO',incremental_vs_cash=m['net_pnl'],
               selection_threshold_met=False,sample_status='ALL_DATES_SEEN_EXPLORATORY',exit_reasons=dict(Counter(t['exit_reason'] for t in ts)))
            m['max_peak_to_final_drawdown_twd']=max((t.get('peak_to_final_drawdown_twd') for t in ts if t.get('peak_to_final_drawdown_twd') is not None),default=None)
            m['max_profit_giveback_twd']=max((t.get('positive_profit_surrendered_twd') for t in ts if t.get('positive_profit_surrendered_twd') is not None),default=None)
            experiments.append({**dims,'statistics_scope':'FULL_PERIOD' if split is None else 'SPLIT_DIAGNOSTIC','split':split or 'ALL_KNOWN_RESEARCH_DATES',**m})
    for r in experiments:
        if r['phase']=='FIXED_EXIT':
            base=next(x for x in experiments if x['phase']=='FIXED_EXIT' and x['scenario']==r['scenario'] and x['variant']=='E0' and x['split']==r['split'])
            r['incremental_net_vs_R0']=r['net_pnl']-base['net_pnl'] if r['net_pnl'] is not None and base['net_pnl'] is not None else None
    for scope in cfg2['scope']:
        experiments.append({'phase':'CASH','scope':scope,'variant':'CASH','scenario':'NO_ORDERS','statistics_scope':'FULL_PERIOD',
            'net_pnl':0,'gross_pnl':0,'max_drawdown_twd':0,'entries':0,'closed_trades':0,'win_rate':None,'profit_factor':None,
            'basis':'NO_POSITION_REFERENCE_NOT_A_SIGNAL_QUALITY_CLAIM'})
    for phase,scope,variants in [('ENTRY',s,VARIANTS) for s in cfg2['scope']]+[('ACCOUNT_EXIT','OWN_EVALUABLE',cfg2['exit_variants']),('FIXED_EXIT','FIXED_COHORT',cfg2['exit_variants'])]:
        for variant in variants:experiments.append({'phase':phase,'scope':scope,'variant':variant,'scenario':'STRICT_EXECUTABLE','statistics_scope':'FULL_PERIOD',
          'status':'UNAVAILABLE_NOT_ZERO','net_pnl':None,'gross_pnl':None,'max_drawdown_twd':None,'entries':None,'closed_trades':None,'win_rate':None,'profit_factor':None})
    trades=[];daily=[];fills=[];issues=list(fixed_issues);portfolios=[];groups=[]
    for key,state in {**states,**fixed_states}.items():
        dims=tag(key)
        for t in state['trades']:
            trades.append({**dims,**public_trade(t)})
            for phase,k in [('ENTRY','entry_fills'),('EXIT','exit_fills')]:
                fills.extend({**dims,'fill_phase':phase,'day':t['day'],'signal_id':t['signal_id'],'symbol':t['symbol'],**f} for f in t[k])
        daily.extend({**dims,**d} for d in state['daily'])
        issues.extend({**dims,**i} for i in state['issues'])
        portfolios.extend({**dims,**p} for p in state['dispositions'])
        for group_key in ['day','symbol','market_regime','timing_group']:
            buckets=defaultdict(list)
            for t in state['trades']:buckets[str(t.get(group_key,'UNKNOWN'))].append(t)
            for group,ts in buckets.items():
                vals=[t['net_pnl'] for t in ts if t['net_pnl'] is not None]
                groups.append({**dims,'group_by':group_key,'group':group,'entries':len(ts),'completed':len(vals),
                    'net_pnl':sum(vals) if len(vals)==len(ts) else None,'wins':sum(v>0 for v in vals),'losses':sum(v<0 for v in vals)})
    output_tables={'experiments':experiments,'trades':trades,'signals':decisions,'daily_results':daily,
       'execution_issues':issues,'fills':fills,'evaluation_mask':masks,'market_decision_mask':market_masks,
       'blocked_pairs':blocked,'paired_exits':paired,'round1_short_diagnostics':diagnostics,
       'portfolio_dispositions':portfolios,'grouped_diagnostics':groups,'valuation_path_inventory':path_meta,
       'round1_references':tables1['experiments'],'data_audit':tables1['DATA_AUDIT'],
       'round1_cd_comparison':json.loads((HERE/'precheck.json').read_text())['C_D_actual_trade_identity']}
    dump(output/'tables.json',output_tables)
    dump(output/'signals_enriched.json',all_signals)
    dump(output/'reuse_receipts.json',reused)
    final=verify()
    manifest={'research_id':'CASH_SHORT_INTRADAY_ROUND2','completed_at':datetime.now(timezone.utc).isoformat(),**SAFETY,
       'source_commit':frozen['source_commit'],'freeze_sha256':sha(HERE/'freeze.json'),
       'config_sha256':sha(HERE/'config.json'),'spec_sha256':sha(HERE/'EXPERIMENT_SPEC.md'),
       'source_root':str(ROOT),'conditional_days':cfg2['conditional_dates'],'all_dates_previously_seen':True,
       'new_unseen_validation':'PENDING','automatic_activation':False,'strict_executable':'NA',
       'tables_sha256':sha(output/'tables.json'),'valuation_paths_sha256':sha(output/'valuation_paths.jsonl.gz'),
       'original_results_unchanged':True,'frozen_input_and_code_checks':len(final['files']),
       'reproduction':'cd stock-strategy && python3 -B -m cash_short_intraday_research_v01.round2.run --output cash_short_intraday_research_v01/round2/results_reproduction',
       'limitations':['UNITS_UNVERIFIED','QUALIFICATION_DISPOSAL_HALT_HISTORY_INCOMPLETE','BOOK_EXCHANGE_CLOCK_UNAVAILABLE','ACTUAL_ACCOUNT_FEE_UNVERIFIED','ALL_13_DATES_SEEN','FIXED_COHORT_NOT_EXECUTABLE_ACCOUNT']}
    dump(output/'run_manifest.json',manifest)
    print('COMPLETE',output,'actual_orders=0',flush=True)
    return output


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True,type=Path)
    run(p.parse_args().output)
