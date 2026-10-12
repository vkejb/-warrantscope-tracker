"""Regression evidence for the isolated audit, not live certification or replay."""
import ast
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import audit
import inspect_sdk


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.evidence = audit.read(HERE / 'results/audit.json')

    def test_frozen_cohort(self):
        self.assertEqual(self.evidence['cohort_dates'], list(audit.DATES))
        self.assertEqual(len(self.evidence['sessions']), 6)

    def test_every_daily_stock_is_accounted_for(self):
        rows = self.evidence['historical_eligibility']
        self.assertEqual(len(rows), 180)
        self.assertEqual(len({(r['date'], r['stock_id']) for r in rows}), 180)
        for date in audit.DATES:
            self.assertEqual(sum(r['date'] == date for r in rows), 30)

    def test_no_verified_pnl_or_rerun_from_unknown_units(self):
        self.assertEqual(self.evidence['unit_status'], 'UNIT_UNVERIFIED')
        self.assertFalse(self.evidence['rerun_performed'])
        for key in ('verified_account_pnl', 'verified_daily_drawdown', 'verified_intraday_drawdown', 'verified_robustness'):
            self.assertIsNone(self.evidence[key])

    def test_missing_broker_evidence_not_exchange_eligibility(self):
        for row in self.evidence['historical_eligibility']:
            self.assertEqual(row['broker_quota_status'], 'BROKER_QUOTA_UNPROVEN')
            self.assertEqual(row['broker_acceptance_status'], 'BROKER_ACCEPTANCE_UNPROVEN')
            self.assertIsNone(row['fully_proven_short_eligibility'])

    def test_missing_tpex_history_not_filled_with_current_list(self):
        tpex = [r for r in self.evidence['historical_eligibility'] if r['market'] == 'TPEX']
        self.assertEqual(len(tpex), 68)
        self.assertTrue(all(r['source_date'] is None and r['exchange_flag'] is None for r in tpex))

    def test_selected_shorts_have_exact_dated_rows(self):
        rows = self.evidence['selected_short_eligibility']
        self.assertEqual({(r['date'], r['stock_id']) for r in rows}, {
            ('20261001', '4956'), ('20261002', '2340'), ('20261005', '6226'),
            ('20261006', '2305'), ('20261008', '6226')})
        self.assertTrue(all(r['source_date'] == r['date'] and r['exchange_flag'] == '' for r in rows))

    def test_wrong_historical_date_fails(self):
        payload = audit.read(audit.STUDY / 'sources/20261008_twse_eligibility.json')
        with self.assertRaisesRegex(ValueError, 'HISTORICAL_DATE_MISMATCH'):
            audit.dated_exchange_row(payload, '20261006', '6226')

    def test_raw_date_mismatch_fails(self):
        payload = audit.read(audit.STUDY / 'sources/20261008_twse_eligibility.json')
        payload['raw_response']['date'] = '20261012'
        with self.assertRaisesRegex(ValueError, 'HISTORICAL_DATE_MISMATCH'):
            audit.dated_exchange_row(payload, '20261008', '6226')

    def test_absent_target_stays_unknown(self):
        payload = audit.read(audit.STUDY / 'sources/20261008_twse_eligibility.json')
        row = audit.dated_exchange_row(payload, '20261008', 'NO_SUCH_CODE')
        self.assertEqual(row['exchange_status'], 'HISTORICAL_TARGET_UNPROVEN')
        self.assertIsNone(row['exchange_flag'])

    def test_mapping_cannot_override_historical_flag(self):
        payload = audit.read(audit.STUDY / 'sources/20261008_twse_eligibility.json')
        payload['codes']['6226'] = False
        with self.assertRaisesRegex(ValueError, 'HISTORICAL_CACHE_MAPPING_MISMATCH'):
            audit.dated_exchange_row(payload, '20261008', '6226')

    def test_legal_regular_partial_fill_is_whole_lots(self):
        self.assertTrue(audit.legal_quantity(1000))
        self.assertTrue(audit.legal_quantity(2000))
        for quantity in (1, 47, 999, 1001):
            self.assertFalse(audit.legal_quantity(quantity))
        self.assertTrue(audit.legal_quantity(47, market='TWSEODD'))
        self.assertIsNone(audit.legal_quantity(47, market='TAIFEX'))

    def test_fractional_and_nonfinite_quantities_rejected(self):
        for quantity in ('0.5', '-1', 'nan', 'Infinity', 'INVALID'):
            with self.assertRaises(ValueError):
                audit.strict_integer(quantity)

    def test_same_requested_size_but_illegal_partial_not_full_profit(self):
        rows = self.evidence['sensitivity_audit']
        one = [r for r in rows if r['volume_multiplier'] == 1 and r['entry_quantity']]
        self.assertEqual(len(one), 48)
        self.assertTrue(all(r['same_requested_quantity'] for r in one))
        self.assertTrue(all(r['audit_status'] == 'INVALID_BOARD_LOT_EXECUTION' for r in one))
        self.assertTrue(all(r['verified_full_strategy_pnl'] is None for r in one))

    def test_invalid_exit_detected_even_if_entry_legal(self):
        row = {'date': '20261008', 'variant': 'S0', 'volume_multiplier': 1,
               'target_quantity': 1000, 'entry_quantity': 1000, 'entry_price': 10,
               'exit_fills': [{'shares': 999}], 'residual_shares': 1}
        result = audit.sensitivity_row(row, {'target_quantity': 1000})
        self.assertEqual(result['audit_status'], 'INVALID_BOARD_LOT_EXECUTION')
        self.assertIsNone(result['verified_full_strategy_pnl'])

    def test_no_target_not_reported_as_size_parity(self):
        self.assertIsNone(audit.sensitivity_row({}, {})['same_requested_quantity'])

    def test_all_raw_streams_flag50_no_channel_or_sdk_proof(self):
        for session in self.evidence['sessions']:
            self.assertEqual(session['manifest_version_keys'], [])
            self.assertEqual(session['historical_sdk_binding'], 'HISTORICAL_SDK_VERSION_UNBOUND')
            for stream in session['streams']:
                self.assertEqual(stream['index_flags'], {'IndexFlag50': stream['rows']})
                self.assertFalse(stream['raw_callback_market_type_preserved'])
                self.assertEqual(stream['invalid_flag50_quantity_shapes'], 0)

    def test_flag51_not_odd_lots_or_first_five_levels(self):
        self.assertEqual(audit.FLAG_FIELD_LAYOUT['IndexFlag51'], 'BOTH_SIDES_LEVELS_6_TO_10')

    def test_source_trace_contains_actual_conversion_once(self):
        replay = [r for r in self.evidence['conversion_trace'] if r['path'].endswith('/replay.py')]
        self.assertEqual(sum('q*volume_multiplier' in r['source'] for r in replay), 1)
        self.assertTrue(any('max(target,1)' in r['source'] for r in replay))
        self.assertTrue(any('exposure.remaining_shares' in r['source'] for r in replay))

    def test_callback_mock_roundtrip_does_not_scale(self):
        # Compile this pure extraction function only; do not import collector/SDK.
        path = audit.STRATEGY / 'yuanta_intraday_shadow_v01/collector_main.py'
        tree = ast.parse(path.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_book_payload')
        namespace = {'_safe_text': str}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
        raw = {f'{side}{kind}{i}': i for side in ('Buy', 'Sell') for kind in ('Price', 'Vol') for i in range(1, 6)}
        result = namespace['_book_payload'](SimpleNamespace(IndexFlag='IndexFlag50', IndexFlag_50=SimpleNamespace(**raw)))
        archived = json.loads(json.dumps(result))
        self.assertEqual(archived['buy_volumes'], ['1', '2', '3', '4', '5'])
        self.assertEqual([audit.strict_integer(v) for v in archived['sell_volumes']], [1, 2, 3, 4, 5])

    def test_legacy_capacity_defect_is_reproduced_without_strategy_replay(self):
        path = audit.STUDY / 'execution.py'
        spec = importlib.util.spec_from_file_location('audit_legacy_execution', path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        now = datetime(2026, 10, 8, 1, tzinfo=timezone.utc)
        book = module.DepthBook(now, ((9., 47),), ((10., 47),), 'MOCK')
        filled, price = module.DepthExecution().fill(book, at=now, side='BUY', quantity=1000, limit_price=10)
        self.assertEqual(filled, 47)
        self.assertFalse(audit.legal_quantity(filled))
        self.assertEqual(price, 10)

    def test_input_and_old_report_hashes_preserved(self):
        for row in self.evidence['inputs'] + self.evidence['conversion_trace']:
            audit.verify_hash(Path(row['path']), row['sha256'])
        for row in self.evidence['affected_old_artifacts']:
            audit.verify_hash(audit.STUDY / row['path'], row['sha256'])

    def test_hash_mismatch_fails(self):
        with self.assertRaisesRegex(ValueError, 'SOURCE_HASH_MISMATCH'):
            audit.verify_hash(HERE / 'audit.py', '0' * 64)

    def test_sdk_binary_and_dependency_versions_are_separate(self):
        sdk = audit.read(HERE / 'sdk_evidence.json')
        api = next(r for r in sdk['files'] if r['path'].endswith('/YuantaSparkAPI.dll'))
        self.assertEqual(api['fixed_versions']['file_version'], '2.2026.713.0')
        self.assertIn('YuantaSparkAPI/2026.7.13.11604', sdk['declared_packages'])
        self.assertEqual(inspect_sdk.fixed_versions(Path(api['path'])), api['fixed_versions'])

    def test_doc_buy_sell_int_does_not_become_unit_proof(self):
        sdk = audit.read(HERE / 'sdk_evidence.json')
        text = next(r['text'] for r in sdk['doc_excerpts'] if r['path'].endswith('SubscribeFiveTickA_IO_Spec.doc'))
        self.assertIn('BuyVol1', text)
        self.assertIn('第一買量', text)
        self.assertEqual(self.evidence['unit_status'], 'UNIT_UNVERIFIED')

    def test_sdk_sources_unchanged(self):
        sdk = audit.read(HERE / 'sdk_evidence.json')
        for row in sdk['files'] + sdk['doc_excerpts']:
            audit.verify_hash(Path(row['path']), row['sha256'])


if __name__ == '__main__':
    unittest.main()
