from datetime import datetime, timedelta
import unittest

from yuanta_live_runtime_v01.strategy import (
    ANTI_CHASE_ENTRY_POLICY,
    LIVE_EXIT_POLICY,
    LONG_MARKET_REGIME_POLICY,
    LiveDirectionEngine,
    ManagedPosition,
    SPEC,
    TAIPEI,
    _candidate_locked_r,
)


class StrategyTests(unittest.TestCase):
    def _engine_with_market_long_signal(self, *, bearish=False, weak_stock=False):
        decision = datetime(2026, 9, 24, 9, 35, 0, tzinfo=TAIPEI)
        engine = LiveDirectionEngine(
            {"2330": "台積電", "0050": "元大台灣50"},
            capital_twd=190000,
            candidate_symbols={"2330"},
            benchmark_symbol="0050",
        )
        for index in range(53):
            at = decision - timedelta(seconds=320 - index * 5)
            stock_gain = 0.001 if weak_stock else 0.01
            stock_price = 99.5 + index * stock_gain
            benchmark_price = (
                102.0 - index * 0.025
                if bearish else 100.0 + index * 0.005
            )
            engine.record_tick(
                "2330", at=at, price=stock_price, volume=10,
                bid=stock_price - 0.1, ask=stock_price + 0.1,
                flag="1", serial=index + 1,
            )
            engine.record_tick(
                "0050", at=at, price=benchmark_price, volume=100,
                bid=benchmark_price - 0.05, ask=benchmark_price + 0.05,
                flag="0" if bearish else "1", serial=index + 1,
            )
        for index in range(12):
            at = decision - timedelta(seconds=55 - index * 5)
            stock_price = (99.2 if weak_stock else 101.0) + index * 0.01
            benchmark_price = (
                100.5 - index * 0.01
                if bearish else 100.3 + index * 0.005
            )
            engine.record_tick(
                "2330", at=at, price=stock_price, volume=100,
                bid=stock_price - 0.1, ask=stock_price + 0.1,
                flag="1", serial=100 + index,
            )
            engine.record_tick(
                "0050", at=at, price=benchmark_price, volume=100,
                bid=benchmark_price - 0.05, ask=benchmark_price + 0.05,
                flag="0" if bearish else "1", serial=100 + index,
            )
        engine.record_book_combined(
            "2330", at=decision,
            buy_prices=[101.0], buy_volumes=[900],
            sell_prices=[101.1], sell_volumes=[100],
        )
        engine._states["2330"].session_open_time = decision.replace(
            hour=9, minute=0, second=0
        )
        return engine, decision

    def _engine_with_long_signal(self, decision=None):
        engine = LiveDirectionEngine({"2330": "台積電"}, capital_twd=190000)
        if decision is None:
            decision = datetime(2026, 9, 24, 9, 35, 0, tzinfo=TAIPEI)
        start = decision - timedelta(seconds=250)
        for index in range(40):
            at = start + timedelta(seconds=index * 5)
            price = 99.5 + index * 0.005
            engine.record_tick("2330", at=at, price=price, volume=10, bid=price - 0.1, ask=price + 0.1, flag="1", serial=index)
        for index in range(12):
            at = decision - timedelta(seconds=55 - index * 5)
            price = 101.0 + index * 0.01
            engine.record_tick("2330", at=at, price=price, volume=100, bid=price - 0.1, ask=price + 0.1, flag="1", serial=100 + index)
        engine.record_book_combined(
            "2330", at=decision,
            buy_prices=[101.0], buy_volumes=[900], sell_prices=[101.1], sell_volumes=[100],
        )
        engine._states["2330"].session_open_time = decision.replace(
            hour=9, minute=0, second=0
        )
        return engine, decision

    def test_long_candidate_is_sized_within_cap(self):
        engine, decision = self._engine_with_long_signal()
        signal = engine.choose_entry(decision, allow_short=False)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "LONG")
        self.assertGreater(signal.quantity, 0)
        self.assertLessEqual(signal.entry_price * signal.quantity, 190000)
        self.assertEqual(signal.quantity % 1000, 0)

    def test_anti_chase_rejects_opening_extension_above_two_percent(self):
        engine, decision = self._engine_with_long_signal()
        engine._states["2330"].session_open_price = 101.11 / 1.0201
        self.assertIsNone(engine.choose_entry(decision, allow_short=False))
        self.assertEqual(
            engine.last_entry_diagnostics["candidates"][0]["gate_reason"],
            "ANTI_CHASE_OPENING_EXTENSION",
        )

    def test_anti_chase_accepts_exact_boundaries(self):
        engine, decision = self._engine_with_long_signal()
        state = engine._states["2330"]
        current = 101.11
        state.session_open_price = current / (
            1 + ANTI_CHASE_ENTRY_POLICY["maximum_directional_opening_extension"]
        )
        state.cumulative_pv = state.cumulative_volume * current / (
            1 + ANTI_CHASE_ENTRY_POLICY["maximum_directional_vwap_extension"]
        )
        signal = engine.choose_entry(decision, allow_short=False)
        self.assertIsNotNone(signal)
        self.assertAlmostEqual(signal.directional_opening_extension, 0.020, places=9)
        self.assertAlmostEqual(signal.directional_vwap_extension, 0.0125, places=9)

    def test_anti_chase_rejects_vwap_extension_above_one_point_two_five_percent(self):
        engine, decision = self._engine_with_long_signal()
        state = engine._states["2330"]
        current = 101.11
        state.cumulative_pv = state.cumulative_volume * current / 1.0126
        self.assertIsNone(engine.choose_entry(decision, allow_short=False))
        self.assertEqual(
            engine.last_entry_diagnostics["candidates"][0]["gate_reason"],
            "ANTI_CHASE_VWAP_EXTENSION",
        )

    def test_anti_chase_fails_closed_when_runtime_started_after_entry_window_opened(self):
        engine, decision = self._engine_with_long_signal()
        engine._states["2330"].session_open_time = decision.replace(
            hour=9, minute=6, second=0
        )
        self.assertIsNone(engine.choose_entry(decision, allow_short=False))

    def test_market_gate_fails_closed_without_benchmark_history(self):
        engine, decision = self._engine_with_market_long_signal()
        engine._states["0050"].ticks.clear()
        engine._states["0050"].cumulative_volume = 0
        engine._states["0050"].cumulative_pv = 0
        self.assertIsNone(engine.choose_entry(decision, allow_short=False))
        self.assertEqual(
            engine.last_entry_diagnostics["reason"],
            "BENCHMARK_MISSING_OR_STALE",
        )

    def test_market_gate_fails_closed_on_stale_benchmark(self):
        engine, decision = self._engine_with_market_long_signal()
        later = decision + timedelta(
            seconds=float(LONG_MARKET_REGIME_POLICY["maximum_staleness_seconds"]) + 1
        )
        self.assertIsNone(engine.choose_entry(later, allow_short=False))
        self.assertEqual(
            engine.last_entry_diagnostics["reason"],
            "BENCHMARK_MISSING_OR_STALE",
        )

    def test_bullish_market_allows_strong_long_candidate(self):
        engine, decision = self._engine_with_market_long_signal()
        signal = engine.choose_entry(decision, allow_short=False)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.market_regime, "BULLISH")
        self.assertGreaterEqual(signal.relative_strength_5m, 0)
        self.assertEqual(signal.required_confirmations, 1)
        self.assertNotEqual(signal.stock_id, LONG_MARKET_REGIME_POLICY["benchmark_symbol"])

    def test_bearish_market_requires_two_confirmations(self):
        engine, decision = self._engine_with_market_long_signal(bearish=True)
        self.assertIsNone(engine.choose_entry(decision, allow_short=False))
        self.assertEqual(
            engine.last_entry_diagnostics["candidates"][0]["gate_reason"],
            "CONFIRMATIONS_INCOMPLETE",
        )
        later = decision + timedelta(seconds=int(SPEC["decision_interval_seconds"]))
        for index in range(1, 7):
            at = decision + timedelta(seconds=index * 5)
            stock_price = 101.11 + index * 0.015
            benchmark_price = 100.39 - index * 0.015
            engine.record_tick(
                "2330", at=at, price=stock_price, volume=100,
                bid=stock_price - 0.1, ask=stock_price,
                flag="1", serial=500 + index,
            )
            engine.record_tick(
                "0050", at=at, price=benchmark_price, volume=100,
                bid=benchmark_price - 0.05, ask=benchmark_price + 0.05,
                flag="0", serial=500 + index,
            )
        engine.record_book_combined(
            "2330", at=later,
            buy_prices=[101.1], buy_volumes=[900],
            sell_prices=[101.2], sell_volumes=[100],
        )
        signal = engine.choose_entry(later, allow_short=False)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.market_regime, "BEARISH")
        self.assertEqual(signal.required_confirmations, 2)
        self.assertGreaterEqual(
            signal.relative_strength_5m,
            LONG_MARKET_REGIME_POLICY["bearish_min_relative_strength"],
        )

    def test_entry_window_starts_at_0905(self):
        self.assertEqual(SPEC["entry_start"], "09:05")

        before_open = datetime(2026, 9, 24, 9, 4, 59, tzinfo=TAIPEI)
        engine, decision = self._engine_with_long_signal(before_open)
        self.assertIsNone(engine.choose_entry(decision, allow_short=False))

        at_open = datetime(2026, 9, 24, 9, 5, 0, tzinfo=TAIPEI)
        engine, decision = self._engine_with_long_signal(at_open)
        self.assertIsNotNone(engine.choose_entry(decision, allow_short=False))

    def test_post_cutoff_does_not_reuse_prior_entry_diagnostics(self):
        engine, decision = self._engine_with_long_signal()
        self.assertIsNotNone(engine.choose_entry(decision, allow_short=False))
        self.assertTrue(engine.last_entry_diagnostics)

        after_cutoff = decision.replace(hour=13, minute=10, second=30)
        self.assertIsNone(engine.choose_entry(after_cutoff, allow_short=False))
        self.assertEqual(engine.last_entry_diagnostics, {})

    def test_hard_exit_is_generated(self):
        engine, _ = self._engine_with_long_signal()
        now = datetime(2026, 9, 24, 13, 20, 1, tzinfo=TAIPEI)
        engine.record_tick("2330", at=now, price=101, volume=10, bid=101.0, ask=101.5, flag="1", serial=999)
        position = ManagedPosition("2330", "台積電", "LONG", 1000, 100.0, "abc", now - timedelta(hours=1))
        decision = engine.evaluate_exit(position, now)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "HARD_EXIT")

    def test_live_stop_is_net_3500_and_gap_uses_observed_quote(self):
        engine, _ = self._engine_with_long_signal()
        now = datetime(2026, 9, 24, 10, 0, tzinfo=TAIPEI)
        position = ManagedPosition(
            "2330", "台積電", "LONG", 1000, 100.0, "abc",
            now - timedelta(minutes=1),
        )
        engine.record_tick(
            "2330", at=now, price=96.5, volume=10,
            bid=96.5, ask=96.6, flag="0", serial=999,
        )
        decision = engine.evaluate_exit(position, now)
        self.assertEqual(LIVE_EXIT_POLICY["stop_loss_net_twd"], 3500.0)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "STOP_LOSS")
        self.assertLessEqual(decision.projected_net_pnl, -3500)

    def test_loss_recovery_to_positive_is_not_an_exit(self):
        engine, _ = self._engine_with_long_signal()
        now = datetime(2026, 9, 24, 10, 0, tzinfo=TAIPEI)
        position = ManagedPosition(
            "2330", "台積電", "LONG", 1000, 100.0, "abc",
            now - timedelta(minutes=1),
        )
        engine.record_tick(
            "2330", at=now, price=98.5, volume=10,
            bid=98.5, ask=98.6, flag="0", serial=999,
        )
        self.assertIsNone(engine.evaluate_exit(position, now))
        engine.record_tick(
            "2330", at=now + timedelta(seconds=1), price=101.0, volume=10,
            bid=101.0, ask=101.1, flag="1", serial=1000,
        )
        decision = engine.evaluate_exit(position, now + timedelta(seconds=1))
        self.assertIsNone(decision)
        self.assertLess(position.worst_return, 0)
        self.assertFalse(position.mfe_protection_armed)

    def test_policy_id_records_removed_loss_recovery_rule(self):
        self.assertEqual(
            LIVE_EXIT_POLICY["policy_id"],
            "HARD_3500_PLUS_MFE_V1_NO_LOSS_RECOVERY",
        )

    def test_mfe_v1_exact_boundaries(self):
        self.assertIsNone(_candidate_locked_r(0.999999))
        self.assertEqual(_candidate_locked_r(1.0), 0.0)
        self.assertAlmostEqual(_candidate_locked_r(1.5), 0.9)
        self.assertAlmostEqual(_candidate_locked_r(2.0), 1.4)
        self.assertAlmostEqual(_candidate_locked_r(3.0), 2.25)

    def test_mfe_floor_never_loosens_and_triggers_exit(self):
        engine, _ = self._engine_with_long_signal()
        now = datetime(2026, 9, 24, 10, 0, tzinfo=TAIPEI)
        position = ManagedPosition(
            "2330", "台積電", "LONG", 1000, 100.0, "abc",
            now - timedelta(minutes=1),
        )
        engine._refresh_mfe_basis(position)
        risk = position.entry_price - position.initial_stop_price
        peak = position.entry_price + 3 * risk
        engine._observe_mfe(
            position,
            price=peak,
            projected_net_pnl=engine.projected_net(position, peak),
            at=now,
        )
        locked = position.locked_profit_price
        self.assertTrue(position.mfe_protection_armed)
        self.assertAlmostEqual(position.locked_profit_r, 2.25)
        engine._observe_mfe(
            position,
            price=position.entry_price + risk,
            projected_net_pnl=engine.projected_net(position, position.entry_price + risk),
            at=now + timedelta(seconds=1),
        )
        self.assertEqual(position.locked_profit_price, locked)
        # The analytical MFE floor need not itself be a valid exchange quote.
        executable_floor = (locked // 0.5) * 0.5
        engine.record_tick(
            "2330", at=now + timedelta(seconds=2), price=locked,
            volume=10, bid=executable_floor, ask=executable_floor + 0.5, flag="0", serial=1000,
        )
        decision = engine.evaluate_exit(position, now + timedelta(seconds=2))
        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "MFE_PROFIT_PROTECTION")

    def test_short_mfe_floor_is_side_symmetric(self):
        engine = LiveDirectionEngine({"2330": "台積電"})
        now = datetime(2026, 9, 24, 10, 0, tzinfo=TAIPEI)
        position = ManagedPosition(
            "2330", "台積電", "SHORT", 1000, 100.0, "abc",
            now - timedelta(minutes=1),
        )
        engine._refresh_mfe_basis(position)
        risk = position.initial_stop_price - position.entry_price
        peak = position.entry_price - 3 * risk
        engine._observe_mfe(
            position,
            price=peak,
            projected_net_pnl=engine.projected_net(position, peak),
            at=now,
        )
        self.assertAlmostEqual(position.locked_profit_r, 2.25)
        self.assertLess(position.locked_profit_price, position.entry_price)
        self.assertTrue(engine._mfe_floor_breached(
            position, position.locked_profit_price + 0.01,
        ))

    def test_partial_fill_rebase_never_loosens_absolute_floor(self):
        engine = LiveDirectionEngine({"2330": "台積電"})
        now = datetime(2026, 9, 24, 10, 0, tzinfo=TAIPEI)
        position = ManagedPosition(
            "2330", "台積電", "LONG", 1000, 100.0, "abc",
            now - timedelta(minutes=1),
        )
        engine._refresh_mfe_basis(position)
        risk = position.entry_price - position.initial_stop_price
        peak = position.entry_price + 3 * risk
        engine._observe_mfe(
            position, price=peak,
            projected_net_pnl=engine.projected_net(position, peak), at=now,
        )
        locked_before = position.locked_profit_price
        position.entry_price = 101.0
        position.quantity = 2000
        engine._refresh_mfe_basis(position)
        self.assertGreaterEqual(position.locked_profit_price, locked_before)
        self.assertGreaterEqual(position.locked_profit_r, 0.0)


if __name__ == "__main__":
    unittest.main()
