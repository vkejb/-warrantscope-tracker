from __future__ import annotations

import statistics
from dataclasses import dataclass

from .config import CFG, FINANCIAL_CODE_EXTRAS, FINANCIAL_CODE_RANGES
from .data_loader import Bar, TaiexBar


@dataclass
class Signal:
    stock_id: str
    name: str
    signal_date: str
    signal_close: float
    signal_low: float
    volume_ratio: float
    daily_return: float
    breakout_ratio: float
    breakout_type: str
    foreign_ratio: float | None = None
    investment_trust_ratio: float | None = None
    combined_ratio: float | None = None


def is_financial(code: str, financial_codes: set[str] | None = None) -> bool:
    if financial_codes is not None:
        return code in financial_codes
    if code in FINANCIAL_CODE_EXTRAS:
        return True
    numeric = int(code)
    return any(lo <= numeric <= hi for lo, hi in FINANCIAL_CODE_RANGES)


def taiex_filter(taiex: list[TaiexBar]) -> dict[str, bool]:
    result = {}
    closes = [b.close for b in taiex]
    for i in range(20, len(taiex)):
        ma20 = statistics.fmean(closes[i-19:i+1])
        prior_ma20 = statistics.fmean(closes[i-20:i])
        result[taiex[i].date] = closes[i] > ma20 and ma20 >= prior_ma20
    return result


def price_candidates(
    stocks: dict[str, list[Bar]],
    taiex: list[TaiexBar],
    financial_codes: set[str] | None = None,
) -> tuple[list[Signal], dict]:
    market_ok = taiex_filter(taiex)
    candidates = []
    counts = {"stock_rows_checked": 0, "financial_excluded": 0, "market_filter_rejected": 0}
    for code, bars in stocks.items():
        if is_financial(code, financial_codes):
            counts["financial_excluded"] += 1
            continue
        closes = [b.close for b in bars]
        for i in range(20, len(bars)):
            bar = bars[i]
            if not CFG.start_date <= bar.date <= CFG.end_date:
                continue
            counts["stock_rows_checked"] += 1
            if not market_ok.get(bar.date, False):
                counts["market_filter_rejected"] += 1
                continue
            avg_volume_prev = statistics.fmean(b.volume for b in bars[i-20:i])
            ma10 = statistics.fmean(closes[i-9:i+1])
            ma20 = statistics.fmean(closes[i-19:i+1])
            ma20_prev = statistics.fmean(closes[i-20:i])
            daily_return = bar.close / bars[i-1].close - 1
            volume_ratio = bar.volume / avg_volume_prev if avg_volume_prev else 0
            previous_high = max(b.high for b in bars[i-20:i])
            breakout_ratio = bar.close / previous_high
            if not (CFG.min_price <= bar.close <= CFG.max_price): continue
            if avg_volume_prev / 1000 < CFG.min_avg_volume_lots: continue
            if not (bar.close > ma20 and ma10 > ma20 and ma20 > ma20_prev): continue
            if not (CFG.min_daily_return <= daily_return <= CFG.max_daily_return): continue
            if volume_ratio < CFG.min_volume_ratio: continue
            if not (CFG.min_breakout_ratio <= breakout_ratio <= CFG.max_breakout_ratio): continue
            kind = "Pre-breakout" if breakout_ratio < 1 else ("Breakout" if breakout_ratio <= 1.03 else "Extended Breakout")
            candidates.append(Signal(code, bar.name, bar.date, bar.close, bar.low, volume_ratio, daily_return, breakout_ratio, kind))
    counts["price_candidate_count"] = len(candidates)
    return candidates, counts


def attach_institutional(signals: list[Signal], stocks: dict[str, list[Bar]], institutional: dict[tuple[str,str],tuple[int,int]]) -> tuple[list[Signal], dict]:
    indices = {code:{bar.date:i for i,bar in enumerate(bars)} for code,bars in stocks.items()}
    accepted=[]; missing=0; rejected=0
    for signal in signals:
        i=indices[signal.stock_id][signal.signal_date]
        days=[b.date for b in stocks[signal.stock_id][i-2:i+1]]
        points=[institutional.get((signal.stock_id,d)) for d in days]
        if len(days)!=3 or any(p is None for p in points):
            missing += 1
            continue
        foreign=sum(p[0] for p in points); trust=sum(p[1] for p in points)
        total_volume=sum(stocks[signal.stock_id][indices[signal.stock_id][d]].volume for d in days)
        if not total_volume:
            missing += 1
            continue
        signal.foreign_ratio=foreign/total_volume
        signal.investment_trust_ratio=trust/total_volume
        signal.combined_ratio=(foreign+trust)/total_volume
        if signal.combined_ratio >= CFG.min_combined_ratio: accepted.append(signal)
        else: rejected += 1
    return accepted,{"institutional_accepted":len(accepted),"institutional_rejected":rejected,"institutional_missing_3d":missing}


def needed_institutional_pairs(signals: list[Signal], stocks: dict[str,list[Bar]]) -> dict[str,list[str]]:
    indices={code:{bar.date:i for i,bar in enumerate(bars)} for code,bars in stocks.items()}
    by_date: dict[str,set[str]]={}
    for signal in signals:
        i=indices[signal.stock_id][signal.signal_date]
        for bar in stocks[signal.stock_id][max(0,i-2):i+1]:
            by_date.setdefault(bar.date,set()).add(signal.stock_id)
    return {date:sorted(codes) for date,codes in sorted(by_date.items())}
