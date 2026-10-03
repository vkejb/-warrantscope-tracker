"""Post-fill controller regressions: synthetic fills and isolated temp state only."""
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from yuanta_broker_execution_v01 import ExecutionIntent, IntentPurpose, LiveOrderStore, Side
from yuanta_live_runtime_v01 import main as runtime_main
from yuanta_live_runtime_v01.tests import test_runtime_safety_regressions as harness_module
from yuanta_live_runtime_v01.tests.test_runtime_safety_regressions import RuntimeHarness
from yuanta_live_runtime_v01.strategy import LiveDirectionEngine, ManagedPosition, TAIPEI
from yuanta_broker_execution_v01.tests.test_adapter import EventHook


class RestoredExitHarness(RuntimeHarness):
    def __init__(self, directory, *, partial_exit=False):
        super().__init__(directory, minute=10)
        self.partial_exit = partial_exit

    def prepare(self):
        super().prepare()
        store = LiveOrderStore(self.directory / "live-orders.sqlite")
        try:
            order, _ = store.reserve(ExecutionIntent(
                "restored-exit", "TEST", Side.SELL, 1000, Decimal("100"),
                purpose=IntentPurpose.EXIT,
            ))
            store.bind_broker_order(order.client_order_id, "MOCK_RESTORED_EXIT")
            store.acknowledge(order.client_order_id)
            if self.partial_exit:
                store.record_fill(order.client_order_id, fill_id="exit-before-restart",
                                  quantity=500, price="100")
        finally:
            store.close()


class CallbackFillHarness(RuntimeHarness):
    def __init__(self, directory):
        super().__init__(directory, minute=10)
        self.callback_halt_at = None
        self.exit_times = []

    def prepare(self):
        store = LiveOrderStore(self.directory / "live-orders.sqlite")
        try:
            order, _ = store.reserve(ExecutionIntent(
                "callback-entry", "TEST", Side.BUY, 1000, Decimal("100"),
            ))
            store.bind_broker_order(order.client_order_id, "MOCK_ENTRY")
            store.acknowledge(order.client_order_id)
            self.entry_id = order.client_order_id
        finally:
            store.close()


class PostFillRecoveryRegressions(TestCase):
    def assert_flat(self, directory, harness, result, heartbeat):
        self.assertEqual(result, 0)
        self.assertEqual(heartbeat["state"], "STOPPED_CLEAN")
        self.assertTrue(heartbeat["broker_flat_confirmed"])
        self.assertFalse(harness.runtime_closed_with_exposure)
        store = LiveOrderStore(Path(directory) / "live-orders.sqlite")
        try:
            self.assertEqual(store.positions(), {})
        finally:
            store.close()

    def test_restored_ack_and_partial_exit_reprice_before_force_flat(self):
        for partial in (False, True):
            with self.subTest(partial=partial), TemporaryDirectory() as temporary:
                harness = RestoredExitHarness(temporary, partial_exit=partial)
                fill_times = []
                original_fill = LiveOrderStore.record_fill

                def observed_fill(store, *args, **kwargs):
                    if kwargs.get("fill_id") == "modified-exit-fill":
                        fill_times.append(harness.current)
                    return original_fill(store, *args, **kwargs)

                with patch.object(LiveOrderStore, "record_fill", observed_fill):
                    result, heartbeat = harness.run()
                self.assert_flat(temporary, harness, result, heartbeat)
                reprices = [row for event, row in harness.events if event == "EXIT_REPRICE_SENT"]
                self.assertEqual(len(reprices), 1)
                self.assertEqual(len(fill_times), 1)
                self.assertEqual(fill_times[0].strftime("%H:%M"), "13:10")
                self.assertEqual(harness.cancel_calls, 0)
                self.assertNotIn("RUNTIME_OPERATIONAL_RECOVERY", harness.criticals)

    def test_archive_initialization_failure_continues_owned_exit_only(self):
        with TemporaryDirectory() as temporary:
            harness = RuntimeHarness(temporary, minute=10)
            original_mock = harness_module.Mock

            def failing_archive_mock(*args, **kwargs):
                value = kwargs.get("return_value")
                if isinstance(value, SimpleNamespace) and getattr(value, "run_id", "") == "mock-archive":
                    return original_mock(side_effect=OSError("synthetic archive unavailable"))
                return original_mock(*args, **kwargs)

            with patch.object(harness_module, "Mock", failing_archive_mock):
                result, heartbeat = harness.run()
            self.assert_flat(temporary, harness, result, heartbeat)
            self.assertEqual(harness.session.connect_calls, 1)
            self.assertEqual(harness.exit_quantities, [1000])
            self.assertIn("RUNTIME_ARCHIVE_INITIALIZATION_FAILED", harness.criticals)

    def test_quote_metadata_write_failure_continues_owned_exit_only(self):
        with TemporaryDirectory() as temporary:
            harness = RuntimeHarness(temporary, minute=10)
            original_write = Path.write_text

            def failing_metadata_write(path, *args, **kwargs):
                if path.name == "quote_market_metadata.json":
                    raise OSError("synthetic quote metadata unavailable")
                return original_write(path, *args, **kwargs)

            with patch.object(Path, "write_text", failing_metadata_write):
                result, heartbeat = harness.run()
            self.assert_flat(temporary, harness, result, heartbeat)
            self.assertEqual(harness.exit_quantities, [1000])
            self.assertIn("RUNTIME_QUOTE_METADATA_WRITE_FAILED", harness.criticals)

    def test_async_fill_then_halt_reconciles_before_immediate_owned_exit(self):
        with TemporaryDirectory() as temporary:
            harness = CallbackFillHarness(temporary)
            original_tick = LiveDirectionEngine.record_tick
            original_reserve = LiveOrderStore.reserve

            def callback_tick(engine, symbol, **kwargs):
                outcome = original_tick(engine, symbol, **kwargs)
                if symbol == "TEST" and harness.callback_halt_at is None:
                    store = harness.session.store
                    store.record_fill(harness.entry_id, fill_id="valid-callback-fill",
                                      quantity=1000, price="100")
                    store.halt("CALLBACK_NORMALIZATION_ERROR:synthetic malformed result")
                    harness.callback_halt_at = harness.current
                return outcome

            def observed_reserve(store, intent, **kwargs):
                if intent.purpose == IntentPurpose.EXIT:
                    self.assertGreaterEqual(harness.adapters[0].reconcile_calls, 2)
                    harness.exit_times.append(harness.current)
                return original_reserve(store, intent, **kwargs)

            with patch.object(LiveDirectionEngine, "record_tick", callback_tick), patch.object(LiveOrderStore, "reserve", observed_reserve):
                result, heartbeat = harness.run()
            self.assert_flat(temporary, harness, result, heartbeat)
            self.assertEqual(len(harness.exit_times), 1)
            self.assertLess((harness.exit_times[0] - harness.callback_halt_at).total_seconds(), 5)
            self.assertIn("RUNTIME_OPERATIONAL_RECOVERY", harness.criticals)
            store = LiveOrderStore(Path(temporary) / "live-orders.sqlite")
            try:
                self.assertEqual(store.control_state()["reason"],
                                 "CALLBACK_NORMALIZATION_ERROR:synthetic malformed result")
            finally:
                store.close()

    def test_startup_publishes_bounded_phase_deadlines(self):
        with TemporaryDirectory() as temporary:
            harness = RuntimeHarness(temporary)
            beats = []
            original = runtime_main.Heartbeat.beat

            def observed_beat(heartbeat, state, **details):
                if state == "STARTING":
                    beats.append((harness.current, details))
                return original(heartbeat, state, **details)

            with patch.object(runtime_main.Heartbeat, "beat", observed_beat):
                result, heartbeat = harness.run()
            self.assert_flat(temporary, harness, result, heartbeat)
            self.assertTrue({"BROKER_CONNECT", "BROKER_RECONCILIATION", "QUOTE_SUBSCRIPTION"}
                            <= {row["startup_stage"] for _at, row in beats})
            for at, row in beats:
                deadline = datetime.fromisoformat(row["startup_deadline_at"])
                self.assertIsNotNone(deadline.tzinfo)
                self.assertTrue(0 < (deadline - at).total_seconds() <= 90)
            self.assertIsNone(harness.session.startup_progress)

    def test_native_clear_record_is_limits_not_trade_or_executable_quote(self):
        engine = LiveDirectionEngine({"TEST": "Test"})
        session = runtime_main._Session(api_types={}, environment="UAT",
            credentials={"account": "MOCK"}, engine=engine, logger=lambda *a, **k: None)
        now = datetime.now(TAIPEI)
        value = SimpleNamespace(StkCode="TEST", SerialNo=-1, BuyPrice=77, SellPrice=63,
            DealPrice=70, DealVol=1, Time=SimpleNamespace(
                bytHour=now.hour, bytMin=now.minute, bytSec=now.second, ushtMSec=0))
        session._on_response(2, 0, "SubscribeStockTick", None, value)
        self.assertEqual(len(engine._states["TEST"].ticks), 0)
        self.assertIsNone(session.last_quote_at)
        self.assertFalse(session._raw_quote_status["TEST"]["last_tick_ingest_accepted"])
        self.assertEqual(session._raw_quote_status["TEST"]["last_tick_ingest_reason"],
                         "BROKER_DAILY_PRICE_LIMITS")
        self.assertFalse(engine.exit_price_is_valid("TEST", Decimal("62.9"), now))
        self.assertTrue(engine.exit_price_is_valid("TEST", Decimal("63"), now))

    def test_real_session_login_retry_reports_bounded_startup_phases(self):
        # Exercise the real connect() loop, but never instantiate a vendor type.
        phases, instances = [], []

        class Trader:
            def __init__(self):
                self.OnResponse = EventHook()
                instances.append(self)

            def SetLogType(self, value):
                pass

            def Open(self, environment):
                pass

            def Login(self, *args):
                if len(instances) == 1:
                    return False
                self.OnResponse.emit(1, 0, "Login", None, SimpleNamespace(
                    LoginStatus=SimpleNamespace(MsgCode="0001", MsgContent="synthetic login")))
                return True

            def Close(self):
                pass

            def Dispose(self):
                pass

        session = runtime_main._Session(
            api_types={"Trader": Trader, "LogType": SimpleNamespace(NONE=0),
                       "Environment": SimpleNamespace(UAT="MOCK")},
            environment="UAT", credentials={"account": "MOCK", "pfx": "MOCK",
                "pfx_password": "MOCK", "trading_password": "MOCK"},
            engine=None, logger=lambda *a, **k: None,
        )
        session.startup_progress = lambda stage, budget: phases.append((stage, budget))
        with patch.object(runtime_main.time, "sleep"):
            session.connect()
        self.assertEqual([stage for stage, _budget in phases], [
            "BROKER_OPEN_1", "BROKER_LOGIN_1", "BROKER_RETRY_WAIT_1",
            "BROKER_OPEN_2", "BROKER_LOGIN_2",
        ])
        self.assertTrue(all(0 < budget <= 90 for _stage, budget in phases))
        session.close()

    def test_final_limit_guard_rejects_illegal_price_before_submission(self):
        now = datetime(2026, 10, 5, 10, 0, tzinfo=TAIPEI)
        engine = LiveDirectionEngine({"TEST": "Test"})
        engine.record_exit_price_limits("TEST", at=now, received_at=now,
                                       lower_limit=63, upper_limit=77)
        engine.record_exit_quote("TEST", at=now, received_at=now, bid=63, ask=63.1)
        position = ManagedPosition("TEST", "Test", "LONG", 1000, 70, "mock-entry", now)
        for price in ("62.9", "63.03", "77.1"):
            with self.subTest(price=price):
                intent = ExecutionIntent("invalid-exit", "TEST", Side.SELL, 1000,
                                         Decimal(price), purpose=IntentPurpose.EXIT)
                with self.assertRaisesRegex(RuntimeError, "EXIT_PRICE_OUTSIDE_BROKER_BOUNDS_OR_TICK_GRID"):
                    runtime_main._exit_pre_send_guard(intent, position=position, engine=engine,
                                                      quote_time=now, max_age_seconds=3, now=now)
        intent = ExecutionIntent("valid-exit", "TEST", Side.SELL, 1000,
                                 Decimal("63"), purpose=IntentPurpose.EXIT)
        runtime_main._exit_pre_send_guard(intent, position=position, engine=engine,
                                          quote_time=now, max_age_seconds=3, now=now)
