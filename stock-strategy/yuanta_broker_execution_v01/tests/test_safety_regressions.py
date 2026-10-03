"""Mock regressions for broker-proven exits and durable callback recovery.

No SDK, credentials, real session, network, or application runtime data is used.
"""

from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from yuanta_broker_execution_v01 import (
    BrokerAdapterError, BrokerOrderStatus, ExecutionIntent, IntentPurpose,
    LiveOrderStore, LiveTradingGate, ReconciliationMismatch, Side,
    StockOrderType, YuantaSparkExecutionAdapter,
)
from yuanta_broker_execution_v01.tests.test_adapter import FakeApi, Obj, api_types


ACCOUNT = "S00000000000"


class BrokerSafetyRegressions(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        blocked = RuntimeError("MOCK_ONLY: SDK/session/credential/network blocked")
        for target in ("yuanta_broker_execution_v01.sdk.load_api_types", "yuanta_broker_execution_v01.load_api_types", "socket.socket", "socket.create_connection", "subprocess.run", "subprocess.Popen"):
            stack.enter_context(patch(target, side_effect=blocked))
        directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="broker-safety-mock-"))
        self.store = stack.enter_context(LiveOrderStore(Path(directory) / "orders.sqlite"))
        self.api = FakeApi()
        self.adapter = stack.enter_context(YuantaSparkExecutionAdapter(
            api=self.api, api_types=api_types(), account=ACCOUNT, store=self.store,
            live_gate=LiveTradingGate.from_environment(cli_live=True, environ={"EXECUTION_MODE": "LIVE", "ENABLE_LIVE_TRADING": "YES"}),
        ))
        self.adapter.reconcile(timeout=1)

    def intent(self, name, **changes):
        defaults = dict(intent_id=f"MOCK-{name}", symbol="3605", side=Side.BUY, quantity=1000, price=Decimal("100"))
        defaults.update(changes)
        return ExecutionIntent(**defaults)

    def remote(self, order, **changes):
        defaults = dict(Account=ACCOUNT, RptType=1, OrderNo=order.broker_order_no or "MOCK-ORDER", CompanyNo=order.symbol, BS="B" if order.side == Side.BUY else "S", Price="100", OrderQty=order.quantity, OkQty=order.filled_quantity, OrderStatus=20, LastOrderStatus=8 if order.filled_quantity == order.quantity else 0, BasketNo=order.basket_no)
        defaults.update(changes)
        return defaults

    def filled_entry(self, quantity=1000):
        order = self.adapter.submit(self.intent("entry", quantity=quantity))
        self.store.bind_broker_order(order.client_order_id, "MOCK-ENTRY")
        self.store.record_fill(order.client_order_id, fill_id="MOCK-ENTRY:1", quantity=quantity, price="100", broker_order_no="MOCK-ENTRY", seq_no="1")
        order = self.store.get(order.client_order_id)
        self.api.merge_rows = [self.remote(order)]
        self.api.positions = {"3605": quantity}
        self.api.sent.clear()
        return order

    def normalized(self, order, **changes):
        result = dict(account=ACCOUNT, order_no=order.broker_order_no or "", symbol=order.symbol, side="B" if order.side == Side.BUY else "S", basket_no=order.basket_no, rpt_type=50, order_status=18)
        result.update(changes)
        return result

    def test_normal_exit_cannot_open_position_from_flat(self):
        with self.assertRaisesRegex(BrokerAdapterError, "reduce an existing"):
            self.adapter.submit(self.intent("flat-exit", side=Side.SELL, purpose=IntentPurpose.EXIT))
        self.assertEqual(self.api.sent, [])
        stored = self.store.get_by_intent("MOCK-flat-exit")
        self.assertEqual(stored.status, BrokerOrderStatus.REJECTED)
        self.assertEqual(stored.identify, 0)

    def test_normal_exit_cannot_sell_baseline_inventory(self):
        self.adapter.position_baseline = {"3605|0": 1000}
        self.api.positions = {"3605": 1000}
        with self.assertRaisesRegex(BrokerAdapterError, "reduce an existing"):
            self.adapter.submit(self.intent("baseline-exit", side=Side.SELL, purpose=IntentPurpose.EXIT))
        self.assertEqual(self.api.sent, [])

    def test_normal_exit_quantity_cannot_exceed_broker_proven_exposure(self):
        self.filled_entry()
        with self.assertRaisesRegex(BrokerAdapterError, "exceeds reconciled"):
            self.adapter.submit(self.intent("over-exit", side=Side.SELL, quantity=2000, purpose=IntentPurpose.EXIT))
        self.assertEqual(self.api.sent, [])

    def test_normal_exit_wrong_direction_is_rejected(self):
        self.filled_entry()
        with self.assertRaisesRegex(BrokerAdapterError, "reduce an existing"):
            self.adapter.submit(self.intent("wrong-direction", side=Side.BUY, purpose=IntentPurpose.EXIT))
        self.assertEqual(self.api.sent, [])

    def test_normal_exit_does_not_net_financing_categories(self):
        self.filled_entry()
        with self.assertRaisesRegex(BrokerAdapterError, "reduce an existing"):
            self.adapter.submit(self.intent("wrong-category", side=Side.SELL, order_type=StockOrderType.MARGIN_BUY, purpose=IntentPurpose.EXIT))
        self.assertEqual(self.api.sent, [])

    def test_normal_exit_is_rejected_after_manual_inventory_change(self):
        self.filled_entry(2000)
        self.api.positions = {"3605": 1000}
        with self.assertRaises(ReconciliationMismatch):
            self.adapter.submit(self.intent("manual-change-exit", side=Side.SELL, quantity=2000, purpose=IntentPurpose.EXIT))
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.store.get_by_intent("MOCK-manual-change-exit").status, BrokerOrderStatus.REJECTED)

    def test_normal_exit_blocks_second_exit_while_first_is_open(self):
        self.filled_entry()
        first = self.adapter.submit(self.intent("exit-one", side=Side.SELL, purpose=IntentPurpose.EXIT))
        self.store.bind_broker_order(first.client_order_id, "MOCK-EXIT")
        first = self.store.get(first.client_order_id)
        self.api.merge_rows.append(self.remote(first))
        with self.assertRaisesRegex(BrokerAdapterError, "open broker order"):
            self.adapter.submit(self.intent("exit-two", side=Side.SELL, purpose=IntentPurpose.EXIT))
        self.assertEqual(len(self.api.sent), 1)

    def test_guard_runs_after_authoritative_reconcile_and_rejects_unsent(self):
        calls = []
        def guard(intent):
            calls.append((intent.intent_id, self.adapter._latest_positions))
            raise ValueError("MOCK quote became stale while reconciling")
        result = self.adapter.submit(self.intent("guard-failure"), pre_send_guard=guard)
        self.assertEqual(calls, [("MOCK-guard-failure", {})])
        self.assertEqual(result.status, BrokerOrderStatus.REJECTED)
        self.assertTrue(result.last_error.startswith("UNSENT_PRE_SEND_GUARD:"))
        self.assertGreater(result.identify, 0)
        self.assertEqual(self.store.get_request(result.identify)["request_status"], "UNSENT_REJECTED")
        self.assertEqual(self.api.sent, [])
        repeated = self.adapter.submit(self.intent("guard-failure"), pre_send_guard=lambda _: True)
        self.assertEqual(repeated.client_order_id, result.client_order_id)
        self.assertEqual(self.api.sent, [])

    def test_false_guard_does_not_send(self):
        result = self.adapter.submit(self.intent("false-guard"), pre_send_guard=lambda _: False)
        self.assertEqual(result.status, BrokerOrderStatus.REJECTED)
        self.assertEqual(self.api.sent, [])

    def test_halt_during_guard_prevents_entry(self):
        def guard(_intent):
            self.store.halt("MOCK emergency during reconciliation")
            return True
        result = self.adapter.submit(self.intent("late-halt"), pre_send_guard=guard)
        self.assertEqual(result.status, BrokerOrderStatus.REJECTED)
        self.assertEqual(self.api.sent, [])

    def test_final_send_boundary_rechecks_halt(self):
        original = self.adapter._stock_list
        def halt_then_build(stock):
            self.store.halt("MOCK halt at final send boundary")
            return original(stock)
        with patch.object(self.adapter, "_stock_list", side_effect=halt_then_build):
            result = self.adapter.submit(self.intent("send-boundary"))
        self.assertEqual(result.status, BrokerOrderStatus.REJECTED)
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.store.pending_requests(), [])

    def test_preorder_query_failure_leaves_no_uncancelable_reserved_order(self):
        self.api.positions = {"2330": 1000}
        with self.assertRaises(ReconciliationMismatch):
            self.adapter.submit(self.intent("preorder-failure"))
        order = self.store.get_by_intent("MOCK-preorder-failure")
        self.assertEqual(order.status, BrokerOrderStatus.REJECTED)
        self.assertEqual(order.identify, 0)
        self.assertTrue(order.last_error.startswith("UNSENT_PREORDER_RECONCILE:"))
        self.assertEqual(self.api.sent, [])

    def test_report_with_conflicting_basket_cannot_fall_back_to_owned_number(self):
        order = self.adapter.submit(self.intent("owned"))
        self.store.bind_broker_order(order.client_order_id, "MOCK-OWNED")
        order = self.store.get(order.client_order_id)
        self.adapter._apply_real_report(self.normalized(order, basket_no="MOCK-UNOWNED", rpt_type=51, order_qty=1000, price="100", seq_no="1"))
        self.assertEqual(self.store.get(order.client_order_id).filled_quantity, 0)

    def test_report_date_conflict_cannot_book_fill(self):
        order = self.adapter.submit(self.intent("dated"))
        self.store.bind_broker_order(order.client_order_id, "MOCK-DATED")
        order = self.store.get(order.client_order_id)
        self.adapter._apply_real_report(self.normalized(order, trade_date="2020/01/01", rpt_type=51, order_qty=1000, price="100", seq_no="1"))
        self.assertEqual(self.store.get(order.client_order_id).filled_quantity, 0)

    def test_legacy_fill_receipt_replay_is_idempotent(self):
        order = self.filled_entry()
        self.adapter._apply_real_report(self.normalized(order, rpt_type=51, order_qty=1000, price="100", seq_no="1"))
        self.assertEqual(self.store.get(order.client_order_id).filled_quantity, 1000)
        self.assertEqual(len(self.store.fills()), 1)

    def test_missing_fill_sequence_fails_closed_instead_of_deduplicating_real_fills(self):
        order = self.adapter.submit(self.intent("missing-sequence", quantity=2000))
        self.store.bind_broker_order(order.client_order_id, "MOCK-SEQ")
        order = self.store.get(order.client_order_id)
        self.adapter._apply_real_report(self.normalized(order, rpt_type=51, order_qty=1000, price="100", seq_no=""))
        self.assertEqual(self.store.get(order.client_order_id).filled_quantity, 0)
        self.assertTrue(self.store.control_state()["reason"].startswith("INVALID_FILL_REPORT:"))

    def test_full_late_fill_can_finish_canceled_order_without_phantom_residual(self):
        order = self.adapter.submit(self.intent("late-full", quantity=2000))
        self.store.bind_broker_order(order.client_order_id, "MOCK-LATE")
        self.store.canceled(order.client_order_id)
        order = self.store.get(order.client_order_id)
        for sequence in ("1", "2"):
            self.adapter._apply_real_report(self.normalized(order, rpt_type=51, order_qty=1000, price="100", seq_no=sequence))
        self.assertEqual(self.store.get(order.client_order_id).status, BrokerOrderStatus.FILLED)
        self.assertEqual(self.store.positions(), {"3605": 2000})

    def test_query_recovers_missing_cancel_confirmation_and_request(self):
        order = self.adapter.submit(self.intent("cancel-query"))
        self.store.bind_broker_order(order.client_order_id, "MOCK-CANCEL")
        self.store.acknowledge(order.client_order_id)
        order = self.adapter.cancel(order.client_order_id)
        self.api.merge_rows = [self.remote(order, OrderStatus=30, LastOrderStatus=2)]
        self.adapter.reconcile(timeout=1)
        self.assertEqual(self.store.get(order.client_order_id).status, BrokerOrderStatus.CANCELED)
        self.assertIsNone(self.store.pending_mutation(order.client_order_id))

    def test_modify_is_not_resolved_without_requested_price_evidence(self):
        order = self.adapter.submit(self.intent("uncertain-modify"))
        self.store.bind_broker_order(order.client_order_id, "MOCK-MODIFY")
        self.store.acknowledge(order.client_order_id)
        self.adapter.modify_price(order.client_order_id, "101")
        order = self.store.get(order.client_order_id)
        self.adapter._apply_merge_live(self.normalized(order, order_qty=1000, ok_qty=0, price="100", order_status=20, last_order_status=20))
        self.assertIsNotNone(self.store.pending_mutation(order.client_order_id))

    def test_duplicate_reduction_without_effective_quantity_does_not_subtract(self):
        order = self.adapter.submit(self.intent("reduction-quantity", quantity=3000))
        self.store.bind_broker_order(order.client_order_id, "MOCK-REDUCE")
        self.store.acknowledge(order.client_order_id)
        self.adapter.reduce_quantity(order.client_order_id, 1000)
        order = self.store.get(order.client_order_id)
        report = self.normalized(order, order_status=4, order_qty=0)
        self.adapter._apply_real_report(report)
        self.adapter._apply_real_report(report)
        self.assertEqual(self.store.get(order.client_order_id).quantity, 3000)
        self.assertIsNotNone(self.store.pending_mutation(order.client_order_id))

    def test_stale_larger_reduction_callback_cannot_increase_quantity(self):
        order = self.adapter.submit(self.intent("stale-reduction", quantity=3000))
        self.store.bind_broker_order(order.client_order_id, "MOCK-REDUCE")
        self.store.acknowledge(order.client_order_id)
        self.adapter.reduce_quantity(order.client_order_id, 1000)
        order = self.store.get(order.client_order_id)
        self.adapter._apply_real_report(self.normalized(order, order_status=4, order_qty=2000))
        self.adapter._apply_real_report(self.normalized(order, order_status=4, order_qty=3000))
        self.assertEqual(self.store.get(order.client_order_id).quantity, 2000)

    def test_reconciliation_lock_has_total_deadline(self):
        self.adapter._reconcile_lock.acquire()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(BrokerAdapterError, "lock timed out"):
                self.adapter.reconcile(timeout=0.04)
        finally:
            self.adapter._reconcile_lock.release()
        self.assertLess(time.monotonic() - started, 0.25)

    def test_callback_queue_drain_cannot_outlive_reconciliation_deadline(self):
        release = threading.Event()
        entered = threading.Event()
        def blocked_callback(_report):
            entered.set()
            release.wait(1)
        def ready_events():
            self.adapter._detail_event.set()
            self.adapter._merge_event.set()
            self.adapter._position_event.set()
        with patch.object(self.adapter, "_apply_real_report", side_effect=blocked_callback), patch.object(self.adapter, "request_reconciliation", side_effect=ready_events):
            self.adapter._queue.put(("real_report", {}))
            self.assertTrue(entered.wait(0.2))
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(BrokerAdapterError, "queue drain timed out"):
                    self.adapter.reconcile(timeout=0.04)
                self.assertLess(time.monotonic() - started, 0.25)
            finally:
                release.set()
                self.adapter._queue.join()

    def test_unknown_identify_order_number_does_not_get_guessed_from_one_pending(self):
        order = self.adapter.submit(self.intent("ambiguous"))
        self.adapter._apply_order_result([dict(identify=999, reply_code=0, order_no="MOCK-NO-PROOF")])
        self.assertEqual(self.store.get(order.client_order_id).status, BrokerOrderStatus.SEND_PENDING)
        self.assertIsNone(self.store.get(order.client_order_id).broker_order_no)
        self.assertEqual(self.store.control_state()["reason"], "AMBIGUOUS_BROKER_ORDER_RESULT")

    def test_order_result_receipt_remains_idempotent_after_durable_restart(self):
        order = self.adapter.submit(self.intent("durable-result"))
        result = dict(identify=order.identify, reply_code=0, order_no="MOCK-DURABLE")
        self.adapter._apply_order_result([result])
        database = self.store.database
        self.adapter.close()
        self.store.close()
        with LiveOrderStore(database) as restarted_store:
            with YuantaSparkExecutionAdapter(api=FakeApi(), api_types=api_types(), account=ACCOUNT, store=restarted_store, live_gate=self.adapter.live_gate) as restarted:
                restarted._apply_order_result([result])
                self.assertFalse(restarted_store.control_state()["halted"])
                self.assertEqual(restarted_store.get(order.client_order_id).broker_order_no, "MOCK-DURABLE")
                self.assertEqual(restarted_store.connection.execute("SELECT COUNT(*) FROM broker_result_receipts").fetchone()[0], 1)

    def test_conflicting_api_result_does_not_reverse_accepted_request(self):
        order = self.adapter.submit(self.intent("conflicting-result"))
        self.adapter._apply_order_result([dict(identify=order.identify, reply_code=0, order_no="MOCK-CONFLICT")])
        self.adapter._apply_order_result([dict(identify=order.identify, reply_code=1, order_no="MOCK-CONFLICT", advisory="MOCK contradictory API result")])
        self.assertTrue(self.store.control_state()["halted"])
        self.assertEqual(self.store.get_request(order.identify)["request_status"], "ACCEPTED")
        self.assertEqual(self.store.get(order.client_order_id).status, BrokerOrderStatus.ACKNOWLEDGED)

    def test_same_account_wrong_symbol_or_side_cannot_corrupt_fill(self):
        order = self.adapter.submit(self.intent("identity-fields"))
        self.store.bind_broker_order(order.client_order_id, "MOCK-IDENTITY")
        order = self.store.get(order.client_order_id)
        for fields in ({"symbol": "2330"}, {"side": "S"}, {"account": ""}):
            self.adapter._apply_real_report(self.normalized(order, rpt_type=51, order_qty=1000, price="100", seq_no="1", **fields))
        self.assertEqual(self.store.get(order.client_order_id).filled_quantity, 0)

    def test_no_basket_current_report_requires_all_identity_fields(self):
        order = self.adapter.submit(self.intent("no-basket"))
        self.store.bind_broker_order(order.client_order_id, "MOCK-NO-BASKET")
        order = self.store.get(order.client_order_id)
        self.adapter._apply_real_report(self.normalized(order, basket_no="", rpt_type=51, order_qty=1000, price="100", seq_no="1"))
        self.assertEqual(self.store.get(order.client_order_id).filled_quantity, 1000)

    def test_authoritative_query_recovers_reduction_without_delta_replay(self):
        order = self.adapter.submit(self.intent("reduce-query", quantity=3000))
        self.store.bind_broker_order(order.client_order_id, "MOCK-REDUCTION-QUERY")
        self.store.acknowledge(order.client_order_id)
        self.adapter.reduce_quantity(order.client_order_id, 1000)
        order = self.store.get(order.client_order_id)
        self.api.merge_rows = [self.remote(order, OrderQty=2000, LastOrderStatus=4)]
        self.adapter.reconcile(timeout=1)
        self.assertEqual(self.store.get(order.client_order_id).quantity, 2000)
        self.assertIsNone(self.store.pending_mutation(order.client_order_id))
        self.adapter.reconcile(timeout=1)
        self.assertEqual(self.store.get(order.client_order_id).quantity, 2000)

    def test_successful_snapshot_position_mismatch_can_requery_without_clearing_halt(self):
        self.api.positions = {"2330": 1000}
        with self.assertRaises(ReconciliationMismatch):
            self.adapter.reconcile(timeout=1)
        self.assertFalse(self.adapter._query_uncertain)
        self.api.positions = {}
        self.assertEqual(self.adapter.reconcile(timeout=1).status, "MATCH")
        self.assertTrue(self.store.control_state()["halted"])

    def test_failed_query_never_becomes_empty_successful_snapshot(self):
        with patch.object(self.api, "GetRealReport", return_value=False):
            with self.assertRaisesRegex(BrokerAdapterError, "query failed"):
                self.adapter.reconcile(timeout=0.2)
        self.assertTrue(self.adapter._query_uncertain)
        self.assertFalse(self.adapter._reconciled)

    def test_blocked_vendor_query_has_deadline_and_requires_fresh_session(self):
        release = threading.Event()
        started = time.monotonic()
        with patch.object(self.api, "GetRealReport", side_effect=lambda *_: release.wait(1)):
            try:
                with self.assertRaisesRegex(BrokerAdapterError, "timed out"):
                    self.adapter.reconcile(timeout=0.04)
                self.assertLess(time.monotonic() - started, 0.25)
                with self.assertRaisesRegex(BrokerAdapterError, "fresh broker session"):
                    self.adapter.reconcile(timeout=0.04)
            finally:
                release.set()
                self.adapter._query_worker.join(0.2)

    def test_missing_snapshot_does_not_extend_budget_for_three_separate_waits(self):
        with patch.object(self.api, "GetRealReport", return_value=True):
            started = time.monotonic()
            with self.assertRaisesRegex(BrokerAdapterError, "GetRealReport timed out"):
                self.adapter.reconcile(timeout=0.04)
            self.assertLess(time.monotonic() - started, 0.25)

    def test_stale_previous_reduction_confirmation_cannot_clear_new_request(self):
        order = self.adapter.submit(self.intent("sequential-reductions", quantity=4000))
        self.store.bind_broker_order(order.client_order_id, "MOCK-SEQUENTIAL-REDUCE")
        self.store.acknowledge(order.client_order_id)
        self.adapter.reduce_quantity(order.client_order_id, 1000)
        order = self.store.get(order.client_order_id)
        previous = self.normalized(order, order_status=4, order_qty=3000)
        self.adapter._apply_real_report(previous)
        self.assertIsNone(self.store.pending_mutation(order.client_order_id))
        self.adapter.reduce_quantity(order.client_order_id, 1000)
        self.adapter._apply_real_report(previous)
        pending = self.store.pending_mutation(order.client_order_id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["payload"]["expected_quantity"], 2000)
        self.assertEqual(self.store.get(order.client_order_id).quantity, 3000)

    def test_stale_previous_price_confirmation_cannot_clear_new_request(self):
        order = self.adapter.submit(self.intent("sequential-prices"))
        self.store.bind_broker_order(order.client_order_id, "MOCK-SEQUENTIAL-PRICE")
        self.store.acknowledge(order.client_order_id)
        self.adapter.modify_price(order.client_order_id, "101")
        order = self.store.get(order.client_order_id)
        previous = self.normalized(order, order_status=20, price="101", order_qty=1000)
        self.adapter._apply_real_report(previous)
        self.assertIsNone(self.store.pending_mutation(order.client_order_id))
        self.adapter.modify_price(order.client_order_id, "102")
        self.adapter._apply_real_report(previous)
        pending = self.store.pending_mutation(order.client_order_id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["payload"]["new_price"], "102")

    def test_unidentified_mutation_failure_cannot_release_new_request(self):
        order = self.adapter.submit(self.intent("ambiguous-mutation-failure"))
        self.store.bind_broker_order(order.client_order_id, "MOCK-MUTATION-FAILURE")
        self.store.acknowledge(order.client_order_id)
        self.adapter.modify_price(order.client_order_id, "101")
        order = self.store.get(order.client_order_id)
        self.adapter._apply_real_report(self.normalized(order, order_status=21, price="100"))
        self.assertIsNotNone(self.store.pending_mutation(order.client_order_id))

    def test_new_success_without_order_number_remains_unknown_and_halted(self):
        order = self.adapter.submit(self.intent("missing-order-number"))
        self.adapter._apply_order_result([dict(identify=order.identify, reply_code=0, order_no="")])
        self.assertEqual(self.store.get(order.client_order_id).status, BrokerOrderStatus.UNKNOWN)
        self.assertTrue(self.store.control_state()["halted"])
        self.assertIsNone(self.store.get(order.client_order_id).broker_order_no)

    def test_new_order_construction_failure_is_durably_unsent_rejected(self):
        with patch.object(self.adapter, "_construct_stock_order", side_effect=ValueError("MOCK rejected SDK property")):
            with self.assertRaises(ValueError):
                self.adapter.submit(self.intent("bad-stock-order"))
        order = self.store.get_by_intent("MOCK-bad-stock-order")
        self.assertEqual(order.status, BrokerOrderStatus.REJECTED)
        self.assertEqual(order.identify, 0)
        self.assertEqual(self.api.sent, [])

    def test_new_payload_failure_is_not_mistaken_for_uncertain_send(self):
        with patch.object(self.adapter, "_stock_list", side_effect=ValueError("MOCK list construction failure")):
            with self.assertRaises(ValueError):
                self.adapter.submit(self.intent("bad-payload"))
        order = self.store.get_by_intent("MOCK-bad-payload")
        self.assertEqual(order.status, BrokerOrderStatus.REJECTED)
        self.assertEqual(self.store.get_request(order.identify)["request_status"], "UNSENT_REJECTED")
        self.assertEqual(self.api.sent, [])

    def test_unsent_cancel_payload_failure_does_not_lock_future_cancel(self):
        order = self.adapter.submit(self.intent("cancel-payload-failure"))
        self.store.bind_broker_order(order.client_order_id, "MOCK-CANCEL-PAYLOAD")
        self.store.acknowledge(order.client_order_id)
        self.api.sent.clear()
        with patch.object(self.adapter, "_stock_list", side_effect=ValueError("MOCK list failure")):
            with self.assertRaises(ValueError):
                self.adapter.cancel(order.client_order_id)
        self.assertEqual(self.store.get(order.client_order_id).status, BrokerOrderStatus.ACKNOWLEDGED)
        self.assertIsNone(self.store.pending_mutation(order.client_order_id))
        self.assertEqual(self.api.sent, [])
        self.adapter.cancel(order.client_order_id)
        self.assertEqual(len(self.api.sent), 1)

    def test_guard_rejection_remains_reconcilable_after_restart(self):
        order = self.adapter.submit(self.intent("guard-restart"), pre_send_guard=lambda _: False)
        self.assertEqual(self.adapter.reconcile(timeout=1).status, "MATCH")
        self.assertFalse(self.store.control_state()["halted"])
        self.adapter.close()
        with LiveOrderStore(self.store.database) as restarted_store:
            with YuantaSparkExecutionAdapter(api=FakeApi(), api_types=api_types(), account=ACCOUNT, store=restarted_store, live_gate=self.adapter.live_gate) as restarted:
                self.assertEqual(restarted.reconcile(timeout=1).status, "MATCH")
                self.assertTrue(restarted_store.is_proven_unsent_rejection(order.client_order_id))
                duplicate = restarted.submit(self.intent("guard-restart"))
                self.assertEqual(duplicate.status, BrokerOrderStatus.REJECTED)
                self.assertEqual(restarted.api.sent, [])

    def test_unsent_exit_guard_does_not_block_next_rescue(self):
        self.filled_entry()
        order = self.adapter.submit(self.intent("stale-exit", side=Side.SELL, purpose=IntentPurpose.EXIT), pre_send_guard=lambda _: False)
        self.assertEqual(order.status, BrokerOrderStatus.REJECTED)
        self.assertEqual(self.adapter.reconcile(timeout=1).status, "MATCH")
        rescue = self.adapter.submit_rescue(self.intent("valid-rescue", side=Side.SELL, purpose=IntentPurpose.EXIT))
        self.assertEqual(rescue.status, BrokerOrderStatus.SEND_PENDING)
        self.assertEqual(len(self.api.sent), 1)

    def test_unsent_construction_and_payload_failures_do_not_poison_reconciliation(self):
        for method in ("_construct_stock_order", "_stock_list"):
            with self.subTest(method=method):
                with patch.object(self.adapter, method, side_effect=ValueError("MOCK builder failure")):
                    with self.assertRaises(ValueError):
                        self.adapter.submit(self.intent(f"unsent-{method}"))
                order = self.store.get_by_intent(f"MOCK-unsent-{method}")
                self.assertTrue(self.store.is_proven_unsent_rejection(order.client_order_id))
                self.assertEqual(self.adapter.reconcile(timeout=1).status, "MATCH")
                self.assertFalse(self.store.control_state()["halted"])

    def test_preorder_reconcile_unsent_cause_is_not_a_missing_order(self):
        self.api.positions = {"2330": 1000}
        with self.assertRaises(ReconciliationMismatch):
            self.adapter.submit(self.intent("failed-snapshot-unsent"))
        self.api.positions = {}
        self.assertEqual(self.adapter.reconcile(timeout=1).status, "MATCH")
        self.assertTrue(self.store.control_state()["halted"])

    def test_rejected_maybe_sent_and_unknown_orders_are_never_skipped(self):
        order = self.adapter.submit(self.intent("broker-rejected"))
        self.store.complete_request(order.identify, success=False, payload={})
        self.store.reject(order.client_order_id, "broker rejected ordinary request")
        self.assertFalse(self.store.is_proven_unsent_rejection(order.client_order_id))
        with self.assertRaises(ReconciliationMismatch):
            self.adapter.reconcile(timeout=1)
        self.store.mark_unknown(order.client_order_id, "MOCK network uncertainty")
        self.assertFalse(self.store.is_proven_unsent_rejection(order.client_order_id))
        with self.assertRaises(ReconciliationMismatch):
            self.adapter.reconcile(timeout=1)

    def test_remote_order_contradicting_unsent_proof_is_not_ignored(self):
        order = self.adapter.submit(self.intent("contradict-unsent"), pre_send_guard=lambda _: False)
        self.api.merge_rows = [self.remote(order)]
        with self.assertRaises(ReconciliationMismatch):
            self.adapter.reconcile(timeout=1)
        self.assertTrue(self.store.control_state()["halted"])

    def test_final_guard_observes_slow_persistence_and_payload_for_all_new_paths(self):
        self.filled_entry()
        original_pending = self.store.mark_send_pending
        original_payload = self.adapter._stock_list
        for path in ("entry", "exit", "rescue"):
            with self.subTest(path=path):
                clock = [0]
                def delayed_pending(client):
                    result = original_pending(client)
                    clock[0] += 10
                    return result
                def delayed_payload(stock):
                    result = original_payload(stock)
                    clock[0] += 10
                    return result
                seen = []
                def guard(_intent):
                    seen.append(clock[0])
                    return clock[0] < 5
                side = Side.BUY if path == "entry" else Side.SELL
                purpose = IntentPurpose.ENTRY if path == "entry" else IntentPurpose.EXIT
                with patch.object(self.store, "mark_send_pending", side_effect=delayed_pending), patch.object(self.adapter, "_stock_list", side_effect=delayed_payload):
                    method = self.adapter.submit_rescue if path == "rescue" else self.adapter.submit
                    order = method(self.intent(f"slow-final-{path}", side=side, purpose=purpose), pre_send_guard=guard)
                self.assertEqual(seen, [20])
                self.assertEqual(order.status, BrokerOrderStatus.REJECTED)
                self.assertEqual(self.store.get_request(order.identify)["request_status"], "UNSENT_REJECTED")
                self.assertEqual(self.api.sent, [])
                self.assertEqual(self.adapter.reconcile(timeout=1).status, "MATCH")

    def test_modify_guard_runs_after_payload_and_preserves_live_order_on_failure(self):
        order = self.adapter.submit(self.intent("modify-final-guard"))
        self.store.bind_broker_order(order.client_order_id, "MOCK-MODIFY-GUARD")
        self.store.acknowledge(order.client_order_id)
        self.api.sent.clear()
        built = []
        original = self.adapter._stock_list
        def payload(stock):
            result = original(stock)
            built.append(stock.Price)
            return result
        def guard(proposed):
            self.assertEqual(built, [101.0])
            self.assertEqual(proposed.price, Decimal("101"))
            raise ValueError("MOCK cutoff crossed during payload build")
        with patch.object(self.adapter, "_stock_list", side_effect=payload):
            with self.assertRaisesRegex(BrokerAdapterError, "UNSENT_PRE_SEND_GUARD"):
                self.adapter.modify_price(order.client_order_id, "101", emergency=True, pre_send_guard=guard)
        current = self.store.get(order.client_order_id)
        self.assertEqual(current.status, BrokerOrderStatus.ACKNOWLEDGED)
        self.assertEqual(current.price, Decimal("100"))
        self.assertIsNone(self.store.pending_mutation(order.client_order_id))
        self.assertEqual(self.store.latest_request(order.client_order_id, "MODIFY_PRICE")["request_status"], "UNSENT_REJECTED")
        self.assertEqual(self.api.sent, [])
        self.adapter.cancel(order.client_order_id, emergency=True)
        self.assertEqual(len(self.api.sent), 1)

    def assert_malformed_snapshot_fails_closed(self, query, response):
        with tempfile.TemporaryDirectory(prefix="broker-malformed-mock-") as directory:
            with LiveOrderStore(Path(directory) / "orders.sqlite") as store:
                api = FakeApi()
                def respond(_account, _language):
                    api.OnResponse.emit(1, 0, query, None, response)
                    return True
                setattr(api, query, respond)
                with YuantaSparkExecutionAdapter(api=api, api_types=api_types(), account=ACCOUNT, store=store, live_gate=self.adapter.live_gate) as adapter:
                    with self.assertRaises(BrokerAdapterError):
                        adapter.reconcile(timeout=0.2)
                    self.assertTrue(adapter._query_uncertain)
                    self.assertFalse(adapter._reconciled)
                    failed_event = {"GetRealReport": adapter._detail_event, "GetRealReportMerge": adapter._merge_event, "GetStoreSummary": adapter._position_event}[query]
                    self.assertFalse(failed_event.is_set())
                    self.assertTrue(store.control_state()["halted"])
                    with self.assertRaises(BrokerAdapterError):
                        adapter.inspect_broker_state(timeout=0.2)
                    self.assertEqual(api.sent, [])

    def test_missing_null_or_noncollection_snapshot_is_never_certified_empty(self):
        for query, field in (("GetRealReport", "RealReportList"), ("GetRealReportMerge", "RealReportMergeList"), ("GetStoreSummary", "StkStoreList")):
            for response in (Obj(), Obj(**{field: None}), Obj(**{field: ""}), Obj(**{field: {}}), Obj(**{field: Obj()})):
                with self.subTest(query=query, response=response.__dict__):
                    self.assert_malformed_snapshot_fails_closed(query, response)

    def test_truncated_dotnet_snapshot_is_never_certified_empty(self):
        class TruncatedList:
            Count = 2
            def __getitem__(self, index):
                if index == 0:
                    return Obj(StkCode="3605", StockQty=1000, TradeKind=0)
                raise RuntimeError("MOCK collection truncated")
        for query, field in (("GetRealReport", "RealReportList"), ("GetRealReportMerge", "RealReportMergeList"), ("GetStoreSummary", "StkStoreList")):
            with self.subTest(query=query):
                self.assert_malformed_snapshot_fails_closed(query, Obj(**{field: TruncatedList()}))

    def test_malformed_inventory_quantity_or_financing_bucket_never_normalizes_flat(self):
        for field, value in (("StockQty", "not-a-number"), ("StockQty", "NaN"), ("StockQty", "Infinity"), ("StockQty", "0.5"), ("StockQty", None), ("StockQty", -1), ("StockQty", False), ("TradeKind", 999), ("TradeKind", "oops"), ("StkCode", "")):
            row = dict(StkCode="3605", StockQty=1000, TradeKind=0)
            row[field] = value
            with self.subTest(field=field, value=value):
                self.assert_malformed_snapshot_fails_closed("GetStoreSummary", Obj(StkStoreList=[Obj(**row)]))
        for field in ("StockQty", "TradeKind", "StkCode"):
            row = dict(StkCode="3605", StockQty=1000, TradeKind=0)
            row.pop(field)
            with self.subTest(missing=field):
                self.assert_malformed_snapshot_fails_closed("GetStoreSummary", Obj(StkStoreList=[Obj(**row)]))

    def test_malformed_order_snapshot_identity_and_quantities_are_rejected(self):
        base = dict(Account=ACCOUNT, RptType=1, OrderNo="MOCK-OPEN", CompanyNo="3605", BS="B", OrderQty=1000, OkQty=0, OrderStatus=20, LastOrderStatus=0, Price="100")
        for field, value in (("Account", "S11111111111"), ("Account", ""), ("OrderNo", ""), ("CompanyNo", ""), ("BS", "UNKNOWN"), ("OrderQty", "oops"), ("OrderQty", "NaN"), ("OrderQty", 0.5), ("OrderQty", -1), ("OkQty", "oops"), ("OkQty", 1001), ("OrderStatus", None), ("LastOrderStatus", "bad"), ("Price", "NaN")):
            row = dict(base)
            row[field] = value
            with self.subTest(field=field, value=value):
                self.assert_malformed_snapshot_fails_closed("GetRealReportMerge", Obj(RealReportMergeList=[Obj(**row)]))
        for field in ("Account", "OrderNo", "CompanyNo", "BS", "OrderQty", "OkQty", "OrderStatus", "LastOrderStatus", "RptType"):
            row = dict(base)
            row.pop(field)
            with self.subTest(missing=field):
                self.assert_malformed_snapshot_fails_closed("GetRealReportMerge", Obj(RealReportMergeList=[Obj(**row)]))

    def test_explicit_empty_dotnet_collections_remain_valid(self):
        class EmptyDotNetList:
            Count = 0
            def __getitem__(self, _index):
                raise IndexError
        for query, field in (("GetRealReport", "RealReportList"), ("GetRealReportMerge", "RealReportMergeList"), ("GetStoreSummary", "StkStoreList")):
            def respond(_account, _language, query=query, field=field):
                self.api.OnResponse.emit(1, 0, query, None, Obj(**{field: EmptyDotNetList()}))
                return True
            setattr(self.api, query, respond)
        self.assertEqual(self.adapter.reconcile(timeout=1).status, "MATCH")
        self.assertFalse(self.store.control_state()["halted"])

    def test_failed_identity_assignment_after_request_is_durably_unsent(self):
        original = self.adapter._set_identity
        def identity(stock, identify):
            if identify:
                raise ValueError("MOCK identity setter failed")
            return original(stock, identify)
        with patch.object(self.adapter, "_set_identity", side_effect=identity):
            with self.assertRaises(ValueError):
                self.adapter.submit(self.intent("failed-identity"))
        order = self.store.get_by_intent("MOCK-failed-identity")
        self.assertEqual(order.status, BrokerOrderStatus.REJECTED)
        self.assertEqual(self.store.get_request(order.identify)["request_status"], "UNSENT_REJECTED")
        self.assertEqual(self.adapter.reconcile(timeout=1).status, "MATCH")
        self.assertEqual(self.api.sent, [])

    def test_valid_integral_decimal_fields_are_never_defaulted_to_zero(self):
        entry = self.filled_entry()
        self.api.merge_rows = [self.remote(entry, OrderQty="1000.0", OkQty="1000.0", RptType="1.0", OrderStatus="20.0", LastOrderStatus="8.0")]
        def positions(_account, _language):
            self.api.OnResponse.emit(1, 0, "GetStoreSummary", None, Obj(StkStoreList=[Obj(StkCode="3605", StockQty="1000.0", TradeKind="0.0")]))
            return True
        self.api.GetStoreSummary = positions
        result = self.adapter.reconcile(timeout=1)
        self.assertEqual(result.broker_positions, {"3605|0": 1000})
        self.assertEqual(self.adapter._latest_merge[0]["ok_qty"], 1000)
