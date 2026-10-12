"""Requirement audit and readable research artifact; never connects externally."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import argparse

MODULE=Path(__file__).resolve().parent


def fmt(value):
    return '—' if value is None else f'{value:,.2f}'


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,default=MODULE/'results_expanded')
    args=parser.parse_args(argv)
    root=args.output_dir
    summary=json.loads((root/'summary.json').read_text())
    contract=json.loads((root/'contract.json').read_text())
    rows=json.loads((root/'daily_results.json').read_text())
    signals=json.loads((root/'qualified_signals.json').read_text())
    ties=json.loads((root/'simultaneous_cases.json').read_text())
    inventory=json.loads((root/'data_inventory.json').read_text())
    reference=json.loads((root/'s0_original_full_fill_reference.json').read_text())
    signal_counts=json.loads((root/'signal_counts.json').read_text())
    daily_inventory=json.loads((root/'daily_inventory.json').read_text())
    quality=json.loads((root/'stream_quality.json').read_text())
    zip_catalog=json.loads((root/'zip_catalog.json').read_text())
    assert not any(r['status']=='QUOTE_ARCHIVE_REQUIRES_AUDIT' for r in zip_catalog)
    assert {q['date'] for q in quality}==set(summary['sample_dates'])
    assert {r['session_date'] for r in daily_inventory if r['included']}==set(summary['sample_dates'])
    for source in (MODULE/'sources').glob('*_twse_eligibility.json'):
        payload=json.loads(source.read_text())
        raw=payload['raw_response']
        assert raw['stat']=='OK' and raw['date']==payload['session_date']
        table=next(t for t in raw['tables'] if '暫停現股賣出後現款買進當沖註記' in t.get('fields',[]))
        index=table['fields'].index('暫停現股賣出後現款買進當沖註記')
        assert payload['codes']=={row[0]:not bool(row[index].strip()) for row in table['data']}
    expected=len(summary['sample_dates'])*4*3*2
    assert len(rows)==expected,(len(rows),expected)
    assert len({(r['date'],r['variant'],r['scenario'],r['volume_multiplier']) for r in rows})==expected
    assert contract['exit_policy']['stop_loss_net_twd']==3500
    assert contract['exit_policy']['mfe_variant']=='MFE_V1'
    assert contract['base_signal_spec']['hard_exit_time']=='13:20'
    assert contract['capital_twd']==190000 and contract['max_trades_per_day']==1
    assert summary['actual_orders']==summary['actual_fills']==summary['broker_connections']==0
    for r in rows:
        if r['status']=='SCORED_TRADE':
            assert r['residual_shares']==0
            assert r['covered_shares']==r['entry_quantity']
            assert r['mfe_net_pnl']>=r['net_pnl']
            assert r['mae_net_pnl']<=r['net_pnl']
            assert r['giveback_net_pnl']>=0
        elif r.get('residual_shares',0):
            assert r['net_pnl'] is None
    for date in summary['sample_dates']:
        options=[s for s in signals if s['date']==date and s['variant']=='S3']
        actual=next(r for r in rows if r['date']==date and r['variant']=='S3')
        if options:
            first=min(str(s['decision_time']) for s in options)
            at=[s for s in options if str(s['decision_time'])==first]
            winner=max(at,key=lambda s:(s['score'],s['entry_price']*s['quantity']))
            assert actual['stock_id']==winner['stock_id']
    tests=subprocess.run([sys.executable,'-m','unittest','discover','-s','short_side_study_v01/tests','-v'],cwd=MODULE.parent,capture_output=True,text=True)
    assert tests.returncode==0,tests.stdout+tests.stderr
    (root/'tests.txt').write_text(tests.stdout+tests.stderr)
    immutable=[]
    for module in ('yuanta_live_runtime_v01','yuanta_broker_execution_v01'):
        result=subprocess.run(['git','diff','--exit-code','--',str(MODULE.parent/module)],cwd=MODULE.parent,capture_output=True,text=True)
        assert result.returncode==0,module+': live source changed'
        immutable.append(module)
    lines=[
        'WarrantScope SHORT side study V0.1',
        '結論：'+summary['conclusion'],
        'SIGNAL_ONLY / ELIGIBILITY_UNPROVEN / NO_TRUE_OUT_OF_SAMPLE_SET',
        '完整資料日期：'+', '.join(summary['sample_dates']),
        f"資料目錄共 {len(inventory)} 個來源；完整且通過雜湊驗證的獨立日期 {len(summary['sample_dates'])} 天。",
        f"另掃描 {len(zip_catalog)} 個壓縮檔的目錄；沒有尚未處理的 tick archive。",
        'S0=原 LONG A；S1=無0050 gate 的 SHORT 診斷；S2=固定對稱 SHORT gate；S3=按時間先後共用一天一筆。',
        'SHORT gate：BEARISH RS<=0 / 1次；NEUTRAL RS<=-0.0025 / 2次；BULLISH RS<=-0.005 / 2次。',
        '固定原始 Top30、base thresholds、30秒決策、side-aware anti-chase、190000資金、費稅、-3500 hard stop、MFE_V1、13:20強平。',
        '五檔限制成交量，賣出以bid、買回以ask。用原有保守限價計成交價格，不以最佳價改善績效。',
        '舊版S0全量成交代理另存 s0_original_full_fill_reference.json；主表是同一代理加五檔容量限制。',
        '原始五檔量單位未在元大API文件明示：1000股/單位與1股/單位均只是敏感度假設。',
        '部分入場取消剩餘量為回測假設；未回補曝險保留null PnL，不能當成已平倉。',
        'net_pnl遇未平倉/無法評分日即為null；completed_subset_net_pnl僅為已完成交易的局部損益。',
        '勝率、PF、平均盈虧以已完成交易計；entered_trades另含已入場但未完成交易。',
        'max_daily_realized_drawdown為逐日已實現DD；max_quote_mark_account_drawdown另計盤中報價估值DD。',
        'MFE/MAE/giveback為可執行方向報價與既有費稅估值，不保證足量深度可實現全部估值。',
        'MFE＝持有期間峰值淨估值；MAE＝最低淨估值；giveback＝峰值淨估值減最終淨損益。MFE可為負，代表從未回到扣費後損益兩平。',
        '新五檔回呼不代表流動性自動補足；僅同價位可見量增加才補入可用量。',
        '', '主表（1000股/五檔量單位假設）：',
    ]
    for m in summary['summaries']:
        if m['volume_multiplier']==1000:
            lines.extend([
                f"{m['variant']} / {m['scenario']}",
                f"  SHORT base訊號boundary={m['raw_short_signal_boundaries']}；合格SHORT boundary={m['approved_short_signal_boundaries']}；回測進場={m['entered_trades']}；完成交易={m['trades']}",
                f"  net={fmt(m['net_pnl'])}；PF={fmt(m['profit_factor'])}；勝率={fmt(None if m['win_rate'] is None else m['win_rate']*100)}%",
                f"  平均獲利={fmt(m['avg_winner'])}；平均虧損={fmt(m['avg_loser'])}；已實現maxDD={fmt(m['max_daily_realized_drawdown'])}；盤中報價估值maxDD={fmt(m['max_quote_mark_account_drawdown'])}",
                f"  平均MFE={fmt(m['mean_mfe_net_pnl'])}；平均MAE={fmt(m['mean_mae_net_pnl'])}；平均giveback={fmt(m['mean_giveback_net_pnl'])}；無法評分／曝險未完日={m['unscorable_or_open_exposure_days']}",
            ])
    lines.extend(['','逐日納入／排除：'])
    for day in daily_inventory:
        reasons=sorted({reason for r in day['excluded_sources'] for reason in r['reasons']})
        lines.append(f"{day['session_date']}: {'納入' if day['included'] else '排除'}; {', '.join(reasons) if reasons else '完整來源品質合格'}")
        for source in day['selected_sources']:
            lines.append('  使用來源：'+source)
    lines.extend(['','每日 base 損益、MFE／MAE／giveback（1000股單位假設）：'])
    for row in rows:
        if row['scenario']=='BASE_CURRENT_EXECUTION' and row['volume_multiplier']==1000:
            lines.append(json.dumps({k:row.get(k) for k in ('date','variant','side','stock_id','status','entry_quantity','residual_shares','net_pnl','mfe_net_pnl','mae_net_pnl','giveback_net_pnl')},ensure_ascii=False))
    lines.extend(['','SHORT訊號數（每檔每個30秒boundary算一個；同一持續訊號可重複計數）：'])
    for count in signal_counts:
        lines.append(json.dumps({k:count[k] for k in ('date','variant','raw_short_boundaries','raw_short_distinct_symbols','approved_short_boundaries')},ensure_ascii=False))
    lines.extend(['','補充性與交易品質：'])
    for item in summary['complementarity']:
        if item['volume_multiplier']==1000:
            lines.append(json.dumps(item,ensure_ascii=False))
    lines.extend(['','Leave-one-day-out、移除最佳／最差交易（不重新選股或調參）：'])
    for item in summary['robustness']:
        if item['volume_multiplier']==1000:
            best=item['remove_best_trade'];worst=item['remove_worst_trade']
            loo='；'.join(f"移除{r['removed_day']}→net {fmt(r['net_pnl'])}" for r in item['leave_one_day_out'])
            lines.extend([f"{item['variant']} / {item['scenario']}",
                          '  '+loo,
                          f"  移除最佳交易→{fmt(None if best is None else best['net_pnl'])}；移除最差交易→{fmt(None if worst is None else worst['net_pnl'])}"])
    lines.extend(['',f'同一決策點雙方向同時合格案例數：{len(ties)}',
                  '歷史TWSE先賣後買資格已快取並套用；TPEX與券商帳戶資格尚未證實。',
                  '資料屬探索樣本；即使日後SUPPORTED也只准prospective shadow。',
                  '本研究無券商登入、無送單、未修改LIVE、未push、未merge、未部署。'])
    lines.extend(['', '官方資料參考：',
                  'TWSE歷史當沖標的與暫停先賣後買註記：https://www.twse.com.tw/exchangeReport/TWTB4U?date=20261002&response=json',
                  '元大五檔API文件僅列BuyVol/SellVol型別與買賣量，沒有在該欄位明示股數轉換：',
                  'https://www.yuanta.com.tw/file-repository/content/sparkapi_docs/行情/最佳五檔行情訂閱/index.html'])
    (root/'report_zh-TW.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    source_paths=list(MODULE.glob('*.py'))+list((MODULE/'tests').glob('*.py'))
    source_paths += [MODULE.parent/'yuanta_live_runtime_v01/strategy.py',MODULE.parent/'yuanta_intraday_shadow_v01/direction_follow_backtest.py',MODULE.parent/'yuanta_intraday_shadow_v01/live_parity_backtest.py',MODULE.parent/'paper_shadow_v01/runner.py',MODULE.parent/'entry_quality_edge_study_v01/analysis.py']
    outputs={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.is_file() and p.name!='validation_manifest.json'}
    manifest={'status':'VERIFIED_OFFLINE_RESEARCH','required_rows':expected,'s0_reference_rows':len(reference),
              'untouched_live_modules':immutable,'contract_hash':contract['contract_hash'],
              'source_hashes':{str(p.relative_to(MODULE.parent)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths},
              'output_hashes':outputs,'eligibility_source_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (MODULE/'sources').glob('*.json')},
              'conclusion':summary['conclusion']}
    manifest['requirement_evidence']={
        'all_archives':'data_inventory.json + daily_inventory.json + zip_catalog.json',
        'top30_ticks_book_0050_coverage':'stream_quality.json + source artifact SHA256',
        'signals_and_actual_model_entries':'signal_counts.json + daily_results.json',
        'four_variants_chronological':'qualified_signals.json + simultaneous_cases.json + chronological assertions',
        'fixed_thresholds_and_exits':'contract.json + inherited-method regression tests',
        'side_aware_depth_partial_uncovered':'execution/replay tests + daily residual_shares/null PnL assertions',
        'fees_direction':'test_short_pnl_direction_and_sell_tax + inherited frozen fee function',
        'metrics_and_daily_pnl':'summary.json + daily_results.json',
        'mfe_mae_giveback_and_dd':'daily mark path + summary metrics + finite realization assertions',
        'complementarity_correlation_quality':'summary.json complementarity',
        'adverse_delay_loo_extreme_removal':'summary.json scenarios/robustness',
        'eligibility_no_true_oos':'eligibility_audit.json + sources + report labels',
        'no_live_mutation':'git diff live modules = empty; no broker imports/calls in study',
    }
    (root/'validation_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':manifest['status'],'rows':expected,'conclusion':summary['conclusion'],'s0_parity':summary['s0_legacy_parity']},ensure_ascii=False))


if __name__=='__main__':
    main()
