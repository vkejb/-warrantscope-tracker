"""Mock-only evidence tests for observed entry and final-send safety gaps.

A passing test means the described gap is reproduced in the current code.  It
does not mean that production is safe or that the gap has been repaired.  No
broker, credentials, runtime directory, or external service is used here.
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from yuanta_broker_execution_v01 import (
    ExecutionIntent,
    IntentPurpose,
    LiveOrderStore,
    LiveTradingGate,
    Side,
    YuantaSparkExecutionAdapter,
)
from yuanta_broker_execution_v01.tests.test_adapter import FakeApi, api_types
from yuanta_live_runtime_v01.risk_manager import RiskLimits, RiskManager
from yuanta_live_runtime_v01.strategy import TAIPEI


class EntryRiskEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LiveOrderStore(Path(self.temp.name) / "mock-orders.sqlite")
        self.api = FakeApi()
        self.adapter = YuantaSparkExecutionAdapter(
            api=self.api,
            api_types=api_types(),
            account="S00000000000",  # Synthetic test fixture, never a real login.
            store=self.store,
            live_gate=LiveTradingGate.from_environment(
                cli_live=True,
                environ={"EXECUTION_MODE": "LIVE", "ENABLE_LIVE_TRADING": "YES"},
            ),
        )
        self.adapter.reconcile(timeout=1)

    def tearDown(self):
        self.adapter.close()
        self.store.close()
        self.temp.cleanup()

    @staticmethod
    def intent(intent_id, *, purpose=IntentPurpose.ENTRY, side=Side.BUY, quantity=1000):
        return ExecutionIntent(
            intent_id=intent_id,
            symbol="TEST",
            side=side,
            quantity=quantity,
            price=Decimal("100"),
            purpose=purpose,
        )

    def test_entry_still_sent_if_halt_activates_during_preorder_reconciliation(self):
        original_reconcile = self.adapter.reconcile

        def reconcile_then_halt(**kwargs):
            result = original_reconcile(**kwargs)
            self.store.halt("AUDIT_KILL_DURING_PREORDER_RECONCILIATION")
            return result

        self.adapter.reconcile = reconcile_then_halt
        self.adapter.submit(self.intent("halt-race"))
        self.assertTrue(self.store.control_state()["halted"])
        self.assertEqual(len(self.api.sent), 1)

    def test_normal_exit_submission_can_create_sell_from_flat(self):
        self.assertEqual(self.store.position_buckets(), {})
        self.adapter.submit(self.intent(
            "flat-exit", purpose=IntentPurpose.EXIT, side=Side.SELL,
        ))
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.api.sent[0][1][0].BuySell, "S")

    def test_normal_exit_submission_can_exceed_reconciled_position(self):
        entry = self.adapter.submit(self.intent("entry"))
        self.store.record_fill(
            entry.client_order_id, fill_id="mock-entry-fill", quantity=1000,
            price="100",
        )
        self.api.merge_rows = [{
            "Account": "S00000000000", "RptType": 1,
            "OrderNo": "mock-completed-entry", "CompanyNo": "TEST", "BS": "B",
            "Price": 100, "LastDealPrice": 100, "AvgDealPrice": 100,
            "BeforeQty": 0, "OrderQty": 1000, "OkQty": 1000, "APCode": 0,
            "OrderStatus": 20, "LastOrderStatus": 8,
            "BasketNo": entry.basket_no, "StkErrorNo": "",
        }]
        self.api.positions = {"TEST": 1000}
        self.adapter.reconcile(timeout=1)
        self.adapter.submit(self.intent(
            "oversized-exit", purpose=IntentPurpose.EXIT, side=Side.SELL,
            quantity=2000,
        ))
        self.assertEqual(self.store.position_buckets(), {"TEST|0": 1000})
        self.assertEqual(self.api.sent[-1][1][0].OrderQty, 2)

    def test_optional_share_cap_can_approve_non_board_lot(self):
        risk = RiskManager(RiskLimits(max_position_per_stock=1500))
        signal = SimpleNamespace(
            stock_id="TEST", side="LONG", quantity=2000, entry_price=50.0,
            decision_time=datetime(2026, 10, 2, 9, 10, tzinfo=TAIPEI),
        )
        result = risk.evaluate_entry(
            signal=signal, quote_age_seconds=0.5, broker_positions={},
            open_orders=[], trades_today=0,
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.intent["quantity_lots"], "1.5")
        with self.assertRaisesRegex(Exception, "multiple of 1000"):
            self.adapter.submit(self.intent("odd-cap", quantity=1500))
        self.assertEqual(self.api.sent, [])


if __name__ == "__main__":
    unittest.main()
