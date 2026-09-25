from datetime import datetime, timedelta
import unittest

from yuanta_live_runtime_v01.strategy import LiveDirectionEngine, ManagedPosition, SPEC, TAIPEI


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


if __name__ == "__main__":
    unittest.main()
