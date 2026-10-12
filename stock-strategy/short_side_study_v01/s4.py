"""Frozen-cohort chronological LONG A + raw SHORT account research.

LONG and SHORT approval states are independent. The account's single daily
attempt is selected before any execution or PnL is inspected. No broker code.
"""
from dataclasses import asdict
import argparse
import hashlib
import json
from pathlib import Path

from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC, _decision_times
from yuanta_intraday_shadow_v01.live_parity_backtest import _feed_until
from yuanta_live_runtime_v01.strategy import ANTI_CHASE_ENTRY_POLICY, LIVE_EXIT_POLICY
from .policy import StudyEngine
from .replay import load_data, replay_trade
from .statistics import metrics, robustness

MODULE=Path(__file__).resolve().parent
REFERENCE=MODULE/'results_expanded'
SCENARIOS=('BASE_CURRENT_EXECUTION','PLUS_1_TICK_ADVERSE','DELAY_5_SECONDS')
VARIANTS=('S0','S1','S4')


def choose_boundary(long_signal, short_signal):
    pool=sorted([s for s in (long_signal,short_signal) if s is not None],key=lambda s:s.stock_id)
    return max(pool,key=lambda s:(s.score,s.entry_price*s.quantity)) if pool else None


def chronological_selection(stocks, market, date, eligibility):
    combined={**stocks,**market}
    metadata={sid:data.get('meta',{}).get('stock_name',sid) for sid,data in combined.items()}
    engines={v:StudyEngine(metadata,candidate_symbols=set(stocks),benchmark_symbol='0050',
                           variant=v,capital_twd=190000,eligibility=eligibility) for v in ('S0','S1')}
    tick_indexes={v:{s:0 for s in combined} for v in engines}
    book_indexes={v:{s:0 for s in combined} for v in engines}
    first={v:None for v in VARIANTS}
    approvals=[];ties=[];regime='UNKNOWN';regime_context=None
    for decision in _decision_times(date,SPEC['entry_start'],SPEC['last_entry_time']):
        boundary={}
        for variant,engine in engines.items():
            _feed_until(engine,combined,tick_indexes[variant],book_indexes[variant],decision)
            candidate=engine.choose_study_entry(decision)
            boundary[variant]=candidate
            if candidate is not None:
                approvals.append({'variant':variant,**asdict(candidate)})
                if first[variant] is None:
                    first[variant]=candidate
                    if variant=='S1':
                        # Observation only: a missing/stale benchmark never
                        # prevents the raw SHORT candidate being approved.
                        regime_context=engine._market_context(decision)
                        regime=regime_context['market_regime'] if regime_context else 'UNKNOWN'
        selected=choose_boundary(boundary['S0'],boundary['S1'])
        if selected is not None and first['S4'] is None:
            first['S4']=selected
        if boundary['S0'] is not None and boundary['S1'] is not None:
            ties.append({'decision_time':decision.isoformat(),
                         'long':asdict(boundary['S0']),'short':asdict(boundary['S1']),
                         'selected_stock_id':selected.stock_id,'selected_side':selected.side,
                         'account_opportunity_already_consumed':first['S4'].decision_time<decision})
    return first,approvals,ties,regime,regime_context


def read_eligibility(date, stocks):
    path=MODULE/'sources'/(date+'_twse_eligibility.json')
    if not path.is_file():
        return {}
    cached=json.loads(path.read_text())
    if cached['session_date']!=date:
        raise RuntimeError('eligibility date mismatch')
    return {sid:cached['codes'].get(sid,False) for sid,data in stocks.items()
            if data.get('meta',{}).get('market')=='TWSE'}


def replacements(daily):
    output=[]
    for scenario in SCENARIOS:
        for unit in (1000,1):
            subset=[r for r in daily if r['scenario']==scenario and r['volume_multiplier']==unit]
            dates=sorted({r['date'] for r in subset})
            for date in dates:
                rows={r['variant']:r for r in subset if r['date']==date}
                long,short,account=rows['S0'],rows['S1'],rows['S4']
                if account['side']=='SHORT':
                    kind='DIRECTION_REPLACEMENT' if long['side'] is not None else 'NEW_SHORT_OPPORTUNITY'
                    displaced=long if long['side'] is not None else None
                elif account['side']=='LONG':
                    kind='LONG_BLOCKS_LATER_SHORT' if short['side'] is not None else 'LONG_ONLY_OPPORTUNITY'
                    displaced=short if short['side'] is not None else None
                else:
                    kind='NO_APPROVED_SIGNAL';displaced=None
                delta=None if account['net_pnl'] is None or long['net_pnl'] is None else account['net_pnl']-long['net_pnl']
                output.append({'date':date,'scenario':scenario,'volume_multiplier':unit,'kind':kind,
                               'account_side':account['side'],'account_stock_id':account.get('stock_id'),
                               'account_signal_time':account.get('signal_time'),'account_net_pnl':account['net_pnl'],
                               's0_net_pnl':long['net_pnl'],'s1_net_pnl':short['net_pnl'],
                               'increment_vs_s0':delta,
                               'displaced_variant':None if displaced is None else displaced['variant'],
                               'displaced_side':None if displaced is None else displaced['side'],
                               'displaced_stock_id':None if displaced is None else displaced.get('stock_id'),
                               'displaced_signal_time':None if displaced is None else displaced.get('signal_time'),
                               'displaced_counterfactual_pnl':None if displaced is None else displaced['net_pnl'],
                               'counterfactual_is_not_in_account':True,
                               'long_losing_day_improved':long['net_pnl'] is not None and long['net_pnl']<0 and delta is not None and delta>0,
                               'long_winner_displaced':account['side']=='SHORT' and long['net_pnl'] is not None and long['net_pnl']>0})
    return output


def summarize(daily, replacement_rows):
    summaries=[];stress=[];regimes=[];attribution=[]
    for scenario in SCENARIOS:
        for unit in (1000,1):
            for variant in VARIANTS:
                rows=[r for r in daily if r['variant']==variant and r['scenario']==scenario and r['volume_multiplier']==unit]
                summaries.append({'variant':variant,'scenario':scenario,'volume_multiplier':unit,**metrics(rows)})
                stress.append({'variant':variant,'scenario':scenario,'volume_multiplier':unit,**robustness(rows)})
            short=[r for r in daily if r['variant']=='S1' and r['scenario']==scenario and r['volume_multiplier']==unit]
            for regime in ('BEARISH','NEUTRAL','BULLISH','UNKNOWN'):
                rows=[r for r in short if r['descriptive_regime_at_signal']==regime]
                regimes.append({'regime':regime,'scenario':scenario,'volume_multiplier':unit,**metrics(rows)})
            records=[r for r in replacement_rows if r['scenario']==scenario and r['volume_multiplier']==unit]
            effects={}
            for kind in ('NEW_SHORT_OPPORTUNITY','DIRECTION_REPLACEMENT','LONG_BLOCKS_LATER_SHORT','LONG_ONLY_OPPORTUNITY','NO_APPROVED_SIGNAL'):
                rows=[r for r in records if r['kind']==kind]
                effects[kind]={'days':len(rows),'dates':[r['date'] for r in rows],
                               'net_increment':sum(r['increment_vs_s0'] for r in rows) if all(r['increment_vs_s0'] is not None for r in rows) else None}
            attribution.append({'scenario':scenario,'volume_multiplier':unit,'effects':effects,
                                'long_losing_days_improved':[r['date'] for r in records if r['long_losing_day_improved']],
                                'long_winners_displaced':[r['date'] for r in records if r['long_winner_displaced']]})
    return {'summaries':summaries,'robustness':stress,'s1_regime_descriptive_only':regimes,'edge_attribution':attribution,
            'unit_status':'UNIT_UNVERIFIED','eligibility':'ELIGIBILITY_UNPROVEN',
            'out_of_sample':'NO_TRUE_OUT_OF_SAMPLE_SET','actual_orders':0,'actual_fills':0,'broker_connections':0}


def write_json(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')


def run(output):
    reference_manifest=json.loads((REFERENCE/'validation_manifest.json').read_text())
    for name,digest in reference_manifest['output_hashes'].items():
        if hashlib.sha256((REFERENCE/name).read_bytes()).hexdigest()!=digest:
            raise RuntimeError('prior reference output changed:'+name)
    sources=[r for r in json.loads((REFERENCE/'data_inventory.json').read_text()) if r['eligible']]
    output.mkdir(parents=True,exist_ok=True)
    contract={'variant':'S4_LONG_A_PLUS_RAW_SHORT','reference_commit':'f7ac2e5',
              'reference_manifest_sha256':hashlib.sha256((REFERENCE/'validation_manifest.json').read_bytes()).hexdigest(),
              'cohort_dates':sorted(r['session_date'] for r in sources),'capital_twd':190000,'daily_attempt_limit':1,
              'short_gate':'NONE_NEW','selection':'FIRST_APPROVED_BOUNDARY_THEN_EXISTING_SCORE_NOTIONAL_STABLE_SYMBOL_ORDER',
              'independent_long_short_confirmation_states':True,'anti_chase':ANTI_CHASE_ENTRY_POLICY,
              'base_spec':SPEC,'exit_policy':LIVE_EXIT_POLICY,'scenarios':SCENARIOS,'volume_units':(1000,1),
              'unit_status':'UNIT_UNVERIFIED','eligibility':'ELIGIBILITY_UNPROVEN','out_of_sample':'NO_TRUE_OUT_OF_SAMPLE_SET'}
    write_json(output/'contract.json',contract)
    daily=[];selections=[];all_approvals=[];all_ties=[];quality=[]
    for source in sources:
        date=source['session_date'];directory=Path(source['source_directory'])
        manifest_bytes=(directory/'run_manifest.json').read_bytes()
        manifest=json.loads(manifest_bytes)
        if manifest['manifest_hash']!=source['source_manifest_hash']:
            raise RuntimeError('frozen archive manifest changed:'+date)
        stocks,market,depths,manifest,coverage=load_data(directory)
        signals,approved,ties,regime,context=chronological_selection(stocks,market,date,read_eligibility(date,stocks))
        selections.append({'date':date,'signals':{v:None if s is None else asdict(s) for v,s in signals.items()},
                           's1_regime_at_signal':regime,'s1_regime_context':context})
        all_approvals.extend({'date':date,**r} for r in approved)
        all_ties.extend({'date':date,**r} for r in ties)
        quality.append({'date':date,'source':str(directory),'source_manifest_hash':manifest['manifest_hash'],'coverage':coverage})
        for variant in VARIANTS:
            signal=signals[variant]
            execution_variant=('S1' if signal and signal.side=='SHORT' else 'S0') if variant=='S4' else variant
            for scenario in SCENARIOS:
                for unit in (1000,1):
                    row=replay_trade(signal,stocks,depths,manifest,execution_variant,scenario,unit)
                    row.update(variant=variant,descriptive_regime_at_signal=regime if signal and signal.side=='SHORT' else None,
                               unit_status='UNIT_UNVERIFIED',selection_before_execution=True)
                    daily.append(row)
        write_json(output/'daily_results.json',daily)
        print(json.dumps({'date':date,'selected':None if signals['S4'] is None else {'side':signals['S4'].side,'stock_id':signals['S4'].stock_id,'at':signals['S4'].decision_time.isoformat()},'short_regime_descriptive':regime},ensure_ascii=False),flush=True)
    replaced=replacements(daily)
    summary=summarize(daily,replaced)
    for name,value in [('summary.json',summary),('selections.json',selections),('approved_boundaries.json',all_approvals),
                       ('simultaneous_cases.json',all_ties),('counterfactual_replacements.json',replaced),('source_quality.json',quality)]:
        write_json(output/name,value)
    print('S4_REPLAY_COMPLETE',flush=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Offline chronological LONG A + raw SHORT account')
    parser.add_argument('--output-dir',type=Path,default=MODULE/'results_s4')
    args=parser.parse_args(argv)
    run(args.output_dir)


if __name__=='__main__':
    main()
