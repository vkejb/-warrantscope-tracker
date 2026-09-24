from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from yuanta_broker_execution_v01 import ExecutionIntent, IntentPurpose, LiveOrderStore, Side
from yuanta_live_runtime_v01 import main as runtime_main
from yuanta_live_runtime_v01.risk_manager import RiskLimits, RiskManager
from yuanta_live_runtime_v01.strategy import LiveDirectionEngine, ManagedPosition, TAIPEI
from yuanta_live_runtime_v01.watchdog import Heartbeat, check_once


class RiskManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = RiskManager(RiskLimits())
        self.now = datetime(2026, 9, 24, 9, 35, tzinfo=TAIPEI)
        self.signal = SimpleNamespace(
            stock_id="3605",
            side="LONG",
            quantity=5000,
            entry_price=30.0,
            decision_time=self.now,
        )

    def evaluate(self, **overrides):
        values = {
            "signal": self.signal,
            "quote_age_seconds": 0.5,
            "broker_positions": {},
            "open_orders": [],
            "trades_today": 0,
        }
        values.update(overrides)
        return self.manager.evaluate_entry(**values)

    def test_entry_uses_multiple_lots_within_order_value(self):
        result = self.evaluate()
        self.assertTrue(result.approved)
        self.assertEqual(result.intent["quantity_lots"], "5")

    def test_optional_share_cap_can_still_limit_to_one_lot(self):
        manager = RiskManager(RiskLimits(max_position_per_stock=1000))
        result = manager.evaluate_entry(
            signal=self.signal,
            quote_age_seconds=0.5,
            broker_positions={},
            open_orders=[],
            trades_today=0,
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.intent["quantity_lots"], "1")

    def test_order_value_cap_resizes_requested_quantity(self):
        self.signal.quantity = 10000
        result = self.evaluate()
        self.assertTrue(result.approved)
        self.assertEqual(result.intent["quantity_lots"], "6")
        self.assertLessEqual(
            Decimal(result.intent["suggested_limit_price"])
            * Decimal(result.intent["quantity_lots"])
            * Decimal("1000"),
            Decimal("190000"),
        )

    def test_stale_quote_is_rejected(self):
        result = self.evaluate(quote_age_seconds=5.1)
        self.assertFalse(result.approved)
        self.assertIn("STALE_QUOTE", result.reasons)

    def test_daily_loss_activates_kill_boundary(self):
        result = self.evaluate(
            realized=Decimal("-3000"), unrealized=Decimal("-2000")
        )
        self.assertFalse(result.approved)
        self.assertIn("MAX_DAILY_LOSS", result.reasons)

    def test_existing_open_order_rejects_new_entry(self):
        order = SimpleNamespace(remaining_quantity=1000)
        result = self.evaluate(open_orders=[order])
        self.assertFalse(result.approved)
        self.assertIn("OPEN_ORDER_EXISTS", result.reasons)


class SafeQuoteTests(unittest.TestCase):
    def test_stale_exit_quote_is_never_returned(self):
        engine = LiveDirectionEngine({"3605": "宏致"})
        now = datetime(2026, 9, 24, 13, 20, tzinfo=TAIPEI)
        engine.record_tick(
            "3605",
            at=now - timedelta(seconds=4),
            price=171.0,
            volume=10,
            bid=170.5,
            ask=171.0,
        )
        position = ManagedPosition(
            "3605", "宏致", "LONG", 1000, 170.0, "entry", now - timedelta(hours=1)
        )
        self.assertIsNone(engine.safe_exit_quote(position, now, max_age_seconds=3.0))
        self.assertIsNone(
            engine.evaluate_exit(position, now, max_quote_age_seconds=3.0)
        )

    def test_fresh_exit_quote_is_bounded_and_available(self):
        engine = LiveDirectionEngine({"3605": "宏致"})
        now = datetime(2026, 9, 24, 13, 20, tzinfo=TAIPEI)
        engine.record_tick(
            "3605", at=now, price=171.0, volume=10, bid=170.5, ask=171.0
        )
        position = ManagedPosition(
            "3605", "宏致", "LONG", 1000, 170.0, "entry", now - timedelta(hours=1)
        )
        quote = engine.safe_exit_quote(position, now, max_age_seconds=3.0)
        self.assertIsNotNone(quote)
        self.assertGreater(quote.price, 0)


class RuntimeGateTests(unittest.TestCase):
    def test_live_environment_flags_are_checked_before_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {}, clear=True), patch.object(
                runtime_main, "load_credentials"
            ) as credentials:
                result = runtime_main.main([
                    "start-prod", "--live", "--runtime-dir", temporary
                ])
            self.assertEqual(result, 1)
            credentials.assert_not_called()

    def test_quote_subscription_rejection_fails_closed(self):
        class FakeList(list):
            def Add(self, value):
                self.append(value)

        class FakeGenericList:
            def __class_getitem__(cls, _item):
                return FakeList

            def __getitem__(self, _item):
                return FakeList

        class Quote:
            pass

        session = runtime_main._Session(
            api_types={
                "List": FakeGenericList,
                "StockTick": Quote,
                "FiveTickA": Quote,
                "Market": SimpleNamespace(TWSE="TWSE", TWOTC="TPEX"),
                "Language": SimpleNamespace(UTF8="UTF8"),
            },
            environment="UAT",
            credentials={"account": "test"},
            engine=None,
            logger=lambda *_args, **_kwargs: None,
        )
        session.api = SimpleNamespace(
            SubscribeStockTick=lambda *_args: False,
            SubscribeFiveTickA=lambda *_args: True,
        )
        with self.assertRaises(RuntimeError):
            session.subscribe([SimpleNamespace(market="TWSE", stock_id="3605")])

    def test_local_kill_writes_marker_without_broker_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(runtime_main, "_run_realtime") as run:
                result = runtime_main.main([
                    "kill", "--runtime-dir", temporary, "--reason", "test"
                ])
            self.assertEqual(result, 0)
            self.assertTrue((Path(temporary) / "EMERGENCY_STOP").is_file())
            run.assert_not_called()

    def test_realized_pnl_uses_actual_fills(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveOrderStore(Path(temporary) / "orders.sqlite")
            try:
                entry, _ = store.reserve(ExecutionIntent(
                    intent_id="entry", symbol="3605", side=Side.BUY,
                    quantity=1000, price=Decimal("100"), purpose=IntentPurpose.ENTRY,
                ))
                store.record_fill(
                    entry.client_order_id, fill_id="entry-fill", quantity=1000, price="100"
                )
                exit_order, _ = store.reserve(ExecutionIntent(
                    intent_id="exit", symbol="3605", side=Side.SELL,
                    quantity=1000, price=Decimal("101"), purpose=IntentPurpose.EXIT,
                ))
                store.record_fill(
                    exit_order.client_order_id, fill_id="exit-fill", quantity=1000, price="101"
                )
                engine = LiveDirectionEngine({"3605": "宏致"})
                pnl = runtime_main._realized_pnl_today(
                    store, engine, datetime.now(TAIPEI)
                )
                self.assertGreater(pnl, Decimal("0"))
            finally:
                store.close()


class WatchdogTests(unittest.TestCase):
    def test_unknown_exit_reconciliation_recovers_original_order_without_rescue(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveOrderStore(Path(temporary) / "orders.sqlite")
            try:
                entry, _ = store.reserve(
                    ExecutionIntent(
                        intent_id="entry-recovery-integration",
                        symbol="3605",
                        side=Side.BUY,
                        quantity=1000,
                        price=Decimal("100"),
                        purpose=IntentPurpose.ENTRY,
                    )
                )
                store.record_fill(
                    entry.client_order_id,
                    fill_id="entry-recovery-fill",
                    quantity=1000,
                    price="100",
                )

                exit_order, _ = store.reserve(
                    ExecutionIntent(
                        intent_id="exit-recovery-integration",
                        symbol="3605",
                        side=Side.SELL,
                        quantity=1000,
                        price=Decimal("99"),
                        purpose=IntentPurpose.EXIT,
                    ),
                    allow_halted=True,
                )
                store.mark_send_pending(exit_order.client_order_id)
                store.bind_broker_order(exit_order.client_order_id, "broker-exit-1")
                store.mark_unknown(
                    exit_order.client_order_id,
                    "simulated broker timeout",
                )

                position = ManagedPosition(
                    "3605",
                    "宏致",
                    "LONG",
                    1000,
                    100.0,
                    entry.client_order_id,
                    datetime.now(TAIPEI) - timedelta(minutes=5),
                )
                position.exit_submitted = True

                adapter = SimpleNamespace()
                adapter.submit_rescue = unittest.mock.Mock()

                def reconcile(*, timeout, strict_positions):
                    self.assertEqual(timeout, 1)
                    self.assertTrue(strict_positions)
                    # Simulate authoritative broker reconciliation discovering
                    # that the original EXIT really exists and is still active.
                    store.acknowledge(exit_order.client_order_id)
                    return SimpleNamespace(status="MATCH")

                adapter.reconcile = reconcile

                action, status, remaining = runtime_main._reconcile_failed_exit(
                    adapter=adapter,
                    store=store,
                    exit_order_id=exit_order.client_order_id,
                    position=position,
                    reconcile_timeout=1,
                )

                self.assertEqual(action, "TRACK_EXISTING")
                self.assertEqual(
                    status,
                    runtime_main.BrokerOrderStatus.ACKNOWLEDGED,
                )
                self.assertEqual(remaining, 1000)
                self.assertTrue(position.exit_submitted)
                self.assertEqual(position.quantity, 1000)

                # The recovered original broker EXIT remains responsible for
                # the exposure. A second rescue order must not be submitted.
                adapter.submit_rescue.assert_not_called()
            finally:
                store.close()

    def test_reconciled_active_exit_keeps_tracking_original_order(self):
        for status in (
            runtime_main.BrokerOrderStatus.SEND_PENDING,
            runtime_main.BrokerOrderStatus.ACKNOWLEDGED,
            runtime_main.BrokerOrderStatus.PARTIALLY_FILLED,
            runtime_main.BrokerOrderStatus.CANCEL_PENDING,
        ):
            with self.subTest(status=status):
                self.assertEqual(
                    runtime_main._classify_reconciled_exit(status, 1000),
                    "TRACK_EXISTING",
                )

    def test_reconciled_terminal_exit_with_remaining_exposure_allows_rescue(self):
        for status in (
            runtime_main.BrokerOrderStatus.FILLED,
            runtime_main.BrokerOrderStatus.CANCELED,
            runtime_main.BrokerOrderStatus.REJECTED,
            runtime_main.BrokerOrderStatus.EXPIRED,
        ):
            with self.subTest(status=status):
                self.assertEqual(
                    runtime_main._classify_reconciled_exit(status, 1000),
                    "RETRY_RESCUE",
                )

    def test_reconciled_exit_with_no_remaining_exposure_is_closed(self):
        for status in (
            runtime_main.BrokerOrderStatus.FILLED,
            runtime_main.BrokerOrderStatus.ACKNOWLEDGED,
            runtime_main.BrokerOrderStatus.UNKNOWN,
        ):
            with self.subTest(status=status):
                self.assertEqual(
                    runtime_main._classify_reconciled_exit(status, 0),
                    "CLOSED",
                )

    def test_unresolved_reconciled_exit_fails_closed(self):
        self.assertEqual(
            runtime_main._classify_reconciled_exit(
                runtime_main.BrokerOrderStatus.UNKNOWN,
                1000,
            ),
            "UNKNOWN",
        )

    def test_clean_heartbeat_is_healthy(self):
        with tempfile.TemporaryDirectory() as temporary:
            heartbeat = Heartbeat(Path(temporary))
            heartbeat.stopped(True)
            self.assertTrue(check_once(Path(temporary), stale_seconds=1))

    def test_missing_heartbeat_is_critical(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("yuanta_live_runtime_v01.watchdog.RuntimeNotifier") as notifier:
                self.assertFalse(check_once(Path(temporary), stale_seconds=1))
                notifier.return_value.critical.assert_called_once()


if __name__ == "__main__":
    unittest.main()
