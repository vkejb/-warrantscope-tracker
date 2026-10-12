"""Offline research entry point. Source data is opened read-only."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC
from yuanta_live_runtime_v01.strategy import ANTI_CHASE_ENTRY_POLICY, LIVE_EXIT_POLICY, LONG_MARKET_REGIME_POLICY
from .inventory import audit_sources, daily_inventory, audit_zip_catalog
from .policy import SHORT_GATE

MODULE = Path(__file__).resolve().parent


def contract():
    result = {
        'research_only':True, 'capital_twd':190000, 'max_trades_per_day':1,
        'base_signal_spec':deepcopy(SPEC), 'anti_chase':deepcopy(ANTI_CHASE_ENTRY_POLICY),
        'exit_policy':deepcopy(LIVE_EXIT_POLICY), 'long_gate':deepcopy(LONG_MARKET_REGIME_POLICY),
        'short_gate':deepcopy(SHORT_GATE),
        'variants':{'S0':'LONG_A', 'S1':'SHORT_BASE_DIAGNOSTIC', 'S2':'SHORT_SYMMETRIC_0050', 'S3':'CHRONOLOGICAL_LONG_A_PLUS_S2'},
        'same_boundary_tie_break':['score','entry_price_times_quantity','sorted_symbol_stable_order'],
        'scenarios':['BASE_CURRENT_EXECUTION','PLUS_1_TICK_ADVERSE','DELAY_5_SECONDS'],
        'qualification':'SIGNAL_ONLY / ELIGIBILITY_UNPROVEN',
        'out_of_sample':'NO_TRUE_OUT_OF_SAMPLE_SET',
        'authorized_broker_connections':0, 'authorized_broker_orders':0,
        'archive_selection':'ALL_DISCOVERED_ARCHIVES_QUALITY_FIRST_NOT_LONG_TRADE_DATE_FILTER',
        'depth_capacity_policy':'POSITIVE_DISPLAYED_SIZE_INCREMENTS_ONLY_NO_DUPLICATE_REPLENISHMENT',
        'conclusion_choices':['SHORT_EDGE_SUPPORTED','SHORT_EDGE_WEAK','NO_SHORT_EDGE','INSUFFICIENT_DATA'],
    }
    result['contract_hash'] = hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description='Isolated offline SHORT study')
    parser.add_argument('--runs-dir',type=Path,default=MODULE.parent/'yuanta_intraday_shadow_v01/runtime/runs')
    parser.add_argument('--archive-root',type=Path,action='append',default=None)
    parser.add_argument('--output-dir',type=Path,default=MODULE/'results_expanded')
    parser.add_argument('--audit-only',action='store_true')
    args=parser.parse_args(argv)
    args.output_dir.mkdir(parents=True,exist_ok=True)
    spec=contract()
    (args.output_dir/'contract.json').write_text(json.dumps(spec,ensure_ascii=False,indent=2)+'\n')
    rows=audit_sources(args.runs_dir,args.archive_root or [MODULE.parents[2]])
    zip_catalog=audit_zip_catalog(args.archive_root or [MODULE.parents[2]])
    (args.output_dir/'zip_catalog.json').write_text(json.dumps(zip_catalog,ensure_ascii=False,indent=2)+'\n')
    if any(r['status']=='QUOTE_ARCHIVE_REQUIRES_AUDIT' for r in zip_catalog):
        raise RuntimeError('Unprocessed compressed quote archives found; inspect zip_catalog.json before completing the study')
    (args.output_dir/'data_inventory.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
    (args.output_dir/'daily_inventory.json').write_text(json.dumps(daily_inventory(rows),ensure_ascii=False,indent=2)+'\n')
    eligible=[r for r in rows if r['eligible']]
    print(json.dumps({'phase':'SOURCE_AUDIT_COMPLETE','eligible_runs':eligible,'total_runs':len(rows),'contract_hash':spec['contract_hash']},ensure_ascii=False),flush=True)
    if not args.audit_only:
        run_research(args,eligible,spec)
    return 0


def run_research(args,eligible,spec):
    from .replay import load_data,select_signals,replay_trade
    from .statistics import metrics,robustness,correlation
    from entry_quality_edge_study_v01.analysis import SessionData,replay_production_a
    results=[]; qualified=[]; simultaneous=[]; eligibility_audit=[]; legacy=[]; signal_counts=[]; stream_quality=[]
    seen=set()
    for source in eligible:
        date=source['session_date']
        if date in seen:
            continue
        seen.add(date)
        directory=Path(source['source_directory'])
        stocks,market,depths,manifest,coverage=load_data(directory)
        stream_quality.append({'date':date,'source':str(directory),'coverage':coverage})
        watchlist=json.loads((directory/'watchlist.json').read_text())
        session=SessionData(date,directory,manifest,watchlist,stocks,market)
        for scenario in spec['scenarios']:
            legacy.append(replay_production_a(session,scenario=scenario))
        cached=MODULE/'sources'/(date+'_twse_eligibility.json')
        eligibility={}
        if cached.is_file():
            payload=json.loads(cached.read_text())
            for sid,data in stocks.items():
                if data.get('meta',{}).get('market')=='TWSE':
                    eligibility[sid]=payload['codes'].get(sid,False)
        eligibility_audit.append({'date':date,'known_exchange_flags':eligibility,'unknown_codes':sorted(set(stocks)-set(eligibility))})
        for variant in ('S0','S1','S2','S3'):
            signal,approved,ties,counts=select_signals(stocks,market,date,variant,eligibility)
            signal_counts.append({'date':date,'variant':variant,**counts})
            qualified.extend({'date':date,'variant':variant,**r} for r in approved)
            simultaneous.extend({'date':date,'variant':variant,**r} for r in ties)
            for scenario in spec['scenarios']:
                for multiplier in (1000,1):
                    result=replay_trade(signal,stocks,depths,manifest,variant,scenario,multiplier)
                    results.append(result)
            print(json.dumps({'date':date,'variant':variant,'first_signal':None if signal is None else {'stock_id':signal.stock_id,'side':signal.side,'at':signal.decision_time.isoformat()},'qualified_boundaries':len(approved)},ensure_ascii=False),flush=True)
        (args.output_dir/'daily_results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,default=str)+'\n')
        (args.output_dir/'s0_original_full_fill_reference.json').write_text(json.dumps(legacy,ensure_ascii=False,indent=2,default=str)+'\n')
        del stocks,market,depths
    summaries=[]; robustness_results=[]; complements=[]
    for scenario in spec['scenarios']:
        for multiplier in (1000,1):
            groups={variant:[r for r in results if r['variant']==variant and r['scenario']==scenario and r['volume_multiplier']==multiplier] for variant in ('S0','S1','S2','S3')}
            for variant,rows in groups.items():
                relevant=[c for c in signal_counts if c['variant']==variant]
                summaries.append({'scenario':scenario,'volume_multiplier':multiplier,'variant':variant,
                                  'raw_short_signal_boundaries':sum(c['raw_short_boundaries'] for c in relevant),
                                  'approved_short_signal_boundaries':sum(c['approved_short_boundaries'] for c in relevant),**metrics(rows)})
                robustness_results.append({'scenario':scenario,'volume_multiplier':multiplier,'variant':variant,**robustness(rows)})
            long,short,combined=groups['S0'],groups['S2'],groups['S3']
            no_long=[]; long_losses=[]
            for a,b in zip(long,short):
                if a['status']=='NO_TRADE' and b['status']=='SCORED_TRADE':
                    no_long.append({'date':a['date'],'short_pnl':b['net_pnl'],'stock_id':b['stock_id']})
                if a['net_pnl'] is not None and a['net_pnl']<0:
                    signals=[q for q in qualified if q['date']==a['date'] and q['variant']=='S2']
                    long_losses.append({'date':a['date'],'long_pnl':a['net_pnl'],'qualified_short_count':len(signals),'short_pnl':b['net_pnl']})
            diagnostic=groups['S1']
            diagnostics_no_long=[];diagnostic_losses=[]
            for a,b in zip(long,diagnostic):
                if a['status']=='NO_TRADE' and b.get('entry_quantity',0)>0:
                    diagnostics_no_long.append({'date':a['date'],'short_pnl':b['net_pnl'],'stock_id':b['stock_id'],'status':b['status']})
                if a['net_pnl'] is not None and a['net_pnl']<0:
                    diagnostic_losses.append({'date':a['date'],'long_pnl':a['net_pnl'],'s1_short_pnl':b['net_pnl'],
                                              's1_short_signal_count':sum(c['approved_short_boundaries'] for c in signal_counts if c['date']==a['date'] and c['variant']=='S1')})
            complements.append({'scenario':scenario,'volume_multiplier':multiplier,'daily_pnl_correlation':correlation(long,short),
                               'diagnostic_s0_s1_daily_pnl_correlation':correlation(long,diagnostic),
                               'diagnostic_long_no_trade_s1_short_trade':diagnostics_no_long,
                               'diagnostic_long_loss_days_s1':diagnostic_losses,
                               'long_no_trade_short_trade':no_long,'long_loss_days_short_qualified':long_losses,
                               'combined_contribution':{side:metrics([r for r in combined if r['side']==side]) for side in ('LONG','SHORT')},
                               'quality_change':{'long_mean_trade_pnl':metrics(long)['mean_trade_pnl'],'combined_mean_trade_pnl':metrics(combined)['mean_trade_pnl'],'long_trades':metrics(long)['trades'],'combined_trades':metrics(combined)['trades'],
                                                 'additional_entered_trades':metrics(combined)['entered_trades']-metrics(long)['entered_trades'],
                                                 'net_pnl_increment':None if metrics(combined)['net_pnl'] is None or metrics(long)['net_pnl'] is None else metrics(combined)['net_pnl']-metrics(long)['net_pnl']}})
    parity=[]
    for reference in legacy:
        actual=next(r for r in results if r['date']==reference['session_date'] and r['variant']=='S0' and r['scenario']==reference['scenario'] and r['volume_multiplier']==1000)
        parity.append({'date':actual['date'],'scenario':actual['scenario'],
                       'original_full_fill_pnl':reference['net_pnl_twd'],'depth_capped_pnl':actual['net_pnl'],
                       'matches':actual['net_pnl'] is not None and abs(reference['net_pnl_twd']-actual['net_pnl'])<.01,
                       'partial_entry':actual.get('partial_entry',False),'partial_exit':actual.get('partial_exit',False)})
    summary={'conclusion':'INSUFFICIENT_DATA','reason':f"Only {len(seen)} independent complete sessions; no true OOS; TPEX eligibility and SPARK depth-volume conversion unproven.",
             'sample_dates':sorted(seen),'qualification':'SIGNAL_ONLY / ELIGIBILITY_UNPROVEN','out_of_sample':'NO_TRUE_OUT_OF_SAMPLE_SET',
             'summaries':summaries,'complementarity':complements,'robustness':robustness_results,'s0_legacy_parity':parity,
             'contract_hash':spec['contract_hash'],'actual_orders':0,'actual_fills':0,'broker_connections':0,
             'limitations':['Displayed capacity is a proxy, not queue-position proof.','Volume multipliers 1000 and 1 are sensitivity assumptions, not verified SDK units.','Uncompleted exposure has null PnL; never treated as flat or zero.','S0 shares unchanged signal/exit policy; depth execution is reported separately from legacy full-fill proxy.','Partial-entry remainder is canceled in the model; no broker cancel latency evidence.','Daily realized drawdown is not intraday mark-to-market drawdown.']}
    primary=next(r for r in summaries if r['variant']=='S2' and r['scenario']=='BASE_CURRENT_EXECUTION' and r['volume_multiplier']==1000)
    summary['conclusion_evidence']={'s2_approved_signal_boundaries':primary['approved_short_signal_boundaries'],
                                    's2_entered_trades':primary['entered_trades'],'s2_closed_trades':primary['trades'],
                                    'independent_days':len(seen),'depth_units_proven':False,'true_oos_days':0}
    if primary['entered_trades']==0:
        summary['reason'] += ' The predeclared S2 gate produced zero entries.'
    else:
        summary['reason'] += f" S2 has only {primary['trades']} completed trades; execution units and independent OOS remain unproven."
    for name,value in [('summary.json',summary),('qualified_signals.json',qualified),('simultaneous_cases.json',simultaneous),('eligibility_audit.json',eligibility_audit),('signal_counts.json',signal_counts),('stream_quality.json',stream_quality)]:
        (args.output_dir/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')
    print(json.dumps({'phase':'REPLAY_COMPLETE','conclusion':summary['conclusion'],'sample_dates':sorted(seen)},ensure_ascii=False),flush=True)


if __name__=='__main__':
    raise SystemExit(main())
