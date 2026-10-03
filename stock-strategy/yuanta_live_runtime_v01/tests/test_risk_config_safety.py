"""Pure risk input regressions; no broker/credentials/application state."""
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase

from yuanta_live_runtime_v01.risk_manager import RiskLimits, RiskManager
from yuanta_live_runtime_v01.strategy import TAIPEI


class RiskConfigSafetyTests(TestCase):
    def signal(self, **overrides):
        values = dict(stock_id="TEST", side="LONG", quantity=2000,
                      entry_price=50, decision_time=datetime(2026, 10, 5, 9, 10, tzinfo=TAIPEI))
        values.update(overrides)
        return SimpleNamespace(**values)

    def evaluate(self, manager=None, signal=None, **overrides):
        values = dict(signal=signal or self.signal(), quote_age_seconds=.5,
                      broker_positions={}, open_orders=[], trades_today=0)
        values.update(overrides)
        return (manager or RiskManager(RiskLimits())).evaluate_entry(**values)

    def test_invalid_limits_are_rejected_before_session(self):
        for field in ("max_daily_loss", "max_order_value", "stale_quote_seconds"):
            for value in ("NaN", "Infinity", "-Infinity", "0", "-1"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    RiskLimits(**{field: Decimal(value)})
        for count in ("max_concurrent_positions", "max_trades_per_day", "max_position_per_stock"):
            for value in (float("nan"), float("inf"), 1.5, True):
                with self.subTest(field=count, value=value), self.assertRaises(ValueError):
                    RiskLimits(**{count: value})

    def test_share_cap_rounds_down_before_approval(self):
        result = self.evaluate(manager=RiskManager(RiskLimits(max_position_per_stock=1500)))
        self.assertTrue(result.approved)
        self.assertEqual(result.intent["quantity_lots"], "1")

    def test_valid_numeric_configuration_is_normalized_before_use(self):
        limits = RiskLimits(max_order_value="190000", max_daily_loss="5000",
                            stale_quote_seconds="5", max_trades_per_day="1",
                            max_concurrent_positions="1", max_position_per_stock="1500")
        self.assertIsInstance(limits.max_order_value, Decimal)
        self.assertIsInstance(limits.max_position_per_stock, int)
        self.assertTrue(self.evaluate(manager=RiskManager(limits)).approved)

    def test_cap_with_odd_lot_residue_never_creates_fractional_board_lot(self):
        manager = RiskManager(RiskLimits(max_position_per_stock=2000))
        result = self.evaluate(manager=manager, broker_positions={"TEST|0": 500})
        self.assertTrue(result.approved)
        self.assertEqual(result.intent["quantity_lots"], "1")
        blocked = self.evaluate(manager=manager, broker_positions={"TEST|0": 1500})
        self.assertFalse(blocked.approved)

    def test_nonfinite_quote_age_rejects_without_exception(self):
        for value in (float("nan"), float("inf"), None, -.1):
            with self.subTest(value=value):
                result = self.evaluate(quote_age_seconds=value)
                self.assertFalse(result.approved)
                self.assertIn("STALE_QUOTE", result.reasons)

    def test_finite_but_unrepresentable_price_fails_closed(self):
        for price in (Decimal("1e-1000"), Decimal("1e1000000")):
            with self.subTest(price=price):
                result = self.evaluate(signal=self.signal(entry_price=price))
                self.assertFalse(result.approved)
                self.assertEqual(result.reasons, ("INVALID_ORDER_SIZE",))

    def test_invalid_signal_or_unknown_pnl_fails_closed(self):
        for field, value in (("quantity", float("nan")), ("quantity", 1000.5),
                             ("entry_price", float("inf")), ("side", "UNKNOWN"),
                             ("stock_id", "")):
            with self.subTest(field=field):
                self.assertFalse(self.evaluate(signal=self.signal(**{field: value})).approved)
        manager = RiskManager(RiskLimits())
        self.assertTrue(manager.loss_kill_required(realized=Decimal("NaN"), unrealized=Decimal(0)))
        result = self.evaluate(realized=Decimal("NaN"))
        self.assertIn("PNL_UNAVAILABLE", result.reasons)
        self.assertFalse(result.approved)

    def test_nonfinite_exit_price_or_fractional_shares_rejected(self):
        manager = RiskManager(RiskLimits())
        position = SimpleNamespace(side="LONG", stock_id="TEST", entry_time=self.signal().decision_time)
        for quantity, price in ((1000, float("nan")), (1000, float("inf")), (1000.5, 50)):
            with self.subTest(quantity=quantity, price=price), self.assertRaises(ValueError):
                manager.approve_exit(position=position, quantity=quantity, price=price,
                                     reason="STOP_LOSS", attempt=1)
        # Partial fills can be odd lots: exit risk approval must keep them.
        valid = manager.approve_exit(position=position, quantity=500, price=50,
                                     reason="STOP_LOSS", attempt=1)
        self.assertEqual(valid["quantity_lots"], "0.5")

    def test_unknown_broker_state_rejects_without_exception_or_approval(self):
        for positions in (None, {"TEST|0": "NaN"}, {"TEST|0": 500.5}):
            with self.subTest(positions=positions):
                result = self.evaluate(broker_positions=positions)
                self.assertEqual(result.reasons, ("BROKER_POSITION_UNAVAILABLE",))
        for count in (None, -1, float("inf"), .5):
            with self.subTest(count=count):
                self.assertEqual(self.evaluate(trades_today=count).reasons,
                                 ("TRADE_COUNT_UNAVAILABLE",))
        for orders in (None, [SimpleNamespace()], [SimpleNamespace(remaining_quantity="NaN")]):
            with self.subTest(orders=orders):
                result = self.evaluate(open_orders=orders)
                self.assertFalse(result.approved)
                self.assertIn("OPEN_ORDER_STATE_UNAVAILABLE", result.reasons)
        for stamp in (None, "bad", datetime(2026, 10, 5, 9, 10)):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.evaluate(signal=self.signal(decision_time=stamp)).reasons,
                                 ("INVALID_SIGNAL_TIME",))
