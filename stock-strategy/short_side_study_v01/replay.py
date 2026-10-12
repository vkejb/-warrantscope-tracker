"""Offline account replay using inherited signals/exits and raw displayed depth."""
from bisect import bisect_right
from dataclasses import asdict
from datetime import timedelta
import json
import math

from paper_shadow_v01.runner import _load_market_context
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC, _decision_times, _projected_net_pnl, load_session
from yuanta_intraday_shadow_v01.exploratory_backtest import _read_jsonl, _tick_size
from yuanta_intraday_shadow_v01.live_parity_backtest import _feed_until, _parse_stamp, _record_book, _record_tick, _validate_full_session_coverage
from yuanta_live_runtime_v01.strategy import ManagedPosition, LiveSignal
from .execution import DepthBook, DepthExecution, Exposure
from .policy import StudyEngine


def load_data(directory):
    manifest=json.loads((directory/'run_manifest.json').read_text())
    stocks, coverage=load_session([directory])
    _validate_full_session_coverage(coverage)
    market=_load_market_context(directory,manifest)
    watchlist=json.loads((directory/'watchlist.json').read_text())
    ids={str(x['stock_id']) for x in watchlist['stocks']}
    stocks={sid:data for sid,data in stocks.items() if sid in ids}
    depths={sid:[] for sid in ids}
    name=next(n for n in manifest['artifacts'] if n in ('books.jsonl.gz','books.jsonl'))
    for sequence,row in enumerate(_read_jsonl(directory/name)):
        sid=str(row.get('stock_id',''))
        if sid not in ids:
            continue
        try:
            bids=tuple((float(p),int(float(q))) for p,q in zip(row['buy_prices'],row['buy_volumes']))
            asks=tuple((float(p),int(float(q))) for p,q in zip(row['sell_prices'],row['sell_volumes']))
            if len(bids)!=5 or len(asks)!=5 or any(not math.isfinite(p) or p<0 or q<0 for p,q in bids+asks):
                continue
            depths[sid].append(DepthBook(_parse_stamp(row['received_at']),bids,asks,str(sequence)))
        except (KeyError,ValueError,TypeError):
            continue
    for rows in depths.values():
        rows.sort(key=lambda b:b.received_at)
    start=_parse_stamp(manifest['ended_at']).replace(hour=9,minute=5,second=0,microsecond=0)
    end=start.replace(hour=13,minute=20)
    for kind in ('ticks','books'):
        observed=market['0050'][kind]
        if not observed or observed[0]['time']>start or observed[-1]['time']<end:
            raise RuntimeError('0050_'+kind.upper()+'_STREAM_COVERAGE_INCOMPLETE')
    coverage['stream_quality']={sid:{'ticks':len(stocks.get(sid,{}).get('ticks',[])),
                                      'books':len(stocks.get(sid,{}).get('books',[])),
                                      'raw_five_level_snapshots':len(depths[sid])} for sid in sorted(ids)}
    coverage['benchmark_coverage']={kind:{'count':len(market['0050'][kind]),
                                         'first':market['0050'][kind][0]['time'].isoformat(),
                                         'last':market['0050'][kind][-1]['time'].isoformat()} for kind in ('ticks','books')}
    return stocks,market,depths,manifest,coverage


def select_signals(stocks,market,date,variant,eligibility=None):
    combined={**stocks,**market}
    engine=StudyEngine({s:d.get('meta',{}).get('stock_name',s) for s,d in combined.items()},
                       candidate_symbols=set(stocks),benchmark_symbol='0050',variant=variant,capital_twd=190000,eligibility=eligibility)
    ticks={s:0 for s in combined}; books={s:0 for s in combined}
    selected=None; qualified=[]; raw_signals=[]; rejected=[]
    for at in _decision_times(date,SPEC['entry_start'],SPEC['last_entry_time']):
        _feed_until(engine,combined,ticks,books,at)
        signal=engine.choose_study_entry(at)
        if signal is not None and selected is None:
            selected=signal
        for row in engine.approved_at_boundary:
            qualified.append(asdict(LiveSignal(**row)))
        raw_signals.extend(engine.raw_at_boundary)
        rejected.extend({'decision_time':at.isoformat(),**r} for r in engine.diagnostics_at_boundary)
    counts={'raw_short_boundaries':sum(r['side']=='SHORT' for r in raw_signals),
            'raw_short_distinct_symbols':len({r['stock_id'] for r in raw_signals if r['side']=='SHORT'}),
            'approved_short_boundaries':sum(r['side']=='SHORT' for r in qualified),
            'approved_boundaries':len(qualified),'raw_signals':raw_signals,'rejections':rejected}
    return selected,qualified,engine.simultaneous_cases,counts


def replay_trade(signal,stocks,depths,manifest,variant,scenario,volume_multiplier):
    date=_parse_stamp(manifest['ended_at']).strftime('%Y%m%d')
    row={'date':date,'variant':variant,'scenario':scenario,'volume_multiplier':volume_multiplier,
         'status':'NO_TRADE','side':None,'net_pnl':0.,'residual_shares':0,'source_run_id':manifest['run_id'],
         'eligibility':'ELIGIBILITY_UNPROVEN','depth_units':'DEPTH_UNITS_UNPROVEN'}
    if signal is None:
        return row
    row.update(side=signal.side,stock_id=signal.stock_id,signal_time=signal.decision_time.isoformat())
    data=stocks[signal.stock_id]; raw_depth=depths[signal.stock_id]
    depth=[DepthBook(b.received_at,tuple((p,q*volume_multiplier) for p,q in b.bids),tuple((p,q*volume_multiplier) for p,q in b.asks),b.snapshot_id) for b in raw_depth]
    times=[b.received_at for b in depth]
    def book_at(at):
        i=bisect_right(times,at)-1
        return depth[i] if i>=0 else None
    delay=5 if scenario=='DELAY_5_SECONDS' else 0
    adverse=1 if scenario=='PLUS_1_TICK_ADVERSE' else 0
    entry_at=signal.decision_time+timedelta(seconds=delay)
    if delay:
        available=sorted({r['time'] for r in data['ticks'] if r['time']>=entry_at}|{b.received_at for b in depth if b.received_at>=entry_at})
        if not available or (available[0]-entry_at).total_seconds()>5:
            return {**row,'status':'ENTRY_DELAY_DATA_UNAVAILABLE','net_pnl':None}
        entry_at=available[0]
    book=book_at(entry_at)
    if book is None:
        return {**row,'status':'ENTRY_DEPTH_MISSING','net_pnl':None}
    limit=signal.entry_price
    if delay:
        base=book.asks[0][0] if signal.side=='LONG' else book.bids[0][0]
        limit=base+_tick_size(base) if signal.side=='LONG' else base-_tick_size(base)
    if adverse:
        step=_tick_size(limit if signal.side=='LONG' else math.nextafter(limit,-math.inf))
        limit += step*(1 if signal.side=='LONG' else -1)
    target=math.floor(190000/(limit*1000))*1000
    model=DepthExecution()
    quantity,price=model.fill(book,at=entry_at,side='BUY' if signal.side=='LONG' else 'SELL',quantity=max(target,1),limit_price=limit)
    if quantity==0:
        return {**row,'status':'UNFILLED_ENTRY','entry_quantity':0}
    # Keep the frozen adverse-limit execution price, with raw depth limiting
    # the quantity. Do not improve PnL by silently substituting best-price VWAP.
    price=limit
    exposure=Exposure(signal.side,quantity,quantity)
    engine=StudyEngine({signal.stock_id:signal.stock_name},variant=variant,capital_twd=190000,adverse_exit_ticks=adverse)
    ti={signal.stock_id:0}; bi={signal.stock_id:0}
    _feed_until(engine,{signal.stock_id:data},ti,bi,entry_at)
    position=ManagedPosition(signal.stock_id,signal.stock_name,signal.side,quantity,price,'OFFLINE',entry_at)
    row.update(entry_time=entry_at.isoformat(),entry_price=price,entry_quantity=quantity,target_quantity=target,partial_entry=quantity<target)
    mark_path=[];mfe_time=None;mae_time=None;mfe=None;mae=None
    exit_value=0.;covered=0
    def observe_mark(at):
        nonlocal mfe,mae,mfe_time,mae_time
        quote=engine.safe_exit_quote(position,at,max_age_seconds=3.)
        if quote is None:
            return
        # Executable-side price marks do not assert that the complete quantity
        # can liquidate at that price. Depth is independently required for fills.
        effective_exit=(exit_value+quote.price*exposure.remaining_shares)/quantity
        mark=_projected_net_pnl(signal.side,price,effective_exit,quantity)[3]
        if not mark_path or mark_path[-1]!=mark:
            mark_path.append(mark)
        if mfe is None or mark>mfe:
            mfe=mark;mfe_time=at.isoformat()
        if mae is None or mark<mae:
            mae=mark;mae_time=at.isoformat()
    observe_mark(entry_at)
    events=[(r['time'],0,'tick',r) for r in data['ticks'][ti[signal.stock_id]:]]
    events += [(r['time'],1,'book',r) for r in data['books'][bi[signal.stock_id]:]]
    end=_parse_stamp(manifest['ended_at'])
    hard=end.replace(hour=13,minute=20,second=0,microsecond=0)
    events += [(at,2,'decision',None) for at in _decision_times(date,SPEC['entry_start'],SPEC['hard_exit_time']) if entry_at<at<=end]
    if not any(e[0]==hard for e in events):
        events.append((hard,2,'decision',None))
    events.sort(key=lambda x:(x[0],x[1]))
    trigger=None; trigger_reason=None; exit_value=0.; covered=0; attempts=[]
    for at,_,kind,event in events:
        if at>end:
            break
        reversal=False
        if kind=='tick':
            _record_tick(engine,signal.stock_id,event)
        elif kind=='book':
            _record_book(engine,signal.stock_id,event)
        else:
            reversal=engine.opposite_signal(position,at)
        observe_mark(at)
        if trigger is None:
            try:
                decision=engine.evaluate_exit(position,at,reversal=reversal,max_quote_age_seconds=3.)
            except ValueError as exc:
                return {**row,'status':'UNSCORABLE_FIXED_MFE_BASIS','net_pnl':None,
                        'residual_shares':exposure.remaining_shares,'error':str(exc),
                        'mfe_net_pnl':mfe,'mae_net_pnl':mae,'giveback_net_pnl':None,'mark_pnl_path':mark_path}
            if at>=hard or decision is not None:
                trigger=at
                trigger_reason=decision.reason if decision is not None else 'HARD_EXIT_QUOTE_UNAVAILABLE'
        if trigger is None or at<trigger+timedelta(seconds=delay):
            continue
        quote=engine.safe_exit_quote(position,at,max_age_seconds=3.)
        book=book_at(at)
        if quote is None or book is None:
            continue
        filled,px=model.fill(book,at=at,side='SELL' if signal.side=='LONG' else 'BUY',quantity=exposure.remaining_shares,limit_price=quote.price)
        if not filled:
            continue
        px=quote.price
        exposure.apply_exit(filled); covered+=filled; exit_value+=filled*px
        attempts.append({'at':at.isoformat(),'shares':filled,'price':px,'remaining':exposure.remaining_shares})
        if exposure.status=='FLAT':
            break
    row.update(exit_trigger_time=None if trigger is None else trigger.isoformat(),exit_reason=trigger_reason,
               covered_shares=covered,residual_shares=exposure.remaining_shares,exit_fills=attempts,
               partial_exit=len(attempts)>1,remaining_entry_cancelled=target-quantity)
    row.update(mfe_net_pnl=mfe,mae_net_pnl=mae,mfe_time=mfe_time,mae_time=mae_time,
               mark_pnl_path=mark_path,mark_basis='COMPLETED_EXIT_VALUE_PLUS_REMAINING_EXECUTABLE_SIDE_QUOTE_NOT_CAPACITY_GUARANTEED')
    if exposure.remaining_shares:
        return {**row,'status':'OPEN_EXPOSURE','net_pnl':None,'giveback_net_pnl':None}
    exit_price=exit_value/quantity
    gross,commission,tax,net=_projected_net_pnl(signal.side,price,exit_price,quantity)
    mark_path.append(net)
    return {**row,'status':'SCORED_TRADE','exit_time':attempts[-1]['at'],'exit_price':exit_price,
            'mfe_net_pnl':max(mfe,net) if mfe is not None else net,'mae_net_pnl':min(mae,net) if mae is not None else net,
            'giveback_net_pnl':max(0.,(mfe if mfe is not None else net)-net),
            'gross_pnl':gross,'commission':commission,'tax':tax,'net_pnl':net}
