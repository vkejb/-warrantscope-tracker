"""Carryover rescue accounting only; synthetic fills and a temporary DB."""
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from yuanta_broker_execution_v01 import ExecutionIntent, IntentPurpose, LiveOrderStore, Side
from yuanta_live_runtime_v01.accounting import AccountingError, execution_pnl, _fees
from yuanta_live_runtime_v01.strategy import SPEC, TAIPEI


class CarryoverAccountingSafetyTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = LiveOrderStore(Path(self.temp.name) / "mock.sqlite")
        self.today = datetime.now(TAIPEI)
        self.spec = dict(SPEC, commission_rate_each_side=0.001425)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def order(self, key, side, quantity, price, *, day=None, exit=False):
        order, _ = self.store.reserve(ExecutionIntent(
            key, "TEST", side, quantity, Decimal(str(price)),
            purpose=IntentPurpose.EXIT if exit else IntentPurpose.ENTRY,
        ))
        if day is not None:
            with self.store.connection:
                self.store.connection.execute(
                    "UPDATE live_orders SET created_at=? WHERE client_order_id=?",
                    (day.isoformat(), order.client_order_id),
                )
        return order

    def fill(self, order, quantity, price, fill_id):
        self.store.record_fill(order.client_order_id, quantity=quantity,
                               price=str(price), fill_id=fill_id)

    def test_carried_long_sale_uses_full_tax_and_does_not_block_rescue_accounting(self):
        entry = self.order("in", Side.BUY, 1000, 100, day=self.today - timedelta(days=1))
        self.fill(entry, 1000, 100, "in")
        out = self.order("out", Side.SELL, 1000, 105, exit=True)
        self.fill(out, 1000, 105, "out")
        pnl = execution_pnl(self.store, self.today, self.spec)
        self.assertEqual(pnl.realized, Decimal(5000 - 143 - 150 - 315))
        self.assertEqual(pnl.open_lots, [])

    def test_partial_carryover_exits_pay_order_minimum_only_once(self):
        entry = self.order("in", Side.BUY, 1000, 100, day=self.today - timedelta(days=1))
        self.fill(entry, 1000, 100, "in")
        out = self.order("out", Side.SELL, 1000, 105, exit=True)
        self.fill(out, 400, 105, "a")
        self.fill(out, 600, 105, "b")
        pnl = execution_pnl(self.store, self.today, self.spec)
        self.assertEqual(pnl.realized, Decimal(4392))

    def test_unrealized_carried_long_reserves_full_exit_tax(self):
        entry = self.order("in", Side.BUY, 1000, 100, day=self.today - timedelta(days=1))
        self.fill(entry, 1000, 100, "in")
        pnl = execution_pnl(self.store, self.today, self.spec)
        self.assertEqual(pnl.unrealized({"TEST": Decimal(100)}, self.spec), Decimal(-586))

    def test_carried_short_recomputes_entry_sell_tax_conservatively(self):
        entry = self.order("in", Side.SELL, 1000, 100, day=self.today - timedelta(days=1))
        self.fill(entry, 1000, 100, "in")
        out = self.order("out", Side.BUY, 1000, 95, exit=True)
        self.fill(out, 1000, 95, "out")
        self.assertEqual(execution_pnl(self.store, self.today, self.spec).realized,
                         Decimal(5000 - 143 - 300 - 136))

    def test_mixed_old_and_today_sale_uses_full_tax_on_whole_order(self):
        old = self.order("old", Side.BUY, 1000, 100, day=self.today - timedelta(days=1))
        self.fill(old, 1000, 100, "old")
        new = self.order("new", Side.BUY, 1000, 100)
        self.fill(new, 1000, 100, "new")
        out = self.order("out", Side.SELL, 2000, 105, exit=True)
        self.fill(out, 1000, 105, "a")
        self.fill(out, 1000, 105, "b")
        self.assertEqual(execution_pnl(self.store, self.today, self.spec).realized,
                         Decimal(10000 - 286 - 300 - 630))

    def test_missing_entry_and_reduced_full_tax_remain_fail_closed(self):
        out = self.order("out", Side.SELL, 1000, 105, exit=True)
        self.fill(out, 1000, 105, "out")
        with self.assertRaises(AccountingError):
            execution_pnl(self.store, self.today, SPEC)
        # It is not acceptable to manufacture a zero-cost entry for an unknown fill.

    def test_invalid_carryover_tax_rejected(self):
        entry = self.order("in", Side.BUY, 1000, 100, day=self.today - timedelta(days=1))
        self.fill(entry, 1000, 100, "in")
        pnl = execution_pnl(self.store, self.today, SPEC)
        for rate in ("0.0015", "NaN", "Infinity"):
            with self.subTest(rate=rate), self.assertRaises(AccountingError):
                pnl.unrealized({"TEST": Decimal(100)}, dict(SPEC, ordinary_stock_sell_tax_rate=rate))

    def test_invalid_cost_config_never_creates_fee_credit_or_nonfinite_pnl(self):
        for field in ("commission_rate_each_side", "minimum_commission_twd", "day_trade_sell_tax_rate"):
            for value in ("-0.003", "NaN", "Infinity", "nonnumeric"):
                with self.subTest(field=field, value=value):
                    spec = dict(SPEC, **{field: value})
                    with self.assertRaises(AccountingError):
                        _fees(Decimal(100000), "SELL", spec)
                    with self.assertRaises(AccountingError):
                        execution_pnl(self.store, self.today, spec)
        zero = dict(SPEC, commission_rate_each_side=0,
                    minimum_commission_twd=0, day_trade_sell_tax_rate=0)
        self.assertEqual(_fees(Decimal(100000), "SELL", zero), Decimal(0))
