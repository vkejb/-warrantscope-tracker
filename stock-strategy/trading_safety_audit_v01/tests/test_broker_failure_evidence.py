"""Audit evidence for currently reproducible broker-state defects.

These tests intentionally assert the observed defective behavior. A passing
test establishes that a defect is reproducible; it does NOT certify trading
safety. They belong to an audit-only suite and should be replaced by safety
regressions when the corresponding production defect is repaired.

Only pure-Python adapter/store code, synthetic accounts, and temporary SQLite
files are used. The vendor SDK, credentials, real sessions, subprocesses and
network connections are blocked. No application runtime state is opened.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from yuanta_broker_execution_v01 import (
    BrokerAdapterError,
    BrokerOrderStatus,
    DuplicateIntentConflict,
    ExecutionIntent,
    LiveOrderStore,
    LiveTradingGate,
    Side,
    StoreError,
    YuantaSparkExecutionAdapter,
)


SYNTHETIC_ACCOUNT = "S00000000000"
OTHER_SYNTHETIC_ACCOUNT = "S11111111111"


class _Object:
    def __init__(self, **values):
        self.__dict__.update(values)


class _EventHook:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def emit(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class _FakeList(list):
    def Add(self, item):
        self.append(item)


class _FakeGenericList:
    def __getitem__(self, _item):
        return _FakeList


class _FakeStockOrder:
    def __init__(self):
        self.Identify = 0


class _FakeLanguage:
    UTF8 = "AUDIT_FAKE_UTF8"


class AuditOnlyApi:
    """In-memory fake; deliberately has no broker login/session facility."""

    def __init__(self):
        self.OnResponse = _EventHook()
        self.send_calls = []

    def SendStockOrder(self, account, orders, language):
        self.send_calls.append((account, list(orders), language))
        return True

    def GetRealReport(self, _account, _language):
        self.OnResponse.emit(1, 0, "GetRealReport", None, _Object(RealReportList=[]))
        return True

    def GetRealReportMerge(self, _account, _language):
        self.OnResponse.emit(
            1, 0, "GetRealReportMerge", None, _Object(RealReportMergeList=[])
        )
        return True

    def GetStoreSummary(self, _account, _language):
        self.OnResponse.emit(1, 0, "GetStoreSummary", None, _Object(StkStoreList=[]))
        return True


class BrokerFailureAuditEvidenceTests(unittest.TestCase):
    """Passing assertions below mean failure evidence remains reproducible."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        blocked = RuntimeError("AUDIT_ONLY: real SDK/session/credential/network use blocked")
        for target in (
            "yuanta_broker_execution_v01.sdk.load_api_types",
            "yuanta_broker_execution_v01.sdk.validate_sdk_contract",
            "yuanta_broker_execution_v01.load_api_types",
            "yuanta_broker_execution_v01.validate_sdk_contract",
            "socket.socket",
            "socket.create_connection",
            "subprocess.Popen",
            "subprocess.run",
        ):
            stack.enter_context(patch(target, side_effect=blocked))
        directory = stack.enter_context(TemporaryDirectory(prefix="broker-audit-evidence-"))
        self.store = stack.enter_context(LiveOrderStore(Path(directory) / "audit.sqlite"))
        self.api = AuditOnlyApi()
        gate = LiveTradingGate.from_environment(
            cli_live=True,
            environ={"EXECUTION_MODE": "LIVE", "ENABLE_LIVE_TRADING": "YES"},
        )
        self.adapter = stack.enter_context(
            YuantaSparkExecutionAdapter(
                api=self.api,
                api_types={
                    "List": _FakeGenericList(),
                    "StockOrder": _FakeStockOrder,
                    "Language": _FakeLanguage,
                },
                account=SYNTHETIC_ACCOUNT,
                store=self.store,
                live_gate=gate,
            )
        )
        self.adapter.reconcile(timeout=1)

    def reserve(self, name: str, quantity: int = 1000):
        order, created = self.store.reserve(
            ExecutionIntent(
                intent_id=f"AUDIT-{name}",
                symbol="3605",
                side=Side.BUY,
                quantity=quantity,
                price=Decimal("100"),
            )
        )
        self.assertTrue(created)
        return order

    def bind(self, name: str, quantity: int = 1000):
        order = self.reserve(name, quantity)
        self.store.bind_broker_order(order.client_order_id, f"AUDIT-{name}")
        self.store.acknowledge(order.client_order_id)
        return self.store.get(order.client_order_id)

    def report(self, order, **changes):
        result = {
            "account": SYNTHETIC_ACCOUNT,
            "basket_no": order.basket_no,
            "order_no": order.broker_order_no or "",
            "symbol": order.symbol,
            "side": "B",
            "rpt_type": 50,
            "order_status": 18,
        }
        result.update(changes)
        return result

    def mark_previous_day(self, order):
        stamp = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with self.store._lock, self.store.connection:
            self.store.connection.execute(
                "UPDATE live_orders SET created_at=? WHERE client_order_id=?",
                (stamp, order.client_order_id),
            )

    def test_audit_evidence_duplicate_completed_new_ack_halts(self):
        order = self.bind("duplicate-new-ack")
        identify = self.store.create_request(order.client_order_id, "NEW")
        self.store.complete_request(identify, success=True)
        self.adapter._apply_order_result(
            [{"identify": identify, "reply_code": 0, "order_no": order.broker_order_no}]
        )
        self.assertEqual(
            self.store.control_state()["reason"], "AMBIGUOUS_BROKER_ORDER_RESULT"
        )
        self.assertEqual(self.api.send_calls, [])

    def test_audit_evidence_duplicate_old_ack_corrupts_next_pending_request(self):
        old = self.bind("old-ack")
        old_id = self.store.create_request(old.client_order_id, "NEW")
        self.store.complete_request(old_id, success=True)
        new = self.reserve("next-pending")
        new_id = self.store.create_request(new.client_order_id, "NEW")
        self.store.mark_send_pending(new.client_order_id)
        with self.assertRaisesRegex(StoreError, "already belongs"):
            self.adapter._apply_order_result(
                [{"identify": old_id, "reply_code": 0, "order_no": old.broker_order_no}]
            )
        self.assertEqual(self.store.get_request(new_id)["request_status"], "ACCEPTED")
        self.assertEqual(
            self.store.get(new.client_order_id).status, BrokerOrderStatus.SEND_PENDING
        )
        self.assertEqual(self.api.send_calls, [])

    def test_audit_evidence_cancel_confirmation_before_api_ack_halts(self):
        order = self.bind("cancel-ack-ordering")
        identify = self.store.create_request(order.client_order_id, "CANCEL")
        self.store.request_cancel(order.client_order_id, "AUDIT fake cancellation")
        self.adapter._apply_real_report(self.report(order, order_status=2))
        self.assertEqual(self.store.get_request(identify)["request_status"], "CONFIRMED")
        self.adapter._apply_order_result(
            [{"identify": identify, "reply_code": 0, "order_no": order.broker_order_no}]
        )
        self.assertEqual(
            self.store.control_state()["reason"], "AMBIGUOUS_BROKER_ORDER_RESULT"
        )
        self.assertEqual(
            self.store.get(order.client_order_id).status, BrokerOrderStatus.CANCELED
        )
        self.assertEqual(self.api.send_calls, [])

    def test_audit_evidence_duplicate_reduction_applies_quantity_twice(self):
        order = self.bind("duplicate-reduction", 3000)
        identify = self.store.create_request(
            order.client_order_id, "REDUCE", {"reduce_by": 1000}
        )
        self.store.complete_request(identify, success=True)
        report = self.report(order, order_status=4)
        self.adapter._apply_real_report(report)
        self.assertEqual(self.store.get(order.client_order_id).quantity, 2000)
        self.adapter._apply_real_report(report)
        self.assertEqual(self.store.get(order.client_order_id).quantity, 1000)
        self.assertEqual(self.store.get_request(identify)["request_status"], "CONFIRMED")

    def test_audit_evidence_delayed_ack_resurrects_canceled_order(self):
        order = self.bind("terminal-ack")
        self.store.canceled(order.client_order_id)
        self.adapter._apply_real_report(self.report(order, order_status=18))
        self.assertEqual(
            self.store.get(order.client_order_id).status, BrokerOrderStatus.ACKNOWLEDGED
        )
        self.assertIn(order.client_order_id, [x.client_order_id for x in self.store.orders(open_only=True)])

    def test_audit_evidence_late_partial_fill_reopens_canceled_residual(self):
        order = self.bind("late-partial", 2000)
        self.store.canceled(order.client_order_id)
        self.adapter._apply_real_report(
            self.report(order, rpt_type=51, order_qty=1000, price="100", seq_no="AUDIT-1")
        )
        current = self.store.get(order.client_order_id)
        self.assertEqual(current.filled_quantity, 1000)
        self.assertEqual(current.remaining_quantity, 1000)
        self.assertEqual(current.status, BrokerOrderStatus.PARTIALLY_FILLED)

    def test_audit_evidence_reused_cross_day_fill_identity_loses_today_fill(self):
        old = self.bind("reused-cross-day")
        self.adapter._apply_real_report(
            self.report(old, rpt_type=51, order_qty=1000, price="100", seq_no="AUDIT-1")
        )
        self.mark_previous_day(old)
        new = self.reserve("today-different-basket")
        self.store.bind_broker_order(new.client_order_id, old.broker_order_no)
        new = self.store.get(new.client_order_id)
        with self.assertRaisesRegex(DuplicateIntentConflict, "different payload"):
            self.adapter._apply_real_report(
                self.report(new, rpt_type=51, order_qty=1000, price="100", seq_no="AUDIT-1")
            )
        self.assertEqual(self.store.get(new.client_order_id).filled_quantity, 0)
        self.assertEqual(len(self.store.fills()), 1)

    def test_audit_evidence_conflicting_report_identity_updates_wrong_position(self):
        order = self.bind("foreign-identity")
        self.adapter._apply_real_report(
            self.report(
                order,
                account=OTHER_SYNTHETIC_ACCOUNT,
                basket_no="AUDIT-external-basket",
                symbol="2330",
                side="S",
                rpt_type=51,
                order_qty=1000,
                price="900",
                seq_no="AUDIT-foreign-1",
            )
        )
        self.assertEqual(self.store.get(order.client_order_id).filled_quantity, 1000)
        self.assertEqual(self.store.positions(), {"3605": 1000})

    def test_audit_evidence_historical_number_hides_external_open_order(self):
        historical = self.bind("historical-external-number")
        self.store.reject(historical.client_order_id, "AUDIT historical terminal order")
        self.mark_previous_day(historical)
        remote = {
            "account": SYNTHETIC_ACCOUNT,
            "basket_no": "AUDIT-unowned-today-basket",
            "order_no": historical.broker_order_no,
            "symbol": "2330",
            "side": "S",
            "order_status": 20,
            "last_order_status": 0,
            "order_qty": 1000,
            "ok_qty": 0,
        }
        self.assertEqual(self.adapter._compare_orders([remote]), [])

    def test_audit_evidence_merge_confirmed_modify_blocks_emergency_cancel(self):
        order = self.bind("lost-modify-detail")
        identify = self.store.create_request(
            order.client_order_id, "MODIFY_PRICE", {"new_price": "101"}
        )
        self.store.complete_request(identify, success=True)
        self.adapter._apply_merge_live(
            self.report(
                order,
                order_qty=1000,
                ok_qty=0,
                order_status=20,
                last_order_status=20,
            )
        )
        self.assertEqual(
            self.store.pending_mutation(order.client_order_id)["request_status"], "ACCEPTED"
        )
        with self.assertRaisesRegex(BrokerAdapterError, "mutation already in flight: MODIFY_PRICE"):
            self.adapter.cancel(order.client_order_id, "AUDIT force flat", emergency=True)
        self.assertEqual(self.api.send_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
