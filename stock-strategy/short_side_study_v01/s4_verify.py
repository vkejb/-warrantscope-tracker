"""Verify S4 chronology, unchanged baselines and separate counterfactuals."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from .s4 import MODULE,REFERENCE,SCENARIOS,VARIANTS,choose_boundary
from .statistics import metrics
from yuanta_intraday_shadow_v01.live_parity_backtest import _parse_stamp
from yuanta_live_runtime_v01.strategy import LiveSignal

ROOT=MODULE/'results_s4'


def fmt(value):
    return '不可評分' if value is None else f'{value:,.2f}'


def restored_signal(raw):
    if raw is None:
        return None
    value=dict(raw)
    value['decision_time']=_parse_stamp(value['decision_time'])
    return LiveSignal(**value)


def main():
    summary=json.loads((ROOT/'summary.json').read_text())
    contract=json.loads((ROOT/'contract.json').read_text())
    daily=json.loads((ROOT/'daily_results.json').read_text())
    reference=json.loads((REFERENCE/'daily_results.json').read_text())
    selections=json.loads((ROOT/'selections.json').read_text())
    replacements=json.loads((ROOT/'counterfactual_replacements.json').read_text())
    approvals=json.loads((ROOT/'approved_boundaries.json').read_text())
    ties=json.loads((ROOT/'simultaneous_cases.json').read_text())
    n=len(contract['cohort_dates'])
    assert len(daily)==n*len(VARIANTS)*len(SCENARIOS)*2
    assert len({(r['date'],r['variant'],r['scenario'],r['volume_multiplier']) for r in daily})==len(daily)
    fields=('status','side','stock_id','signal_time','entry_quantity','entry_price','exit_time','exit_price',
            'gross_pnl','commission','tax','net_pnl','residual_shares','mfe_net_pnl','mae_net_pnl','giveback_net_pnl')
    prior={(r['date'],r['variant'],r['scenario'],r['volume_multiplier']):r for r in reference}
    current={(r['date'],r['variant'],r['scenario'],r['volume_multiplier']):r for r in daily}
    parity=[]
    for r in daily:
        key=(r['date'],r['variant'],r['scenario'],r['volume_multiplier'])
        if r['variant'] in ('S0','S1'):
            for field in fields:
                assert r.get(field)==prior[key].get(field),(key,field)
            parity.append({'date':r['date'],'variant':r['variant'],'scenario':r['scenario'],
                           'volume_multiplier':r['volume_multiplier'],'matches_prior':True})
        else:
            # One selected side only; account PnL must equal that independent
            # side's replay, never the sum or ex-post maximum of both sides.
            selected_variant='S1' if r['side']=='SHORT' else 'S0'
            chosen=current[(r['date'],selected_variant,r['scenario'],r['volume_multiplier'])]
            for field in fields:
                assert r.get(field)==chosen.get(field),(key,field,'selected replay differs')
        assert r['unit_status']=='UNIT_UNVERIFIED'
        if r['status']=='SCORED_TRADE':
            assert r['residual_shares']==0 and r['covered_shares']==r['entry_quantity']
        if r.get('residual_shares',0):
            assert r['net_pnl'] is None
    for record in selections:
        signals={v:restored_signal(s) for v,s in record['signals'].items()}
        sides=[s for v,s in signals.items() if v in ('S0','S1') and s is not None]
        if sides:
            first_time=min(s.decision_time for s in sides)
            long=signals['S0'] if signals['S0'] and signals['S0'].decision_time==first_time else None
            short=signals['S1'] if signals['S1'] and signals['S1'].decision_time==first_time else None
            expected=choose_boundary(long,short)
            assert signals['S4']==expected
        else:
            assert signals['S4'] is None
    for regime_group in summary['s1_regime_descriptive_only']:
        rows=[r for r in daily if r['variant']=='S1' and r['scenario']==regime_group['scenario']
              and r['volume_multiplier']==regime_group['volume_multiplier']
              and r['descriptive_regime_at_signal']==regime_group['regime']]
        assert regime_group['entered_trades']==metrics(rows)['entered_trades']
        assert regime_group['net_pnl']==metrics(rows)['net_pnl']
    for entry in summary['edge_attribution']:
        selected=[r for r in daily if r['scenario']==entry['scenario'] and r['volume_multiplier']==entry['volume_multiplier']]
        long=metrics([r for r in selected if r['variant']=='S0'])
        account=metrics([r for r in selected if r['variant']=='S4'])
        if long['net_pnl'] is not None and account['net_pnl'] is not None:
            assert sum(e['net_increment'] for e in entry['effects'].values())==account['net_pnl']-long['net_pnl']
    assert contract['short_gate']=='NONE_NEW' and contract['daily_attempt_limit']==1
    assert contract['exit_policy']['stop_loss_net_twd']==3500
    assert contract['exit_policy']['mfe_variant']=='MFE_V1'
    assert summary['actual_orders']==summary['actual_fills']==summary['broker_connections']==0
    tests=subprocess.run([sys.executable,'-m','unittest','discover','-s','short_side_study_v01/tests','-v'],cwd=MODULE.parent,capture_output=True,text=True)
    assert tests.returncode==0,tests.stdout+tests.stderr
    (ROOT/'tests.txt').write_text(tests.stdout+tests.stderr,encoding='utf-8')
    for directory in ('yuanta_live_runtime_v01','yuanta_broker_execution_v01'):
        check=subprocess.run(['git','diff','--exit-code','f7ac2e5','--',str(MODULE.parent/directory)],cwd=MODULE.parent,capture_output=True,text=True)
        assert check.returncode==0,directory+' was modified'
    lines=[
        'S4：LONG A + raw SHORT 真正 chronological 帳戶回測',
        'UNIT_UNVERIFIED / ELIGIBILITY_UNPROVEN / NO_TRUE_OUT_OF_SAMPLE_SET',
        '固定比較樣本：'+', '.join(contract['cohort_dates']),
        '原S0/S1共72筆日期×成交情境×單位回放逐筆核對一致；S4每天只有一次交易機會。',
        '先完整approved的方向佔用當日機會；僅同一boundary同時合格才用原score/notional排序。',
        'LONG保留原A；SHORT維持S1原base、anti-chase、confirmation，不新增0050 gate。',
        '反事實交易損益獨立列示，不加入帳戶；不因LONG虧損後再開SHORT。',
        '五檔、部分成交、未回補、費稅、資金、-3500 hard stop、MFE_V1、reversal、13:20退場全部沿用。',
        'max DD並列每日已實現與盤中報價估值；估值不保證足量深度能全部成交。',
        'net為不可評分時保留null，不能用已完成交易的局部小計冒充完整帳戶損益。',
        '若有不可評分日，勝率/PF/已實現DD只描述已完成交易子集合，不能當成完整帳戶風險績效。',
        '', '兩種五檔單位假設並列：',
    ]
    for scenario in SCENARIOS:
        lines.append(scenario)
        for variant in VARIANTS:
            for unit in (1000,1):
                m=next(r for r in summary['summaries'] if r['variant']==variant and r['scenario']==scenario and r['volume_multiplier']==unit)
                lines.append(f"  {variant} / {unit}股每單位：進場{m['entered_trades']}，完成{m['trades']}，net={fmt(m['net_pnl'])}，PF={fmt(m['profit_factor'])}，勝率={fmt(None if m['win_rate'] is None else m['win_rate']*100)}%，平均勝={fmt(m['avg_winner'])}，平均敗={fmt(m['avg_loser'])}，已實現DD={fmt(m['max_daily_realized_drawdown'])}，報價估值DD={fmt(m['max_quote_mark_account_drawdown'])}，未完成/不可評分日={m['unscorable_or_open_exposure_days']}")
    lines.extend(['','每日實際選擇與反事實（base，1000股/單位假設）：'])
    for r in replacements:
        if r['scenario']=='BASE_CURRENT_EXECUTION' and r['volume_multiplier']==1000:
            lines.extend([f"{r['date']}：{r['account_side']} {r['account_stock_id']} @ {r['account_signal_time']}；帳戶net={fmt(r['account_net_pnl'])}；比LONG增減={fmt(r['increment_vs_s0'])}",
                          f"  {r['kind']}；被取代={r['displaced_side']} {r['displaced_stock_id']} @ {r['displaced_signal_time']}；反事實net={fmt(r['displaced_counterfactual_pnl'])}（未加進帳戶）"])
    lines.extend(['','S1 0050 regime僅描述，不形成任何新門檻（base，1000股/單位假設）：'])
    for r in summary['s1_regime_descriptive_only']:
        if r['scenario']=='BASE_CURRENT_EXECUTION' and r['volume_multiplier']==1000:
            lines.append(f"{r['regime']}：完成交易{r['trades']}；net={fmt(r['net_pnl'])}")
    lines.extend(['分類依first approved signal時點，UNKNOWN不擋SHORT；樣本太少，不能據此挑regime或改threshold。',
                  '', '改善來源（1000股/單位假設）：'])
    for r in summary['edge_attribution']:
        if r['volume_multiplier']==1000:
            lines.append(r['scenario']+': '+json.dumps(r['effects'],ensure_ascii=False))
            lines.append('LONG losing days改善='+str(r['long_losing_days_improved'])+'；LONG winners被取代='+str(r['long_winners_displaced']))
    lines.extend(['','壓力：remove-best / remove-worst / leave-one-day-out（兩種單位）：'])
    for r in summary['robustness']:
        best=r['remove_best_trade'];worst=r['remove_worst_trade']
        lines.append(f"{r['variant']} / {r['scenario']} / {r['volume_multiplier']}股：移除最佳net={fmt(None if best is None else best['net_pnl'])}；移除最差net={fmt(None if worst is None else worst['net_pnl'])}")
        lines.append('  '+'；'.join(f"去掉{v['removed_day']}→{fmt(v['net_pnl'])}" for v in r['leave_one_day_out']))
    lines.extend(['',f'同boundary兩方向同時approved案例數：{len(ties)}（含當日機會已用掉後的診斷，詳simultaneous_cases.json）',
                  '研究判讀：raw SHORT仍值得隔離prospective shadow，尚未證實正期望。',
                  'S4改善LONG-only的損益，但base與兩項execution stress均仍虧損；不代表可交易edge。',
                  '本樣本改善來自方向替換；新增SHORT機會貢獻為負；沒有搶掉LONG winner，但LONG也擋住後來獲利SHORT。',
                  '正式SHORT execution layer目前應延後，先證實五檔單位、歷史/當下資格並累積prospective資料。',
                  'UNIT_UNVERIFIED且1股假設帳戶不能完整評分，不可猜測實際股數或實盤結論。',
                  '沒有修改LIVE、登入券商、送單、push、merge或部署。'])
    (ROOT/'report_zh-TW.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    source_paths=[MODULE/'s4.py',MODULE/'s4_verify.py',MODULE/'policy.py',MODULE/'replay.py',MODULE/'execution.py',MODULE/'statistics.py',MODULE/'tests/test_s4.py',MODULE.parent/'yuanta_live_runtime_v01/strategy.py']
    manifest={'status':'VERIFIED_S4_CHRONOLOGICAL_ACCOUNT','rows':len(daily),'baseline_parity_rows':len(parity),
              'cohort_dates':contract['cohort_dates'],'unit_status':'UNIT_UNVERIFIED','eligibility':'ELIGIBILITY_UNPROVEN',
              'output_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.iterdir() if p.is_file() and p.name!='validation_manifest.json'},
              'source_hashes':{str(p.relative_to(MODULE.parent)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths},
              'audit':{'daily_single_attempt':True,'selection_before_pnl':True,'no_losing_long_restart':True,
                       'counterfactuals_separate':True,'both_unit_scenarios':True,'no_short_gate_added':True,
                       'original_s0_s1_unchanged':True,'live_source_unchanged':True}}
    (ROOT/'baseline_parity.json').write_text(json.dumps(parity,indent=2)+'\n')
    manifest['output_hashes']['baseline_parity.json']=hashlib.sha256((ROOT/'baseline_parity.json').read_bytes()).hexdigest()
    (ROOT/'validation_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':manifest['status'],'rows':len(daily),'unchanged_baseline_rows':len(parity),'ties':len(ties)},ensure_ascii=False))


if __name__=='__main__':
    main()
