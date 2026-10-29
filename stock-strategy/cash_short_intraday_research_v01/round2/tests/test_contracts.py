"""Independent synthetic contracts joining the Round 2 config and pure APIs.

No archived price/result data are read and no performance is calculated here.
Two narrow edge contracts explicitly cover diagnostic freshness and net=0 costs.
"""
from copy import deepcopy
from datetime import timedelta
import ast
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from cash_short_intraday_research_v01 import replay
from cash_short_intraday_research_v01.round2 import diagnostics as d
from cash_short_intraday_research_v01.round2 import features as f
from cash_short_intraday_research_v01.round2.tests.test_features import (
    CONFIG, OPEN, dataset, raw_signal, tick,
)


ROUND2 = Path(__file__).resolve().parents[1]


class FrozenConfigContracts(unittest.TestCase):
    def test_config_is_exact_six_fixed_variants_with_e0(self):
        config = json.loads((ROUND2 / 'config.json').read_text())
        self.assertEqual(tuple(config['entry_variants']), f.VARIANTS)
        self.assertEqual(config['entry_exit_model'], 'E0')
        self.assertEqual(config['rs_300_max'], f.RS300_MAXIMUM)
        self.assertEqual(config['vwap_slope_seconds'], f.VWAP_HISTORY_SECONDS)
        self.assertEqual(config['maximum_entry_distance_R4'], f.ENTRY_DISTANCE_MAXIMUM)
        self.assertEqual(config['capital_twd'], 190000)
        self.assertEqual(config['maximum_trades_per_day'], 1)
        self.assertEqual(config['maximum_positions'], 1)
        self.assertEqual(config['risk_per_trade_twd'], 3500)

    def test_config_stays_shadow_zero_live_and_no_activation(self):
        config = json.loads((ROUND2 / 'config.json').read_text())
        self.assertEqual(config['mode'], 'SHADOW_ONLY')
        self.assertIs(config['NO_LIVE_ORDER'], True)
        self.assertEqual(config['actual_orders'], 0)
        self.assertIs(config['automatic_selection_or_activation'], False)
        self.assertEqual(config['selection_minimum_closed'], 10)
        self.assertEqual(config['selection_minimum_distinct_days'], 5)
        self.assertIs(config['selection_requires_positive_net'], True)

    def test_new_pure_modules_have_no_network_sdk_or_operational_imports(self):
        forbidden = ('socket', 'subprocess', 'requests', 'urllib', 'httpx',
                     'yuanta_spark', 'yuanta_live_runtime_v01.main', 'collector_main',
                     'auto_runner', 'risk_manager', 'broker', 'order_adapter')
        for name in ('features.py', 'diagnostics.py'):
            tree = ast.parse((ROUND2 / name).read_text())
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(node.module or '')
            for module in imports:
                self.assertFalse(any(part in module.lower() for part in forbidden), (name, module))


class CausalAvailabilityContracts(unittest.TestCase):
    def test_late_received_exchange_print_cannot_rewrite_historical_vwap(self):
        data = dataset()
        # Exchange time equals the historical cursor but this update is only
        # received one second later. It may affect t, never the t-180 snapshot.
        data['events'].append(tick(180, 60.0,
                                   received_at=OPEN + timedelta(seconds=181),
                                   serial=182, volume=1000000,
                                   bid=59.9, ask=60.1))
        data['events'].sort(key=lambda e: (e['received_at'], e['sequence'], e['symbol']))
        signal = raw_signal(data)
        out = f.build_feature_context(data, [signal], CONFIG, decision_times=[])
        feature = out['signals'][0]['round2_features']
        self.assertEqual(feature['vwap_180s_ago'], 40.0)
        self.assertEqual(feature['vwap_180s_ago_snapshot_at'],
                         (OPEN + timedelta(seconds=180)).isoformat())
        self.assertGreater(feature['vwap_now'], 59)

    def test_common_mask_is_availability_not_success_or_profit(self):
        data = dataset()
        signal = raw_signal(data, reference=40.0)
        out = f.build_feature_context(data, [signal], CONFIG, decision_times=[])
        enriched = out['signals'][0]
        self.assertTrue(out['common_mask'][0]['common_evaluable'])
        self.assertFalse(f.evaluate_variant(enriched, 'R4')['pass'])
        self.assertFalse(any('pnl' in key.lower() for key in out['common_mask'][0]))

    def test_unrelated_missing_candidate_does_not_remove_valid_common_signal(self):
        data = dataset()
        data['metadata']['9999'] = {'name': 'never observed synthetic candidate'}
        data['candidates'].add('9999')
        out = f.build_feature_context(data, [raw_signal(data)], CONFIG, decision_times=[])
        self.assertTrue(out['common_mask'][0]['common_evaluable'])

    def test_r1_matches_archived_weak_gate_at_both_known_outcomes(self):
        for last in (49.95, 50.05):
            with self.subTest(last=last):
                data = dataset()
                for event in data['events']:
                    if event['kind'] == 'tick' and event['symbol'] == '0050' and event['received_at'] >= OPEN + timedelta(seconds=300):
                        event.update(price=last, bid=last - .05, ask=last + .05)
                original = raw_signal(data)
                enriched = f.build_signal_features(data, [original], CONFIG)[0]
                self.assertIs(f.evaluate_variant(enriched, 'R1')['pass'],
                              original['features']['market_filter_pass'])

    def test_unknown_benchmark_is_not_no_long_and_not_cash_performance(self):
        data = dataset(benchmark=False)
        target = OPEN + timedelta(seconds=360)
        out = f.build_feature_context(data, [raw_signal(data)], CONFIG, decision_times=[target])
        market = next(row for row in out['market_decision_mask'] if row['time'] == target.isoformat())
        self.assertIs(market['long_market_evaluable'], False)
        self.assertIsNone(market['market_filter_pass'])
        self.assertNotIn('long_signal_absent', market)
        self.assertNotIn('net_pnl', market)
        self.assertIsNone(f.evaluate_variant(out['signals'][0], 'R5')['pass'])

    def test_full_grid_is_original_491_entry_decisions_not_exit_grid(self):
        data = dataset()
        out = f.build_feature_context(data, [], CONFIG)
        self.assertEqual(len(out['market_decision_mask']), 491)
        self.assertEqual(out['market_decision_mask'][0]['time'],
                         OPEN.replace(minute=5).isoformat())
        self.assertEqual(out['market_decision_mask'][-1]['time'],
                         OPEN.replace(hour=13, minute=10).isoformat())


class DiagnosticEdgeContracts(unittest.TestCase):
    def test_target_known_exchange_quote_must_not_be_over_ten_seconds_old(self):
        target = OPEN + timedelta(minutes=6)
        book = {'kind': 'book', 'symbol': '2466',
                'received_at': target - timedelta(seconds=9),
                'exchange_time': target - timedelta(seconds=15),
                'bids': [(39.8, 100)], 'asks': [(39.85, 100)]}
        index = replay.index_day({'events': [book]})
        quote, status = d._usable_book(index, '2466', target, 10)
        self.assertIsNone(quote, 'known quote is 15 seconds old at the target cursor')
        self.assertIn('STALE', status)

    def test_cost_positive_gross_to_exact_zero_net_matches_spec_leq_zero(self):
        # Isolate the reporting predicate, not an executable synthetic trade.
        trade = {'signal_id': 'cost-zero-boundary', 'quantity': 1000,
                 'entry_unfilled_quantity': 0, 'gross_pnl': 100,
                 'net_pnl': 0, 'exit_fills': [], 'path': [],
                 'marked_net_pnl': 0}
        signal = {'features': {}}
        horizon = {'fixed_original_quantity_hypothetical_quote_gross_pnl': None,
                   'penalized_quote_mark_price': None}
        with patch.object(d, '_mark_at_target', return_value=horizon), \
             patch.object(d, '_causal_vwap_features', return_value={'vwap_declining_180s': None}), \
             patch.object(d, 'fill_cost_decomposition', return_value={}), \
             patch.object(d, '_observed_interval_quality', return_value={}):
            out = d.diagnose(deepcopy(trade), deepcopy(trade), signal, {}, {}, {})
        self.assertTrue(out['flag_fees_and_tax_flip_gross_profit_to_net_loss'],
                        'SPEC/config say gross>0 and net<=0, including net=0')

    def test_e0_parity_rejects_changed_trigger_even_with_same_total_pnl(self):
        original = {'exit_fills': [], 'net_pnl': -123,
                    'exit_trigger_time': OPEN + timedelta(minutes=6)}
        replayed = {**original, 'exit_trigger_time': OPEN + timedelta(minutes=6, seconds=1)}
        with self.assertRaisesRegex(d.FixedEntryMismatch, 'exit_trigger_time'):
            d.assert_e0_parity(original, replayed)


if __name__ == '__main__':
    unittest.main()
