#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

if __package__ in {None,""}:
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from v21.backtest import simulate
    from v21.config import CFG
    from v21.data_loader import load_institutional,load_ohlcv_archives,load_stock_info,load_taiex
    from v21.metrics import RETURN_FIELDS,grouped_summary,summarize
    from v21.report import build_report
    from v21.signals import attach_institutional,needed_institutional_pairs,price_candidates
    from v21.validation import validate_results
else:
    from .backtest import simulate
    from .config import CFG
    from .data_loader import load_institutional,load_ohlcv_archives,load_stock_info,load_taiex
    from .metrics import RETURN_FIELDS,grouped_summary,summarize
    from .report import build_report
    from .signals import attach_institutional,needed_institutional_pairs,price_candidates
    from .validation import validate_results


def write_csv(path: Path,rows: list[dict]):
    path.parent.mkdir(parents=True,exist_ok=True)
    if not rows:
        path.write_text("",encoding="utf-8"); return
    fields=[]
    for row in rows:
        for key in row:
            if key not in fields: fields.append(key)
    with path.open("w",newline="",encoding="utf-8-sig") as handle:
        writer=csv.DictWriter(handle,fieldnames=fields,lineterminator="\n"); writer.writeheader(); writer.writerows(rows)


def main():
    p=argparse.ArgumentParser(); p.add_argument("archives",nargs="+",type=Path); p.add_argument("--taiex",type=Path,required=True); p.add_argument("--stock-info",type=Path); p.add_argument("--institutional",type=Path); p.add_argument("--output-dir",type=Path,required=True); p.add_argument("--prepare-only",action="store_true")
    args=p.parse_args(); stocks,stock_audit=load_ohlcv_archives(args.archives); taiex,taiex_audit=load_taiex(args.taiex)
    financial_codes,stock_info_audit=load_stock_info(args.stock_info) if args.stock_info else (None,{"status":"not supplied; code heuristic used"})
    candidates,signal_audit=price_candidates(stocks,taiex,financial_codes); args.output_dir.mkdir(parents=True,exist_ok=True)
    base_audit={"config":CFG.__dict__,"ohlcv":stock_audit,"taiex":taiex_audit,"stock_info":stock_info_audit,"signal_prefilter":signal_audit,
      "limitations":["缺少逐日歷史處置股與全額交割股狀態，因此這兩項排除未執行。","金融股使用證券主檔產業標籤排除，但該主檔不是完整的逐日 point-in-time 產業快照。" if args.stock_info else "未提供證券主檔，金融股僅用 2800-2899、5876、5880 的代碼規則排除。","ETF 與特殊商品使用一般四碼股票代碼規則排除。","一般市場日線 Open 不是歷史盤中零股第一筆成交價。","OHLCV 缺少完整的公司行動調整因子。","保留歷史／已下市代碼以降低存活者偏誤，但缺少永久證券 ID，無法排除所有代碼重用偏誤。","法人資料由櫃買中心官方資料與 FinMind 的 TWSE／TPEx 資料組成；重疊資料比對為零差異。","原規格未定義同時持股上限、資金分配或訊號競爭，因此只能做獨立交易統計，不能視為 30,000 元資金的可實現權益曲線。"]}
    if args.prepare_only:
        needs=needed_institutional_pairs(candidates,stocks)
        (args.output_dir/"institutional_needs.json").write_text(json.dumps(needs,ensure_ascii=False),encoding="utf-8")
        base_audit["institutional_dates_needed"]=len(needs); base_audit["institutional_pairs_needed"]=sum(map(len,needs.values()))
        (args.output_dir/"data_audit.json").write_text(json.dumps(base_audit,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(base_audit,ensure_ascii=False,indent=2)); return
    if not args.institutional: p.error("--institutional is required unless --prepare-only")
    institutional,inst_audit=load_institutional(args.institutional)
    needs=needed_institutional_pairs(candidates,stocks)
    required_pairs={(code,date) for date,codes in needs.items() for code in codes}
    available_required_pairs=sum(pair in institutional for pair in required_pairs)
    base_audit["institutional_requirements"]={
        "dates_needed":len(needs),
        "pairs_needed":len(required_pairs),
        "pairs_available":available_required_pairs,
        "pairs_missing":len(required_pairs)-available_required_pairs,
        "synthetic_zero_fills":0,
    }
    signals,inst_signal_audit=attach_institutional(candidates,stocks,institutional)
    trades,events=simulate(signals,stocks,[bar.date for bar in taiex])
    validation=validate_results(signals,trades,events,stocks,taiex,institutional,financial_codes)
    (args.output_dir/"validation_summary.json").write_text(json.dumps(validation,ensure_ascii=False,indent=2),encoding="utf-8")
    if not validation["passed"]:
        raise RuntimeError(f"V2.1 validation failed: {validation['failure_samples']}")
    yearly=[]
    years=["2022","2023","2024","2025","2026","2022-2026"]
    for year in years:
        selected=trades if year=="2022-2026" else [r for r in trades if r["signal_date"].startswith(year)]
        signal_events=events if year=="2022-2026" else [r for r in events if r["signal_date"].startswith(year)]
        for scenario,field in RETURN_FIELDS.items():
            row={"year":year,"scenario":scenario,"total_signals":len(signal_events),"actual_entries":sum(e["status"] in {"entered","censored"} for e in signal_events),
                 "cancelled_entries":sum(e["status"]=="cancelled" for e in signal_events),"censored_entries":sum(e["status"]=="censored" for e in signal_events),
                 "skipped_existing_position":sum(e.get("cancel_reason")=="Existing Position" for e in signal_events),
                 "cancel_below_signal_low":sum(e.get("cancel_reason")=="Below Signal Low" for e in signal_events),"cancel_gap_over_5pct":sum(e.get("cancel_reason")=="Gap >5%" for e in signal_events)}
            row.update(summarize(selected,field)); yearly.append(row)
    write_csv(args.output_dir/"trades.csv",trades); write_csv(args.output_dir/"signal_events.csv",events); write_csv(args.output_dir/"yearly_summary.csv",yearly)
    breakout=[]; exits=[]
    overall={}
    for scenario,field in RETURN_FIELDS.items():
        overall[scenario]=summarize(trades,field)
        breakout.extend({"scenario":scenario,**row} for row in grouped_summary(trades,"breakout_type",field))
        exits.extend({"scenario":scenario,**row} for row in grouped_summary(trades,"exit_reason",field,True))
    write_csv(args.output_dir/"breakout_type_summary.csv",breakout)
    write_csv(args.output_dir/"exit_reason_summary.csv",exits)
    signal_counts={"final_signals":len(signals),"completed_trades":len(trades),"cancelled":sum(e["status"]=="cancelled" for e in events),"censored":sum(e["status"]=="censored" for e in events),"skipped_existing_position":sum(e["status"]=="skipped" for e in events)}
    summary_payload={"overall":overall,"cost_assumptions":{
        "commission_rate":CFG.commission_rate,"commission_discount":CFG.commission_discount,"minimum_commission":CFG.minimum_commission,
        "stock_transaction_tax":CFG.stock_transaction_tax,"per_trade_notional":CFG.per_trade_notional,"one_way_slippage_scenarios":CFG.slippage_scenarios},
        "signal_counts":signal_counts}
    (args.output_dir/"backtest_summary.json").write_text(json.dumps(summary_payload,ensure_ascii=False,indent=2),encoding="utf-8")
    base_audit.update(institutional=inst_audit,institutional_signal=inst_signal_audit,validation=validation,final_signal_count=len(signals),completed_trade_count=len(trades))
    (args.output_dir/"data_audit.json").write_text(json.dumps(base_audit,ensure_ascii=False,indent=2),encoding="utf-8")
    (args.output_dir/"backtest_report.md").write_text(build_report(overall,yearly,breakout,exits,base_audit,signal_counts),encoding="utf-8")
    print(json.dumps(base_audit,ensure_ascii=False,indent=2))


if __name__=="__main__": main()
