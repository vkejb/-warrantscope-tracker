"""Pure mock proofs for reused API Identify and operation rejection recovery.

No vendor SDK, login, credentials, services or real order boundary is used.
"""
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yuanta_broker_execution_v01 import (
    BrokerOrderStatus, ExecutionIntent, LiveOrderStore, LiveTradingGate,
    ReconciliationMismatch, Side, YuantaSparkExecutionAdapter,
)
from yuanta_broker_execution_v01.tests.test_adapter import FakeApi, Obj, api_types


ACCOUNT = "S00000000000"


class SnapshotApi(FakeApi):
    def __init__(self):
        super().__init__()
        self.detail_rows = []

    def GetRealReport(self, _account, _language):
        self.OnResponse.emit(1, 0, "GetRealReport", None,
                             Obj(RealReportList=[Obj(**row) for row in self.detail_rows]))
        return True


class MutationResultIdentityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        blocked = RuntimeError("MOCK_ONLY: SDK, network and subprocesses forbidden")
        for target in ("yuanta_broker_execution_v01.sdk.load_api_types",
                       "yuanta_broker_execution_v01.load_api_types",
                       "socket.socket", "socket.create_connection",
                       "subprocess.run", "subprocess.Popen"):
            self.stack.enter_context(patch(target, side_effect=blocked))
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="mutation-identity-mock-"))
        self.store = self.stack.enter_context(LiveOrderStore(Path(directory) / "synthetic.sqlite"))
        self.api = SnapshotApi()
        self.gate = LiveTradingGate.from_environment(
            cli_live=True, environ={"EXECUTION_MODE": "LIVE", "ENABLE_LIVE_TRADING": "YES"})
        self.adapter = self.stack.enter_context(YuantaSparkExecutionAdapter(
            api=self.api, api_types=api_types(), account=ACCOUNT,
            store=self.store, live_gate=self.gate))
        self.adapter.reconcile(timeout=1)
        order, _ = self.store.reserve(ExecutionIntent(
            intent_id="MOCK-PARTIAL-ENTRY", symbol="3605", side=Side.BUY,
            quantity=2000, price=Decimal("100")))
        self.new_id = self.store.create_request(order.client_order_id, "NEW")
        self.store.mark_send_pending(order.client_order_id)
        self.adapter._apply_order_result([dict(identify=self.new_id, reply_code=0,
                                              order_no="MOCK-ORDER")])
        self.store.record_fill(order.client_order_id, fill_id="MOCK-FILL", quantity=1000,
                               price="100", broker_order_no="MOCK-ORDER", seq_no="MOCK-FILL")
        self.store.finalize_latest_request(order.client_order_id, "NEW", success=True)
        self.order_id = order.client_order_id
        self.refresh_snapshot()

    def order(self):
        return self.store.get(self.order_id)

    def detail(self, **changes):
        order = self.order()
        row = dict(Account=ACCOUNT, RptType=50, OrderNo="MOCK-ORDER", CompanyNo="3605",
                   BS="B", BasketNo=order.basket_no, OrderStatus=18, OrderQty=order.quantity,
                   Price="100", TradeKind=0, SeqNo=0)
        row.update(changes)
        return row

    def normalized(self, **changes):
        row = dict(account=ACCOUNT, rpt_type=50, order_no="MOCK-ORDER", symbol="3605",
                   side="B", basket_no=self.order().basket_no, order_status=18,
                   order_qty=self.order().quantity, price="100", trade_kind=0)
        row.update(changes)
        return row

    def refresh_snapshot(self, **changes):
        order = self.order()
        row = dict(Account=ACCOUNT, RptType=1, OrderNo="MOCK-ORDER", CompanyNo="3605",
                   BS="B", BasketNo=order.basket_no, OrderStatus=20, LastOrderStatus=0,
                   OrderQty=order.quantity, OkQty=order.filled_quantity, Price="100")
        row.update(changes)
        self.api.merge_rows = [row]
        self.api.positions = {"3605": 1000}
        if not self.api.detail_rows:
            self.api.detail_rows = [self.detail()]
        return self.adapter.reconcile(timeout=1)

    def ambiguous_result(self, **changes):
        row = dict(identify=self.new_id, reply_code=1, order_no="MOCK-ORDER",
                   err_no="MOCK-REJECT", advisory="synthetic rejected mutation")
        row.update(changes)
        self.adapter._apply_order_result([row])

    def test_exact_current_cancel_rejection_allows_one_paced_retry(self):
        self.adapter.cancel(self.order_id, "mock entry timeout")
        failed = self.store.latest_request(self.order_id, "CANCEL")
        self.adapter._apply_order_result([dict(identify=failed["identify"], reply_code=1,
                                              order_no="MOCK-ORDER", err_no="MOCK-CURRENT-REJECT")])
        self.assertEqual(self.store.get_request(self.new_id)["request_status"], "CONFIRMED")
        self.assertIsNone(self.store.pending_mutation(self.order_id))
        self.assertEqual(self.store.get_request(failed["identify"])["request_status"], "REJECTED")
        self.assertEqual(self.order().status, BrokerOrderStatus.PARTIALLY_FILLED)
        self.assertEqual(self.store.positions(), {"3605": 1000})
        self.assertEqual(self.refresh_snapshot(LastOrderStatus=3).status, "MATCH")
        self.adapter.cancel(self.order_id, "mock paced recovery", emergency=True)
        retry = self.store.pending_mutation(self.order_id)
        self.assertNotEqual(retry["identify"], failed["identify"])
        self.assertEqual(len(self.api.sent), 2)
        self.assertTrue(all(call[1][0].TradeKind == 4 for call in self.api.sent))
        # A delayed copy of the first rejection cannot clear the retry barrier.
        self.adapter._apply_real_report(self.normalized(order_status=3, trade_kind=4))
        self.assertEqual(self.store.pending_mutation(self.order_id)["identify"], retry["identify"])
        self.ambiguous_result(err_no="MOCK-SECOND-REJECT")
        self.adapter._apply_real_report(self.normalized(order_status=3, trade_kind=4))
        with self.assertRaisesRegex(ReconciliationMismatch, "unresolved_broker_mutation"):
            self.refresh_snapshot(LastOrderStatus=3)
        self.assertEqual(len(self.api.sent), 2)

    def test_late_new_ack_cannot_accept_or_release_cancel(self):
        self.adapter.cancel(self.order_id)
        cancel = self.store.pending_mutation(self.order_id)
        self.ambiguous_result(reply_code=0, err_no="", advisory="late NEW acknowledgment")
        self.assertEqual(self.store.get_request(self.new_id)["request_status"], "CONFIRMED")
        self.assertEqual(self.store.get_request(cancel["identify"])["request_status"], "SEND_PENDING")
        self.assertTrue(self.store.get_request(cancel["identify"])["payload"]["result_identity_uncertain"])
        with self.assertRaisesRegex(ReconciliationMismatch, "unresolved_broker_mutation"):
            self.refresh_snapshot()
        self.assertEqual(self.order().status, BrokerOrderStatus.CANCEL_PENDING)
        self.assertEqual(len(self.api.sent), 1)

    def test_prior_manual_rejection_in_snapshot_prevents_first_local_release(self):
        self.api.detail_rows = [self.detail(OrderStatus=3, TradeKind=4)]
        self.refresh_snapshot(LastOrderStatus=3)
        self.adapter.cancel(self.order_id)
        cancel = self.store.pending_mutation(self.order_id)
        self.assertNotIn("rejection_baseline", cancel["payload"])
        self.ambiguous_result()
        self.adapter._apply_real_report(self.normalized(order_status=3, trade_kind=4))
        with self.assertRaisesRegex(ReconciliationMismatch, "unresolved_broker_mutation"):
            self.refresh_snapshot(LastOrderStatus=3)
        self.assertEqual(self.store.pending_mutation(self.order_id)["identify"], cancel["identify"])

    def test_failure_without_operation_or_owned_basket_does_not_release(self):
        self.adapter.cancel(self.order_id)
        cancel = self.store.pending_mutation(self.order_id)
        self.ambiguous_result()
        for changes in ({"trade_kind": 0}, {"trade_kind": 4, "basket_no": ""},
                        {"trade_kind": 4, "order_no": ""}):
            self.adapter._apply_real_report(self.normalized(order_status=3, **changes))
            self.assertEqual(self.store.pending_mutation(self.order_id)["identify"], cancel["identify"])

    def test_live_failure_before_first_cancel_cannot_release_it_when_replayed(self):
        # Even a complete earlier query cannot prove that this observed manual
        # failure or an unobserved delayed rejection belongs to our later send.
        old_failure = self.normalized(order_status=3, trade_kind=4)
        self.adapter._apply_real_report(old_failure)
        self.adapter.cancel(self.order_id)
        cancel = self.store.pending_mutation(self.order_id)
        self.adapter._apply_real_report(old_failure)
        self.assertEqual(self.store.pending_mutation(self.order_id)["identify"], cancel["identify"])
        self.assertEqual(self.store.get_request(cancel["identify"])["request_status"], "SEND_PENDING")
        # A newer query that omits the older failure cannot erase uncertainty.
        with self.assertRaisesRegex(ReconciliationMismatch, "unresolved_broker_mutation"):
            self.refresh_snapshot()
        self.adapter.close()
        self.adapter = self.stack.enter_context(YuantaSparkExecutionAdapter(
            api=self.api, api_types=api_types(), account=ACCOUNT,
            store=self.store, live_gate=self.gate))
        with self.assertRaisesRegex(ReconciliationMismatch, "unresolved_broker_mutation"):
            self.refresh_snapshot()

    def test_first_operation_and_clean_snapshot_do_not_prove_delayed_rejection(self):
        self.adapter.cancel(self.order_id)
        self.adapter._apply_real_report(self.normalized(order_status=3, trade_kind=4))
        self.assertIsNotNone(self.store.pending_mutation(self.order_id))
        with self.assertRaisesRegex(ReconciliationMismatch, "unresolved_broker_mutation"):
            self.refresh_snapshot(LastOrderStatus=3)

    def test_unattributed_price_rejection_preserves_order_and_barrier(self):
        self.adapter.modify_price(self.order_id, "101")
        modify = self.store.pending_mutation(self.order_id)
        self.ambiguous_result()
        self.adapter._apply_real_report(self.normalized(order_status=21, trade_kind=7))
        self.assertEqual(self.store.get_request(modify["identify"])["request_status"], "SEND_PENDING")
        self.assertEqual(self.store.pending_mutation(self.order_id)["identify"], modify["identify"])
        self.assertEqual(self.order().price, Decimal("100"))
        with self.assertRaisesRegex(ReconciliationMismatch, "unresolved_broker_mutation"):
            self.refresh_snapshot(LastOrderStatus=21)
        self.assertEqual([call[1][0].TradeKind for call in self.api.sent], [7])

    def test_terminal_broker_evidence_resolves_ambiguous_cancel_without_retry(self):
        self.adapter.cancel(self.order_id)
        self.ambiguous_result()
        self.adapter._apply_real_report(self.normalized(order_status=2, trade_kind=4))
        self.assertEqual(self.refresh_snapshot(OrderStatus=30, LastOrderStatus=2).status, "MATCH")
        self.assertIsNone(self.store.pending_mutation(self.order_id))
        self.assertEqual(self.order().status, BrokerOrderStatus.CANCELED)
        self.assertEqual(self.store.positions(), {"3605": 1000})
        self.assertEqual(len(self.api.sent), 1)

    def test_authoritative_price_target_resolves_ambiguous_acceptance(self):
        self.adapter.modify_price(self.order_id, "101")
        self.ambiguous_result(reply_code=0, err_no="", advisory="unproven reused acceptance")
        self.assertIsNotNone(self.store.pending_mutation(self.order_id))
        self.assertEqual(self.refresh_snapshot(Price="101", LastOrderStatus=20).status, "MATCH")
        self.assertIsNone(self.store.pending_mutation(self.order_id))
        self.assertEqual(self.store.positions(), {"3605": 1000})
        self.assertEqual(len(self.api.sent), 1)

    def test_unattributed_rejection_after_restart_does_not_release_barrier(self):
        self.adapter.cancel(self.order_id)
        self.ambiguous_result()
        self.adapter.close()
        self.adapter = self.stack.enter_context(YuantaSparkExecutionAdapter(
            api=self.api, api_types=api_types(), account=ACCOUNT,
            store=self.store, live_gate=self.gate))
        self.api.detail_rows = [self.detail(OrderStatus=3, TradeKind=4)]
        with self.assertRaisesRegex(ReconciliationMismatch, "unresolved_broker_mutation"):
            self.refresh_snapshot(LastOrderStatus=3)
        self.assertIsNotNone(self.store.pending_mutation(self.order_id))
        self.assertEqual(self.order().status, BrokerOrderStatus.CANCEL_PENDING)
        self.assertEqual(len(self.api.sent), 1)

    def test_unproven_api_failure_before_first_cancel_cannot_release_later_mutation(self):
        self.adapter._apply_order_result([dict(identify=999, reply_code=1,
                                              order_no="MOCK-ORDER", err_no="MOCK-OLDER-FAILURE")])
        self.adapter.cancel(self.order_id, emergency=True)
        cancel = self.store.pending_mutation(self.order_id)
        self.adapter._apply_real_report(self.normalized(order_status=3, trade_kind=4))
        self.assertEqual(self.store.pending_mutation(self.order_id)["identify"], cancel["identify"])
        with self.assertRaisesRegex(ReconciliationMismatch, "unresolved_broker_mutation"):
            self.refresh_snapshot()

    def test_old_price_target_cannot_release_request_returning_to_same_price(self):
        self.adapter.modify_price(self.order_id, "101")
        self.refresh_snapshot(Price="101", LastOrderStatus=20)
        old_report = self.normalized(order_status=20, trade_kind=7, price="101")
        self.adapter.modify_price(self.order_id, "102")
        self.refresh_snapshot(Price="102", LastOrderStatus=20)
        self.adapter.modify_price(self.order_id, "101")
        current = self.store.pending_mutation(self.order_id)
        self.adapter._apply_real_report(old_report)
        self.adapter._apply_merge_live(dict(old_report, ok_qty=1000, last_order_status=20))
        self.assertEqual(self.store.pending_mutation(self.order_id)["identify"], current["identify"])
        # Even the detailed query may replay the old target; current aggregate
        # state is still 102, so it cannot confirm this new 101 request.
        self.api.detail_rows = [self.detail(OrderStatus=20, TradeKind=7, Price="101")]
        self.refresh_snapshot(Price="102", LastOrderStatus=20)
        self.assertEqual(self.store.get_request(current["identify"])["request_status"], "SEND_PENDING")
        self.refresh_snapshot(Price="101", LastOrderStatus=20)
        self.assertIsNone(self.store.pending_mutation(self.order_id))
        self.assertEqual(len(self.api.sent), 3)

    def test_query_started_before_mutation_cannot_confirm_later_price_target(self):
        # The existing successful snapshot observed 100 before this request.
        # Its old fence does not contain the newly created modification.
        self.adapter.modify_price(self.order_id, "100")
        pending = self.store.pending_mutation(self.order_id)
        remote = dict(self.normalized(price="100"), ok_qty=1000, last_order_status=20)
        self.adapter._resolve_mutation_from_remote(self.order(), remote, authoritative_snapshot=True)
        self.assertEqual(self.store.pending_mutation(self.order_id)["identify"], pending["identify"])
        self.refresh_snapshot(Price="100", LastOrderStatus=20)
        self.assertIsNone(self.store.pending_mutation(self.order_id))

    def test_ambiguity_is_durable_across_adapter_restart(self):
        self.adapter.cancel(self.order_id)
        self.ambiguous_result()
        self.adapter.close()
        self.adapter = self.stack.enter_context(YuantaSparkExecutionAdapter(
            api=self.api, api_types=api_types(), account=ACCOUNT,
            store=self.store, live_gate=self.gate))
        with self.assertRaisesRegex(ReconciliationMismatch, "unresolved_broker_mutation"):
            self.refresh_snapshot()
        self.assertEqual(len(self.api.sent), 1)
