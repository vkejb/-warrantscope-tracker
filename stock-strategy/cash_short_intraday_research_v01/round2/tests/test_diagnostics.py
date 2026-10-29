"""Synthetic-only fixed-cohort exits and point-in-time diagnostic contracts."""
from copy import deepcopy
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from cash_short_intraday_research_v01 import replay
from cash_short_intraday_research_v01.round2 import diagnostics as d


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 2, 10, tzinfo=ZoneInfo('Asia/Taipei'))
        self.cfg = {
            'capital_twd': 190000, 'risk_per_trade_twd': 3500,
            'book_stale_seconds': 10, 'commission_rate': .000855,
            'minimum_commission': 20, 'sell_tax_rate': .0015,
            'execution_cutoff': '13:25', 'cover_start': '13:20',
            'latency_seconds': 1, 'quote_wait_seconds': 3,
            'extra_slippage_ticks': 1, 'depth_unit_multiplier': 1000,
            'strict_evidence': False,
            'exit_E2': {'minimum_hold_seconds': 300, 'net_r_below': .25,
                        'buy_flow_above': .10, 'large_buy_flow_above': 0},
        }
        self.signal = {'signal_id': 'fixed-test', 'day': '20261002', 'time': self.at,
                       'symbol': 'TEST', 'side': 'SHORT', 'entry_model': 'S1',
                       'score': 1, 'structure_high': 101, 'features': {}}
        self.data = {'day': '20261002', 'metadata': {'TEST': 'Synthetic'},
                     'candidates': ['TEST'], 'events': [
                         self.book(0), self.book(1), self.book(5, bid=101, ask=101.5),
                         self.book(6, bid=101, ask=101.5),
                         self.book(10, bid=103, ask=103.5), self.book(11, bid=103, ask=103.5)]}
        self.index = replay.index_day(self.data)
        self.original, _ = replay.simulate_trade(self.signal, self.data, self.index, 190000, self.cfg)
        self.assertIsNotNone(self.original)

    def book(self, seconds, bid=100, ask=100.5, depth=100, **extra):
        at = self.at + timedelta(seconds=seconds)
        return {'kind': 'book', 'symbol': 'TEST', 'received_at': at,
                'exchange_time': at, 'bids': [(bid, depth)], 'asks': [(ask, depth)],
                'sequence': seconds, **extra}

    def test_e0_full_fill_cost_timing_parity(self):
        actual, _ = d.fixed_trade(self.original, self.signal, self.data, self.index, self.cfg)
        self.assertTrue(d.assert_e0_parity(self.original, actual))
        self.assertEqual(actual['entry_fills'], self.original['entry_fills'])
        self.assertEqual(actual['exit_fills'], self.original['exit_fills'])
        self.assertEqual(actual['remaining_quantity'], 0)

    def test_e1_same_entry_and_quantity_different_delayed_cover(self):
        actual, _ = d.fixed_trade(self.original, self.signal, self.data, self.index, self.cfg, 'E1')
        self.assertEqual(actual['entry_fills'], self.original['entry_fills'])
        self.assertEqual(actual['quantity'], self.original['quantity'])
        self.assertEqual(actual['exit_reason'], 'STRUCTURE_INVALIDATION')
        self.assertEqual(actual['exit_trigger_time'], self.at + timedelta(seconds=5))
        self.assertEqual(actual['exit_time'], self.at + timedelta(seconds=6))
        self.assertEqual(actual['exit_price'], 102)
        self.assertNotEqual(actual['exit_price'], 101.5, 'trigger quote is not a fill')

    def test_e2_waits_full_300_seconds_and_positive_buyer_flow(self):
        ticks = []
        for seconds in range(2, 302):
            at = self.at + timedelta(seconds=seconds)
            ticks.append({'kind': 'tick', 'symbol': 'TEST', 'received_at': at,
                          'exchange_time': at, 'price': 99, 'volume': 100,
                          'bid': 99, 'ask': 99.1, 'flag': '1', 'serial': seconds})
        data = {**self.data, 'events': sorted([
            self.book(0), self.book(1), *ticks,
            self.book(300, 99, 99.1), self.book(301, 99, 99.1), self.book(302, 99, 99.1)
        ], key=lambda e: e['received_at'])}
        index = replay.index_day(data)
        original, _ = replay.simulate_trade(self.signal, data, index, 190000, self.cfg)
        actual, _ = d.fixed_trade(original, self.signal, data, index, self.cfg, 'E2')
        self.assertEqual(actual['exit_reason'], 'FIVE_MINUTE_WEAK_BUY_FLOW')
        self.assertEqual(actual['exit_trigger_time'], self.at + timedelta(seconds=301))
        self.assertEqual(actual['exit_time'], self.at + timedelta(seconds=302))
        self.assertEqual(actual['entry_fills'], original['entry_fills'])

    def test_local_injection_never_mutates_module_globals(self):
        before = dict(replay.simulate_trade.__globals__)
        d.fixed_trade(self.original, self.signal, self.data, self.index, self.cfg, 'E1')
        self.assertEqual(set(before), set(replay.simulate_trade.__globals__))
        for name, value in before.items():
            self.assertIs(value, replay.simulate_trade.__globals__[name])
        self.assertIs(replay.target_quantity, before['target_quantity'])

    def test_injection_exception_leaves_original_function_and_namespace_intact(self):
        original_function, original_target = replay.simulate_trade, replay.target_quantity
        bad = deepcopy(self.original)
        bad['entry_fills'][0]['price'] += .1
        bad['entry_price'] += .1
        with self.assertRaisesRegex(d.FixedEntryMismatch, 'FIXED_ENTRY_FILL_PARITY_FAILED'):
            d.fixed_trade(bad, self.signal, self.data, self.index, self.cfg)
        self.assertIs(replay.simulate_trade, original_function)
        self.assertIs(replay.target_quantity, original_target)

    def test_missing_archived_fills_fail_closed(self):
        original = {k: v for k, v in self.original.items() if k != 'entry_fills'}
        with self.assertRaisesRegex(d.FixedEntryMismatch, 'ARCHIVED_ENTRY_FILLS_REQUIRED'):
            d.fixed_trade(original, self.signal, self.data, self.index, self.cfg)

    def test_e0_cannot_hide_changed_exit_or_cost(self):
        for field, value in [('net_pnl', 123456), ('sell_tax', 0), ('exit_price', 99)]:
            original = {**self.original, field: value}
            with self.assertRaisesRegex(d.FixedEntryMismatch, 'E0_ARCHIVE_PARITY_FAILED:' + field):
                d.fixed_trade(original, self.signal, self.data, self.index, self.cfg)

    def test_known_historical_suspension_blocks_even_fixed_cohort(self):
        with self.assertRaisesRegex(d.FixedEntryMismatch, 'KNOWN_HISTORICAL'):
            d.fixed_trade(self.original, self.signal,
                          {**self.data, 'known_short_ineligible': {'TEST': True}}, self.index, self.cfg)

    def test_odd_lot_archived_fill_rejected(self):
        original = deepcopy(self.original)
        original['entry_fills'][0]['quantity'] = 999
        original['quantity'] = 999
        with self.assertRaisesRegex(d.FixedEntryMismatch, 'QUANTITY_INVALID'):
            d.fixed_trade(original, self.signal, self.data, self.index, self.cfg)

    def test_unsupported_exit_not_silently_used(self):
        with self.assertRaisesRegex(ValueError, 'ONLY_FROZEN'):
            d.fixed_trade(self.original, self.signal, self.data, self.index, self.cfg, 'BEST_AFTER_LOOKING')

    def test_horizon_uses_only_book_at_or_before_target(self):
        # Target is entry t+1 +60 = t+61; a future t+62 quote cannot leak.
        data = {**self.data, 'events': [self.book(60, bid=99, ask=99.1),
                                      self.book(62, bid=80, ask=80.1)]}
        result = d._mark_at_target(self.original, replay.index_day(data), self.cfg, 60)
        self.assertEqual(result['observed_best_cover_ask'], 99.1)
        self.assertEqual(result['quote_age_seconds'], 1)
        self.assertTrue(result['already_closed_by_target'])
        self.assertEqual(result['actual_total_marked_pnl_by_target'], self.original['net_pnl'])
        self.assertNotEqual(result['fixed_original_quantity_hypothetical_quote_net_pnl'],
                            result['actual_realized_pnl_by_target'])

    def test_horizon_stale_quote_is_unknown_not_zero_or_last_price_fill(self):
        result = d._mark_at_target(self.original, self.index, self.cfg, 60)
        self.assertIsNone(result['fixed_original_quantity_hypothetical_quote_net_pnl'])
        self.assertEqual(result['quote_status'], 'BOOK_STALE_AT_TARGET')
        self.assertEqual(result['actual_total_marked_pnl_by_target'], self.original['net_pnl'])

    def test_known_exchange_timestamp_must_also_be_fresh_at_target(self):
        # A six-second callback delay plus nine-second receipt age is not a
        # fresh 10-second exchange observation at the target.
        target = self.at + timedelta(seconds=61)
        delayed = self.book(52, exchange_time=self.at + timedelta(seconds=46))
        index = replay.index_day({**self.data, 'events': [delayed]})
        quote, status = d._usable_book(index, 'TEST', target, 10)
        self.assertIsNone(quote)
        self.assertEqual(status, 'STALE_OR_FUTURE_EXCHANGE_TARGET_BOOK')

    def test_future_exchange_timestamp_beyond_skew_is_not_valued(self):
        target = self.at + timedelta(seconds=61)
        delayed = self.book(61, exchange_time=target + timedelta(seconds=.501))
        index = replay.index_day({**self.data, 'events': [delayed]})
        quote, status = d._usable_book(index, 'TEST', target, 10)
        self.assertIsNone(quote)
        self.assertEqual(status, 'STALE_OR_FUTURE_EXCHANGE_TARGET_BOOK')

    def test_horizon_open_position_missing_book_keeps_unknown_mark(self):
        trade = {**self.original, 'exit_fills': [], 'remaining_quantity': 1000}
        result = d._mark_at_target(trade, self.index, self.cfg, 60)
        self.assertEqual(result['actual_remaining_quantity_by_target'], 1000)
        self.assertIsNone(result['actual_unrealized_quote_pnl_by_target'])
        self.assertIsNone(result['actual_total_marked_pnl_by_target'])

    def test_positive_peak_is_not_larger_loss_called_giveback(self):
        trade = {'path': [{'time': self.at, 'net': 500},
                          {'time': self.at + timedelta(seconds=1), 'net': -2000}],
                 'marked_net_pnl': -2000, 'mfe_net_twd': 500, 'mae_net_twd': -2000}
        result = d._path_diagnostics(trade)
        self.assertEqual(result['profit_surrendered_twd'], 500)
        self.assertEqual(result['drawdown_peak_to_final_twd'], 2500)
        self.assertEqual(result['positive_peak_net_time'], self.at)

    def test_floor_zero_mfe_has_no_invented_profit_time(self):
        trade = {'path': [{'time': self.at, 'net': -10},
                          {'time': self.at + timedelta(seconds=1), 'net': -50}],
                 'marked_net_pnl': -50, 'mfe_net_twd': 0, 'mae_net_twd': -50}
        result = d._path_diagnostics(trade)
        self.assertEqual(result['observed_peak_net_twd'], -10)
        self.assertEqual(result['positive_peak_net_twd'], 0)
        self.assertIsNone(result['mfe_time'])
        self.assertEqual(result['profit_surrendered_twd'], 0)
        self.assertEqual(result['drawdown_peak_to_final_twd'], 50)

    def test_tick_penalty_decomposition_is_explicit_not_no_slippage_pnl(self):
        result = d.fill_cost_decomposition(self.original, self.signal, self.index, self.cfg)
        self.assertTrue(result['decomposition_complete'])
        self.assertAlmostEqual(result['known_entry_extra_tick_penalty_twd'], 100)
        self.assertAlmostEqual(result['known_exit_extra_tick_penalty_twd'], 500)
        self.assertIsNone(result['causal_total_slippage_effect'])
        self.assertEqual(result['entry_signal_to_fill_level_change_twd'], 0)

    def test_quote_mismatch_decomposition_is_unknown(self):
        original = deepcopy(self.original)
        original['exit_fills'][0]['book_sequence'] = 'missing'
        result = d.fill_cost_decomposition(original, self.signal, self.index, self.cfg)
        self.assertFalse(result['decomposition_complete'])
        self.assertIsNone(result['known_total_extra_tick_penalty_twd'])

    def test_vwap_comparison_uses_each_snapshot_receipt_cutoff(self):
        opening = self.at.replace(hour=9)
        rows = []
        for seconds, price, received_extra in [(0, 100, 0), (180, 102, 0), (360, 98, 0),
                                               (120, 200, 241)]:
            at = opening + timedelta(seconds=seconds)
            rows.append({'kind': 'tick', 'symbol': 'TEST', 'received_at': at + timedelta(seconds=received_extra),
                         'exchange_time': at, 'price': price, 'volume': 100,
                         'bid': price - .5, 'ask': price + .5, 'flag': '1', 'serial': seconds + 1})
        data = {**self.data, 'events': sorted(rows, key=lambda e: e['received_at'])}
        signal = {**self.signal, 'time': opening + timedelta(seconds=360)}
        result = d._causal_vwap_features(signal, data, self.cfg)
        self.assertEqual(result['vwap_at_signal_minus_180s'], 101)
        self.assertEqual(result['vwap_at_signal_reconstructed'], 100)
        self.assertTrue(result['vwap_declining_180s'])
        self.assertIsNone(result['reconstructed_market_filter_pass'])

    def test_vwap_stale_history_keeps_direction_unknown_even_with_nonnull_values(self):
        opening = self.at.replace(hour=9)
        rows = []
        for seconds, price in [(0, 100), (60, 102), (360, 98)]:
            at = opening + timedelta(seconds=seconds)
            rows.append({'kind': 'tick', 'symbol': 'TEST', 'received_at': at,
                         'exchange_time': at, 'price': price, 'volume': 100,
                         'bid': price - .5, 'ask': price + .5, 'flag': '1', 'serial': seconds + 1})
        signal = {**self.signal, 'time': opening + timedelta(seconds=360)}
        result = d._causal_vwap_features(signal, {**self.data, 'events': rows}, self.cfg)
        self.assertFalse(result['vwap_slope_available'])
        self.assertIsNone(result['vwap_declining_180s'])
        self.assertIsNone(result['vwap_at_signal_minus_180s'])
        self.assertIn('HISTORICAL_TICK_STALE', result['vwap_slope_unavailable_reasons'])

    def test_vwap_late_first_tick_is_not_verified_historical_direction(self):
        opening = self.at.replace(hour=9)
        rows = []
        for seconds, price in [(60, 100), (180, 102), (360, 98)]:
            at = opening + timedelta(seconds=seconds)
            rows.append({'kind': 'tick', 'symbol': 'TEST', 'received_at': at,
                         'exchange_time': at, 'price': price, 'volume': 100,
                         'bid': price - .5, 'ask': price + .5, 'flag': '1', 'serial': seconds + 1})
        signal = {**self.signal, 'time': opening + timedelta(seconds=360)}
        result = d._causal_vwap_features(signal, {**self.data, 'events': rows}, self.cfg)
        self.assertFalse(result['vwap_slope_available'])
        self.assertIsNone(result['vwap_declining_180s'])
        self.assertIn('HISTORICAL_OPENING_FIRST_TICK_MISSING_OR_LATE',
                      result['vwap_slope_unavailable_reasons'])

    def test_vwap_uses_enriched_features_without_rescanning_archive(self):
        signal = {**self.signal, 'round2_features': {
            'vwap_now': 100, 'vwap_180s_ago': 101, 'vwap_slope_available': True,
            'benchmark_return_300s': -.001, 'rs_300': -.002, 'market_weak': True}}
        result = d._causal_vwap_features(signal, {}, self.cfg)
        self.assertTrue(result['vwap_declining_180s'])
        self.assertEqual(result['vwap_delta_180s'], -1)
        self.assertTrue(result['reconstructed_market_filter_pass'])

    def test_vwap_explicit_enriched_unavailability_never_falls_back_or_infers(self):
        signal = {**self.signal, 'round2_features': {
            'vwap_now': 100, 'vwap_180s_ago': 101, 'vwap_slope_available': False,
            'vwap_slope_unavailable_reasons': ['HISTORICAL_TICK_STALE']}}
        result = d._causal_vwap_features(signal, {}, self.cfg)
        self.assertFalse(result['vwap_slope_available'])
        self.assertIsNone(result['vwap_declining_180s'])
        self.assertIsNone(result['vwap_delta_180s'])
        self.assertIn('HISTORICAL_TICK_STALE', result['vwap_slope_unavailable_reasons'])

    def test_fees_tax_erosion_includes_positive_gross_break_even_net(self):
        # Diagnostic classification boundary only, not a simulated execution.
        for gross, net, expected in [(100, 0, True), (100, -1, True), (100, 1, False),
                                     (0, -1, False), (None, None, None)]:
            original = {**self.original, 'gross_pnl': gross, 'net_pnl': net}
            signal = {**self.signal, 'round2_features': {}}
            result = d.diagnose(original, deepcopy(original), signal, self.data, self.index, self.cfg)
            self.assertIs(result['flag_fees_and_tax_flip_gross_profit_to_net_loss'], expected)

    def test_partial_cover_is_relative_to_remaining_not_initial_quantity(self):
        trade = {'quantity': 3000, 'exit_fills': [
            {'quantity': 1000, 'time': self.at, 'order_id': 1},
            {'quantity': 2000, 'time': self.at + timedelta(seconds=1), 'order_id': 2}]}
        self.assertEqual(d._partial_cover_orders(trade), 1)

    def test_observed_gap_is_retained_without_claiming_outage(self):
        result = d._observed_interval_quality(self.original, self.signal, self.index)
        self.assertGreater(result['max_book_observed_receipt_gap_seconds'], 60)
        self.assertIn('NOT_PROVEN_OUTAGE', result['interval_gap_basis'])
        self.assertEqual(result['tick_events_in_diagnostic_interval'], 0)

    def test_partial_entry_is_kept_and_later_cover_cannot_overfill(self):
        # 47k principal permits 4 lots, but entry snapshot has only 1 lot.
        signal = {**self.signal, 'structure_high': 10.2}
        rows = [self.book(0, 10, 10.05), self.book(1, 10, 10.05, depth=10),
                self.book(10, 11, 11.05), self.book(11, 11, 11.05)]
        data = {**self.data, 'events': rows}
        index = replay.index_day(data)
        original, _ = replay.simulate_trade(signal, data, index, 47000, self.cfg)
        self.assertGreater(original['entry_unfilled_quantity'], 0)
        actual, _ = d.fixed_trade(original, signal, data, index, self.cfg)
        self.assertEqual(actual['quantity'], 1000)
        self.assertEqual(actual['closed_quantity'] + actual['remaining_quantity'], 1000)
        self.assertEqual(actual['entry_fills'], original['entry_fills'])

    def test_unfilled_cover_retains_exposure(self):
        data = {**self.data, 'events': [self.book(0), self.book(1),
                                      self.book(10, bid=103, ask=103.5, depth=0)]}
        index = replay.index_day(data)
        original, _ = replay.simulate_trade(self.signal, data, index, 190000, self.cfg)
        actual, issues = d.fixed_trade(original, self.signal, data, index, self.cfg)
        self.assertEqual(actual['remaining_quantity'], 1000)
        self.assertIsNone(actual['net_pnl'])
        self.assertIn('COVER_INCOMPLETE_EXPOSURE_RETAINED', [x['code'] for x in issues])

    def test_no_network_or_subprocess_during_synthetic_replay(self):
        with patch('socket.create_connection', side_effect=AssertionError('NO_NETWORK')), \
             patch('subprocess.run', side_effect=AssertionError('NO_SUBPROCESS')), \
             patch('subprocess.Popen', side_effect=AssertionError('NO_SUBPROCESS')):
            d.fixed_trade(self.original, self.signal, self.data, self.index, self.cfg, 'E2')


if __name__ == '__main__':
    unittest.main()
