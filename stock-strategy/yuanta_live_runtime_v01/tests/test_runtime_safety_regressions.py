"""Safety regressions with full runtime loops and isolated mocked broker state.

No vendor SDK, credentials, network, installed process, real runtime, or actual
LIVE environment is used. All runtime/account locks and SQLite stores are temp.
"""
from contextlib import ExitStack, redirect_stdout
from datetime import datetime as RealDateTime, timedelta, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from yuanta_broker_execution_v01 import (
    BrokerOrderStatus, ExecutionIntent, IntentPurpose, LiveOrderStore,
    ReconciliationResult, Side,
)
from yuanta_live_runtime_v01 import main as runtime_main
from yuanta_live_runtime_v01.strategy import LiveDirectionEngine, ManagedPosition, SPEC, TAIPEI


class RuntimeHarness:
    """Run the actual controller against a deterministic in-memory broker."""
    def __init__(self, directory, *, minute=20, partial=False, late_fill=False,
                 reject_cancel=False, lost_cancel=False, transient_query=False,
                 poison_query=False, recovery=False, quotes=True,
                 held_quote_outage=False, notification_init_failure=False,
                 subscription_failure=False, observer_failure=None,
                 notification_critical_failure=False, corrupt_checkpoint=False,
                 broker_mismatch=False):
        self.directory = Path(directory)
        self.current = RealDateTime.now(TAIPEI).replace(hour=13, minute=minute, second=0, microsecond=0)
        self.partial, self.late_fill = partial, late_fill
        self.reject_cancel, self.lost_cancel = reject_cancel, lost_cancel
        self.transient_query, self.poison_query = transient_query, poison_query
        self.quotes, self.recovery = quotes, recovery
        self.held_quote_outage = held_quote_outage
        self.notification_init_failure = notification_init_failure
        self.subscription_failure = subscription_failure
        self.observer_failure = observer_failure
        self.notification_critical_failure = notification_critical_failure
        self.corrupt_checkpoint = corrupt_checkpoint
        self.broker_mismatch = broker_mismatch
        self.initial_feed_completed = False
        self.events, self.criticals, self.adapters = [], [], []
        self.cancel_calls = 0
        self.exit_quantities = []
        self.exit_price_types = []
        self.session = None
        self.runtime_closed_with_exposure = False
        self.baseline = self.directory / "position_baseline.json"
        runtime_main._write_baseline(self.baseline, {})
        runtime_main._write_baseline_metadata(self.baseline, account="MOCK_SAFETY_ACCOUNT",
                                              captured_at=self.current.isoformat())
        self.entry_quantity = 2000 if partial or late_fill else 1000

    def prepare(self):
        store = LiveOrderStore(self.directory / "live-orders.sqlite")
        try:
            # Recovery must use a past entry timestamp, not the wall clock of
            # the machine running this deterministic market-clock fixture.
            with patch("yuanta_broker_execution_v01.store.utc_now",
                       return_value=(self.current - timedelta(minutes=10)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")):
                order, _ = store.reserve(ExecutionIntent("mock-entry", "TEST", Side.BUY,
                                                        self.entry_quantity, Decimal("100")))
            store.bind_broker_order(order.client_order_id, "MOCK_ENTRY")
            store.acknowledge(order.client_order_id)
            store.record_fill(order.client_order_id, fill_id="entry-first", quantity=1000, price="100")
            position = ManagedPosition("TEST", "Test", "LONG", 1000, 100,
                                       order.client_order_id, self.current - timedelta(minutes=10))
            runtime_main._checkpoint_position(store, position, None)
            if self.corrupt_checkpoint:
                checkpoint = store.position_checkpoint(order.client_order_id)
                checkpoint["mfe_r"] = "nan"
                store.save_position_checkpoint(order.client_order_id, checkpoint)
            self.entry_id = order.client_order_id
            if self.lost_cancel:
                out, _ = store.reserve(ExecutionIntent("old-exit", "TEST", Side.SELL, 1000,
                                                       Decimal("100"), purpose=IntentPurpose.EXIT))
                store.bind_broker_order(out.client_order_id, "MOCK_OLD_EXIT")
                store.acknowledge(out.client_order_id)
                store.request_cancel(out.client_order_id, "fallback")
                identify = store.create_request(out.client_order_id, "CANCEL")
                store.complete_request(identify, success=True)
                self.old_exit_id = out.client_order_id
            if self.recovery:
                (self.directory / "FORCE_FLAT_REQUEST").write_text("mock scheduled request\n")
                (self.directory / "STOP_REQUEST").write_text("mock human stop\n")
                (self.directory / "quote_market_metadata.json").write_text(json.dumps({
                    "TEST": {"stock_name": "Test", "market": "TWSE"},
                }))
        finally:
            store.close()

    def sleep(self, seconds):
        if self.held_quote_outage:
            seconds = max(seconds, 1)
        self.current += timedelta(seconds=seconds)
        if self.current.minute >= 29:
            raise AssertionError("mock runtime failed to flatten within the deterministic test window")
        if self.session and self.quotes:
            self.session.feed()

    def run(self):
        self.prepare()
        harness = self
        class Clock(RealDateTime):
            @classmethod
            def now(cls, tz=None):
                return harness.current if tz is None else harness.current.astimezone(tz)

        class Session:
            def __init__(self, **kwargs):
                self.account = "MOCK_SAFETY_ACCOUNT"
                self.engine = kwargs["engine"]
                self.api = object()
                self.last_quote_at = harness.current
                self.quote_started_at = harness.current
                self.items = []
                self.connect_calls = 0
                harness.session = self

            def connect(self):
                self.connect_calls += 1
                self.api = object()

            def feed(self):
                for item in self.items:
                    if harness.held_quote_outage and harness.initial_feed_completed and item.stock_id == "TEST":
                        continue
                    self.engine.record_tick(item.stock_id, at=harness.current,
                                            price=100, bid=99.9, ask=100, volume=1)
                    self.engine.record_book_combined(item.stock_id, at=harness.current,
                        buy_prices=[99.9], buy_volumes=[100],
                        sell_prices=[100], sell_volumes=[100])
                self.last_quote_at = harness.current
                harness.initial_feed_completed = True

            def subscribe(self, items):
                self.items = list(items)
                self.quote_started_at = harness.current
                if harness.subscription_failure:
                    raise ConnectionError("mock quote subscription unavailable")
                if harness.quotes:
                    self.feed()

            def archive_decision_evidence(self, *args, **kwargs):
                if harness.observer_failure == "archive":
                    raise OSError("mock isolated archive write failure")

            def close(self):
                if hasattr(self, "store") and self.store.positions():
                    harness.runtime_closed_with_exposure = True
                self.api = None

        class Adapter:
            def __init__(self, **kwargs):
                self.store = kwargs["store"]
                self.position_baseline = kwargs["position_baseline"]
                self.reconcile_calls = 0
                self._query_uncertain = False
                harness.adapters.append(self)
                harness.session.store = self.store

            def reconcile(self, **kwargs):
                self.reconcile_calls += 1
                if harness.broker_mismatch:
                    self.store.halt("POSITION_MISMATCH")
                    raise RuntimeError("mock broker/local position mismatch")
                if len(harness.adapters) == 1 and harness.transient_query and self.reconcile_calls == 2:
                    self._query_uncertain = harness.poison_query
                    raise TimeoutError("mock snapshot timeout")
                if harness.lost_cancel and self.reconcile_calls >= 2:
                    self.store.finalize_latest_request(harness.old_exit_id, "CANCEL", success=True)
                    self.store.canceled(harness.old_exit_id)
                positions = self.store.position_buckets()
                return ReconciliationResult("MATCH", positions, {}, positions, positions, [])

            def inspect_broker_state(self, **kwargs):
                return SimpleNamespace(positions=self.store.position_buckets(),
                                       open_orders=self.store.orders(open_only=True))

            def cancel(self, identity, reason, *, emergency=False):
                harness.cancel_calls += 1
                self.store.request_cancel(identity, reason)
                request = self.store.create_request(identity, "CANCEL")
                if harness.reject_cancel and harness.cancel_calls == 1:
                    self.store.complete_request(request, success=False)
                    self.store.cancel_failed(identity, "mock cancellation rejected")
                else:
                    if harness.late_fill:
                        self.store.record_fill(identity, fill_id="entry-late", quantity=1000, price="101")
                        harness.late_fill = False
                    self.store.complete_request(request, success=True)
                    self.store.finalize_latest_request(identity, "CANCEL", success=True)
                    self.store.canceled(identity)

            def submit(self, intent, **kwargs):
                if intent.purpose != IntentPurpose.EXIT:
                    raise AssertionError("test must never submit a new entry")
                return self.submit_rescue(intent, **kwargs)

            def submit_rescue(self, intent, **kwargs):
                guard = kwargs.get("pre_send_guard")
                if guard is not None:
                    guard(intent)
                if intent.purpose != IntentPurpose.EXIT:
                    raise AssertionError("test recovery attempted a new entry")
                remaining = self.store.positions().get(intent.symbol, 0)
                if not 0 < intent.quantity <= remaining:
                    raise AssertionError("mock exit would exceed broker-owned remaining exposure")
                order, _ = self.store.reserve(intent, allow_halted=True)
                self.store.bind_broker_order(order.client_order_id, f"MOCK_EXIT_{len(harness.exit_quantities)}")
                self.store.acknowledge(order.client_order_id)
                harness.exit_quantities.append(intent.quantity)
                harness.exit_price_types.append(intent.price_type.value)
                self.store.record_fill(order.client_order_id,
                    fill_id=f"exit-fill-{len(harness.exit_quantities)}", quantity=intent.quantity, price="99.8")
                return self.store.get(order.client_order_id)

            def close(self):
                pass

        class Notifier:
            def __init__(self, *args, **kwargs):
                pass
            def critical(self, code, message, **kwargs):
                harness.criticals.append(code)
                if harness.notification_critical_failure:
                    raise OSError("mock critical notification unavailable")
            def emit(self, event, row):
                harness.events.append((event, row))
                if harness.observer_failure == "notifier":
                    raise OSError("mock isolated notification write failure")
                if event == "LIVE_TRADE_COMPLETE_CONTINUE_ARCHIVE":
                    harness.current = harness.current.replace(minute=25)
            def close(self, **kwargs):
                pass

        args = runtime_main.parse_args(["start-uat", "--live", "--runtime-dir", str(self.directory),
                                       "--baseline", str(self.baseline), "--vendor-dir", str(self.directory)])
        args.account_lock_root = self.directory / "account-locks"
        args.recover_force_flat = self.recovery
        gate = SimpleNamespace(authorized=True, public_snapshot=lambda: {"test_mock_gate": True})
        items = [SimpleNamespace(stock_id="TEST", stock_name="Test", market="TWSE")]
        if self.held_quote_outage:
            items.append(SimpleNamespace(stock_id="OTHER", stock_name="Other stock", market="TWSE"))
        with ExitStack() as stack:
            for name, value in (
                ("datetime", Clock), ("_validate_watchlist_day", Mock(side_effect=AssertionError("must not validate research") if self.recovery else None)),
                ("load_stage_a_watchlist", Mock(side_effect=AssertionError("must not load research") if self.recovery else None,
                                               return_value=({"signal_date": "20261002"}, items, {}))),
                ("load_credentials", Mock(return_value={"account": "MOCK_SAFETY_ACCOUNT"})),
                ("load_api_types", Mock(return_value={})), ("_extend_quote_types", Mock(return_value={})),
                ("_Session", Session), ("YuantaSparkExecutionAdapter", Adapter),
                ("RuntimeNotifier", Notifier), ("AsyncTradingNotifier", Mock(side_effect=OSError("mock outbox unavailable")) if self.notification_init_failure else Notifier),
                ("AppendOnlyRun", Mock(return_value=SimpleNamespace(
                    run_id="mock-archive", run_dir=self.directory, mode="MOCK",
                    finalize=lambda **kwargs: {"event_counts": {}, "actual_orders": 0,
                                              "actual_fills": 0, "broker_order_calls": 0}))),
            ):
                stack.enter_context(patch.object(runtime_main, name, value))
            stack.enter_context(patch.object(runtime_main.LiveTradingGate, "from_environment", return_value=gate))
            stack.enter_context(patch.object(runtime_main.time, "sleep", side_effect=self.sleep))
            stack.enter_context(patch.object(runtime_main.signal, "signal"))
            if self.observer_failure == "heartbeat":
                original_beat = runtime_main.Heartbeat.beat
                count = 0
                def failing_beat(heartbeat, state, **details):
                    nonlocal count
                    count += 1
                    if count >= 3:
                        raise OSError("mock isolated heartbeat write failure")
                    return original_beat(heartbeat, state, **details)
                stack.enter_context(patch.object(runtime_main.Heartbeat, "beat", failing_beat))
            elif self.observer_failure == "log":
                stack.enter_context(patch.object(runtime_main, "_append_jsonl",
                                                 side_effect=OSError("mock isolated log write failure")))
            stack.enter_context(redirect_stdout(io.StringIO()))
            result = runtime_main._run_realtime(args, environment="UAT", submit_live=True)
        heartbeat = json.loads((self.directory / "heartbeat.json").read_text())
        return result, heartbeat


class FullRuntimeSafetyTests(TestCase):
    def run_case(self, **kwargs):
        with TemporaryDirectory(prefix="full-runtime-safety-") as temporary:
            harness = RuntimeHarness(temporary, **kwargs)
            result, heartbeat = harness.run()
            store = LiveOrderStore(Path(temporary) / "live-orders.sqlite")
            try:
                positions, halted = store.positions(), store.control_state()["halted"]
            finally:
                store.close()
            self.assertEqual(result, 0)
            self.assertEqual(positions, {})
            self.assertEqual(heartbeat["state"], "STOPPED_CLEAN")
            self.assertTrue(heartbeat["broker_flat_confirmed"])
            return harness, heartbeat, halted

    def test_primary_1320_force_flat_then_authoritative_flat_proof(self):
        harness, _heartbeat, _halted = self.run_case()
        self.assertEqual(harness.exit_quantities, [1000])
        self.assertFalse(harness.runtime_closed_with_exposure)

    def test_rejected_entry_cancel_retries_and_late_fill_remaining_is_rescued(self):
        harness, _heartbeat, _halted = self.run_case(partial=True, reject_cancel=True, late_fill=True)
        self.assertEqual(harness.cancel_calls, 2)
        self.assertEqual(harness.exit_quantities, [1000, 1000])
        self.assertIn("FILLED_EXIT_REMAINING_EXPOSURE", [event for event, _ in harness.events])

    def test_accepted_lost_cancel_report_reconciles_before_market_rescue(self):
        harness, _heartbeat, _halted = self.run_case(minute=23, lost_cancel=True)
        self.assertEqual(harness.exit_quantities, [1000])
        self.assertEqual(harness.exit_price_types, ["MARKET"])

    def test_transient_query_error_preserves_controller_until_owned_exit(self):
        harness, _heartbeat, halted = self.run_case(minute=10, transient_query=True)
        self.assertTrue(halted)
        self.assertIn("RUNTIME_OPERATIONAL_RECOVERY", harness.criticals)
        self.assertFalse(harness.runtime_closed_with_exposure)

    def test_poisoned_snapshot_requires_fresh_adapter_before_owned_exit(self):
        harness, _heartbeat, halted = self.run_case(minute=10, transient_query=True, poison_query=True)
        self.assertTrue(halted)
        self.assertEqual(len(harness.adapters), 2)
        self.assertEqual(harness.session.connect_calls, 2)

    def test_held_symbol_outage_is_detected_while_other_stocks_keep_streaming(self):
        harness, _heartbeat, halted = self.run_case(minute=19, held_quote_outage=True)
        self.assertTrue(halted)
        self.assertIn("HELD_SYMBOL_QUOTE_STALE", harness.criticals)
        self.assertEqual(harness.exit_price_types, ["MARKET"])

    def test_notification_outbox_constructor_failure_does_not_abort_owned_recovery(self):
        harness, _heartbeat, _halted = self.run_case(minute=23, notification_init_failure=True)
        self.assertIn("TRADING_NOTIFICATION_INITIALIZATION_FAILED", harness.criticals)
        self.assertEqual(harness.exit_price_types, ["MARKET"])

    def test_outbox_and_initial_critical_failure_still_reaches_owned_recovery(self):
        harness, _heartbeat, _halted = self.run_case(minute=23, notification_init_failure=True,
                                                   notification_critical_failure=True)
        self.assertEqual(harness.exit_price_types, ["MARKET"])

    def test_failed_quote_subscription_with_existing_exposure_keeps_market_recovery(self):
        harness, _heartbeat, _halted = self.run_case(minute=23, quotes=False, subscription_failure=True)
        self.assertIn("EXIT_ONLY_QUOTE_SUBSCRIPTION_FAILED", harness.criticals)
        self.assertEqual(harness.exit_price_types, ["MARKET"])

    def test_persistent_observer_failures_do_not_block_owned_force_flat(self):
        for observer in ("heartbeat", "log", "archive", "notifier"):
            with self.subTest(observer=observer), TemporaryDirectory(prefix="observer-exit-") as temporary:
                harness = RuntimeHarness(temporary, minute=23, observer_failure=observer)
                result, heartbeat = harness.run()
                store = LiveOrderStore(Path(temporary) / "live-orders.sqlite")
                try:
                    self.assertEqual(store.positions(), {})
                    self.assertTrue(store.control_state()["halted"])
                finally:
                    store.close()
                self.assertEqual(result, 0)
                self.assertEqual(harness.exit_quantities, [1000])
                self.assertEqual(harness.exit_price_types, ["MARKET"])
                self.assertFalse(harness.runtime_closed_with_exposure)
                if observer == "heartbeat":
                    # No fabricated fresh heartbeat or flat proof on failed IO.
                    self.assertNotEqual(heartbeat["state"], "STOPPED_CLEAN")
                    self.assertNotIn("broker_flat_confirmed_at", heartbeat)

    def test_scheduled_recovery_ignores_missing_research_and_preserves_human_stop(self):
        with TemporaryDirectory(prefix="recovery-prerequisite-") as temporary:
            harness = RuntimeHarness(temporary, minute=23, recovery=True, quotes=False)
            # The recovery path must never request a research seal/calendar.
            with patch.object(runtime_main, "load_stage_a_watchlist", side_effect=AssertionError("must not load research")):
                result, heartbeat = harness.run()
            self.assertEqual(result, 0)
            self.assertTrue(heartbeat["broker_flat_confirmed"])
            self.assertTrue((Path(temporary) / "STOP_REQUEST").exists())
            self.assertFalse((Path(temporary) / "FORCE_FLAT_REQUEST").exists())
            self.assertIn("TEST", {item.stock_id for item in harness.session.items})

    def test_corrupt_strategy_checkpoint_halts_but_cannot_block_owned_exit_recovery(self):
        harness, _heartbeat, halted = self.run_case(minute=23, recovery=True,
                                                   corrupt_checkpoint=True)
        self.assertTrue(halted)
        self.assertEqual(harness.exit_quantities, [1000])
        recovered = next(row for event, row in harness.events if event == "RUNTIME_STATE_RECOVERED")
        self.assertEqual(recovered["state"]["pending_exit_reason"], "INVALID_POSITION_CHECKPOINT")

    def test_corrupt_strategy_checkpoint_normal_start_remains_fail_closed(self):
        with TemporaryDirectory(prefix="corrupt-checkpoint-normal-") as temporary:
            harness = RuntimeHarness(temporary, minute=23, corrupt_checkpoint=True)
            with self.assertRaisesRegex(RuntimeError, "position checkpoint is invalid"):
                harness.run()
            self.assertEqual(harness.exit_quantities, [])
            store = LiveOrderStore(Path(temporary) / "live-orders.sqlite")
            try:
                self.assertTrue(store.control_state()["halted"])
                self.assertEqual(store.positions(), {"TEST": 1000})
            finally:
                store.close()

    def test_corrupt_checkpoint_cannot_bypass_broker_ownership_mismatch(self):
        with TemporaryDirectory(prefix="corrupt-checkpoint-mismatch-") as temporary:
            harness = RuntimeHarness(temporary, minute=23, recovery=True,
                                     corrupt_checkpoint=True, broker_mismatch=True)
            with self.assertRaisesRegex(RuntimeError, "broker/local position mismatch"):
                harness.run()
            self.assertEqual(harness.exit_quantities, [])


class AdmissionAndExitQuoteSafetyTests(TestCase):
    def setUp(self):
        self.now = RealDateTime(2026, 10, 5, 13, 5, tzinfo=TAIPEI)
        self.engine = LiveDirectionEngine({"TEST": "Test"})
        self.position = ManagedPosition("TEST", "Test", "LONG", 1000, 100, "entry", self.now)

    def test_current_single_bid_can_exit_long_without_enabling_entry(self):
        self.engine.record_exit_quote("TEST", at=self.now, received_at=self.now, bid=110, ask=0)
        self.assertIsNotNone(self.engine.safe_exit_quote(self.position, self.now))
        self.assertFalse(self.engine.record_tick("TEST", at=self.now, price=110, volume=1, bid=110, ask=0))

    def test_absent_new_bid_does_not_resurrect_older_valid_trade_quote(self):
        self.engine.record_tick("TEST", at=self.now, price=100, volume=1, bid=99.9, ask=100)
        stamp = self.now + timedelta(seconds=1)
        self.engine.record_exit_quote("TEST", at=stamp, received_at=stamp, bid=0, ask=100)
        self.assertIsNone(self.engine.safe_exit_quote(self.position, stamp))

    def test_sentinel_new_ask_does_not_resurrect_older_short_quote(self):
        self.position.side = "SHORT"
        self.engine.record_tick("TEST", at=self.now, price=100, volume=1, bid=99.9, ask=100)
        stamp = self.now + timedelta(seconds=1)
        self.engine.record_exit_quote("TEST", at=stamp, received_at=stamp, bid=100, ask=99999.9999)
        self.assertIsNone(self.engine.safe_exit_quote(self.position, stamp))

    def test_recovered_symbol_is_monitor_only(self):
        self.engine.add_monitor_symbol("OLD", "Old position")
        self.assertIn("OLD", self.engine._states)
        self.assertNotIn("OLD", self.engine._candidate_symbols)

    def test_entry_final_guard_blocks_stale_cutoff_and_stop(self):
        candidate = SimpleNamespace(stock_id="TEST", decision_time=self.now)
        self.engine.record_tick("TEST", at=self.now, price=100, volume=1, bid=99.9, ask=100)
        self.engine.record_book_combined("TEST", at=self.now, buy_prices=[99.9],
                                        buy_volumes=[100], sell_prices=[100], sell_volumes=[100])
        with TemporaryDirectory(prefix="entry-guard-") as temporary:
            runtime = Path(temporary)
            store = Mock()
            store.control_state.return_value = {"halted": False}
            kwargs = dict(candidate=candidate, engine=self.engine, store=store,
                          runtime_dir=runtime, max_age_seconds=5)
            runtime_main._entry_pre_send_guard(**kwargs, now=self.now)
            with self.assertRaisesRegex(RuntimeError, "QUOTE_STALE"):
                runtime_main._entry_pre_send_guard(**kwargs, now=self.now + timedelta(seconds=6))
            with self.assertRaisesRegex(RuntimeError, "WINDOW_CLOSED"):
                runtime_main._entry_pre_send_guard(**kwargs, now=self.now.replace(minute=11))
            (runtime / "FORCE_FLAT_REQUEST").write_text("mock stop")
            with self.assertRaisesRegex(RuntimeError, "STOP_REQUESTED"):
                runtime_main._entry_pre_send_guard(**kwargs, now=self.now)

    def test_entry_guard_rejects_old_book_with_fresh_trade(self):
        old = self.now - timedelta(seconds=10)
        self.engine.record_book_combined("TEST", at=old, buy_prices=[99.9],
                                        buy_volumes=[100], sell_prices=[100], sell_volumes=[100])
        self.engine.record_tick("TEST", at=self.now, price=100, volume=1, bid=99.9, ask=100)
        with TemporaryDirectory(prefix="book-guard-") as temporary:
            store = Mock()
            store.control_state.return_value = {"halted": False}
            with self.assertRaisesRegex(RuntimeError, "BOOK_STALE"):
                runtime_main._entry_pre_send_guard(
                    candidate=SimpleNamespace(stock_id="TEST", decision_time=self.now),
                    engine=self.engine, store=store, runtime_dir=Path(temporary),
                    max_age_seconds=5, now=self.now,
                )

    def test_exit_final_guard_rejects_delayed_stale_quote_and_market_cutoff(self):
        quote_time = self.now
        self.engine.record_tick("TEST", at=quote_time, price=100, volume=1, bid=99.9, ask=100)
        intent = ExecutionIntent("guard-exit", "TEST", Side.SELL, 1000, Decimal("99.8"),
                                 purpose=IntentPurpose.EXIT)
        kwargs = dict(position=self.position, engine=self.engine, quote_time=quote_time,
                      max_age_seconds=3)
        runtime_main._exit_pre_send_guard(intent, **kwargs, now=quote_time)
        with self.assertRaisesRegex(RuntimeError, "CAPTURED_QUOTE_STALE"):
            runtime_main._exit_pre_send_guard(intent, **kwargs, now=quote_time + timedelta(seconds=4))
        with self.assertRaisesRegex(RuntimeError, "CUTOFF"):
            runtime_main._exit_pre_send_guard(intent, **kwargs, now=quote_time.replace(minute=29, second=50))
        market = runtime_main._as_market_fallback(intent)
        runtime_main._exit_pre_send_guard(market, **kwargs, now=quote_time.replace(minute=23))
        with self.assertRaisesRegex(RuntimeError, "NOT_DUE"):
            runtime_main._exit_pre_send_guard(market, **kwargs, now=quote_time)

    def test_intervals_reject_nan_infinity_zero_and_negative(self):
        for name in ("quote_readiness_timeout", "order_reconcile_seconds"):
            for value in (float("nan"), float("inf"), 0, -1):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    runtime_main._validate_runtime_intervals(SimpleNamespace(**{name: value}))

    def test_readiness_requires_real_candidate_and_benchmark_tick_and_book(self):
        engine = LiveDirectionEngine({"TEST": "Test", "0050": "ETF"},
                                     candidate_symbols={"TEST"}, benchmark_symbol="0050")
        for symbol in ("TEST", "0050"):
            engine.record_tick(symbol, at=self.now, price=100, volume=1, bid=99.9, ask=100)
            engine.record_book_combined(symbol, at=self.now, buy_prices=[99.9], buy_volumes=[100],
                                        sell_prices=[100], sell_volumes=[100])
        items = [SimpleNamespace(stock_id=symbol) for symbol in ("TEST", "0050")]
        with patch.object(runtime_main, "datetime") as clock:
            clock.now.return_value = self.now
            ready = runtime_main._wait_for_quote_readiness(engine, items, timeout_seconds=.001)
        self.assertEqual(ready["candidate_ready_count"], 1)
        self.assertEqual(set(ready["ready_symbols"]), {"TEST", "0050"})
