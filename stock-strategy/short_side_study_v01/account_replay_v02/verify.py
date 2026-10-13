"""Verify full frozen cohort/account attribution and label all unit assumptions."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .. import s4
from ..s4_verify import restored_signal
from .run import HERE, safe_metrics, safe_robustness

ROOT = HERE / 'results'
FIELDS = ('status', 'side', 'stock_id', 'signal_time', 'entry_quantity', 'entry_price', 'exit_time', 'exit_price',
          'gross_pnl', 'commission', 'tax', 'net_pnl', 'residual_shares', 'target_quantity', 'exit_fills')


def read(name):
    return json.loads((ROOT / name).read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fmt(value):
    return 'n.a.' if value is None else f'{value:,.2f}'


def verify():
    contract, daily, summary = read('contract.json'), read('daily_results.json'), read('summary.json')
    previous = json.loads((s4.MODULE / 'results_s4/daily_results.json').read_text())
    previous_contract = json.loads((s4.MODULE / 'results_s4/contract.json').read_text())
    provenance = read('provenance.json')
    for name, expected in provenance['legacy_s4_output_hashes'].items():
        assert digest(s4.MODULE / 'results_s4' / name) == expected, name
    for name, expected in provenance['unchanged_legacy_code_hashes'].items():
        assert digest(s4.MODULE.parent / name) == expected, name
    for key in ('cohort_dates', 'capital_twd', 'daily_attempt_limit', 'short_gate', 'selection',
                'independent_long_short_confirmation_states', 'anti_chase', 'base_spec', 'exit_policy', 'scenarios', 'volume_units'):
        assert contract[key] == previous_contract[key], key
    assert contract['short_gate'] == 'NONE_NEW' and contract['daily_attempt_limit'] == 1
    assert contract['exit_policy']['stop_loss_net_twd'] == 3500
    assert contract['exit_policy']['mfe_variant'] == 'MFE_V1'
    assert contract['fill_increment_shares'] == 1000
    assert len(daily) == 108
    index = {(r['date'], r['variant'], r['scenario'], r['volume_multiplier']): r for r in daily}
    prior = {(r['date'], r['variant'], r['scenario'], r['volume_multiplier']): r for r in previous}
    assert len(index) == 108 and set(index) == set(prior)
    parity, quantities = [], []
    for key, row in index.items():
        if row['volume_multiplier'] == 1000:
            for field in FIELDS:
                # Added target metadata is present for previously unfilled entries only.
                if field == 'target_quantity' and field not in prior[key]:
                    continue
                assert row.get(field) == prior[key].get(field), (key, field)
            parity.append({'key': list(key), 'matches_original_1000_unit_result': True})
        assert row['unit_status'] == 'UNIT_UNVERIFIED' and row['eligibility'] == 'ELIGIBILITY_UNPROVEN'
        for name in ('entry_quantity', 'covered_shares', 'residual_shares'):
            quantity = row.get(name)
            if quantity is not None:
                assert quantity >= 0 and quantity % 1000 == 0, (key, name, quantity)
        for fill in row.get('exit_fills', []):
            assert fill['shares'] > 0 and fill['shares'] % 1000 == 0
        for fill in row.get('depth_fill_evidence', []):
            assert fill['quantity'] > 0 and fill['quantity'] % 1000 == 0
        if row.get('residual_shares', 0):
            assert row['net_pnl'] is None and row['status'] != 'SCORED_TRADE'
        if row['status'] == 'SCORED_TRADE':
            assert row['covered_shares'] == row['entry_quantity'] and row['residual_shares'] == 0
        requests = row.get('execution_requests', [])
        if requests:
            assert requests[0]['side'] == ('SELL' if row['side'] == 'SHORT' else 'BUY')
            assert all(r['side'] == ('BUY' if row['side'] == 'SHORT' else 'SELL') for r in requests[1:])
        if row['volume_multiplier'] == 1 and row.get('side'):
            pair = index[(row['date'], row['variant'], row['scenario'], 1000)]
            assert row['target_quantity'] == pair['target_quantity']
            quantities.append({'date': row['date'], 'variant': row['variant'], 'scenario': row['scenario'],
                               'same_target_shares': row['target_quantity'], 'legal_fill_increment': 1000})
        if row['variant'] == 'S4':
            chosen = index[(row['date'], 'S1' if row['side'] == 'SHORT' else 'S0', row['scenario'], row['volume_multiplier'])]
            for field in FIELDS:
                assert row.get(field) == chosen.get(field), (key, field, 'ACCOUNT_NOT_SELECTED_SIDE_REPLAY')
    selections = read('selections.json')
    approvals = read('approved_boundaries.json')
    assert len(selections) == 6
    for entry in selections:
        signals = {v: restored_signal(raw) for v, raw in entry['signals'].items()}
        candidates = [s for v, s in signals.items() if v in ('S0', 'S1') and s is not None]
        if candidates:
            first = min(s.decision_time for s in candidates)
            boundary = {v: s if s and s.decision_time == first else None for v, s in signals.items()}
            assert signals['S4'] == s4.choose_boundary(boundary['S0'], boundary['S1'])
            approved = [r for r in approvals if r['date'] == entry['date']]
            assert min(restored_signal({k: v for k, v in r.items() if k not in ('date', 'variant')}).decision_time for r in approved) == first
        else:
            assert signals['S4'] is None
    for group in summary['summaries']:
        subset = [r for r in daily if all(r[k] == group[k] for k in ('variant', 'scenario', 'volume_multiplier'))]
        for k, value in safe_metrics(subset).items():
            assert group[k] == value, ('SUMMARY', k)
    for group in summary['s1_regime_descriptive_only']:
        subset = [r for r in daily if r['variant'] == 'S1' and r['scenario'] == group['scenario']
                  and r['volume_multiplier'] == group['volume_multiplier'] and r['descriptive_regime_at_signal'] == group['regime']]
        assert group['net_pnl'] == safe_metrics(subset)['net_pnl']
        assert group['trades'] == safe_metrics(subset)['trades']
    replacements = read('counterfactual_replacements.json')
    assert len(replacements) == 36
    for row in replacements:
        key = (row['date'], row['scenario'], row['volume_multiplier'])
        account = index[(key[0], 'S4', key[1], key[2])]
        assert row['account_net_pnl'] == account['net_pnl']
        assert row['counterfactual_is_not_in_account']
        if row['displaced_variant']:
            displaced = index[(key[0], row['displaced_variant'], key[1], key[2])]
            assert row['displaced_counterfactual_pnl'] == displaced['net_pnl']
    for group in summary['edge_attribution']:
        metrics = {v: next(m for m in summary['summaries'] if m['variant'] == v and m['scenario'] == group['scenario']
                          and m['volume_multiplier'] == group['volume_multiplier']) for v in ('S0', 'S4')}
        if all(m['net_pnl'] is not None for m in metrics.values()):
            assert sum(e['net_increment'] for e in group['effects'].values()) == metrics['S4']['net_pnl'] - metrics['S0']['net_pnl']
    assert len(summary['robustness']) == 18
    for group in summary['robustness']:
        assert len(group['leave_one_day_out']) == 6
        assert set(r['removed_day'] for r in group['leave_one_day_out']) == set(contract['cohort_dates'])
        subset = [r for r in daily if all(r[k] == group[k] for k in ('variant', 'scenario', 'volume_multiplier'))]
        for key, value in safe_robustness(subset).items():
            assert group[key] == value, ('ROBUSTNESS', key)
    assert summary['actual_orders'] == summary['actual_fills'] == summary['broker_connections'] == 0
    s4.write_json(ROOT / 'baseline_parity.json', parity)
    s4.write_json(ROOT / 'quantity_parity.json', quantities)
    return daily, summary, replacements


def report(daily, summary, replacements, test_count):
    lines = ['S4：LONG A + raw SHORT，合法整股容量的 chronological 帳戶回放',
             'UNIT_UNVERIFIED / ELIGIBILITY_UNPROVEN / NO_TRUE_OUT_OF_SAMPLE_SET',
             '本報告取代舊1股敏感度的非法小股數成交解讀，不覆寫舊檔。',
             '原base訊號、SHORT thresholds、confirmation、anti-chase、資金190000、成本、-3500 hard stop、MFE_V1與13:20 exit不變。',
             'LONG與SHORT獨立approved；最早approved佔用當日唯一機會，即使未成交也不轉向其他交易。',
             '同一boundary才用原score/notional/stable symbol排序，沒有事後選盈利方向或LONG虧損後再開SHORT。',
             'SHORT使用bid深度，cover使用ask深度。0050 regime只是first SHORT approved時的描述，沒有新gate。',
             '唯一執行修正：raw-share容量記帳後，每價位成交必須為1000股倍數；相同請求數量，不偷縮倉、不混零股。',
             '未成交進場餘量假設立即撤除是研究模型假設，沒有券商受理/撤單驗證；不是正式execution認證。',
             '未完成回補/缺報價結果保留null；不以完成子集冒充完整帳戶損益、PF、勝率或回撤。',
             '每日已實現DD與盤中quote-mark DD並列；估值報價不保證能足量清倉。', '', '各版完整比較：']
    for m in summary['summaries']:
        lines.append(f"{m['variant']} / {m['scenario']} / {m['volume_multiplier']}股每單位：進場{m['entered_trades']}；完成{m['trades']}；net {fmt(m['net_pnl'])}；PF {fmt(m['profit_factor'])}；win% {fmt(None if m['win_rate'] is None else m['win_rate']*100)}；avg win {fmt(m['avg_winner'])}；avg loss {fmt(m['avg_loser'])}；daily DD {fmt(m['max_daily_realized_drawdown'])}；intraday mark DD {fmt(m['max_quote_mark_account_drawdown'])}；未完成/未知日 {m['unscorable_or_open_exposure_days']}")
    lines += ['', '每日帳戶選擇/被取代交易（base，1000假設；反事實未加入帳戶）：']
    for r in replacements:
        if r['scenario'] == 'BASE_CURRENT_EXECUTION' and r['volume_multiplier'] == 1000:
            lines.append(f"{r['date']}：{r['account_side']} {r['account_stock_id']} @ {r['account_signal_time']}；net {fmt(r['account_net_pnl'])}；vs LONG {fmt(r['increment_vs_s0'])}")
            lines.append(f"  {r['kind']}；被取代 {r['displaced_side']} {r['displaced_stock_id']} @ {r['displaced_signal_time']}；counterfactual {fmt(r['displaced_counterfactual_pnl'])}")
    lines += ['', '1股假設的每日選擇與真正成交（base；未成交為模型零交易，不是資料未知被填零）：']
    for r in daily:
        if r['variant'] == 'S4' and r['scenario'] == 'BASE_CURRENT_EXECUTION' and r['volume_multiplier'] == 1:
            lines.append(f"{r['date']} {r['side']} {r.get('stock_id')}：status {r['status']}；請求 {r.get('target_quantity')}；成交 {r.get('entry_quantity',0)}；殘餘 {r['residual_shares']}；net {fmt(r['net_pnl'])}")
    lines += ['', 'S1 regime僅描述（base，1000假設）：']
    for r in summary['s1_regime_descriptive_only']:
        if r['scenario'] == 'BASE_CURRENT_EXECUTION' and r['volume_multiplier'] == 1000:
            lines.append(f"{r['regime']}：trade count {r['trades']}；net {fmt(r['net_pnl'])}")
    lines += ['', '改善來源（base，1000假設）：']
    group = next(g for g in summary['edge_attribution'] if g['scenario'] == 'BASE_CURRENT_EXECUTION' and g['volume_multiplier'] == 1000)
    for kind, effect in group['effects'].items():
        lines.append(f"{kind}：{effect['days']}日 {effect['dates']}；vs S0增量 {fmt(effect['net_increment'])}")
    lines.append('LONG losing days改善：' + str(group['long_losing_days_improved']))
    lines.append('LONG winners被取代：' + str(group['long_winners_displaced']))
    lines += ['', '壓力：remove-best / remove-worst / leave-one-day-out（全部情境、兩種單位）：']
    for r in summary['robustness']:
        best, worst = r['remove_best_trade'], r['remove_worst_trade']
        lines.append(f"{r['variant']} / {r['scenario']} / {r['volume_multiplier']}：remove best net {fmt(None if best is None else best['net_pnl'])}；remove worst net {fmt(None if worst is None else worst['net_pnl'])}")
        lines.append('  ' + '；'.join(f"remove {v['removed_day']} net {fmt(v['net_pnl'])}" for v in r['leave_one_day_out']))
    lines += ['', '最後四項回答（1000假設只描述樣本，不是已確認實盤edge）：',
             '1. raw SHORT值得維持隔離prospective shadow來累積證據；5筆樣本、移除最佳與單位/資格未確認，不能稱正期望已成立。',
             '2. S4比LONG-only少虧，但base、+1tick、5sec delay仍為負；每日已實現DD反而較大，不構成可交易優勢。',
             '3. 本樣本主要由較早SHORT替換一筆LONG虧損而改善；新增SHORT機會是負貢獻。沒有搶掉LONG winner，但也有較早LONG擋住後續獲利SHORT。',
             '4. 不建議現在開始正式SHORT execution layer；先確認單位、資格/額度與prospective結果。只建議SHORT shadow，不啟用LIVE。',
             '1股假設按合法整張容量評估的成交/績效與1000假設並列；不可選擇較好假設猜真實單位。',
             f'{test_count}項離線測試通過；1000假設54筆與舊S4輸出逐筆相同，1假設只修正非法成交增量。',
             '舊S4、單位稽核與LIVE原碼hash保持不變；無券商登入/下單，無push/merge/部署。']
    return '\n'.join(lines) + '\n'


def main():
    daily, summary, replacements = verify()
    logs, count = [], 0
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    for path in (s4.MODULE / 'tests', HERE / 'tests', s4.MODULE / 'unit_eligibility_audit_v01/tests'):
        test = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(path), '-v'],
                              cwd=s4.MODULE.parent, capture_output=True, text=True, env=env)
        text = test.stdout + test.stderr
        logs += [str(path), text]
        if test.returncode:
            raise RuntimeError(text)
        count += int(re.search(r'Ran (\d+) tests', text).group(1))
    (ROOT / 'tests.txt').write_text('\n'.join(logs))
    (ROOT / 'report_zh-TW.txt').write_text(report(daily, summary, replacements, count))
    manifest = {'status': 'VERIFIED_CONDITIONAL_LEGAL_LOT_CHRONOLOGICAL_ACCOUNT', 'rows': len(daily),
                'tests_passed': count, '1000_unit_original_parity_rows': 54,
                'unit_status': 'UNIT_UNVERIFIED', 'eligibility': 'ELIGIBILITY_UNPROVEN',
                'live_or_strategy_source_changes': False, 'new_short_threshold_or_0050_gate': False,
                'output_hashes': {p.name: digest(p) for p in ROOT.iterdir() if p.is_file() and p.name != 'validation_manifest.json'},
                'implementation_hashes': {str(p.relative_to(HERE)): digest(p) for p in HERE.rglob('*.py')}}
    s4.write_json(ROOT / 'validation_manifest.json', manifest)
    print(json.dumps({'status': manifest['status'], 'tests_passed': count, 'rows': len(daily)}))


if __name__ == '__main__':
    main()
