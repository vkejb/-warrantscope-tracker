"""Deterministic evidence of current exit-safety defects, using mocks only.

Passing these tests means the documented defect was reproduced. It is NOT a
safety certification. Once production fixes change the behavior, these tests
should be retired or converted to tests of the intended repaired invariant.

Some lifecycle branches are nested in _run_realtime. The helper compiles the
exact current AST branch into a one-iteration function rather than copying its
logic. This proves that branch's behavior with the stated mocked inputs only;
it does not execute startup, broker callbacks, the complete runtime loop, or an
installed LaunchAgent. All stores and lock files are in TemporaryDirectory.
"""
from __future__ import annotations

import ast
import contextlib
import copy
from datetime import datetime, timedelta
from decimal import Decimal
import io
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
from yuanta_live_runtime_v01.strategy import (
    LiveDirectionEngine, ManagedPosition, TAIPEI,
)


def current_loop_branch(condition: str):
    """Locate a direct runtime loop branch by its normalized condition."""
    source = Path(runtime_main.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    runner = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_realtime"
    )
    loop = next(
        node for node in ast.walk(runner)
        if isinstance(node, ast.While)
        and isinstance(node.test, ast.Constant) and node.test.value is True
    )
    direct = [node for node in loop.body if isinstance(node, ast.If)]
    exact = [node for node in direct if condition == ast.unparse(node.test)]
    matches = exact or [
        node for node in direct if condition in ast.unparse(node.test)
    ]
    if len(matches) != 1:
        raise AssertionError(f"audit branch locator is no longer unique: {condition}")
    return copy.deepcopy(matches[0])


def execute_branch(node, state):
    """Run one exact branch, allowing its native return/continue semantics."""
    assigned = sorted({
        value.id for value in ast.walk(node)
        if isinstance(value, ast.Name) and isinstance(value.ctx, ast.Store)
    })
    body = [ast.Global(names=assigned)] if assigned else []
    body.append(ast.For(
        target=ast.Name(id="_audit_iteration", ctx=ast.Store()),
        iter=ast.Tuple(elts=[ast.Constant(value=None)], ctx=ast.Load()),
        body=[node], orelse=[],
    ))
    function = ast.FunctionDef(
        name="_audit_step",
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
                           kw_defaults=[], defaults=[]),
        body=body, decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = dict(runtime_main.__dict__)
    namespace.update(state)
    exec(compile(module, "<current-runtime-exit-audit>", "exec"), namespace)
    namespace["_audit_step"]()
    return namespace


class ExitFailureEvidenceTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="exit-safety-audit-")
        self.directory = Path(self.temporary.name)
        self.store = LiveOrderStore(self.directory / "audit.sqlite")
        self.now = datetime(2026, 10, 5, 13, 23, tzinfo=TAIPEI)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def order(self, identity, quantity, *, exit_order=False):
        order, _created = self.store.reserve(ExecutionIntent(
            identity, "TEST", Side.SELL if exit_order else Side.BUY,
            quantity, Decimal("100"),
            purpose=IntentPurpose.EXIT if exit_order else IntentPurpose.ENTRY,
        ))
        self.store.bind_broker_order(order.client_order_id, f"mock-{identity}")
        self.store.acknowledge(order.client_order_id)
        return self.store.get(order.client_order_id)

    def fill(self, order, quantity):
        self.store.record_fill(order.client_order_id, fill_id=f"fill-{order.intent_id}",
                               quantity=quantity, price="100")

    def position(self, entry, quantity=1000):
        return ManagedPosition("TEST", "Test", "LONG", quantity, 100,
                               entry.client_order_id, self.now - timedelta(hours=4))

    def test_evidence_rejected_entry_cancel_latches_and_never_retries(self):
        entry = self.order("partial-entry", 2000)
        self.fill(entry, 1000)
        adapter = Mock()
        adapter.cancel.side_effect = lambda identity, reason: self.store.request_cancel(identity, reason)
        state = dict(
            store=self.store, entry_order_id=entry.client_order_id,
            entry_signal=SimpleNamespace(stock_id="TEST", stock_name="Test",
                                         side="LONG", entry_price=100),
            entry_submitted_at=self.now - timedelta(seconds=20),
            position=None, pending_exit_reason="SCHEDULED_FORCE_FLAT_1320",
            now=self.now, args=SimpleNamespace(entry_timeout=15, reconcile_timeout=1),
            adapter=adapter, exit_only=True, entry_cancel_requested=False,
            log=Mock(),
        )
        branch = current_loop_branch("entry_order_id is not None")
        state = execute_branch(branch, state)
        self.store.cancel_failed(entry.client_order_id, "mock broker cancel rejection")
        for seconds in range(1, 6):
            state["now"] = self.now + timedelta(seconds=seconds)
            state = execute_branch(branch, state)
        self.assertEqual(adapter.cancel.call_count, 1)
        adapter.reconcile.assert_not_called()
        self.assertTrue(state["entry_cancel_requested"])
        self.assertEqual(self.store.get(entry.client_order_id).status,
                         BrokerOrderStatus.PARTIALLY_FILLED)
        self.assertEqual(state["position"].quantity, 1000)

    def test_evidence_accepted_cancel_mutation_blocks_market_fallback_without_timeout(self):
        entry = self.order("entry", 1000)
        self.fill(entry, 1000)
        exit_order = self.order("exit", 1000, exit_order=True)
        self.store.request_cancel(exit_order.client_order_id, "13:23 fallback")
        identify = self.store.create_request(exit_order.client_order_id, "CANCEL")
        self.store.complete_request(identify, success=True)
        position = self.position(entry)
        position.exit_submitted = True
        adapter, notifier = Mock(), Mock()
        state = dict(
            store=self.store, exit_order_id=exit_order.client_order_id,
            position=position, adapter=adapter, notifier=notifier,
            market_fallback_due=True, market_cutoff_reached=False,
            last_exit_reprice=self.now - timedelta(seconds=30),
            args=SimpleNamespace(exit_reprice_seconds=5), now=self.now,
            market_fallback_cancel_requested=True, log=Mock(),
        )
        branch = current_loop_branch("exit_order_id is not None")
        for seconds in (0, 60, 180, 360):
            state["now"] = self.now + timedelta(seconds=seconds)
            state = execute_branch(branch, state)
        self.assertEqual(self.store.pending_mutation(exit_order.client_order_id)["request_status"],
                         "ACCEPTED")
        self.assertEqual(self.store.get(exit_order.client_order_id).status,
                         BrokerOrderStatus.CANCEL_PENDING)
        adapter.cancel.assert_not_called()
        adapter.reconcile.assert_not_called()
        adapter.submit_rescue.assert_not_called()
        notifier.critical.assert_not_called()

    def test_evidence_fully_filled_exit_with_remaining_exposure_raises_not_rescues(self):
        entry = self.order("entry", 2000)
        self.fill(entry, 2000)
        exit_order = self.order("small-exit", 1000, exit_order=True)
        self.fill(exit_order, 1000)
        adapter = Mock()
        adapter.position_baseline = {}
        adapter.inspect_broker_state.return_value = SimpleNamespace(
            positions={"TEST|0": 1000}, open_orders=[],
        )
        state = dict(
            store=self.store, exit_order_id=exit_order.client_order_id,
            market_fallback_due=False, adapter=adapter,
            args=SimpleNamespace(reconcile_timeout=1),
        )
        with self.assertRaisesRegex(RuntimeError, "flat not confirmed"):
            execute_branch(current_loop_branch("exit_order_id is not None"), state)
        self.assertEqual(self.store.positions(), {"TEST": 1000})
        self.assertTrue(self.store.control_state()["halted"])
        adapter.submit_rescue.assert_not_called()

    def test_evidence_fresh_book_does_not_support_1320_exit_when_trade_tick_stale(self):
        now = self.now.replace(minute=20)
        engine = LiveDirectionEngine({"TEST": "Test"})
        position = ManagedPosition("TEST", "Test", "LONG", 1000, 100,
                                   "mock-entry", now - timedelta(hours=4))
        self.assertTrue(engine.record_tick(
            "TEST", at=now - timedelta(minutes=10), price=100, bid=99.9,
            ask=100, volume=10,
        ))
        book = engine.ingest_book_combined(
            "TEST", at=now, buy_prices=[99.9, 99.8], buy_volumes=[100, 100],
            sell_prices=[100, 100.1], sell_volumes=[100, 100],
        )
        self.assertTrue(book.accepted)
        self.assertIsNone(engine.safe_exit_quote(position, now))
        self.assertIsNone(engine.evaluate_exit(position, now))
        self.assertEqual(runtime_main._force_flat_market_phase(now), "LIMIT")

    def test_evidence_locked_limit_tick_with_missing_ask_is_unusable_for_exit(self):
        engine = LiveDirectionEngine({"TEST": "Test"})
        result = engine.ingest_tick("TEST", at=self.now, price=110, bid=110,
                                    ask=0, volume=10)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "INVALID_PRICE_VOLUME_OR_SPREAD")

    def test_evidence_cutoff_warning_is_skipped_when_exit_already_submitted(self):
        entry = self.order("entry", 1000)
        self.fill(entry, 1000)
        position = self.position(entry)
        position.exit_submitted = True
        notifier = Mock()
        state = dict(position=position, next_exit_retry_at=None,
                     now=self.now.replace(minute=29, second=51),
                     market_cutoff_reached=True, market_cutoff_alerted=False,
                     notifier=notifier, store=self.store)
        branch = current_loop_branch("position is not None and (not position.exit_submitted)")
        execute_branch(branch, state)
        notifier.critical.assert_not_called()
        self.assertFalse(self.store.control_state()["halted"])

    def test_evidence_preflight_ready_requires_no_actual_market_data_callback(self):
        session = SimpleNamespace(account="MOCK_ACCOUNT", api=object(),
                                  connect=Mock(), subscribe=Mock(), close=Mock(),
                                  last_quote_at=None)
        adapter = Mock()
        adapter.reconcile.return_value = ReconciliationResult(
            "MATCH", {}, {}, {}, {}, [],
        )
        args = SimpleNamespace(runtime_dir=self.directory,
                               baseline=self.directory / "baseline.json",
                               vendor_dir=self.directory / "vendor",
                               reconcile_timeout=1)
        items = [SimpleNamespace(stock_id="TEST", stock_name="Test", market="TWSE")]
        output = io.StringIO()
        with patch.object(runtime_main, "_validate_watchlist_day"), \
             patch.object(runtime_main, "load_stage_a_watchlist",
                          return_value=({"signal_date": "20261002"}, items, {})), \
             patch.object(runtime_main, "load_credentials", return_value={}), \
             patch.object(runtime_main, "load_api_types", return_value={}), \
             patch.object(runtime_main, "_extend_quote_types", return_value={}), \
             patch.object(runtime_main, "_baseline_is_current", return_value=True), \
             patch.object(runtime_main, "_Session", return_value=session), \
             patch.object(runtime_main, "YuantaSparkExecutionAdapter", return_value=adapter), \
             contextlib.redirect_stdout(output):
            result = runtime_main._preflight(args, "UAT")
        self.assertEqual(result, 0)
        self.assertIn('"status": "READY"', output.getvalue())
        self.assertIsNone(session.last_quote_at)
        session.subscribe.assert_called_once()
