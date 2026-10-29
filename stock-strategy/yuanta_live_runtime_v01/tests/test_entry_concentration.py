"""Regression contracts for the fixed LONG-entry buy concentration condition."""
from datetime import datetime, timedelta
import hashlib
import json
import math
import unittest
from unittest.mock import patch

from yuanta_live_runtime_v01 import strategy
from yuanta_live_runtime_v01.tests import test_strategy as existing_fixtures


class EntryBuyConcentrationTests(unittest.TestCase):
    def setUp(self):
        self.decision = datetime(2026, 10, 29, 9, 35, tzinfo=strategy.TAIPEI)

    def _engine(self, *symbols, market=False):
        symbols = symbols or ('A',)
        names = {symbol: symbol for symbol in symbols}
        if market:
            names['0050'] = 'benchmark'
        return strategy.LiveDirectionEngine(
            names, capital_twd=190000, candidate_symbols=set(symbols),
            benchmark_symbol='0050' if market else None,
        )

    def _signal(self, symbol='A', at=None, *, score=.8, side='LONG'):
        long = side == 'LONG'
        return {
            'stock_id': symbol, 'stock_name': symbol, 'side': side,
            'decision_time': at or self.decision, 'score': score,
            'volume_delta': .7 if long else -.7,
            'large_trade_delta': .7 if long else -.7,
            'vwap_gap': .003 if long else -.003,
            'book_imbalance': .3 if long else -.3,
            'spread_bps': 10., 'entry_price': 100., 'quantity': 1000,
            'return_5m': .01 if long else -.01,
            'directional_opening_extension': .01,
            'directional_vwap_extension': .003,
            'breakout_boundary_price': 99.5 if long else 100.5,
        }

    def _tick(self, engine, symbol, *, at=None, received_at=None, volume=10,
              flag='1', price=100., serial=1):
        return engine.ingest_tick(
            symbol, at=at or self.decision, received_at=received_at or at or self.decision,
            price=price, volume=volume, bid=99.9, ask=100.1,
            flag=flag, serial=serial,
        )

    def _buys(self, engine, symbol='A', volumes=(10, 10, 10, 10), *,
              at=None, serial_start=1):
        at = at or self.decision
        for index, volume in enumerate(volumes):
            result = self._tick(engine, symbol, at=at-timedelta(seconds=len(volumes)-index),
                                volume=volume, serial=serial_start+index)
            self.assertTrue(result.accepted, result.reason)

    def _candidate(self, engine, symbol='A'):
        return next(row for row in engine.last_entry_diagnostics['candidates']
                    if row['stock_id'] == symbol)

    def _reject_entry(self):
        engine = self._engine()
        self._buys(engine, volumes=(70, 10, 10, 10))
        with patch.object(engine, '_signal_for', side_effect=lambda symbol, at: self._signal(symbol, at)):
            self.assertIsNone(engine.choose_entry(self.decision, allow_short=False))
        self.assertEqual(self._candidate(engine)['gate_reason'], 'BUY_CONCENTRATION_EXCEEDS_LIMIT')
        return engine

    def test_policy_is_the_fixed_long_only_30_second_25_percent_condition(self):
        self.assertEqual(strategy.ENTRY_BUY_CONCENTRATION_POLICY, {
            'policy_id': 'LONG_ACTIVE_BUY_CONCENTRATION_30S_CAP25_V1',
            'window_seconds': 30,
            'maximum_single_buy_share': .25,
            'applies_to': 'NEW_LONG_ENTRIES_ONLY',
        })

    def test_exact_25_percent_passes_and_logs_metrics(self):
        engine = self._engine()
        self._buys(engine)
        with patch.object(engine, '_signal_for', side_effect=lambda symbol, at: self._signal(symbol, at)):
            selected = engine.choose_entry(self.decision, allow_short=False)
        self.assertIsNotNone(selected)
        candidate = self._candidate(engine)
        self.assertEqual(candidate['gate_reason'], 'BASE_SIGNAL_CONFIRMED')
        self.assertEqual(candidate['entry_buy_concentration']['concentration'], .25)
        self.assertEqual(candidate['entry_buy_concentration']['buy_volume'], 40)
        self.assertEqual(engine.last_entry_diagnostics['entry_buy_concentration_policy'],
                         strategy.ENTRY_BUY_CONCENTRATION_POLICY)

    def test_existing_strategy_identity_hash_includes_the_new_condition(self):
        current = dict(strategy.LONG_MARKET_REGIME_POLICY)
        self.assertEqual(current['long_entry_buy_concentration_policy'],
                         strategy.ENTRY_BUY_CONCENTRATION_POLICY)
        previous = dict(current)
        previous.pop('long_entry_buy_concentration_policy')
        def identity(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        self.assertNotEqual(identity(current), identity(previous))

    def test_greater_than_25_percent_rejects(self):
        engine = self._engine()
        self._buys(engine, volumes=(11, 10, 10, 9))
        with patch.object(engine, '_signal_for', side_effect=lambda symbol, at: self._signal(symbol, at)):
            self.assertIsNone(engine.choose_entry(self.decision, allow_short=False))
        self.assertEqual(self._candidate(engine)['gate_reason'], 'BUY_CONCENTRATION_EXCEEDS_LIMIT')
        self.assertAlmostEqual(self._candidate(engine)['entry_buy_concentration']['concentration'], .275)

    def test_window_both_boundaries_inclusive_but_older_tick_excluded(self):
        engine = self._engine()
        for serial, (seconds, volume) in enumerate(((-30.000001, 1000), (-30, 10),
                                                   (-20, 10), (-10, 10), (0, 10)), 1):
            outcome = self._tick(engine, 'A', at=self.decision+timedelta(seconds=seconds),
                                 volume=volume, serial=serial)
            self.assertTrue(outcome.accepted, outcome.reason)
        result = engine._entry_buy_concentration('A', self.decision)
        self.assertEqual(result['window_tick_count'], 4)
        self.assertEqual(result['buy_volume'], 40)
        self.assertEqual(result['concentration'], .25)

    def test_future_exchange_tick_does_not_enter_window_even_if_accepted(self):
        engine = self._engine()
        self._buys(engine)
        outcome = self._tick(engine, 'A', at=self.decision+timedelta(seconds=.25),
                             received_at=self.decision, volume=1000, serial=10)
        self.assertTrue(outcome.accepted, outcome.reason)
        result = engine._entry_buy_concentration('A', self.decision)
        self.assertEqual(result['buy_volume'], 40)
        self.assertEqual(result['concentration'], .25)

    def test_future_receipt_does_not_enter_window(self):
        engine = self._engine()
        self._buys(engine)
        outcome = self._tick(engine, 'A', at=self.decision,
                             received_at=self.decision+timedelta(seconds=1), volume=1000, serial=10)
        self.assertTrue(outcome.accepted, outcome.reason)
        result = engine._entry_buy_concentration('A', self.decision)
        self.assertEqual(result['buy_volume'], 40)
        self.assertEqual(result['window_tick_count'], 4)

    def test_sell_and_zero_volume_are_not_buy_denominator(self):
        engine = self._engine()
        self._buys(engine)
        self.assertTrue(self._tick(engine, 'A', volume=10000, flag='0', serial=10).accepted)
        self.assertTrue(self._tick(engine, 'A', volume=0, flag='1', serial=11).accepted)
        result = engine._entry_buy_concentration('A', self.decision)
        self.assertEqual(result['buy_volume'], 40)
        self.assertEqual(result['buy_tick_count'], 4)
        self.assertEqual(result['window_tick_count'], 6)
        self.assertEqual(result['concentration'], .25)

    def test_no_active_buys_is_unavailable_and_rejects_new_long(self):
        engine = self._engine()
        self._tick(engine, 'A', volume=100, flag='0')
        self._tick(engine, 'A', volume=0, flag='1', serial=2)
        self.assertIsNone(engine._entry_buy_concentration('A', self.decision)['concentration'])
        with patch.object(engine, '_signal_for', side_effect=lambda symbol, at: self._signal(symbol, at)):
            self.assertIsNone(engine.choose_entry(self.decision, allow_short=False))
        self.assertEqual(self._candidate(engine)['gate_reason'], 'BUY_CONCENTRATION_UNAVAILABLE')

    def test_unknown_flag_keeps_original_midpoint_classification(self):
        engine = self._engine()
        self._buys(engine)
        self._tick(engine, 'A', flag='', volume=10, price=100.1, serial=10)
        self._tick(engine, 'A', flag='unknown', volume=10000, price=99.9, serial=11)
        result = engine._entry_buy_concentration('A', self.decision)
        self.assertEqual(result['buy_volume'], 50)
        self.assertEqual(result['buy_tick_count'], 5)
        self.assertEqual(result['inferred_flag_count'], 2)
        self.assertEqual(result['concentration'], .2)

    def test_duplicate_callback_is_not_counted_twice(self):
        engine = self._engine()
        self._buys(engine)
        first = engine._entry_buy_concentration('A', self.decision)
        duplicate = self._tick(engine, 'A', at=self.decision-timedelta(seconds=1), serial=4)
        self.assertFalse(duplicate.accepted)
        self.assertEqual(engine._entry_buy_concentration('A', self.decision), first)

    def test_nonfinite_volume_is_rejected_before_concentration(self):
        engine = self._engine()
        self._buys(engine)
        for serial, volume in enumerate((math.inf, math.nan), 10):
            self.assertFalse(self._tick(engine, 'A', volume=volume, serial=serial).accepted)
        self.assertEqual(engine._entry_buy_concentration('A', self.decision)['buy_volume'], 40)

    def test_overflowing_buy_total_rejects_with_json_safe_diagnostics(self):
        engine = self._engine()
        self._buys(engine, volumes=(1e308, 1e308, 1e308, 1e308))
        metrics = engine._entry_buy_concentration('A', self.decision)
        self.assertIsNone(metrics['concentration'])
        self.assertIsNone(metrics['buy_volume'])
        json.dumps(metrics, allow_nan=False)
        with patch.object(engine, '_signal_for', side_effect=lambda symbol, at: self._signal(symbol, at)):
            self.assertIsNone(engine.choose_entry(self.decision, allow_short=False))
        self.assertEqual(self._candidate(engine)['gate_reason'], 'BUY_CONCENTRATION_UNAVAILABLE')

    def test_filters_all_confirmed_candidates_before_original_ranking(self):
        engine = self._engine('A', 'B')
        self._buys(engine, 'A', volumes=(70, 10, 10, 10))
        self._buys(engine, 'B')
        def signals(symbol, at):
            return self._signal(symbol, at, score=.99 if symbol == 'A' else .8)
        with patch.object(engine, '_signal_for', side_effect=signals):
            selected = engine.choose_entry(self.decision, allow_short=False)
        self.assertEqual(selected.stock_id, 'B')
        self.assertEqual(self._candidate(engine, 'A')['gate_reason'], 'BUY_CONCENTRATION_EXCEEDS_LIMIT')
        self.assertEqual(self._candidate(engine, 'B')['gate_reason'], 'BASE_SIGNAL_CONFIRMED')

    def test_rejection_preserves_confirmation_and_next_pass_enters_immediately(self):
        engine = self._engine(market=True)
        market = {'market_regime': 'BEARISH', 'benchmark_vwap_gap': -.001,
                  'benchmark_return_5m': -.005}
        self._buys(engine, volumes=(70, 10, 10, 10))
        with patch.object(engine, '_market_context', return_value=market), \
                patch.object(engine, '_signal_for', side_effect=lambda symbol, at: self._signal(symbol, at)):
            self.assertIsNone(engine.choose_entry(self.decision, allow_short=False))
            self.assertEqual(self._candidate(engine)['gate_reason'], 'CONFIRMATIONS_INCOMPLETE')
            second = self.decision+timedelta(seconds=30)
            self._buys(engine, volumes=(70, 10, 10, 10), at=second, serial_start=100)
            self.assertIsNone(engine.choose_entry(second, allow_short=False))
            self.assertEqual(self._candidate(engine)['gate_reason'], 'BUY_CONCENTRATION_EXCEEDS_LIMIT')
            self.assertEqual(engine._previous['A'][1], 2)
            third = second+timedelta(seconds=30)
            self._buys(engine, at=third, serial_start=200)
            selected = engine.choose_entry(third, allow_short=False)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.required_confirmations, 2)
        self.assertEqual(self._candidate(engine)['streak'], 3)
        self.assertEqual(engine._previous['A'][1], 3)

    def test_short_entry_does_not_consult_buy_concentration(self):
        engine = self._engine()
        with patch.object(engine, '_signal_for', side_effect=lambda symbol, at: self._signal(symbol, at, side='SHORT')), \
                patch.object(engine, '_entry_buy_concentration', side_effect=AssertionError('entry condition is LONG only')) as concentration:
            selected = engine.choose_entry(self.decision, allow_short=True)
        self.assertEqual(selected.side, 'SHORT')
        concentration.assert_not_called()

    def test_real_ingest_base_breakout_passes_with_distributed_buys(self):
        engine, at = existing_fixtures.StrategyTests()._engine_with_long_signal()
        selected = engine.choose_entry(at, allow_short=False)
        self.assertIsNotNone(selected)
        metrics = self._candidate(engine, '2330')['entry_buy_concentration']
        self.assertLessEqual(metrics['concentration'], .25)
        self.assertGreaterEqual(metrics['buy_tick_count'], 4)

    def test_real_ingest_base_breakout_rejected_for_one_dominant_buy(self):
        engine, at = existing_fixtures.StrategyTests()._engine_with_long_signal()
        engine.record_tick('2330', at=at, price=101.12, volume=2000,
                           bid=101.0, ask=101.5, flag='1', serial=1000)
        self.assertIsNotNone(engine._signal_for('2330', at))
        self.assertIsNone(engine.choose_entry(at, allow_short=False))
        self.assertEqual(self._candidate(engine, '2330')['gate_reason'], 'BUY_CONCENTRATION_EXCEEDS_LIMIT')

    def test_entry_rejection_does_not_disable_original_stop(self):
        engine = self._reject_entry()
        at = self.decision+timedelta(minutes=5)
        engine.record_tick('A', at=at, price=96.5, volume=10, bid=96.5, ask=96.6, flag='0', serial=100)
        position = strategy.ManagedPosition('A', 'A', 'LONG', 1000, 100., 'fixture', self.decision)
        self.assertEqual(engine.evaluate_exit(position, at).reason, 'STOP_LOSS')
        self.assertEqual(strategy.LIVE_EXIT_POLICY['stop_loss_net_twd'], 3500.)

    def test_entry_rejection_does_not_disable_original_mfe_exit(self):
        engine = self._reject_entry()
        at = self.decision+timedelta(minutes=5)
        position = strategy.ManagedPosition('A', 'A', 'LONG', 1000, 100., 'fixture', self.decision)
        engine.record_tick('A', at=at, price=110., volume=10, bid=110., ask=110.5, flag='1', serial=100)
        self.assertIsNone(engine.evaluate_exit(position, at))
        self.assertTrue(position.mfe_protection_armed)
        later = at+timedelta(seconds=1)
        engine.record_tick('A', at=later, price=107., volume=10, bid=107., ask=107.5, flag='0', serial=101)
        self.assertEqual(engine.evaluate_exit(position, later).reason, 'MFE_PROFIT_PROTECTION')

    def test_entry_rejection_does_not_disable_original_hard_exit(self):
        engine = self._reject_entry()
        at = self.decision.replace(hour=13, minute=20, second=1)
        engine.record_tick('A', at=at, price=101., volume=10, bid=101., ask=101.5, flag='1', serial=100)
        position = strategy.ManagedPosition('A', 'A', 'LONG', 1000, 100., 'fixture', self.decision)
        self.assertEqual(engine.evaluate_exit(position, at).reason, 'HARD_EXIT')

    def test_entry_rejection_does_not_disable_original_opposite_signal(self):
        engine = self._reject_entry()
        position = strategy.ManagedPosition('A', 'A', 'LONG', 1000, 100., 'fixture', self.decision)
        with patch.object(engine, '_signal_for', side_effect=lambda symbol, at: self._signal(symbol, at, side='SHORT')), \
                patch.object(engine, '_entry_buy_concentration', side_effect=AssertionError('entry filter must not run during exit')):
            at = self.decision+timedelta(seconds=30)
            self.assertFalse(engine.opposite_signal(position, at))
            self.assertTrue(engine.opposite_signal(position, at+timedelta(seconds=30)))


if __name__ == '__main__':
    unittest.main()
