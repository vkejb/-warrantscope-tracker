#!/usr/bin/env python3
"""Daily cross-sectional strength research with next-open execution."""

from __future__ import annotations

import argparse, heapq, json, math
from collections import defaultdict
from pathlib import Path

from run_backtest import END_DATE, START_DATE, benchmark_result, cost, load_archives


VARIANTS = [
    {"id": "daily_fill", "name": "每日掃描／只補空缺", "rotate": False, "mechanical": False, "confirm": 1},
    {"id": "daily_confirm3", "name": "連續強勢3日／只補空缺", "rotate": False, "mechanical": False, "confirm": 3},
    {"id": "daily_confirm5", "name": "連續強勢5日／只補空缺", "rotate": False, "mechanical": False, "confirm": 5},
    {"id": "daily_buffer30", "name": "每日掃描／跌出30名才替換", "rotate": True, "mechanical": False, "confirm": 1},
    {"id": "daily_top10", "name": "每日機械持有前10名", "rotate": True, "mechanical": True, "confirm": 1},
]


def daily_rankings(stocks, limit=50):
    """Build top ranks once; all variants consume the identical ex-ante signals."""
    heaps = defaultdict(list)
    for code, bars in stocks.items():
        values = [b.close * b.volume for b in bars]
        norms = [b.norm for b in bars]
        returns = [0.0] + [math.log(norms[i] / norms[i-1]) for i in range(1, len(bars))]
        pv = [0.0]; pn = [0.0]; pr = [0.0]; pr2 = [0.0]
        for v, n, r in zip(values, norms, returns):
            pv.append(pv[-1]+v); pn.append(pn[-1]+n); pr.append(pr[-1]+r); pr2.append(pr2[-1]+r*r)
        for i in range(252, len(bars)):
            b = bars[i]
            if not START_DATE <= b.date <= END_DATE:
                continue
            avg_value = (pv[i+1]-pv[i-59])/60
            sma200 = (pn[i+1]-pn[i-199])/200
            momentum = norms[i-20]/norms[i-252]-1
            momentum20 = norms[i]/norms[i-20]-1
            mean_r = (pr[i+1]-pr[i-59])/60
            variance = max(0.0, (pr2[i+1]-pr2[i-59])/60-mean_r*mean_r)
            vol = math.sqrt(variance*252)
            if not (b.close >= 20 and avg_value >= 50_000_000 and norms[i] > sma200 and momentum > 0 and momentum20 > 0 and .05 < vol <= .55):
                continue
            score = momentum/vol + .25*momentum20/vol
            item = (score, code, b.name)
            heap = heaps[b.date]
            if len(heap) < limit: heapq.heappush(heap, item)
            elif score > heap[0][0]: heapq.heapreplace(heap, item)
    return {d: [{"score": s, "code": c, "name": n} for s,c,n in sorted(h, reverse=True)] for d,h in heaps.items()}


def benchmark_regime(benchmark):
    result = {}
    norms = [b.norm for b in benchmark]; prefix = [0.0]
    for n in norms: prefix.append(prefix[-1]+n)
    for i, b in enumerate(benchmark):
        if i >= 199: result[b.date] = b.norm > (prefix[i+1]-prefix[i-199])/200
    return result


def simulate(stocks, benchmark, rankings, variant, capital=30_000):
    dates = [b.date for b in benchmark if START_DATE <= b.date <= END_DATE]
    stock_index = {c: {b.date:i for i,b in enumerate(bars)} for c,bars in stocks.items()}
    regime = benchmark_regime(benchmark)
    cash, positions, pending, trades, curve = capital, {}, None, [], []
    strength_streak = {}
    year_start, year_end = {}, {}

    def norm_at(code, date):
        i = stock_index[code].get(date)
        return stocks[code][i].norm if i is not None else None

    for day_no, date in enumerate(dates):
        # Execute yesterday's decisions at today's open.
        if pending:
            exits, desired = pending
            for code, reason in exits.items():
                if code not in positions: continue
                i = stock_index[code].get(date)
                if i is None: continue
                p, b = positions.pop(code), stocks[code][i]
                open_norm = b.norm*b.open/b.close
                gross = p["gross"]*open_norm/p["entry"]
                fee = cost(gross, "SELL"); cash += gross-fee
                trades.append({"code":code,"pnl":gross-fee-p["gross"]-p["fee"],"costs":fee+p["fee"],"reason":reason})
            wanted = [x for x in desired if x["code"] not in positions]
            slots = 10-len(positions); allocation = cash/slots if slots else 0
            for pick in wanted[:slots]:
                i = stock_index[pick["code"]].get(date)
                if i is None: continue
                b = stocks[pick["code"]][i]; shares = math.floor(allocation/b.open)
                gross = shares*b.open; fee = cost(gross,"BUY") if shares else 0
                if not shares or gross+fee > cash: continue
                cash -= gross+fee
                entry = b.norm*b.open/b.close
                positions[pick["code"]] = {"gross":gross,"fee":fee,"entry":entry,"peak":entry,"days":0}

        ranking = rankings.get(date, []); rank = {x["code"]:i+1 for i,x in enumerate(ranking)}
        strong = {x["code"] for x in ranking[:30]}
        strength_streak = {code: strength_streak.get(code, 0) + 1 for code in strong}
        exits = {}
        market_on = regime.get(date, False)
        for code, p in positions.items():
            close = norm_at(code,date)
            if close is None: continue
            p["days"] += 1; p["peak"] = max(p["peak"],close)
            i = stock_index[code][date]
            sma100 = sum(b.norm for b in stocks[code][i-99:i+1])/100 if i >= 99 else close
            loss, draw = close/p["entry"]-1, close/p["peak"]-1
            if not market_on: exits[code]="market"
            elif loss <= -.10: exits[code]="stop"
            elif close < sma100 and draw <= -.05: exits[code]="trend"
            elif p["peak"]/p["entry"] >= 1.10 and draw <= -.10: exits[code]="trailing"

        desired = [x for x in ranking[:10] if strength_streak.get(x["code"], 0) >= variant["confirm"]] if market_on else []
        if market_on and variant["rotate"]:
            replaceable = [c for c,p in positions.items() if c not in exits and p["days"] >= 5 and (variant["mechanical"] or rank.get(c,999)>30)]
            newcomers = [x for x in desired if x["code"] not in positions]
            max_replace = len(replaceable) if variant["mechanical"] else min(1,len(replaceable))
            for code in sorted(replaceable, key=lambda c:rank.get(c,999), reverse=True)[:min(max_replace,len(newcomers))]: exits[code]="rank"
        pending = (exits, desired)

        equity = cash
        for code,p in positions.items():
            close=norm_at(code,date)
            if close is not None: equity += p["gross"]*close/p["entry"]
        curve.append(equity); year_start.setdefault(date[:4],equity); year_end[date[:4]]=equity

    # Mark remaining positions to the final available close, including exit costs.
    for code,p in positions.items():
        close=norm_at(code,dates[-1]) or p["entry"]; gross=p["gross"]*close/p["entry"]; fee=cost(gross,"SELL")
        cash += gross-fee; trades.append({"code":code,"pnl":gross-fee-p["gross"]-p["fee"],"costs":fee+p["fee"],"reason":"end"})
    curve[-1]=cash; year_end[dates[-1][:4]]=cash
    peak=dd=0
    for e in curve: peak=max(peak,e); dd=max(dd,(peak-e)/peak if peak else 0)
    annual={y:round((year_end[y]/year_start[y]-1)*100,2) for y in year_start}
    return {"id":variant["id"],"name":variant["name"],"summary":{"final_equity":round(cash,2),"total_return_pct":round((cash/capital-1)*100,2),"cagr_pct":round(((cash/capital)**(1/6)-1)*100,2),"max_drawdown_pct":round(dd*100,2),"trades":len(trades),"win_rate_pct":round(sum(t["pnl"]>0 for t in trades)/len(trades)*100,2),"total_costs":round(sum(t["costs"] for t in trades),2)},"annual":annual}


def main():
    p=argparse.ArgumentParser(); p.add_argument("archives",nargs="+",type=Path); p.add_argument("--output",type=Path,required=True); a=p.parse_args()
    stocks,benchmark=load_archives(a.archives); rankings=daily_rankings(stocks)
    out={"method":"Daily close ranking and next-open execution. Three initial variants plus exploratory 3/5-day persistence checks; persistence checks are not eligible for model selection because they were added after the first result.","benchmark":benchmark_result(benchmark),"variants":[simulate(stocks,benchmark,rankings,v) for v in VARIANTS]}
    a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps(out,ensure_ascii=False,indent=2))


if __name__=="__main__": main()
