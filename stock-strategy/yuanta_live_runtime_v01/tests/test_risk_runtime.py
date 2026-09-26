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


class ArchiveRuntimeTests(unittest.TestCase):
    def test_signal_identifier_is_stable_and_non_sensitive(self):
        candidate = SimpleNamespace(
            stock_id="3605",
            stock_name="宏致",
            side="LONG",
            decision_time=datetime(
                2026,
                9,
                24,
                9,
                9,
                tzinfo=TAIPEI,
            ),
        )

        one = runtime_main._signal_identifier(
            "20260924",
            candidate,
        )
        two = runtime_main._signal_identifier(
            "20260924",
            candidate,
        )

        self.assertEqual(one, two)
        self.assertIn("20260924", one)
        self.assertIn("3605", one)
        self.assertIn("LONG", one)

        rendered = one.lower()
        for forbidden in (
            "account",
            "password",
            "token",
            "pfx",
            "baseline",
        ):
            self.assertNotIn(
                forbidden,
                rendered,
            )

    def test_normal_trade_close_continues_archive(self):
        self.assertEqual(
            runtime_main._post_close_action(
                emergency=False,
                graceful_stop=False,
            ),
            "CONTINUE_ARCHIVE",
        )

    def test_emergency_close_stops_runtime(self):
        self.assertEqual(
            runtime_main._post_close_action(
                emergency=True,
                graceful_stop=False,
            ),
            "STOP_EMERGENCY",
        )

    def test_graceful_close_stops_runtime(self):
        self.assertEqual(
            runtime_main._post_close_action(
                emergency=False,
                graceful_stop=True,
            ),
            "STOP_GRACEFUL",
        )

    def test_emergency_has_priority_over_graceful(self):
        self.assertEqual(
            runtime_main._post_close_action(
                emergency=True,
                graceful_stop=True,
            ),
            "STOP_EMERGENCY",
        )

    def test_archive_counter_delta_is_non_negative(self):
        before = {
            "new_requests": 5,
            "fills": 4,
            "requests": 9,
        }
        after = {
            "new_requests": 7,
            "fills": 5,
            "requests": 12,
        }

        self.assertEqual(
            runtime_main._archive_counter_delta(
                before,
                after,
            ),
            {
                "new_requests": 2,
                "fills": 1,
                "requests": 3,
            },
        )

        self.assertEqual(
            runtime_main._archive_counter_delta(
                after,
                before,
            ),
            {
                "new_requests": 0,
                "fills": 0,
                "requests": 0,
            },
        )

    def test_start_parser_uses_existing_shadow_archive_root(self):
        args = runtime_main.parse_args(
            ["observe-prod"]
        )
        self.assertEqual(
            args.archive_runtime_dir.resolve(),
            runtime_main.DEFAULT_ARCHIVE_RUNTIME_DIR.resolve(),
        )

    def test_empty_store_archive_counters_are_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveOrderStore(
                Path(temporary)
                / "orders.sqlite"
            )
            try:
                self.assertEqual(
                    runtime_main._store_archive_counters(
                        store
                    ),
                    {
                        "new_requests": 0,
                        "fills": 0,
                        "requests": 0,
                    },
                )
            finally:
                store.close()


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

    def test_quote_callback_feeds_engine_and_archive_from_same_session(self):
        class FakeEngine:
            def __init__(self):
                self.ticks = []
                self.books = []

            def record_tick(self, symbol, **payload):
                self.ticks.append((symbol, payload))

            def record_book_combined(self, symbol, **payload):
                self.books.append((symbol, payload))

            def record_book_side(self, symbol, **payload):
                self.books.append((symbol, payload))

        class FakeArchive:
            def __init__(self):
                self.events = []
                self.errors = 0

            def append(self, kind, payload):
                self.events.append((kind, payload))

            def callback_error(self):
                self.errors += 1

        engine = FakeEngine()
        archive = FakeArchive()
        logs = []

        item = SimpleNamespace(
            stock_id="3605",
            stock_name="宏致",
            market="TWSE",
            rank=1,
            score=0.5,
        )

        session = runtime_main._Session(
            api_types={},
            environment="PROD",
            credentials={"account": "test"},
            engine=engine,
            logger=lambda event, **payload: logs.append(
                (event, payload)
            ),
            archive=archive,
            archive_signal_date="20260924",
            archive_items={"3605": item},
        )

        quote_time = SimpleNamespace(
            bytHour=9,
            bytMin=9,
            bytSec=0,
            ushtMSec=123,
        )

        tick = SimpleNamespace(
            StkCode="3605",
            Time=quote_time,
            SerialNo=123,
            BuyPrice="181.5",
            SellPrice="182",
            DealPrice="182",
            DealVol="10",
            InOutFlag="1",
            Type="0",
        )

        session._on_response(
            2,
            0,
            "SubscribeStockTick",
            None,
            tick,
        )

        levels = {}
        for i in range(1, 6):
            levels[f"BuyPrice{i}"] = str(182 - i * 0.5)
            levels[f"BuyVol{i}"] = str(100 + i)
            levels[f"SellPrice{i}"] = str(182 + i * 0.5)
            levels[f"SellVol{i}"] = str(200 + i)

        book = SimpleNamespace(
            StkCode="3605",
            Time=quote_time,
            IndexFlag="50",
            IndexFlag_50=SimpleNamespace(**levels),
        )

        session._on_response(
            2,
            0,
            "SubscribeFiveTickA",
            None,
            book,
        )

        self.assertEqual(len(engine.ticks), 1)
        self.assertEqual(len(engine.books), 1)
        self.assertEqual(len(archive.events), 2)
        self.assertEqual(archive.errors, 0)

        tick_kind, tick_payload = archive.events[0]
        book_kind, book_payload = archive.events[1]

        self.assertEqual(tick_kind, "ticks")
        self.assertEqual(book_kind, "books")
        self.assertEqual(tick_payload["stock_id"], "3605")
        self.assertEqual(tick_payload["event_type"], "STOCK_TICK")
        self.assertEqual(tick_payload["quote_time"], "09:09:00.123")
        self.assertEqual(book_payload["event_type"], "FIVE_LEVEL")
        self.assertEqual(book_payload["stock_id"], "3605")
        self.assertIn("buy_prices", book_payload)
        self.assertIn("sell_prices", book_payload)

        rendered = str(archive.events).lower()
        for forbidden in (
            "account",
            "password",
            "token",
            "pfx",
            "baseline",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_archive_failure_does_not_block_strategy_callback(self):
        class FakeEngine:
            def __init__(self):
                self.calls = 0

            def record_tick(self, *_args, **_kwargs):
                self.calls += 1

        class BrokenArchive:
            def __init__(self):
                self.errors = 0

            def append(self, *_args, **_kwargs):
                raise OSError("disk test failure")

            def callback_error(self):
                self.errors += 1

        engine = FakeEngine()
        archive = BrokenArchive()
        logs = []

        session = runtime_main._Session(
            api_types={},
            environment="PROD",
            credentials={"account": "test"},
            engine=engine,
            logger=lambda event, **payload: logs.append(
                (event, payload)
            ),
            archive=archive,
            archive_signal_date="20260924",
            archive_items={
                "3605": SimpleNamespace(
                    stock_name="宏致",
                    market="TWSE",
                    rank=1,
                    score=0.5,
                )
            },
        )

        tick = SimpleNamespace(
            StkCode="3605",
            Time=SimpleNamespace(
                bytHour=9,
                bytMin=9,
                bytSec=0,
                ushtMSec=0,
            ),
            SerialNo=1,
            BuyPrice="181.5",
            SellPrice="182",
            DealPrice="182",
            DealVol="10",
            InOutFlag="1",
            Type="0",
        )

        session._on_response(
            2,
            0,
            "SubscribeStockTick",
            None,
            tick,
        )

        self.assertEqual(engine.calls, 1)
        self.assertEqual(archive.errors, 1)
        self.assertTrue(
            any(
                event == "ARCHIVE_CALLBACK_ERROR"
                for event, _payload in logs
            )
        )

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

    def test_second_runtime_is_blocked_before_broker_connect(self):
        class FakeSession:
            def __init__(self):
                self.connect_calls = 0
                self.close_calls = 0

            def connect(self):
                self.connect_calls += 1

            def close(self):
                self.close_calls += 1

        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            first = runtime_main._acquire_runtime_instance_lock(runtime)

            fake_session = FakeSession()
            fake_notifier = SimpleNamespace(close=lambda **_kwargs: None)

            args = SimpleNamespace(
                runtime_dir=runtime,
                baseline=runtime / "position_baseline.json",
                vendor_dir=runtime / "vendor",
                live=False,
                recover_emergency=False,
                capital=190000,
                max_daily_loss=5000,
                max_order_value=190000,
                max_position_per_stock=0,
                max_concurrent_positions=1,
                max_trades_per_day=1,
                entry_quote_staleness=5.0,
                short_entry_order_type=None,
                short_cover_order_type=None,
            )

            try:
                with patch.object(
                    runtime_main,
                    "load_stage_a_watchlist",
                    return_value=({"signal_date": "20260925"}, [], None),
                ), patch.object(
                    runtime_main,
                    "_validate_watchlist_day",
                ), patch.object(
                    runtime_main,
                    "load_credentials",
                    return_value={
                        "pfx_password": "test",
                        "trading_password": "test",
                    },
                ), patch.object(
                    runtime_main,
                    "load_api_types",
                    return_value={},
                ), patch.object(
                    runtime_main,
                    "_extend_quote_types",
                    return_value={},
                ), patch.object(
                    runtime_main,
                    "_load_baseline",
                    return_value={},
                ), patch.object(
                    runtime_main,
                    "_Session",
                    return_value=fake_session,
                ), patch.object(
                    runtime_main,
                    "AsyncTradingNotifier",
                    return_value=fake_notifier,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "another realtime runtime already owns",
                    ):
                        runtime_main._run_realtime(
                            args,
                            environment="PROD",
                            submit_live=False,
                        )

                self.assertEqual(fake_session.connect_calls, 0)
                self.assertEqual(fake_session.close_calls, 1)
            finally:
                runtime_main._release_runtime_instance_lock(first)

    def test_preflight_refuses_running_runtime_before_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            first = runtime_main._acquire_runtime_instance_lock(
                runtime
            )

            try:
                with patch.object(
                    runtime_main,
                    "load_credentials",
                ) as credentials:
                    result = runtime_main.main(
                        [
                            "preflight-prod",
                            "--runtime-dir",
                            temporary,
                        ]
                    )

                self.assertEqual(result, 1)
                credentials.assert_not_called()

            finally:
                runtime_main._release_runtime_instance_lock(
                    first
                )

    def test_preflight_releases_lock_after_local_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)

            with patch.object(
                runtime_main,
                "_load_baseline",
                side_effect=RuntimeError(
                    "test local failure"
                ),
            ):
                result = runtime_main.main(
                    [
                        "preflight-prod",
                        "--runtime-dir",
                        temporary,
                    ]
                )

            self.assertEqual(result, 1)

            second = runtime_main._acquire_runtime_instance_lock(
                runtime
            )
            try:
                self.assertFalse(second.closed)
            finally:
                runtime_main._release_runtime_instance_lock(
                    second
                )

    def test_clear_halt_refuses_running_runtime_before_broker_connect(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            first = runtime_main._acquire_runtime_instance_lock(runtime)

            try:
                with patch.object(
                    runtime_main,
                    "_connect_for_control",
                ) as connect:
                    result = runtime_main.main([
                        "clear-halt",
                        "--runtime-dir",
                        temporary,
                        "--reason",
                        "test",
                    ])

                self.assertEqual(result, 1)
                connect.assert_not_called()
            finally:
                runtime_main._release_runtime_instance_lock(first)

    def test_persistent_stop_request_blocks_runtime_before_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            (runtime / "STOP_REQUEST").write_text(
                "test\n",
                encoding="utf-8",
            )

            with patch.object(
                runtime_main,
                "load_credentials",
            ) as credentials:
                result = runtime_main.main([
                    "observe-prod",
                    "--runtime-dir",
                    temporary,
                ])

            self.assertEqual(result, 1)
            credentials.assert_not_called()

    def test_runtime_instance_lock_blocks_second_holder(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            first = runtime_main._acquire_runtime_instance_lock(runtime)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "another realtime runtime already owns",
                ):
                    runtime_main._acquire_runtime_instance_lock(runtime)
            finally:
                runtime_main._release_runtime_instance_lock(first)

    def test_runtime_instance_lock_reacquires_after_release_with_stale_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)

            first = runtime_main._acquire_runtime_instance_lock(runtime)
            runtime_main._release_runtime_instance_lock(first)

            lock_path = runtime / runtime_main.RUNTIME_LOCK_FILENAME
            self.assertTrue(lock_path.is_file())

            # The file remains, but no process owns the kernel lock anymore.
            second = runtime_main._acquire_runtime_instance_lock(runtime)
            try:
                self.assertFalse(second.closed)
            finally:
                runtime_main._release_runtime_instance_lock(second)

    def test_local_stop_writes_graceful_marker_for_fresh_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            heartbeat = Path(temporary) / "heartbeat.json"
            heartbeat.write_text(
                '{"pid": %d, "at": "%s"}'
                % (
                    runtime_main.os.getpid(),
                    datetime.now(TAIPEI).isoformat(),
                ),
                encoding="utf-8",
            )
            with patch.object(runtime_main, "_run_realtime") as run:
                result = runtime_main.main([
                    "stop", "--runtime-dir", temporary, "--reason", "test"
                ])
            self.assertEqual(result, 0)
            self.assertTrue((Path(temporary) / "STOP_REQUEST").is_file())
            self.assertFalse((Path(temporary) / "EMERGENCY_STOP").exists())
            run.assert_not_called()

    def test_local_stop_refuses_stale_heartbeat_without_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            heartbeat = Path(temporary) / "heartbeat.json"
            heartbeat.write_text(
                '{"pid": %d, "at": "%s"}'
                % (
                    runtime_main.os.getpid(),
                    (datetime.now(TAIPEI) - timedelta(seconds=30)).isoformat(),
                ),
                encoding="utf-8",
            )

            result = runtime_main.main([
                "stop", "--runtime-dir", temporary, "--reason", "test"
            ])

            self.assertEqual(result, 1)
            self.assertFalse((Path(temporary) / "STOP_REQUEST").exists())
            self.assertFalse((Path(temporary) / "EMERGENCY_STOP").exists())

    def test_local_stop_without_runtime_does_not_leave_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = runtime_main.main([
                "stop", "--runtime-dir", temporary, "--reason", "test"
            ])
            self.assertEqual(result, 0)
            self.assertFalse((Path(temporary) / "STOP_REQUEST").exists())
            self.assertFalse((Path(temporary) / "EMERGENCY_STOP").exists())

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
