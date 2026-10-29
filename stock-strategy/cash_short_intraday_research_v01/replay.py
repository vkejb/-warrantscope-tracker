"""Offline portfolio replay. Quote-model scenarios are NOT verified executions."""
from __future__ import annotations
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import math
import statistics
import hashlib
import json
from decimal import Decimal, ROUND_CEILING

from yuanta_live_runtime_v01.strategy import LiveDirectionEngine, ManagedPosition
from .execution import cost, execute_ioc, tick_size, shift_price
from .signals import SignalScanner, feed_engine


def stamp(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def decision_grid(day, tz):
    start = datetime.strptime(day+'090500', '%Y%m%d%H%M%S').replace(tzinfo=tz)
    return [start+timedelta(seconds=30*n) for n in range(491)]


def scan_day(data, config):
    scanner = SignalScanner(data['metadata'], set(data['candidates']), config)
    events = data['events']
    if not events:
        return [], []
    grid = decision_grid(data['day'], events[0]['received_at'].tzinfo)
    cursor = 0
    signals, snapshots = [], []
    for now in grid:
        while cursor < len(events) and events[cursor]['received_at'] <= now:
            scanner.ingest(events[cursor])
            cursor += 1
        current = scanner.decide(now)
        for row in current:
            row = dict(row)
            row['day'] = data['day']
            signals.append(row)
        if current:
            snapshots.extend({'time': now, 'symbol': x['symbol'], 'features': x['features']}
                             for x in current)
        snapshots.extend({'record_type':'LONG_APPROVAL_DIAGNOSTIC',**x} for x in getattr(scanner,'long_diagnostics',[]))
    snapshots.extend({'record_type':'STATE_TRANSITION',**x} for x in scanner.transitions)
    snapshots.append({'record_type':'INGEST_DIAGNOSTIC','ingest_counts':dict(scanner.ingest_counts),
                      'rejection_counts':dict(scanner.rejection_counts)})
    return signals, snapshots


def index_day(data):
    books, events = defaultdict(list), defaultdict(list)
    for event in data['events']:
        events[event['symbol']].append(event)
        if event['kind'] == 'book':
            books[event['symbol']].append(event)
    return {'books': books, 'events': events,
            'book_times': {s:[x['received_at'] for x in rows] for s,rows in books.items()}}


def last_book(index, symbol, now):
    times = index['book_times'].get(symbol, [])
    at = bisect_right(times, now)-1
    return index['books'][symbol][at] if at >= 0 else None


def future_books(index, symbol, now):
    rows = index['books'].get(symbol, [])
    times = index['book_times'].get(symbol, [])
    return rows[bisect_left(times, now):bisect_right(times, now+timedelta(seconds=10))]


def fee(notional, config):
    return max(int(config['minimum_commission']), int((Decimal(str(round(notional, 6)))*Decimal(str(config['commission_rate']))).to_integral_value(rounding=ROUND_CEILING)))


def tax(notional, config):
    return int((Decimal(str(round(notional, 6)))*Decimal(str(config['sell_tax_rate']))).to_integral_value(rounding=ROUND_CEILING))


def target_quantity(signal, quote, equity, config):
    side = signal['side']
    levels = quote.get('asks' if side == 'LONG' else 'bids', [])
    if not levels:
        return 0, 'NO_SIGNAL_SIDE_QUOTE'
    observed_price = float(levels[0][0])
    # Size against the adverse entry limit, including fees. For SHORT the
    # lower limit is worst structural risk; a higher price is worst notional.
    price = shift_price(observed_price,2,1)
    risk_price = shift_price(observed_price,2,-1) if side=='SHORT' else price
    cap = min(float(config['capital_twd']), float(equity))
    # Reserve round-trip fees and one risk budget in addition to entry principal.
    # This cannot increase original maximum-cap size; all strategies use it.
    quantity = math.floor(cap/(price*1000))*1000
    while quantity > 0 and price*quantity+fee(price*quantity, config) > cap:
        quantity -= 1000
    structure = signal.get('structure_high')
    if side == 'SHORT' and signal['entry_model'] in ('S1', 'S2') and structure:
        while quantity > 0:
            risk = -cost(side, risk_price, max(risk_price, float(structure))+tick_size(float(structure)),
                         quantity, config)['net']
            if risk <= config['risk_per_trade_twd']:
                break
            quantity -= 1000
    return quantity, 'MINIMUM_BOARD_LOT_EXCEEDS_CAP_OR_RISK' if quantity <= 0 else 'OK'


def _weighted(fills):
    quantity = sum(int(x['quantity']) for x in fills)
    return sum(x['price']*x['quantity'] for x in fills)/quantity if quantity else None


def _mark(side, entry_price, original_qty, remaining, exit_fills, mark, config):
    """Actual modeled exits plus marked residual; never substitutes a final fill."""
    entry_value = entry_price*original_qty
    closed = original_qty-remaining
    exit_value = sum(x['price']*x['quantity'] for x in exit_fills)
    exit_fees = sum(fee(sum(x['price']*x['quantity'] for x in order), config)
                    for order in _group_orders(exit_fills).values())
    entry_fee = fee(entry_value, config)
    if side == 'SHORT':
        entry_tax = tax(entry_value,config)
        realized = entry_price*closed-exit_value-entry_fee*(closed/original_qty)-entry_tax*(closed/original_qty)-exit_fees
        unrealized = None if remaining and mark is None else (
            (entry_price-float(mark))*remaining-(entry_fee+entry_tax)*(remaining/original_qty)
            - (fee(float(mark)*remaining,config) if remaining else 0)) if remaining else 0.0
        sell_tax = entry_tax
    else:
        sell_tax = sum(tax(sum(x['price']*x['quantity'] for x in order),config)
                  for order in _group_orders(exit_fills).values())
        realized = exit_value-entry_price*closed-entry_fee*(closed/original_qty)-exit_fees-sell_tax
        unrealized = None if remaining and mark is None else (
            (float(mark)-entry_price)*remaining-entry_fee*(remaining/original_qty)
            - (fee(float(mark)*remaining,config)+tax(float(mark)*remaining,config) if remaining else 0)) if remaining else 0.0
    return {'realized_pnl':round(realized,2), 'unrealized_pnl':None if unrealized is None else round(unrealized,2),
            'marked_net':None if unrealized is None else round(realized+unrealized,2),
            'commission':entry_fee+exit_fees, 'tax':sell_tax}


def _group_orders(fills):
    orders = defaultdict(list)
    for x in fills:
        orders[x.get('order_id', str(x['time']))].append(x)
    return orders


def simulate_trade(signal, data, index, equity, config, exit_model='E0'):
    now, symbol, side = stamp(signal['time']), signal['symbol'], signal['side']
    quote = last_book(index, symbol, now)
    issues = []
    if quote is None or (now-quote['received_at']).total_seconds() > config['book_stale_seconds']:
        return None, [{'time':now,'symbol':symbol,'code':'NO_FRESH_SIGNAL_BOOK'}]
    quantity, reason = target_quantity(signal, quote, equity, config)
    if not quantity:
        return None, [{'time':now,'symbol':symbol,'code':reason}]
    used = {}
    result = execute_ioc('BUY' if side=='LONG' else 'SELL', quantity, now, quote,
                         future_books(index,symbol,now), config, used_depth=used)
    for code in result.get('issues', []):
        issues.append({'time':now,'symbol':symbol,'code':str(code),'phase':'ENTRY'})
    fills = result['fills']
    qty = sum(x['quantity'] for x in fills)
    if not qty:
        return None, issues+[{'time':now,'symbol':symbol,'code':'UNFILLED_ENTRY_IOC','requested_quantity':quantity}]
    entry_price, entry_time = _weighted(fills), max(stamp(x['time']) for x in fills)
    if qty < quantity:
        issues.append({'time':entry_time,'symbol':symbol,'code':'PARTIAL_ENTRY_REMAINDER_CANCELED_MODEL',
                       'requested_quantity':quantity,'filled_quantity':qty,'remaining_quantity':quantity-qty})
    if entry_price*qty+fee(entry_price*qty,config)>min(equity,config['capital_twd'])+1e-6:
        issues.append({'time':entry_time,'symbol':symbol,'code':'ENTRY_CAP_BREACH_EXPOSURE_RETAINED'})
    engine = LiveDirectionEngine({symbol:data['metadata'].get(symbol,symbol)}, candidate_symbols={symbol})
    position = ManagedPosition(symbol, data['metadata'].get(symbol,symbol), side, qty, entry_price,
                               'OFFLINE_MODEL_ONLY', entry_time)
    # Derivation is pure, uses only entry price/quantity and frozen fees.
    initial_stop = engine._derive_initial_stop_price(position)
    price_risk = abs(entry_price-initial_stop)
    remaining, cover_fills = qty, []
    path, trigger_time, exit_reason = [], None, None
    peak_net, trough_net, last_mark = 0.0, 0.0, None
    last_mark_time, last_decision = None, None
    pending_until, order_number = None, 0
    pending_fills=[]
    full_cover_time = None
    day_events = index['events'][symbol]
    grid_start=now.replace(hour=9,minute=5,second=0,microsecond=0)
    exit_decisions=[{'kind':'decision','symbol':symbol,'received_at':grid_start+timedelta(seconds=30*n)} for n in range(511)]
    day_events=sorted([*day_events,*exit_decisions],key=lambda e:(e['received_at'],e['kind']=='decision'))
    for event in day_events:
        at = event['received_at']
        feed_engine(engine,event)
        if at < entry_time:
            continue
        due=[x for x in pending_fills if stamp(x['time'])<=at]
        pending_fills=[x for x in pending_fills if stamp(x['time'])>at]
        if due:
            cover_fills.extend(due)
            remaining-=sum(x['quantity'] for x in due)
            if remaining<0:
                raise AssertionError('COVER_OVERFILL')
            if remaining==0:
                full_cover_time=max(stamp(x['time']) for x in cover_fills)
                mark=_mark(side,entry_price,qty,0,cover_fills,None,config)
                path.append({'time':full_cover_time,'net':mark['marked_net'],'remaining_quantity':0})
                break
        if at.time().strftime('%H:%M') >= config['execution_cutoff']:
            break
        if event['kind'] not in ('book','decision'):
            continue
        is_decision=event['kind']=='decision'
        quote_event=last_book(index,symbol,at) if is_decision else event
        if quote_event is None or (at-quote_event['received_at']).total_seconds()>10:
            continue
        bids, asks = quote_event.get('bids',[]), quote_event.get('asks',[])
        levels = bids if side=='LONG' else asks
        if not levels or not 0 < levels[0][0] < 99999:
            continue
        # A quote mark is not a fill and never removes remaining exposure.
        px = float(levels[0][0])
        last_mark = shift_price(px,int(config.get('extra_slippage_ticks',1)),-1 if side=='LONG' else 1)
        last_mark_time = at
        mark = _mark(side,entry_price,qty,remaining,cover_fills,last_mark,config)
        if mark['marked_net'] is not None:
            path.append({'time':at,'net':mark['marked_net'],'remaining_quantity':remaining})
            peak_net=max(peak_net,mark['marked_net']); trough_net=min(trough_net,mark['marked_net'])
        if pending_until is not None and at < pending_until:
            continue
        if pending_until is not None:
            pending_until = None
        if trigger_time is None:
            reversal=False
            if is_decision:
                reversal=engine.opposite_signal(position,at)
                last_decision=at
            # Explicitly register available book-side quotes; no trade-price fill fallback.
            engine.record_exit_quote(symbol,at=quote_event.get('exchange_time') or quote_event['received_at'],received_at=quote_event['received_at'],
                                     bid=bids[0][0] if bids else None,ask=asks[0][0] if asks else None)
            decision=engine.evaluate_exit(position,at,reversal=reversal,max_quote_age_seconds=10)
            if decision:
                exit_reason=decision.reason
            elif at.time().strftime('%H:%M') >= config['cover_start']:
                exit_reason='HARD_EXIT_1320'
            elif exit_model=='E1' and side=='SHORT' and signal.get('structure_high') and px >= float(signal['structure_high'])+tick_size(float(signal['structure_high'])):
                exit_reason='STRUCTURE_INVALIDATION'
            elif exit_model=='E2' and side=='SHORT' and (at-entry_time).total_seconds()>=config['exit_E2']['minimum_hold_seconds']:
                rows=list(engine._states[symbol].ticks)
                recent=[x for x in rows if at-timedelta(seconds=60)<=x['time']<=at and x['received_at']<=at]
                reference=[x for x in rows if at-timedelta(seconds=300)<=x['time']<=at and x['received_at']<=at]
                total=sum(x['volume'] for x in recent)
                flow=sum(engine._signed_volume(x) for x in recent)/total if total else None
                threshold=engine._percentile90([x['volume'] for x in reference]) if reference else 0
                large=[x for x in recent if x['volume']>=threshold]
                large_total=sum(x['volume'] for x in large)
                large_flow=sum(engine._signed_volume(x) for x in large)/large_total if large_total else None
                if flow is not None and large_flow is not None and mark['marked_net'] is not None and mark['marked_net']/3500<config['exit_E2']['net_r_below'] and flow>config['exit_E2']['buy_flow_above'] and large_flow>config['exit_E2']['large_buy_flow_above']:
                    exit_reason='FIVE_MINUTE_WEAK_BUY_FLOW'
            if exit_reason:
                trigger_time=at
        if trigger_time is None:
            continue
        order_number+=1
        cover_config={**config,'is_cover':side=='SHORT','is_exit':True}
        cover=execute_ioc('SELL' if side=='LONG' else 'BUY',remaining,at,quote_event,
                          future_books(index,symbol,at),cover_config,used_depth=used)
        for code in cover.get('issues',[]):
            issues.append({'time':at,'symbol':symbol,'code':str(code),'phase':'EXIT'})
        new=[]
        for fill in cover['fills']:
            item={**fill,'order_id':order_number}
            if item['quantity']>remaining-sum(x['quantity'] for x in new):
                raise AssertionError('COVER_OVERFILL')
            new.append(item)
        pending_fills.extend(new)
        pending_until=max((stamp(x['time']) for x in new),default=at+timedelta(seconds=config['latency_seconds']+config['quote_wait_seconds']))
    mark=_mark(side,entry_price,qty,remaining,cover_fills,last_mark,config)
    exit_price=_weighted(cover_fills)
    gross = ((exit_price-entry_price)*(qty-remaining)*(1 if side=='LONG' else -1)) if exit_price else 0.0
    if remaining:
        issues.append({'time':last_mark_time or entry_time,'symbol':symbol,'code':'COVER_INCOMPLETE_EXPOSURE_RETAINED',
                       'remaining_quantity':remaining,'estimated_additional_general_sell_tax':round(entry_price*remaining*0.0015,2) if side=='SHORT' else None,
                       'unestimable_costs':'BROKER_STOCK_DIFFERENCE_BORROW_BUYIN_FEES_AND_PRICE_GAP'})
    entry_value=round(entry_price*qty,6)
    exit_value=round(sum(x['price']*x['quantity'] for x in cover_fills),6)
    cash_delta=(-entry_value+exit_value if side=='LONG' else entry_value-exit_value)-mark['commission']-mark['tax']
    trade={
        'signal_id':signal['signal_id'],'day':data['day'],'symbol':symbol,'stock_name':data['metadata'].get(symbol,symbol),
        'side':side,'entry_model':signal['entry_model'],'exit_model':exit_model,'signal_time':now,
        'entry_time':entry_time,'entry_price':entry_price,'requested_quantity':quantity,'quantity':qty,
        'entry_unfilled_quantity':quantity-qty,'exit_time':full_cover_time,'exit_price':exit_price,
        'exit_trigger_time':trigger_time,'exit_reason':exit_reason or 'NO_EXECUTABLE_EXIT',
        'closed_quantity':qty-remaining,'remaining_quantity':remaining,
        'fill_status':'CLOSED' if not remaining else 'OPEN_COVER_INCOMPLETE',
        'gross_pnl':round(gross,2) if not remaining else None,'realized_gross_pnl':round(gross,2),
        'commission':mark['commission'],'sell_tax':mark['tax'],
        'net_pnl':mark['marked_net'] if not remaining else None,
        'realized_pnl':mark['realized_pnl'],'unrealized_pnl':mark['unrealized_pnl'],
        'marked_net_pnl':mark['marked_net'],'mark_time':last_mark_time,
        'net_R':mark['marked_net']/3500 if mark['marked_net'] is not None else None,
        'initial_stop_price':initial_stop,'initial_price_R':price_risk,
        'mfe_net_twd':round(peak_net,2),'mae_net_twd':round(trough_net,2),
        'maximum_profit_giveback_twd':round(peak_net-(mark['marked_net'] or 0),2),
        'mfe_price_R':position.mfe_r,'holding_seconds':((full_cover_time or last_mark_time or entry_time)-entry_time).total_seconds(),
        'last_exit_evaluation_time':last_mark_time,'original_stop_net_twd':3500,
        'last_untriggered_reason':None if trigger_time else 'NO_EXIT_TRIGGER_WITH_EXECUTABLE_QUOTE',
        'model_cash_delta':round(cash_delta,2),'restricted_short_proceeds':entry_price*remaining if side=='SHORT' else 0,
        'cash_basis':'MODEL_PRE_SETTLEMENT_NOT_ACTUAL_BROKER_CASH',
        'cost_status':'CODE_RATE_PROXY_ACTUAL_ACCOUNT_UNVERIFIED',
        'evidence_status':'CONDITIONAL_UNITS_ELIGIBILITY_BOOK_CLOCK_UNVERIFIED',
        'market_regime':signal.get('features',{}).get('market_regime','UNKNOWN'),
        'relative_strength_5m':signal.get('features',{}).get('relative_strength_5m'),
        'timing_group':'EARLY' if now.hour==9 and now.minute<30 else 'MIDDAY',
        'entry_fills':fills,'exit_fills':cover_fills,'path':path,
    }
    return trade, issues


def accepted(signal, model, market_filter):
    if signal['entry_model']!=model:
        return False
    return market_filter!='WEAK' or signal.get('features',{}).get('market_filter_pass') is True


def choose_candidates(signals, mode, short_model, market_filter):
    longs=[s for s in signals if s['entry_model']=='LONG']
    shorts=[s for s in signals if accepted(s,short_model,market_filter)]
    if mode=='A': return longs
    if mode=='B': return shorts
    if mode=='C': return longs+shorts
    if mode=='D': return longs if longs else shorts
    raise ValueError(mode)


def run_portfolio(days, signals_by_day, config, mode, short_model='S1', market_filter='NONE',exit_model='E0',cache=None, initial_equity=None, prior_peak=None, prior_max_dd=0.0, prior_residual=False):
    equity=float(config['capital_twd'] if initial_equity is None else initial_equity)
    peak=max(equity,prior_peak if prior_peak is not None else equity); max_dd=prior_max_dd
    trades, daily, issues, dispositions = [],[],[],[]
    cache={} if cache is None else cache
    blocked_residual=prior_residual
    for day,data in days.items():
        starting=equity
        day_signals=signals_by_day.get(day,[])
        selected=None; index=data['index']
        by_time=defaultdict(list)
        for signal in day_signals: by_time[stamp(signal['time'])].append(signal)
        for at,rows in sorted(by_time.items()):
            attempted_this_boundary=False
            for raw_signal in rows:
                if mode!='A' and raw_signal['entry_model']==short_model:
                    market=raw_signal.get('features',{}).get('market_filter_pass')
                    reason=None
                    if market_filter=='WEAK' and market is not True:
                        reason='MARKET_DATA_UNAVAILABLE' if market is None else 'MARKET_WEAK_FILTER_FAILED'
                    elif mode=='D' and any(s['entry_model']=='LONG' for s in rows):
                        reason='QUALIFIED_LONG_EXISTS_AT_CURRENT_DECISION'
                    if reason:
                        dispositions.append({'day':day,'signal_id':raw_signal['signal_id'],'symbol':raw_signal['symbol'],
                                             'time':at,'reason':reason,'side':raw_signal['side']})
            eligible=choose_candidates(rows,mode,short_model,market_filter)
            eligible.sort(key=lambda s:(-float(s['score']),s['side']!='LONG',s['symbol']))
            for signal in eligible:
                if signal['side']=='SHORT' and data.get('known_short_ineligible',{}).get(signal['symbol']) is True:
                    reason='KNOWN_HISTORICAL_SELL_FIRST_SUSPENSION'
                elif blocked_residual:
                    reason='PRIOR_DAY_RESIDUAL_EXPOSURE_BLOCKS_NEW_ENTRY'
                elif selected is not None:
                    reason='SHORT_USED_DAILY_BUDGET_MISSED_LONG' if selected['side']=='SHORT' and signal['side']=='LONG' else 'DAILY_TRADE_LIMIT'
                elif attempted_this_boundary:
                    reason='HIGHER_PRIORITY_IOC_PENDING_AT_THIS_DECISION'
                else:
                    attempted_this_boundary=True
                    fingerprint=hashlib.sha256(json.dumps(config,sort_keys=True,default=str).encode()).hexdigest()
                    key=(day,signal['signal_id'],exit_model,round(equity,2),fingerprint)
                    if key not in cache:
                        cache[key]=simulate_trade(signal,data,index,equity,config,exit_model if signal['side']=='SHORT' else 'E0')
                    trade, execution_issues=cache[key]
                    issues.extend({**x,'signal_id':signal['signal_id'],'day':day} for x in execution_issues)
                    if trade:
                        selected=dict(trade); trades.append(selected)
                        reason='ADOPTED_PARTIAL' if trade['entry_unfilled_quantity'] else 'ADOPTED'
                    else:
                        reason='NO_FILL_OR_CAP_RISK'
                dispositions.append({'day':day,'signal_id':signal['signal_id'],'symbol':signal['symbol'],
                                     'time':at,'reason':reason,'side':signal['side']})
        day_realized=0.0; day_unrealized=0.0; day_net=0.0
        if selected:
            day_realized=selected['realized_pnl']
            day_unrealized=selected['unrealized_pnl']
            day_net=selected['marked_net_pnl']
            for point in sorted(selected['path'],key=lambda p:p['time']):
                marked_equity=starting+point['net']
                peak=max(peak,marked_equity)
                max_dd=max(max_dd,peak-marked_equity)
            if day_net is not None: equity=starting+day_net
            blocked_residual=selected['remaining_quantity']>0
        if blocked_residual and selected is None:
            # No subsequent-day recovery source is silently invented.
            day_net=None; day_realized=None; day_unrealized=None
        peak=max(peak,equity)
        max_dd=max(max_dd,peak-equity)
        daily.append({'day':day,'starting_equity':starting,'net_pnl':day_net,'realized_pnl':day_realized,
                      'unrealized_pnl':day_unrealized,'equity':equity if day_net is not None else None,
                      'drawdown_twd':peak-equity,'running_max_drawdown_twd':max_dd,
                      'fees':selected['commission'] if selected else 0,'sell_tax':selected['sell_tax'] if selected else 0,
                      'entry_quantity':selected['quantity'] if selected else 0,
                      'end_quantity':selected['remaining_quantity'] if selected else (None if blocked_residual else 0),
                      'positions':selected['symbol'] if selected and selected['remaining_quantity'] else '',
                      'capital_used':selected['entry_price']*selected['quantity'] if selected else 0,
                      'available_capital':None if blocked_residual else min(equity,config['capital_twd']),
                      'cash':starting+selected['model_cash_delta'] if selected else (None if blocked_residual else equity),
                      'restricted_short_proceeds':selected['restricted_short_proceeds'] if selected else None if blocked_residual else 0,
                      'cash_basis':'MODEL_PRE_SETTLEMENT_NOT_ACTUAL_BROKER_CASH',
                      'account_complete':not blocked_residual,'external_cashflow':0})
    return {'trades':trades,'daily':daily,'issues':issues,'dispositions':dispositions,
            'metrics':summarize(trades,daily,dispositions), 'ending_equity':equity,
            'peak_equity':peak,'max_drawdown':max_dd,'residual':blocked_residual}


def summarize(trades,daily,dispositions):
    closed=[t for t in trades if t['net_pnl'] is not None]
    values=[t['net_pnl'] for t in closed]
    wins=sum(x>0 for x in values); losses=sum(x<0 for x in values)
    profit=sum(x for x in values if x>0); loss=-sum(x for x in values if x<0)
    streak=longest=0
    for t in closed:
        streak=streak+1 if t['net_pnl']<0 else 0; longest=max(longest,streak)
    complete=bool(daily) and all(x['account_complete'] for x in daily)
    nets=[d['net_pnl'] for d in daily if d['net_pnl'] is not None]
    total=sum(nets) if complete else None
    best=max(nets) if nets else None
    return {'effective_days':len(daily),'independent_stock_days':len({(t['day'],t['symbol']) for t in trades}),
            'signal_count':len(dispositions),'entries':len(trades),'closed_trades':len(closed),
            'candidate_universe_stock_days':len(daily)*30,
            'qualified_signal_stock_days':len({(x['day'],x['symbol']) for x in dispositions}),
            'wins':wins,'losses':losses,'win_rate':wins/len(closed) if closed else None,
            'profit_factor':profit/loss if loss else None,'pf_status':'NO_LOSSES' if profit and not loss else 'NO_CLOSED_TRADES' if not values else 'DEFINED',
            'gross_pnl':sum(t['gross_pnl'] for t in closed) if complete else None,
            'commission':sum(t['commission'] for t in trades),'sell_tax':sum(t['sell_tax'] for t in trades),
            'net_pnl':round(total,2) if total is not None else None,'completed_subset_net':sum(values),
            'mean_trade_net':statistics.mean(values) if values else None,'median_trade_net':statistics.median(values) if values else None,
            'maximum_single_loss':min([x for x in values if x<0],default=0) if values else None,
            'worst_single_trade':min(values) if values else None,'worst_day':min(nets) if nets else None,
            'max_consecutive_losses':longest,'max_drawdown_twd':max((d['running_max_drawdown_twd'] for d in daily),default=None) if complete else None,
            'observed_partial_max_drawdown_twd':max((d['running_max_drawdown_twd'] for d in daily),default=None),
            'max_drawdown_fraction':max((d['running_max_drawdown_twd'] for d in daily),default=0)/190000 if daily and complete else None,
            'entry_unfilled_quantity':sum(t['entry_unfilled_quantity'] for t in trades),
            'partial_entry_count':sum(t['entry_unfilled_quantity']>0 for t in trades),
            'unfilled_entry_attempts':sum(x['reason']=='NO_FILL_OR_CAP_RISK' for x in dispositions),
            'cover_incomplete_quantity':sum(t['remaining_quantity'] for t in trades),
            'mean_mfe_twd':statistics.mean([t['mfe_net_twd'] for t in trades]) if trades else None,
            'mean_mae_twd':statistics.mean([t['mae_net_twd'] for t in trades]) if trades else None,
            'mean_holding_seconds':statistics.mean([t['holding_seconds'] for t in trades]) if trades else None,
            'max_profit_giveback_twd':max((t['maximum_profit_giveback_twd'] for t in trades),default=None),
            'net_ex_best_day':round(total-best,2) if total is not None and best is not None else None,
            'missed_long_signals_due_short':sum(x['reason']=='SHORT_USED_DAILY_BUDGET_MISSED_LONG' for x in dispositions),
            'account_complete':complete,'status':'CONDITIONAL_SCENARIO_NOT_VERIFIED_EXECUTABLE_PERFORMANCE' if daily else 'NO_EVALUABLE_DATA',
            'skip_reasons':dict(Counter(x['reason'] for x in dispositions if not x['reason'].startswith('ADOPTED')))}
