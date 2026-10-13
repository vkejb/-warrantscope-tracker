"""Deliver evidence for paper wiring/mock safety, without starting any service."""
from datetime import datetime
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

from .pipeline import HERE, VARIANTS, sha, write
from .main import DEFAULT_RUNTIME, load_activation, is_prospective, status, source_hashes


def main():
    validation = HERE / 'validation'
    day = validation / '20261013_final'
    source = json.loads((day / 'source.json').read_text())
    assert source['status'] == 'PAPER_DAY_COMPLETE'
    assert source['observation_scope'] == 'ENGINEERING_ACCEPTANCE_NOT_PROSPECTIVE'
    assert source['coverage']['callback_errors'] == 0
    assert source['quality']['invalid_depth_rows'] == 0
    directory = HERE.parent / source['source_directory']
    assert sha(directory / 'run_manifest.json') == source['manifest_sha256']
    assert sha(directory / 'watchlist.json') == source['watchlist_sha256']
    for name, expected in source['artifact_hashes'].items():
        assert sha(directory / name) == expected, name
    qualification = json.loads((day / 'qualification.json').read_text())
    assert len(qualification['rows']) == 30
    assert all(r['broker_account_permission'] == r['borrowable_supply'] == r['broker_quota'] == 'UNKNOWN' for r in qualification['rows'])
    evidence = {}
    for account in VARIANTS:
        report = json.loads((day / account / 'report.json').read_text())
        assert report['account'] == account and report['not_added_to_other_accounts']
        assert report['unit_status'] == 'UNIT_UNVERIFIED'
        assert report['unconditional_net_pnl'] is None
        evidence[account] = {'report': str((day / account / 'report.json').relative_to(HERE)), 'units': {}}
        for unit in (1000,1):
            row = json.loads((day / account / f'unit_{unit}/trade.json').read_text())
            assert row['variant'] == account and row['volume_multiplier'] == unit
            assert row['signal_sample_count'] <= 1
            assert row['observation_scope'] == 'ENGINEERING_ACCEPTANCE_NOT_PROSPECTIVE'
            if row.get('residual_shares',0):
                assert row['net_pnl'] is None
            if row.get('side') == 'SHORT' and row.get('entry_quantity',0):
                state = json.loads((day / account / f'unit_{unit}/cover_safety.json').read_text())
                assert state['remaining_filled_short'] == row['residual_shares']
                assert state['baseline_source'] == 'SYNTHETIC_NOT_REAL_ACCOUNT'
                if row['status'] == 'SCORED_TRADE':
                    assert state['status'] == 'FLAT'
            evidence[account]['units'][str(unit)] = {'status': row['status'], 'entry_quantity': row.get('entry_quantity',0),
                    'residual_shares': row['residual_shares'], 'conditional_net_pnl': row['net_pnl']}
    logs, total = [], 0
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    for folder in (HERE / 'tests', HERE.parent / 'intraday_paper_execution_v01/tests',
                   HERE.parent / 'short_side_study_v01/tests', HERE.parent / 'short_side_study_v01/account_replay_v02/tests'):
        result = subprocess.run([sys.executable,'-m','unittest','discover','-s',str(folder),'-v'],
                                cwd=HERE.parent, env=env, capture_output=True, text=True)
        log = result.stdout + result.stderr
        logs += [str(folder), log]
        if result.returncode:
            raise RuntimeError(log)
        total += int(re.search(r'Ran (\d+) tests',log).group(1))
    (validation / 'tests.txt').write_text('\n'.join(logs))
    activation = load_activation(DEFAULT_RUNTIME)
    assert not is_prospective(source,activation)
    prospective = status(DEFAULT_RUNTIME)
    assert prospective['included_dates'] == []
    assert all(m['entered_trades'] == 0 and not m['automatic_live_enable'] for a in prospective['accounts'].values() for m in a.values())
    write(validation / 'activation_receipt.json',activation)
    write(validation / 'prospective_status.json',prospective)
    proposal = plistlib.loads((HERE / 'schedule.example.plist').read_bytes())
    assert proposal['Label'] == 'com.linyunyan.warrantscope.raw-short-paper'
    assert proposal['ProgramArguments'][-2:] == ['drain','--fetch-public']
    agent = Path('/Users/linyunyan/Library/LaunchAgents/com.linyunyan.warrantscope.yuanta-intraday-shadow.plist')
    actual = plistlib.loads(agent.read_bytes())
    assert 'yuanta_intraday_shadow_v01.auto_runner' in actual['ProgramArguments']
    assert not (agent.parent / 'com.linyunyan.warrantscope.raw-short-paper.plist').exists()
    protected = ('yuanta_live_runtime_v01','yuanta_broker_execution_v01','yuanta_intraday_shadow_v01/auto_runner.py','paper_shadow_v01','intraday_paper_execution_v01')
    diff = subprocess.run(['git','diff','--exit-code','HEAD','--',*protected], cwd=HERE.parent,capture_output=True,text=True)
    assert diff.returncode == 0, 'Existing LONG/collector/paper engine source changed'
    wiring = {'existing': {'collector': 'EXISTING_READ_ONLY_ARCHIVE_REUSED', 'scheduled_long_paper': 'ALREADY_WIRED_LONG_ONLY',
                          's1_selector_and_lifecycle': 'REUSED_FROZEN_SHORT_SIDE_AND_LEGAL_REPLAY', 'mock_execution': 'REUSED_EXISTING_PAPER_ENGINE'},
              'new': {'raw_s1_daily_wiring': 'MANUAL_ONE_SHOT_POST_SESSION_READY', 'independent_accounts': list(VARIANTS),
                      'short_mock_cover_safety': 'VERIFIED_OFFLINE', 'schedule': 'PROPOSAL_ONLY_NOT_INSTALLED',
                      'live_short_routing': 'NOT_IMPLEMENTED'}, 'source_hashes':source_hashes(),
              'installed_collector_plist_sha256':sha(agent), 'qualification':'UNKNOWN_WITH_DAILY_SNAPSHOT_NO_BROKER_ENTITLEMENT_PROOF',
              'engineering_day_is_not_prospective':True, 'prospective_entries':0, 'tests_passed':total, 'accounts':evidence}
    write(validation / 'wiring_status.json',wiring)
    enabled = datetime.fromisoformat(activation['enabled_at']).astimezone(ZoneInfo('Asia/Taipei'))
    lines = ['raw SHORT S1紙上接線與回補安全離線驗收',
             '重用既有S1/S4訊號與退出管理、legal lot容量及PaperExecutionEngine；未修改正常LONG下單/adapter/OrderQty/回報/正式DB。',
             '新接線：既有行情封存→訊號→紙上進場→持倉管理→回補→獨立S0/S1/S4日報。',
             '執行模式是手動單次收盤後因果回放，不是已部署的即時SHORT系統。原collector與LaunchAgent未更動。',
             '排程缺口：新SHORT紙上作業未安裝，交付方案與範例plist；本輪未copy/launchctl/啟停任何服務。',
             f'實際凍結/手動紙上啟用：{enabled.isoformat()}；prospective起點依這份activation收據，不倒填。',
             '目前新prospective紙上交易=0。2026/10/13行情開始早於啟用，只是工程驗收，不列入新樣本。',
             '二十筆只是各紙上帳戶/單位假設重新檢視節點，永不自動上線；不等待樣本。',
             'UNIT_UNVERIFIED只指行情五檔到研究容量換算，不代表正常LONG下單有問題。',
             '原始五檔字串與源檔hash保留；兩單位容量損益僅為假設，不相加、不事後選方向。',
             '0050對S1只描述；stock pool/base/anti-chase/confirmation/190000資金/一日一筆/-3500停損/MFE/13:20不變。',
             '連續訊號合併episode，並非獨立交易；同日重跑不增加样本，來源衝突留存且不算完整帳戶結果。',
             '已成交剩餘空單與未終結BUY保留量獨立管理，合成baseline不被回補侵入。從未讀取實際baseline或帳戶庫存。',
             '未回補不能標FLAT；跨日未完成不重設部位，新日帳會保留該帳戶/假設曝險並禁止新進場。',
             '紙上跨日曝險仍需資料與mock對帳後續處理；本輪未另造跨日稅費/借券或券商救援模型。',
             '每日已保存30檔當沖/暫停先賣後買資格欄位；本日無可用exact-day官方快取，保持UNKNOWN。',
             '首次公開TWSE抓取DNS失敗已記錄於20261013_engineering/qualification.json，未反覆抓取或用今日清單補歷史。',
             '權限、券源、額度皆UNKNOWN，並非下單授權。ELIGIBILITY_UNPROVEN保留。',
             'source.coverage_status沿用原loader的PARTIAL_SESSION標記；本验收只證明原interval/hash检查通过，不主張LIVE輸入對等或完整交易資格。',
             f'{total}項離線測試通過，包含全部指定部分成交/重複/拒單/取消中成交/重啟/人工回補/無賣盤/跨強平情境。',
             '', '工程驗收的獨立帳戶（不是prospective）：']
    for account,data in evidence.items():
        lines.append(account + ': ' + json.dumps(data['units'],ensure_ascii=False))
    lines += ['', '交付：README.txt（唯讀重跑/狀態命令）；schedule_plan_zh-TW.txt與schedule.example.plist（未安裝）；',
              'pending_yuanta_zh-TW.txt（只問行情容量，不重做LONG下單）；validation各帳戶report.json、mock安全狀態、測試与hash清單。',
              '不登入、不送單、不啟停交易/行情程序、不改正式DB/風控、不push/merge/部署。']
    (validation / 'delivery_zh-TW.txt').write_text('\n'.join(lines)+'\n')
    write(validation / 'completion_audit.json', {'requirements': {
              'existing_wiring_checked_and_reused': 'wiring_status.json',
              'frozen_s1_no_new_0050_gate': 'activation_receipt.json and tests/test_pipeline.py',
              'independent_s0_s1_s4_accounts_and_dedup': '20261013_final/*/report.json and signal_episodes.json',
              'raw_depth_preserved_conditional_capacity_only': '20261013_final/*/raw_entry_book.json and unit_*/trade.json',
              'daily_exchange_qualification_and_unknown_broker_fields': '20261013_final/qualification.json',
              'all_named_cover_mock_cases_and_synthetic_baseline': 'tests.txt and tests/test_safety.py',
              'actual_activation_no_backfill_zero_new_samples': 'activation_receipt.json and prospective_status.json',
              'readonly_replay_commands_and_uninstalled_schedule': '../README.txt and ../schedule_plan_zh-TW.txt',
              'pending_yuanta_scope_only': '../pending_yuanta_zh-TW.txt'},
              'gaps_explicit': ['Schedule not installed by design', 'Exchange exact-day evidence unavailable remains UNKNOWN',
                                'Broker permission/supply/quota UNKNOWN', 'Five-level conversion UNIT_UNVERIFIED',
                                'Uncovered cross-day paper exposures retained and new entries blocked'],
              'not_a_live_short_or_normal_long_fault_certification': True,
              'not_waiting_for_new_samples': True})
    outputs = {str(p.relative_to(HERE)):sha(p) for p in validation.rglob('*') if p.is_file()
               and '.sqlite' not in p.name and p.name!='validation_manifest.json'}
    write(validation / 'validation_manifest.json', {'status':'VERIFIED_MANUAL_PAPER_WIRING_AND_OFFLINE_COVER_SAFETY',
              'tests_passed':total, 'output_hashes':outputs, 'source_hashes':source_hashes(),
              'implementation_hashes': {str(p.relative_to(HERE)):sha(p) for p in HERE.rglob('*.py')},
              'prospective_entries':0, 'scheduler_installed':False, 'live_short_routing':False})
    print(json.dumps({'status':'VERIFIED_MANUAL_PAPER_WIRING_AND_OFFLINE_COVER_SAFETY','tests_passed':total,
                      'prospective_entries':0,'scheduler_installed':False},ensure_ascii=False))


if __name__ == '__main__':
    main()
