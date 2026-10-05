"""Mock-only coverage for verified manual-trade coexistence.

No SDK, credentials, network, broker session, or real order route is used.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from yuanta_broker_execution_v01 import (
    BrokerOrderStatus,
    ExecutionIntent,
    ExternalManualOrderConflict,
    LiveOrderStore,
    LiveTradingGate,
    ReconciliationMismatch,
    Side,
    YuantaSparkExecutionAdapter,
)
from yuanta_broker_execution_v01.tests.test_adapter import (
    FakeApi,
    Obj,
    api_types,
)


TAIPEI = ZoneInfo("Asia/Taipei")
ACCOUNT = "S12341234567"


class ExternalManualInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LiveOrderStore(Path(self.temp.name) / "orders.sqlite")
        self.addCleanup(self.store.close)
        self.api = FakeApi()
        now = datetime.now(TAIPEI)
        self.captured_at = now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        self.adapter = self.make_adapter()
        self.addCleanup(lambda: self.adapter.close())

    @staticmethod
    def live_gate():
        return LiveTradingGate.from_environment(
            cli_live=True,
            environ={"EXECUTION_MODE": "LIVE", "ENABLE_LIVE_TRADING": "YES"},
        )

    def make_adapter(self, *, baseline=None, listener=None):
        return YuantaSparkExecutionAdapter(
            api=self.api,
            api_types=api_types(),
            account=ACCOUNT,
            store=self.store,
            live_gate=self.live_gate(),
            position_baseline=baseline or {},
            position_baseline_captured_at=self.captured_at.isoformat(),
            external_inventory_listener=listener,
        )

    def manual_row(
        self,
        *,
        order_no: str,
        symbol: str,
        side: str = "B",
        quantity: int = 1000,
        filled: int = 1000,
        order_status: int = 20,
        last_order_status: int = 8,
        basket_no: str = "",
        order_type: int = 0,
    ):
        stamp = datetime.now(TAIPEI)
        return {
            "Account": ACCOUNT,
            "RptType": 1,
            "OrderNo": order_no,
            "CompanyNo": symbol,
            "BS": side,
            "OrderType": order_type,
            "Price": 100,
            "LastDealPrice": 100,
            "AvgDealPrice": 100,
            "BeforeQty": 0,
            "OrderQty": quantity,
            "OkQty": filled,
            "APCode": 0,
            "OrderStatus": order_status,
            "LastOrderStatus": last_order_status,
            "BasketNo": basket_no,
            "StkErrorNo": "",
            "TradeDate": stamp.strftime("%Y%m%d"),
            "OrderTime": Obj(
                bytHour=stamp.hour,
                bytMin=stamp.minute,
                bytSec=stamp.second,
                ushtMSec=max(1, stamp.microsecond // 1000),
            ),
        }

    @staticmethod
    def intent(intent_id: str, symbol: str = "3605"):
        return ExecutionIntent(
            intent_id=intent_id,
            symbol=symbol,
            side=Side.BUY,
            quantity=1000,
            price=Decimal("100"),
        )

    def test_unrelated_manual_buy_is_adopted_and_persists_across_restart(self):
        events = []
        self.adapter.external_inventory_listener = events.append
        self.api.merge_rows = [
            self.manual_row(order_no="m001", symbol="2330")
        ]
        self.api.positions = {"2330": 1000}

        result = self.adapter.reconcile(timeout=1)

        self.assertEqual(result.external_position_adjustments, {"2330|0": 1000})
        self.assertEqual(result.position_baseline, {"2330|0": 1000})
        self.assertEqual(len(result.external_orders_adopted), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(self.store.position_buckets(), {})

        self.adapter.close()
        self.adapter = self.make_adapter()
        again = self.adapter.reconcile(timeout=1)
        self.assertEqual(again.position_baseline, {"2330|0": 1000})
        self.assertEqual(again.external_orders_adopted, [])

    def test_manual_sale_of_baseline_holding_updates_only_external_baseline(self):
        self.adapter.close()
        self.adapter = self.make_adapter(baseline={"0050|0": 1000})
        self.api.merge_rows = [
            self.manual_row(order_no="m002", symbol="0050", side="S")
        ]
        self.api.positions = {}

        result = self.adapter.reconcile(timeout=1)

        self.assertEqual(result.external_position_adjustments, {"0050|0": -1000})
        self.assertEqual(result.position_baseline, {})
        self.assertEqual(self.store.position_buckets(), {})

    def test_unrelated_partial_fill_advances_monotonically(self):
        self.api.merge_rows = [
            self.manual_row(
                order_no="m003",
                symbol="2317",
                quantity=1000,
                filled=400,
                last_order_status=0,
            )
        ]
        self.api.positions = {"2317": 400}
        first = self.adapter.reconcile(timeout=1)
        self.assertEqual(first.external_position_adjustments, {"2317|0": 400})

        self.api.merge_rows = [
            self.manual_row(order_no="m003", symbol="2317")
        ]
        self.api.positions = {"2317": 1000}
        second = self.adapter.reconcile(timeout=1)
        self.assertEqual(second.external_position_adjustments, {"2317|0": 1000})
        self.assertEqual(second.external_orders_adopted[0]["delta_quantity"], 600)

    def test_open_manual_order_on_other_symbol_does_not_block_reconcile(self):
        self.api.merge_rows = [
            self.manual_row(
                order_no="m004",
                symbol="2330",
                filled=0,
                last_order_status=0,
            )
        ]
        self.api.positions = {}
        self.assertEqual(self.adapter.reconcile(timeout=1).status, "MATCH")
        self.assertFalse(self.store.control_state()["halted"])

    def test_unknown_basket_open_order_still_halts(self):
        self.api.merge_rows = [
            self.manual_row(
                order_no="m004b",
                symbol="2330",
                filled=0,
                last_order_status=0,
                basket_no="UNKNOWN-BASKET",
            )
        ]
        self.api.positions = {}

        with self.assertRaises(ReconciliationMismatch):
            self.adapter.reconcile(timeout=1)
        self.assertTrue(self.store.control_state()["halted"])

    def test_open_manual_order_on_entry_symbol_skips_candidate_without_halt(self):
        self.adapter.reconcile(timeout=1)
        self.api.merge_rows = [
            self.manual_row(
                order_no="m005",
                symbol="3605",
                filled=0,
                last_order_status=0,
            )
        ]
        self.api.positions = {}

        with self.assertRaises(ExternalManualOrderConflict):
            self.adapter.submit(self.intent("same-symbol"))

        self.assertEqual(self.api.sent, [])
        self.assertFalse(self.store.control_state()["halted"])
        self.assertEqual(
            self.store.get_by_intent("same-symbol").status,
            BrokerOrderStatus.REJECTED,
        )
        later = self.adapter.submit(self.intent("different-symbol", symbol="2330"))
        self.assertEqual(later.status, BrokerOrderStatus.SEND_PENDING)
        self.assertEqual(len(self.api.sent), 1)

    def test_partial_manual_fill_on_entry_symbol_is_adopted_then_candidate_skipped(self):
        self.adapter.reconcile(timeout=1)
        self.api.merge_rows = [
            self.manual_row(
                order_no="m005p",
                symbol="3605",
                quantity=1000,
                filled=400,
                last_order_status=0,
            )
        ]
        self.api.positions = {"3605": 400}

        with self.assertRaises(ExternalManualOrderConflict):
            self.adapter.submit(self.intent("same-symbol-partial"))

        self.assertEqual(self.api.sent, [])
        self.assertFalse(self.store.control_state()["halted"])
        self.assertEqual(self.adapter.position_baseline, {"3605|0": 400})
        self.assertEqual(
            self.store.external_position_adjustments(
                datetime.now(TAIPEI).strftime("%Y%m%d")
            ),
            {"3605|0": 400},
        )

    def test_completed_manual_fill_does_not_permanently_block_same_symbol_entry(self):
        self.adapter.reconcile(timeout=1)
        self.api.merge_rows = [
            self.manual_row(order_no="m005b", symbol="3605")
        ]
        self.api.positions = {"3605": 1000}

        submitted = self.adapter.submit(self.intent("after-manual-fill"))

        self.assertEqual(submitted.status, BrokerOrderStatus.SEND_PENDING)
        self.assertEqual(self.adapter.position_baseline, {"3605|0": 1000})
        self.assertEqual(
            self.store.external_position_adjustments(
                datetime.now(TAIPEI).strftime("%Y%m%d")
            ),
            {"3605|0": 1000},
        )
        self.assertEqual(len(self.api.sent), 1)

        self.store.bind_broker_order(submitted.client_order_id, "bot005b")
        self.store.record_fill(
            submitted.client_order_id,
            fill_id="bot005b:1",
            quantity=1000,
            price="100",
            broker_order_no="bot005b",
        )
        bot_row = self.manual_row(
            order_no="bot005b",
            symbol="3605",
            basket_no=submitted.basket_no,
        )
        self.api.merge_rows = [
            self.manual_row(order_no="m005b", symbol="3605"),
            bot_row,
        ]
        self.api.positions = {"3605": 2000}
        self.assertEqual(self.adapter.reconcile(timeout=1).status, "MATCH")

        self.api.merge_rows[0] = self.manual_row(
            order_no="m005b",
            symbol="3605",
            quantity=2000,
            filled=2000,
        )
        self.api.positions = {"3605": 3000}
        with self.assertRaises(ReconciliationMismatch):
            self.adapter.reconcile(timeout=1)
        self.assertTrue(self.store.control_state()["halted"])

    def test_manual_conflict_cannot_mask_unexplained_position_mismatch(self):
        self.adapter.reconcile(timeout=1)
        self.api.merge_rows = [
            self.manual_row(
                order_no="m005c",
                symbol="3605",
                filled=0,
                last_order_status=0,
            )
        ]
        self.api.positions = {"2330": 1000}

        with self.assertRaises(ReconciliationMismatch):
            self.adapter.submit(self.intent("conflict-plus-mismatch"))

        self.assertEqual(self.api.sent, [])
        self.assertTrue(self.store.control_state()["halted"])

    def test_manual_fill_on_strategy_position_symbol_halts(self):
        reserved, _ = self.store.reserve(self.intent("owned-position"))
        self.store.bind_broker_order(reserved.client_order_id, "bot001")
        self.store.record_fill(
            reserved.client_order_id,
            fill_id="bot001:1",
            quantity=1000,
            price="100",
            broker_order_no="bot001",
        )
        bot_row = self.manual_row(
            order_no="bot001",
            symbol="3605",
            basket_no=reserved.basket_no,
        )
        external_row = self.manual_row(order_no="m006", symbol="3605")
        self.api.merge_rows = [bot_row, external_row]
        self.api.positions = {"3605": 2000}

        with self.assertRaises(ReconciliationMismatch):
            self.adapter.reconcile(timeout=1)
        self.assertTrue(self.store.control_state()["halted"])

    def test_unknown_nonempty_basket_is_never_adopted_as_manual(self):
        self.api.merge_rows = [
            self.manual_row(
                order_no="m007",
                symbol="2330",
                basket_no="UNKNOWN-BASKET",
            )
        ]
        self.api.positions = {"2330": 1000}

        with self.assertRaises(ReconciliationMismatch):
            self.adapter.reconcile(timeout=1)
        self.assertEqual(self.store.external_position_adjustments(
            datetime.now(TAIPEI).strftime("%Y%m%d")
        ), {})

    def test_prebaseline_manual_order_is_not_double_counted(self):
        stamp = self.captured_at
        row = self.manual_row(order_no="m008", symbol="2330")
        row["OrderTime"] = Obj(
            bytHour=stamp.hour,
            bytMin=stamp.minute,
            bytSec=stamp.second,
            ushtMSec=0,
        )
        self.adapter.close()
        self.adapter = self.make_adapter(baseline={"2330|0": 1000})
        self.api.merge_rows = [row]
        self.api.positions = {"2330": 1000}

        result = self.adapter.reconcile(timeout=1)
        self.assertEqual(result.external_orders_adopted, [])
        self.assertEqual(result.position_baseline, {"2330|0": 1000})


if __name__ == "__main__":
    unittest.main()
