from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from raw_short_shadow_v01.pipeline import write
from raw_short_shadow_v01.prospective_acceptance import audit_day, classify_account, first_complete_source, replay_raw_selection
from short_side_study_v01.policy import StudyEngine


class ProspectiveAcceptanceTests(TestCase):
    def test_s1_scans_raw_candidates_without_live_short_flag_or_long_gate(self):
        at = datetime(2026, 10, 14, 9, 10, tzinfo=ZoneInfo('Asia/Taipei'))
        symbols = [f'{n:04d}' for n in range(30)]
        engine = StudyEngine({**{s: s for s in symbols}, '0050': '0050'}, candidate_symbols=set(symbols),
                             benchmark_symbol='0050', variant='S1', capital_twd=190000)
        seen = []
        def raw_signal(symbol, decision):
            seen.append(symbol)
            if symbol != symbols[-1]:
                return None
            return {'stock_id': symbol, 'stock_name': symbol, 'side': 'SHORT', 'decision_time': decision,
                    'score': .8, 'volume_delta': -.5, 'large_trade_delta': -.5, 'vwap_gap': -.01,
                    'book_imbalance': -.3, 'spread_bps': 10., 'entry_price': 100., 'quantity': 1000,
                    'return_5m': None, 'directional_opening_extension': .005,
                    'directional_vwap_extension': .005, 'breakout_boundary_price': 101.}
        with patch.object(engine, '_signal_for', side_effect=raw_signal), \
             patch.object(engine, '_market_context', return_value=None), \
             patch.object(engine, 'choose_entry', side_effect=AssertionError('LIVE gate called')):
            selected = engine.choose_study_entry(at)
        self.assertEqual(set(seen), set(symbols))
        self.assertEqual(selected.stock_id, symbols[-1])
        self.assertEqual(selected.side, 'SHORT')

    def test_no_signal_and_missing_data_are_distinct(self):
        self.assertEqual(classify_account(None, source_ok=True, candidate_data_ok=True, benchmark_ok=True), 'NO_SIGNAL')
        self.assertEqual(classify_account(None, source_ok=False, candidate_data_ok=True, benchmark_ok=True), 'DATA_MISSING')
        self.assertEqual(classify_account(None, source_ok=True, candidate_data_ok=False, benchmark_ok=True), 'DATA_MISSING')
        self.assertEqual(classify_account(None, source_ok=True, candidate_data_ok=True, benchmark_ok=False), 'DATA_MISSING')

    def test_raw_trace_replays_all_30_without_long_filtered_pool(self):
        at = datetime(2026, 10, 14, 9, 10, tzinfo=ZoneInfo('Asia/Taipei'))
        symbols = [f'{n:04d}' for n in range(30)]
        def raw_signal(engine, symbol, decision):
            if symbol != symbols[-1]:
                return None
            return {'stock_id': symbol, 'stock_name': symbol, 'side': 'SHORT', 'decision_time': decision,
                    'score': .8, 'volume_delta': -.5, 'large_trade_delta': -.5, 'vwap_gap': -.01,
                    'book_imbalance': -.3, 'spread_bps': 10., 'entry_price': 100., 'quantity': 1000,
                    'return_5m': None, 'directional_opening_extension': .005,
                    'directional_vwap_extension': .005, 'breakout_boundary_price': 101.}
        stocks = {s: {'ticks': [], 'books': []} for s in symbols}
        with patch.object(StudyEngine, '_signal_for', raw_signal), \
             patch.object(StudyEngine, '_market_context', return_value=None), \
             patch('short_side_study_v01.s4._feed_until'), \
             patch('short_side_study_v01.s4._decision_times', return_value=[at]):
            (selected, _, _, _, _), trace = replay_raw_selection(stocks, {'0050': {'ticks': [], 'books': []}}, '20261014', {})
        self.assertIsNone(selected['S0'])
        self.assertEqual(selected['S1'].stock_id, symbols[-1])
        self.assertTrue(trace['all_candidate_symbols_scanned_at_every_boundary'])
        self.assertTrue(trace['selected_s1_was_raw_short_candidate'])
        self.assertEqual(trace['minimum_candidates_per_boundary'], 30)
        self.assertFalse(trace['live_allow_short_flag_used'])

    def test_first_complete_source_starts_on_october_14_and_requires_coverage(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for name, start, end in (
                ('old', '2026-10-13T00:50:00Z', '2026-10-13T05:35:00Z'),
                ('short', '2026-10-14T01:10:00Z', '2026-10-14T05:35:00Z'),
                ('eligible', '2026-10-14T00:50:00Z', '2026-10-14T05:35:00Z')):
                write(root / name / 'run_manifest.json', {'run_id': name, 'started_at': start, 'ended_at': end,
                                                           'status': 'COMPLETE', 'event_counts': {'callback_errors': 0}})
            source, rejected = first_complete_source(root, {'enabled_at': '2026-10-13T07:16:00+00:00'})
            self.assertEqual(source.name, 'eligible')
            self.assertEqual([r['run_id'] for r in rejected], ['short'])

    def test_complete_published_day_with_no_signal_is_not_data_missing(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'archive'
            day = root / 'paper'
            manifest = {'run_id': 'eligible', 'started_at': '2026-10-14T00:50:00Z',
                        'ended_at': '2026-10-14T05:35:00Z'}
            write(source / 'run_manifest.json', manifest)
            from raw_short_shadow_v01.pipeline import sha
            write(day / 'source.json', {'status': 'PAPER_DAY_COMPLETE',
                                      'observation_scope': 'PROSPECTIVE_AFTER_ACTUAL_ACTIVATION',
                                      'unit_status': 'UNIT_UNVERIFIED'})
            write(day / 'qualification.json', {'session_date': '20261014', 'rows': [
                {'stock_id': f'{n:04d}', 'sell_first_suspended': 'UNKNOWN'} for n in range(30)]})
            write(day / 'signal_episodes.json', [])
            for account in ('S0', 'S1', 'S4'):
                write(day / account / 'report.json', {'logical_signal_samples': 0, 'unit_status': 'UNIT_UNVERIFIED'})
                for unit in (1000, 1):
                    write(day / account / f'unit_{unit}/trade.json', {
                        'variant': account, 'volume_multiplier': unit, 'unit_status': 'UNIT_UNVERIFIED',
                        'status': 'NO_TRADE', 'signal_sample_count': 0, 'independent_trade_sample': False,
                        'entry_quantity': 0, 'residual_shares': 0,
                        'cover_safety': {'status': 'NO_FILLED_SHORT_TO_MANAGE'}})
            stocks = {f'{n:04d}': {'ticks': [1], 'books': [1]} for n in range(30)}
            depths = {s: [1] for s in stocks}
            watch = {'stocks': [{'stock_id': s} for s in stocks]}
            market = {'0050': {'ticks': [1], 'books': [1]}}
            with patch('raw_short_shadow_v01.prospective_acceptance.inspect_published', return_value={
                'session_date': '20261014', 'source_manifest_sha256': sha(source / 'run_manifest.json')}), \
                patch('raw_short_shadow_v01.prospective_acceptance.load_inputs', return_value=(
                    stocks, market, depths, {}, manifest, watch, {},
                    {'invalid_depth_rows': 0, 'market_context': 'AVAILABLE_DESCRIPTIVE_ONLY'})), \
                patch('raw_short_shadow_v01.prospective_acceptance.replay_raw_selection', return_value=((
                    {'S0': None, 'S1': None, 'S4': None}, [], [], 'UNKNOWN', None),
                    {'all_candidate_symbols_scanned_at_every_boundary': True,
                     'selected_s1_was_raw_short_candidate': True, 'live_allow_short_flag_used': False})):
                result = audit_day(source, day, {'enabled_at': '2026-10-13T07:16:00+00:00'})
            self.assertEqual(result['status'], 'ACCEPTED')
            self.assertEqual({a['status'] for a in result['accounts'].values()}, {'NO_SIGNAL'})
            self.assertEqual(result['unit_status'], 'UNIT_UNVERIFIED')
            self.assertTrue(result['fills_are_not_broker_verified'])
