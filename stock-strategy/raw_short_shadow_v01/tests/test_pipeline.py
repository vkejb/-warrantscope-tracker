from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from raw_short_shadow_v01.pipeline import episodes, qualification, load_inputs, process_day, write
from raw_short_shadow_v01.main import is_prospective, contract, owned_runtime, run_day, status, carried_positions
from short_side_study_v01.s4 import chronological_selection
from short_side_study_v01.tests.test_s4 import signal


class WiringTests(TestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 13, 9, 10, tzinfo=ZoneInfo('Asia/Taipei'))

    def test_consecutive_approved_boundaries_are_one_episode_not_samples(self):
        rows = [{'variant': 'S1', 'stock_id': 'TEST', 'side': 'SHORT', 'decision_time': self.at + timedelta(seconds=n*30)} for n in range(10)]
        result = episodes(rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['boundary_count'], 10)
        self.assertFalse(result[0]['is_trade_sample'])

    def test_signal_gap_starts_episode_but_not_new_trade_sample(self):
        rows = [{'variant': 'S1', 'stock_id': 'TEST', 'side': 'SHORT', 'decision_time': self.at + timedelta(seconds=n)} for n in (0, 30, 90)]
        self.assertEqual(len(episodes(rows)), 2)
        self.assertTrue(all(not r['is_trade_sample'] for r in episodes(rows)))

    def test_missing_0050_never_adds_short_gate(self):
        at = self.at
        class FakeEngine:
            def __init__(self, *args, variant, **kw):
                self.variant = variant
            def choose_study_entry(self, decision):
                return signal('SHORT', decision) if self.variant == 'S1' else None
            def _market_context(self, decision):
                return None
        with patch('short_side_study_v01.s4.StudyEngine', FakeEngine), patch('short_side_study_v01.s4._feed_until'), patch('short_side_study_v01.s4._decision_times', return_value=[at]):
            selected, _, _, regime, _ = chronological_selection({'TEST': {'ticks': [], 'books': []}}, {'0050': {'ticks': [], 'books': []}}, '20261013', {})
        self.assertIsNotNone(selected['S1'])
        self.assertEqual(selected['S4'].side, 'SHORT')
        self.assertEqual(regime, 'UNKNOWN')

    def test_frozen_rules_and_isolated_accounts_no_live_capability(self):
        frozen = contract()
        self.assertEqual(frozen['accounts'], ['S0','S1','S4'])
        self.assertEqual(frozen['capital_twd'], 190000)
        self.assertEqual(frozen['daily_attempts_per_account'], 1)
        self.assertEqual(frozen['exit_policy']['stop_loss_net_twd'], 3500)
        self.assertEqual(frozen['exit_policy']['mfe_variant'], 'MFE_V1')
        self.assertEqual(frozen['base_spec']['hard_exit_time'], '13:20')
        self.assertFalse(frozen['short_0050_gate'])
        self.assertFalse(frozen['real_short_routing'])
        self.assertEqual(frozen['twenty_entries'], 'REVIEW_NODE_ONLY_NEVER_ENABLE_LIVE')

    def test_no_backdated_prospective_session(self):
        activation = {'enabled_at': '2026-10-13T06:50:00+00:00'}
        self.assertFalse(is_prospective({'started_at': '2026-10-13T00:50:00Z'}, activation))
        self.assertTrue(is_prospective({'started_at': '2026-10-14T00:50:00Z'}, activation))

    def test_official_runtime_paths_cannot_be_written(self):
        with self.assertRaises(ValueError):
            owned_runtime(Path('yuanta_live_runtime_v01/runtime'))

    def test_daily_qualification_preserves_unknown_and_exact_date(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            watch = {'stocks':[{'stock_id':'A','market':'TWSE'},{'stock_id':'B','market':'TPEX'}]}
            q, vetoes = qualification(watch, '20261013', root)
            self.assertEqual(len(q['rows']), 2)
            self.assertEqual(q['rows'][0]['sell_first_suspended'], 'UNKNOWN')
            self.assertEqual(q['rows'][1]['broker_quota'], 'UNKNOWN')
            self.assertEqual(vetoes, {})
            write(root / '20261013_twse_eligibility.json', {'session_date':'20261012','raw_response':{'date':'20261012'}})
            with self.assertRaises(ValueError):
                qualification(watch, '20261013', root / 'output', root)

    def test_known_suspension_only_reuses_existing_veto(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            data = {'session_date':'20261013','url':'https://www.twse.com.tw/exchangeReport/TWTB4U?date=20261013&response=json',
                    'raw_response':{'date':'20261013','stat':'OK','tables':[{'fields':['證券代號','暫停現股賣出後現款買進當沖註記'], 'data':[['A','Y'],['B','']]}]}}
            write(root / '20261013_twse_eligibility.json', data)
            q, vetoes = qualification({'stocks':[{'stock_id':s,'market':'TWSE'} for s in ('A','B','C')]}, '20261013', root / 'day', root)
            self.assertEqual(vetoes, {'A':False})
            self.assertIs(q['rows'][1]['sell_first_suspended'], False)
            self.assertEqual(q['rows'][2]['sell_first_suspended'], 'UNKNOWN')

    def test_duplicate_day_not_counted_and_conflict_retained(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            runtime = root / 'paper'
            runtime.mkdir()
            write(source / 'run_manifest.json', {'run_id':'mock-run','started_at':'2026-10-14T00:50:00Z','ended_at':'2026-10-14T05:35:00Z'})
            def fake_process(directory, output, **kw):
                write(output / 'source.json', {'status':'MOCK_TEST_FIXTURE'})
                return {'status':'MOCK_TEST_FIXTURE'}
            with patch('raw_short_shadow_v01.main.owned_runtime', side_effect=lambda x:x), patch('raw_short_shadow_v01.main.load_activation', return_value={'enabled_at':'2026-10-13T06:50:00+00:00'}), patch('raw_short_shadow_v01.main.process_day', side_effect=fake_process):
                self.assertEqual(run_day(source,runtime)['status'], 'PUBLISHED')
                self.assertEqual(run_day(source,runtime)['status'], 'ALREADY_PUBLISHED_NO_NEW_SAMPLE')
                write(source / 'run_manifest.json', {'run_id':'mock-different-run','started_at':'2026-10-14T00:50:00Z','ended_at':'2026-10-14T05:35:00Z','changed':True})
                self.assertEqual(run_day(source,runtime)['status'], 'DATA_CONFLICT_DO_NOT_COUNT')
                self.assertTrue((runtime / 'conflicts/20261014').exists())

    def test_twenty_entries_review_does_not_enable_live(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for n in range(20):
                day = root / 'days' / str(n)
                for account in ('S0','S1','S4'):
                    for unit in (1000,1):
                        write(day / account / f'unit_{unit}/trade.json', {'date':str(n),'status':'SCORED_TRADE','net_pnl':1,'entry_quantity':1000})
            with patch('raw_short_shadow_v01.main.owned_runtime', side_effect=lambda x:x), patch('raw_short_shadow_v01.main.inspect_published', return_value={}):
                result = status(root)
            self.assertTrue(result['accounts']['S1']['1000']['review_node_reached'])
            self.assertFalse(result['accounts']['S1']['1000']['automatic_live_enable'])
            self.assertFalse(result['live_send_available'])
            self.assertEqual(result['accounts']['S1']['1000']['entered_trades'],20)

    def test_bad_archive_preserved_as_missing_data_for_all_accounts(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            write(root / 'source/run_manifest.json', {'run_id':'MOCK_INCOMPLETE','started_at':'2026-10-13T00:50:00Z','ended_at':'2026-10-13T01:00:00Z'})
            write(root / 'source/watchlist.json', {'stocks':[]})
            result = process_day(root / 'source', root / 'report')
            self.assertEqual(result['status'], 'DATA_UNAVAILABLE')
            for account in ('S0','S1','S4'):
                row = json.loads((root / 'report' / account / 'report.json').read_text())
                self.assertIsNone(row['net_pnl'])
                self.assertEqual(row['status'],'DATA_UNAVAILABLE')

    def test_uncovered_paper_position_not_reset_next_day(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            write(root / 'days/20261014/S1/unit_1000/trade.json', {'date':'20261014','side':'SHORT','stock_id':'TEST','residual_shares':1000,'entry_quantity':2000,'net_pnl':None})
            with patch('raw_short_shadow_v01.main.inspect_published', return_value={}):
                carried = carried_positions(root)
            self.assertEqual(carried[('S1',1000)]['residual_shares'],1000)
            self.assertNotIn(('S0',1000), carried)
            self.assertNotIn(('S1',1), carried)

    def test_account_specific_missing_day_not_hidden_as_zero(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            write(root / 'days/20261014/S0/unit_1000/trade.json', {'date':'20261014','status':'SCORED_TRADE','net_pnl':1,'entry_quantity':1000})
            with patch('raw_short_shadow_v01.main.owned_runtime', side_effect=lambda x:x), patch('raw_short_shadow_v01.main.inspect_published', return_value={}):
                result = status(root)
            self.assertEqual(result['accounts']['S0']['1000']['net_pnl'],1)
            self.assertIsNone(result['accounts']['S1']['1000']['net_pnl'])
            self.assertEqual(result['accounts']['S1']['1000']['unscorable_or_open_exposure_days'],1)
