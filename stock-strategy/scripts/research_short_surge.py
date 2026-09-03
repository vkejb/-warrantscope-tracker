#!/usr/bin/env python3
"""Research 2-10 day price-volume surges with conservative daily-bar execution."""

from __future__ import annotations

import argparse, heapq, json, math
from collections import defaultdict
from pathlib import Path

from run_backtest import END_DATE, START_DATE, benchmark_result, cost, load_archives

TARGET = 0.08
STOP = 0.05
MAX_HOLD = 10
MAX_POSITIONS = 5

VARIANTS = [
    {"id":"surge_raw","name":"價量突破","confirm":False,"cool":False,"compression":False},
    {"id":"surge_confirm3","name":"價量突破＋連續3日轉強","confirm":True,"cool":False,"compression":False},
    {"id":"surge_cool","name":"價量突破＋過熱排除","confirm":False,"cool":True,"compression":False},
    {"id":"surge_compression","name":"量縮盤整後突破（探索）","confirm":False,"cool":False,"compression":True},
]


def build_rankings(stocks, limit=30):
    heaps = {v["id"]:defaultdict(list) for v in VARIANTS}
    for code,bars in stocks.items():
        norms=[b.norm for b in bars]; vols=[b.volume for b in bars]; vals=[b.close*b.volume for b in bars]
        pn=[0.0]; pv=[0.0]; pval=[0.0]
        for n,v,val in zip(norms,vols,vals): pn.append(pn[-1]+n); pv.append(pv[-1]+v); pval.append(pval[-1]+val)
        for i in range(200,len(bars)):
            b=bars[i]
            if not START_DATE<=b.date<=END_DATE: continue
            sma20=(pn[i+1]-pn[i-19])/20; sma60=(pn[i+1]-pn[i-59])/60; sma200=(pn[i+1]-pn[i-199])/200
            avg_vol=(pv[i]-pv[i-20])/20; avg_value=(pval[i]-pval[i-20])/20
            ret5=norms[i]/norms[i-5]-1; ret20=norms[i]/norms[i-20]-1
            volume_ratio=b.volume/avg_vol if avg_vol else 0
            close_location=(b.close-b.low)/(b.high-b.low) if b.high>b.low else .5
            near_breakout=norms[i]>=max(norms[i-20:i])*.995
            distance=norms[i]/sma20-1
            prior_range=max(norms[i-10:i])/min(norms[i-10:i])-1
            prior5_volume=sum(vols[i-5:i])/5
            common=(b.close>=20 and avg_value>=50_000_000 and norms[i]>sma20>sma60>sma200 and
                    .02<=ret5<=.18 and 0<ret20<=.35 and 1.3<=volume_ratio<=5 and
                    close_location>=.65 and near_breakout and distance<=.20)
            if not common: continue
            score=ret5*4+ret20*1.5+min(volume_ratio,3)*.08+close_location*.1
            for variant in VARIANTS:
                if variant["confirm"] and not (norms[i]>norms[i-1]>norms[i-2] and norms[i]/norms[i-2]-1>=.02): continue
                if variant["cool"] and not (volume_ratio<=3 and distance<=.10 and ret5<=.12): continue
                if variant["compression"] and not (prior_range<=.10 and prior5_volume<=avg_vol*.90 and distance<=.08 and ret5<=.12): continue
                item=(score,code,b.name,norms[i])
                heap=heaps[variant["id"]][b.date]
                if len(heap)<limit: heapq.heappush(heap,item)
                elif score>heap[0][0]: heapq.heapreplace(heap,item)
    return {vid:{d:[{"score":s,"code":c,"name":n,"signal_norm":sn} for s,c,n,sn in sorted(h,reverse=True)] for d,h in days.items()} for vid,days in heaps.items()}


def market_regime(benchmark):
    norms=[b.norm for b in benchmark]; prefix=[0.0]; out={}
    for n in norms: prefix.append(prefix[-1]+n)
    for i,b in enumerate(benchmark):
        if i>=199: out[b.date]=b.norm>(prefix[i+1]-prefix[i-199])/200
    return out


def simulate(stocks,benchmark,rankings,variant,capital=30_000,transaction_cost=cost):
    dates=[b.date for b in benchmark if START_DATE<=b.date<=END_DATE]
    indices={c:{b.date:i for i,b in enumerate(bars)} for c,bars in stocks.items()}
    regime=market_regime(benchmark); cash=capital; positions={}; pending=[]; trades=[]; curve=[]; ambiguous=0
    year_start={}; year_end={}

    def finish(code,gross,fee,reason,date):
        nonlocal cash
        p=positions.pop(code); cash+=gross-fee
        trades.append({"code":code,"entry_date":p["entry_date"],"exit_date":date,"reason":reason,
                       "pnl":gross-fee-p["gross"]-p["fee"],"costs":fee+p["fee"],"days":p["days"]})

    for date in dates:
        # Exit intentions persist through suspensions until an actual bar exists.
        for code,p in list(positions.items()):
            if p.get("pending_exit"):
                i=indices[code].get(date)
                if i is None: continue
                b=stocks[code][i]; open_norm=b.norm*b.open/b.close
                gross=p["gross"]*open_norm/p["entry"]; finish(code,gross,transaction_cost(gross,"SELL"),p["pending_exit"],date)
        # Signals generated at the prior close enter at today's open, with a 3% gap cap.
        slots=MAX_POSITIONS-len(positions); allocation=cash/slots if slots else 0
        for pick in pending:
            if len(positions)>=MAX_POSITIONS or pick["code"] in positions: continue
            i=indices[pick["code"]].get(date)
            if i is None: continue
            b=stocks[pick["code"]][i]; open_norm=b.norm*b.open/b.close
            if open_norm/pick["signal_norm"]>1.03: continue
            shares=math.floor(allocation/b.open); gross=shares*b.open; fee=transaction_cost(gross,"BUY") if shares else 0
            if not shares or gross+fee>cash: continue
            cash-=gross+fee; positions[pick["code"]]={"gross":gross,"fee":fee,"entry":open_norm,"entry_date":date,"days":0,"pending_exit":None}

        # If target and stop are both touched in one daily bar, assume the stop occurred first.
        for code,p in list(positions.items()):
            p["days"]+=1
            i=indices[code].get(date)
            if i is None:
                if p["days"]>=MAX_HOLD: p["pending_exit"]="time"
                continue
            b=stocks[code][i]; factor=b.norm/b.close
            op,hi,lo,cl=[x*factor for x in (b.open,b.high,b.low,b.close)]
            stop=p["entry"]*(1-STOP); target=p["entry"]*(1+TARGET)
            if op<=stop: exit_norm=op; reason="stop_gap"
            elif op>=target: exit_norm=target; reason="target_gap"
            elif lo<=stop:
                if hi>=target: ambiguous+=1
                exit_norm=stop; reason="stop"
            elif hi>=target: exit_norm=target; reason="target"
            else:
                if p["days"]>=MAX_HOLD: p["pending_exit"]="time"
                continue
            gross=p["gross"]*exit_norm/p["entry"]; finish(code,gross,transaction_cost(gross,"SELL"),reason,date)

        pending=rankings.get(date,[])[:MAX_POSITIONS] if regime.get(date,False) else []
        if not regime.get(date,False):
            for p in positions.values(): p["pending_exit"]="market"
        equity=cash
        for code,p in positions.items():
            i=indices[code].get(date)
            if i is not None: equity+=p["gross"]*stocks[code][i].norm/p["entry"]
        curve.append(equity); year_start.setdefault(date[:4],equity); year_end[date[:4]]=equity

    for code,p in list(positions.items()):
        i=indices[code].get(dates[-1])
        if i is None:
            finish(code,0,0,"unknown_liquidation",dates[-1])
        else:
            close=stocks[code][i].norm; gross=p["gross"]*close/p["entry"]; finish(code,gross,transaction_cost(gross,"SELL"),"end",dates[-1])
    curve[-1]=cash; year_end[dates[-1][:4]]=cash
    peak=dd=0
    for e in curve: peak=max(peak,e); dd=max(dd,(peak-e)/peak if peak else 0)
    annual={y:round((year_end[y]/year_start[y]-1)*100,2) for y in year_start}
    target_hits=sum(t["reason"].startswith("target") for t in trades)
    return {"id":variant["id"],"name":variant["name"],"summary":{"final_equity":round(cash,2),
      "total_return_pct":round((cash/capital-1)*100,2),"cagr_pct":round(((cash/capital)**(1/6)-1)*100,2),
      "max_drawdown_pct":round(dd*100,2),"trades":len(trades),"win_rate_pct":round(sum(t["pnl"]>0 for t in trades)/len(trades)*100,2) if trades else 0,
      "target_hit_rate_pct":round(target_hits/len(trades)*100,2) if trades else 0,"average_holding_days":round(sum(t["days"] for t in trades)/len(trades),2) if trades else 0,
      "ambiguous_same_bar_count":ambiguous,"ambiguous_same_bar_pct":round(ambiguous/len(trades)*100,2) if trades else 0,
      "total_costs":round(sum(t["costs"] for t in trades),2)},"annual":annual,
      "exit_reasons":{r:sum(t["reason"]==r for t in trades) for r in sorted({t["reason"] for t in trades})}}


def main():
    p=argparse.ArgumentParser(); p.add_argument("archives",nargs="+",type=Path); p.add_argument("--output",type=Path,required=True); a=p.parse_args()
    stocks,benchmark=load_archives(a.archives); rankings=build_rankings(stocks)
    out={"method":"Three short-surge variants fixed before first execution. A compression-breakout diagnostic was added only after all three failed and is exploratory, not eligible for model selection. Signal at close, entry next open; +8% target, -5% stop, 10-day maximum; same-bar ambiguity resolves to stop.",
         "limitations":"Theoretical regular-lot daily OHLC execution, not historical odd-lot first trades. Corporate actions use heuristic normalization; no locked-limit order-book simulation.",
         "benchmark":benchmark_result(benchmark),"variants":[simulate(stocks,benchmark,rankings[v["id"]],v) for v in VARIANTS],
         "zero_cost_sensitivity":{v["id"]:simulate(stocks,benchmark,rankings[v["id"]],v,transaction_cost=lambda gross,side:0)["summary"] for v in VARIANTS}}
    a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps(out,ensure_ascii=False,indent=2))


if __name__=="__main__": main()
