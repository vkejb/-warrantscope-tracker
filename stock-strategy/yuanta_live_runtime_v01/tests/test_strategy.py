from datetime import datetime, timedelta
import unittest

from yuanta_live_runtime_v01.strategy import (
    LIVE_EXIT_POLICY,
    LiveDirectionEngine,
    ManagedPosition,
    SPEC,
    TAIPEI,
    _candidate_locked_r,
)


class StrategyTests(unittest.TestCase):
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
        return engine, decision

    def test_long_candidate_is_sized_within_cap(self):
        engine, decision = self._engine_with_long_signal()
        signal = engine.choose_entry(decision, allow_short=False)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "LONG")
        self.assertGreater(signal.quantity, 0)
        self.assertLessEqual(signal.entry_price * signal.quantity, 190000)
        self.assertEqual(signal.quantity % 1000, 0)

    def test_entry_window_starts_at_0905(self):
        self.assertEqual(SPEC["entry_start"], "09:05")

        before_open = datetime(2026, 9, 24, 9, 4, 59, tzinfo=TAIPEI)
        engine, decision = self._engine_with_long_signal(before_open)
        self.assertIsNone(engine.choose_entry(decision, allow_short=False))

        at_open = datetime(2026, 9, 24, 9, 5, 0, tzinfo=TAIPEI)
        engine, decision = self._engine_with_long_signal(at_open)
        self.assertIsNotNone(engine.choose_entry(decision, allow_short=False))

    def test_hard_exit_is_generated(self):
        engine, _ = self._engine_with_long_signal()
        now = datetime(2026, 9, 24, 13, 20, 1, tzinfo=TAIPEI)
        engine.record_tick("2330", at=now, price=101, volume=10, bid=100.9, ask=101.0, flag="1", serial=999)
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
        engine.record_tick(
            "2330", at=now + timedelta(seconds=2), price=locked,
            volume=10, bid=locked, ask=locked + 0.1, flag="0", serial=1000,
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
