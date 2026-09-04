from __future__ import annotations

import math
from dataclasses import dataclass

from .config import CFG
from .data_loader import Bar
from .signals import Signal


def net_return(entry: float, exit: float, slippage: float) -> float:
    buy_price=entry*(1+slippage); sell_price=exit*(1-slippage)
    shares=math.floor(CFG.per_trade_notional/buy_price)
    if shares<=0: return math.nan
    buy=shares*buy_price; sell=shares*sell_price
    buy_fee=max(CFG.minimum_commission,round(buy*CFG.commission_rate*CFG.commission_discount))
    sell_fee=max(CFG.minimum_commission,round(sell*CFG.commission_rate*CFG.commission_discount))
    tax=round(sell*CFG.stock_transaction_tax)
    return (sell-sell_fee-tax-buy-buy_fee)/(buy+buy_fee)


def simulate(
    signals: list[Signal],
    stocks: dict[str, list[Bar]],
    market_dates: list[str],
) -> tuple[list[dict], list[dict]]:
    ordered_market_dates = sorted(set(market_dates))
    market_index = {date: i for i, date in enumerate(ordered_market_dates)}
    market_next = dict(zip(ordered_market_dates, ordered_market_dates[1:]))
    by_stock={}
    for signal in signals: by_stock.setdefault(signal.stock_id,[]).append(signal)
    trades=[]; events=[]
    for code,stock_signals in by_stock.items():
        bars=stocks[code]
        by_date={b.date:b for b in bars}
        blocked_until=""
        for signal in sorted(stock_signals,key=lambda s:s.signal_date):
            event={"stock_id":code,"signal_date":signal.signal_date,"status":"","cancel_reason":""}
            if blocked_until and signal.signal_date < blocked_until:
                event.update(status="skipped",cancel_reason="Existing Position")
                events.append(event); continue
            expected_entry_date=market_next.get(signal.signal_date)
            entry_bar=by_date.get(expected_entry_date)
            if entry_bar is None:
                event.update(status="cancelled",cancel_reason="Missing T+1 Open")
                events.append(event); continue
            gap=entry_bar.open/signal.signal_close-1
            if entry_bar.open < signal.signal_low:
                event.update(status="cancelled",cancel_reason="Below Signal Low")
                events.append(event); continue
            if gap > CFG.max_entry_gap:
                event.update(status="cancelled",cancel_reason="Gap >5%")
                events.append(event); continue
            entry=entry_bar.open; stop=max(entry*CFG.hard_stop_pct,signal.signal_low)
            highest=entry; lows=[]; highs=[]; close_returns={}; exit_price=None; exit_date=None; reason=None; held_days=0
            trigger_date=None; exit_delay_sessions=0; censor_reason="Insufficient Future Data"
            entry_market_i=market_index[entry_bar.date]
            for holding_day in range(1,CFG.max_holding_days+1):
                calendar_i=entry_market_i+holding_day-1
                if calendar_i>=len(ordered_market_dates): break
                current_date=ordered_market_dates[calendar_i]
                held_days=holding_day
                bar=by_date.get(current_date)
                if bar is None:
                    if holding_day>=CFG.max_holding_days:
                        censor_reason="Missing Day 8 Close"
                    continue
                highest=max(highest,bar.high); highs.append(bar.high); lows.append(bar.low)
                if holding_day in (1,3,5,8): close_returns[holding_day]=bar.close/entry-1
                trailing_active=highest>=entry*CFG.trailing_activation_pct
                trigger=None
                if bar.close < stop: trigger="Close Confirmed Stop"
                elif trailing_active and bar.close < highest*CFG.trailing_drawdown_pct: trigger="Trailing Stop"
                elif holding_day==5 and bar.close<=entry: trigger="Day 5 Weakness"
                if trigger:
                    trigger_date=current_date
                    expected_exit_date=market_next.get(current_date)
                    exit_bar=by_date.get(expected_exit_date)
                    if exit_bar is not None:
                        exit_price=exit_bar.open; exit_date=exit_bar.date; reason=trigger
                    else:
                        censor_reason="Missing D+1 Open After Exit Signal"
                    break
                if holding_day>=CFG.max_holding_days:
                    exit_price=bar.close; exit_date=bar.date; reason="Day 8 Time Stop"; trigger_date=current_date
                    break
            if exit_price is None:
                blocked_until="99999999"
                event.update(status="censored",cancel_reason=censor_reason,entry_date=entry_bar.date)
                if trigger_date:
                    event["exit_trigger_date"]=trigger_date
                events.append(event)
                continue
            exit_market_i=market_index[exit_date]
            post=[
                by_date[date].high
                for date in ordered_market_dates[exit_market_i+1:exit_market_i+6]
                if date in by_date
            ]
            gross=exit_price/entry-1
            row={
                "stock_id":code,"name":signal.name,"signal_date":signal.signal_date,"entry_date":entry_bar.date,
                "entry_price":entry,"exit_date":exit_date,"exit_price":exit_price,"holding_days":held_days,"exit_reason":reason,
                "exit_trigger_date":trigger_date,"exit_execution_delay_sessions":exit_delay_sessions,
                "gross_return":gross,"net_return":net_return(entry,exit_price,0.0),"net_return_slippage_0_1":net_return(entry,exit_price,0.001),
                "volume_ratio":signal.volume_ratio,"daily_return":signal.daily_return,"breakout_ratio":signal.breakout_ratio,"breakout_type":signal.breakout_type,
                "foreign_ratio":signal.foreign_ratio,"investment_trust_ratio":signal.investment_trust_ratio,"combined_ratio":signal.combined_ratio,
                "signal_low":signal.signal_low,"initial_stop_price":stop,"t1_gap_pct":gap,
                "mfe":max(highs)/entry-1 if highs else None,"mae":min(lows)/entry-1 if lows else None,
                "day1_close_return":close_returns.get(1),"day3_close_return":close_returns.get(3),"day5_close_return":close_returns.get(5),"day8_close_return":close_returns.get(8),
                "post_exit_5d_max_return":max(post)/exit_price-1 if post else None,
            }
            trades.append(row); blocked_until=exit_date
            event.update(status="entered",cancel_reason="",entry_date=entry_bar.date,exit_date=exit_date,exit_trigger_date=trigger_date)
            events.append(event)
    return trades,events
