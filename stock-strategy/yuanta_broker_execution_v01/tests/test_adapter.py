from decimal import Decimal
from pathlib import Path
import tempfile
import time
import unittest

from yuanta_broker_execution_v01 import (
    BrokerOrderStatus,
    BrokerStateHalted,
    ExecutionIntent,
    IntentPurpose,
    LiveExecutionDisabled,
    LiveTradingGate,
    LiveOrderStore,
    ReconciliationMismatch,
    ReconciliationRequired,
    Side,
    StockOrderType,
    YuantaSparkExecutionAdapter,
)


class EventHook:
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


class FakeList(list):
    def Add(self, value):
        self.append(value)


class FakeGenericList:
    def __class_getitem__(cls, _item):
        return FakeList

    def __getitem__(self, _item):
        return FakeList


class FakeStockOrder:
    def __init__(self):
        self.Identify = 0


class FakeLanguage:
    UTF8 = "UTF8"


class Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeApi:
    def __init__(self):
        self.OnResponse = EventHook()
        self.sent = []
        self.send_result = True
        self.merge_rows = []
        self.positions = {}

    def SendStockOrder(self, account, orders, language):
        self.sent.append((account, list(orders), language))
        return self.send_result

    def GetRealReport(self, account, language):
        value = Obj(RealReportList=[])
        self.OnResponse.emit(1, 0, "GetRealReport", None, value)
        return True

    def GetRealReportMerge(self, account, language):
        rows = [Obj(**row) for row in self.merge_rows]
        value = Obj(RealReportMergeList=rows)
        self.OnResponse.emit(1, 1, "GetRealReportMerge", None, value)
        return True

    def GetStoreSummary(self, account, language):
        rows = []
        for raw_symbol, value in self.positions.items():
            symbol, _, encoded_trade_kind = str(raw_symbol).partition("|")
            if isinstance(value, tuple):
                quantity, trade_kind = value
            else:
                quantity = value
                trade_kind = int(encoded_trade_kind or 0)
            rows.append(Obj(StkCode=symbol, StockQty=quantity, TradeKind=trade_kind))
        value = Obj(StkStoreList=rows)
        self.OnResponse.emit(1, 2, "GetStoreSummary", None, value)
        return True


def api_types():
    return {
        "List": FakeGenericList,
        "StockOrder": FakeStockOrder,
        "Language": FakeLanguage,
    }


class AdapterTests(unittest.TestCase):
    @staticmethod
    def live_gate():
        return LiveTradingGate.from_environment(
            cli_live=True,
            environ={"EXECUTION_MODE": "LIVE", "ENABLE_LIVE_TRADING": "YES"},
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LiveOrderStore(Path(self.temp.name) / "orders.sqlite")
        self.api = FakeApi()
        self.adapter = YuantaSparkExecutionAdapter(
            api=self.api,
            api_types=api_types(),
            account="S12341234567",
            store=self.store,
            live_gate=self.live_gate(),
        )
        self.adapter.reconcile(timeout=1)

    def tearDown(self):
        self.adapter.close()
        self.store.close()
        self.temp.cleanup()

    def intent(
        self,
        intent_id="signal-1",
        quantity=1000,
        side=Side.BUY,
        order_type=StockOrderType.CASH,
    ):
        return ExecutionIntent(
            intent_id=intent_id,
            symbol="3605",
            side=side,
            quantity=quantity,
            price=Decimal("171.5"),
            order_type=order_type,
        )

    def drain(self):
        self.adapter._queue.join()

    def test_live_gate_defaults_off(self):
        other_api = FakeApi()
        other = YuantaSparkExecutionAdapter(
            api=other_api,
            api_types=api_types(),
            account="S12341234567",
            store=self.store,
        )
        try:
            with self.assertRaises(LiveExecutionDisabled):
                other.submit(self.intent("blocked"))
        finally:
            other.close()

    def test_live_send_requires_startup_reconciliation_by_default(self):
        other_api = FakeApi()
        other_store = LiveOrderStore(Path(self.temp.name) / "reconcile-first.sqlite")
        other = YuantaSparkExecutionAdapter(
            api=other_api,
            api_types=api_types(),
            account="S12341234567",
            store=other_store,
            live_gate=self.live_gate(),
        )
        try:
            with self.assertRaises(ReconciliationRequired):
                other.submit(self.intent("needs-reconcile"))
            result = other.reconcile(timeout=1)
            self.assertEqual(result.status, "MATCH")
            submitted = other.submit(self.intent("after-reconcile"))
            self.assertEqual(submitted.status, BrokerOrderStatus.SEND_PENDING)
        finally:
            other.close()
            other_store.close()

    def test_submit_maps_intent_and_duplicate_does_not_resend(self):
        first = self.adapter.submit(self.intent())
        self.assertEqual(first.status, BrokerOrderStatus.SEND_PENDING)
        self.assertEqual(len(self.api.sent), 1)
        stock = self.api.sent[0][1][0]
        self.assertEqual(stock.Account, "S12341234567")
        self.assertEqual(stock.StkCode, "3605")
        self.assertEqual(stock.OrderQty, 1000)
        self.assertEqual(stock.TradeKind, 0)
        self.assertEqual(stock.Price, 171.5)
        self.assertEqual(stock.PriceFlag, " ")
        self.assertEqual(len(stock.BasketNo), 32)

        second = self.adapter.submit(self.intent())
        self.assertEqual(first.client_order_id, second.client_order_id)
        self.assertEqual(len(self.api.sent), 1)

    def test_submit_rejects_raw_signal_or_mapping(self):
        with self.assertRaises(TypeError):
            self.adapter.submit({
                "intent_id": "raw-signal",
                "symbol": "3605",
                "side": "BUY",
                "quantity": 1000,
                "price": 171.5,
            })
        self.assertEqual(len(self.api.sent), 0)

    def test_rescue_exit_is_allowed_while_halted_but_entry_is_not(self):
        entry = self.adapter.submit(self.intent("filled-entry"))
        self.store.record_fill(
            entry.client_order_id,
            fill_id="filled-entry-1",
            quantity=1000,
            price="171.5",
        )
        self.api.sent.clear()
        self.store.halt("emergency")
        with self.assertRaises(BrokerStateHalted):
            self.adapter.submit(self.intent("blocked-entry"))
        rescue = ExecutionIntent(
            intent_id="rescue-exit-1",
            symbol="3605",
            side=Side.SELL,
            quantity=1000,
            price=Decimal("171.0"),
            order_type=StockOrderType.CASH,
            purpose=IntentPurpose.EXIT,
        )
        order = self.adapter.submit_rescue(rescue)
        self.assertEqual(order.status, BrokerOrderStatus.SEND_PENDING)
        self.assertEqual(len(self.api.sent), 1)

    def test_rescue_exit_cannot_create_exposure_from_flat(self):
        self.store.halt("emergency")
        rescue = ExecutionIntent(
            intent_id="unsafe-rescue",
            symbol="3605",
            side=Side.SELL,
            quantity=1000,
            price=Decimal("171.0"),
            purpose=IntentPurpose.EXIT,
        )
        with self.assertRaisesRegex(Exception, "reduce an existing"):
            self.adapter.submit_rescue(rescue)

    def test_broker_inspection_uses_actual_remote_positions(self):
        self.api.positions = {"3605": 1000}
        snapshot = self.adapter.inspect_broker_state(timeout=1)
        self.assertEqual(snapshot.positions, {"3605|0": 1000})
        self.assertEqual(snapshot.open_orders, [])

    def test_send_result_binds_order_number(self):
        stored = self.adapter.submit(self.intent())
        value = Obj(
            ResultList=[
                Obj(
                    Identify=stored.identify,
                    ReplyCode=0,
                    OrderNO="f0001",
                    ErrType="",
                    ErrNO="",
                    Advisory="",
                )
            ]
        )
        self.api.OnResponse.emit(1, 1, "SendStockOrder", None, value)
        self.drain()
        updated = self.store.get(stored.client_order_id)
        self.assertEqual(updated.broker_order_no, "f0001")
        self.assertEqual(updated.status, BrokerOrderStatus.ACKNOWLEDGED)

    def test_partial_fill_then_cancel(self):
        stored = self.adapter.submit(self.intent())
        self.store.bind_broker_order(stored.client_order_id, "f0002")
        self.store.acknowledge(stored.client_order_id)

        fill = Obj(
            Account="S12341234567",
            RptType=51,
            OrderNo="f0002",
            CompanyNo="3605",
            BS="B",
            Price=171.5,
            BeforeQty=0,
            OrderQty=400,
            TradeKind=1,
            APCode=0,
            BasketNo=stored.basket_no,
            OrderStatus=8,
            SeqNo=7,
            StkErrorNo="00000",
            OrderErrorNo="",
        )
        self.api.OnResponse.emit(2, 2, "RR_RealReport", None, fill)
        self.drain()
        partial = self.store.get(stored.client_order_id)
        self.assertEqual(partial.filled_quantity, 400)
        self.assertEqual(partial.status, BrokerOrderStatus.PARTIALLY_FILLED)

        pending = self.adapter.cancel(stored.client_order_id)
        self.assertEqual(pending.status, BrokerOrderStatus.CANCEL_PENDING)
        self.assertEqual(self.api.sent[-1][1][0].TradeKind, 4)

        cancel_report = Obj(
            Account="S12341234567",
            RptType=50,
            OrderNo="f0002",
            CompanyNo="3605",
            BS="B",
            Price=171.5,
            BeforeQty=1000,
            OrderQty=600,
            TradeKind=4,
            APCode=0,
            BasketNo=stored.basket_no,
            OrderStatus=2,
            SeqNo=0,
            StkErrorNo="00000",
            OrderErrorNo="",
        )
        self.api.OnResponse.emit(2, 3, "RR_RealReport", None, cancel_report)
        self.drain()
        done = self.store.get(stored.client_order_id)
        self.assertEqual(done.status, BrokerOrderStatus.CANCELED)
        self.assertEqual(self.store.positions(), {"3605": 400})

    def test_duplicate_fill_seq_is_idempotent(self):
        stored = self.adapter.submit(self.intent())
        self.store.bind_broker_order(stored.client_order_id, "f0003")
        for _ in range(2):
            fill = Obj(
                Account="S12341234567",
                RptType=51,
                OrderNo="f0003",
                CompanyNo="3605",
                BS="B",
                Price=171.5,
                BeforeQty=0,
                OrderQty=500,
                TradeKind=1,
                APCode=0,
                BasketNo=stored.basket_no,
                OrderStatus=8,
                SeqNo=9,
                StkErrorNo="00000",
                OrderErrorNo="",
            )
            self.api.OnResponse.emit(2, 2, "RR_RealReport", None, fill)
        self.drain()
        self.assertEqual(self.store.get(stored.client_order_id).filled_quantity, 500)

    def test_false_send_fails_closed_and_never_retries(self):
        self.api.send_result = False
        with self.assertRaises(Exception):
            self.adapter.submit(self.intent())
        stored = self.store.get_by_intent("signal-1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, BrokerOrderStatus.UNKNOWN)
        self.assertTrue(self.store.control_state()["halted"])
        with self.assertRaises(BrokerStateHalted):
            self.adapter.submit(self.intent("signal-2"))

    def test_modify_and_reduce_map_trade_kind(self):
        stored = self.adapter.submit(self.intent())
        self.store.bind_broker_order(stored.client_order_id, "f0004")
        self.store.acknowledge(stored.client_order_id)

        self.adapter.modify_price(stored.client_order_id, "172")
        modified = self.api.sent[-1][1][0]
        self.assertEqual(modified.TradeKind, 7)
        self.assertEqual(modified.OrderNo, "f0004")
        self.assertEqual(modified.Price, 172.0)

        # A second mutation is blocked until the first one reaches a terminal
        # broker report, preventing cancel/replace races.
        with self.assertRaises(Exception):
            self.adapter.reduce_quantity(stored.client_order_id, 200)
        modify_report = Obj(
            Account="S12341234567", RptType=50, OrderNo="f0004",
            CompanyNo="3605", BS="B", Price=172.0, BeforeQty=1000,
            OrderQty=1000, TradeKind=6, APCode=0, BasketNo=stored.basket_no,
            OrderStatus=20, SeqNo=0, StkErrorNo="00000", OrderErrorNo="",
        )
        self.api.OnResponse.emit(2, 2, "RR_RealReport", None, modify_report)
        self.drain()

        self.adapter.reduce_quantity(stored.client_order_id, 200)
        reduced = self.api.sent[-1][1][0]
        self.assertEqual(reduced.TradeKind, 3)
        self.assertEqual(reduced.OrderQty, 200)

    def test_reconciliation_matches_orders_and_positions(self):
        stored = self.adapter.submit(self.intent())
        self.store.bind_broker_order(stored.client_order_id, "f0005")
        self.store.acknowledge(stored.client_order_id)
        self.store.record_fill(
            stored.client_order_id,
            fill_id="f0005:1",
            quantity=1000,
            price="171.5",
            broker_order_no="f0005",
            seq_no="1",
        )
        self.api.merge_rows = [
            {
                "Account": "S12341234567",
                "RptType": 1,
                "OrderNo": "f0005",
                "CompanyNo": "3605",
                "BS": "B",
                "Price": 171.5,
                "LastDealPrice": 171.5,
                "AvgDealPrice": 171.5,
                "BeforeQty": 0,
                "OrderQty": 1000,
                "OkQty": 1000,
                "APCode": 0,
                "OrderStatus": 20,
                "LastOrderStatus": 8,
                "BasketNo": stored.basket_no,
                "StkErrorNo": "",
            }
        ]
        self.api.positions = {"3605": 1000}
        result = self.adapter.reconcile(timeout=1)
        self.assertEqual(result.status, "MATCH")

    def test_reconciliation_ignores_prior_day_terminal_order_history(self):
        stored = self.adapter.submit(self.intent(intent_id="historical-terminal"))
        self.store.bind_broker_order(stored.client_order_id, "f-old")
        self.store.acknowledge(stored.client_order_id)
        self.store.record_fill(
            stored.client_order_id,
            fill_id="f-old:1",
            quantity=1000,
            price="171.5",
            broker_order_no="f-old",
            seq_no="1",
        )
        with self.store._lock, self.store.connection:
            self.store.connection.execute(
                "UPDATE live_orders SET created_at=?, updated_at=? WHERE client_order_id=?",
                ("2020-01-01T00:00:00.000Z", "2020-01-01T00:00:00.000Z", stored.client_order_id),
            )
        self.api.merge_rows = []
        self.api.positions = {"3605": 1000}
        result = self.adapter.reconcile(timeout=1)
        self.assertEqual(result.status, "MATCH")

    def test_reconciliation_position_mismatch_halts(self):
        stored = self.adapter.submit(self.intent())
        self.store.bind_broker_order(stored.client_order_id, "f0006")
        self.store.acknowledge(stored.client_order_id)
        self.api.merge_rows = [
            {
                "Account": "S12341234567",
                "RptType": 1,
                "OrderNo": "f0006",
                "CompanyNo": "3605",
                "BS": "B",
                "Price": 171.5,
                "LastDealPrice": 0,
                "AvgDealPrice": 0,
                "BeforeQty": 0,
                "OrderQty": 1000,
                "OkQty": 0,
                "APCode": 0,
                "OrderStatus": 20,
                "LastOrderStatus": 0,
                "BasketNo": stored.basket_no,
                "StkErrorNo": "",
            }
        ]
        self.api.positions = {"2330": 1000}
        with self.assertRaises(ReconciliationMismatch):
            self.adapter.reconcile(timeout=1)
        self.assertTrue(self.store.control_state()["halted"])


    def test_cancel_send_ack_does_not_clear_cancel_pending(self):
        stored = self.adapter.submit(self.intent())
        self.store.bind_broker_order(stored.client_order_id, "f0007")
        self.store.acknowledge(stored.client_order_id)
        pending = self.adapter.cancel(stored.client_order_id)
        self.assertEqual(pending.status, BrokerOrderStatus.CANCEL_PENDING)

        cancel_identify = self.api.sent[-1][1][0].Identify
        value = Obj(
            ResultList=[
                Obj(
                    Identify=cancel_identify,
                    ReplyCode=0,
                    OrderNO="f0007",
                    ErrType="",
                    ErrNO="",
                    Advisory="",
                )
            ]
        )
        self.api.OnResponse.emit(1, 1, "SendStockOrder", None, value)
        self.drain()
        self.assertEqual(
            self.store.get(stored.client_order_id).status,
            BrokerOrderStatus.CANCEL_PENDING,
        )

    def test_reduce_success_updates_effective_quantity(self):
        stored = self.adapter.submit(self.intent(quantity=1000))
        self.store.bind_broker_order(stored.client_order_id, "f0008")
        self.store.acknowledge(stored.client_order_id)
        self.adapter.reduce_quantity(stored.client_order_id, 200)

        reduce_identify = self.api.sent[-1][1][0].Identify
        result = Obj(
            ResultList=[
                Obj(
                    Identify=reduce_identify,
                    ReplyCode=0,
                    OrderNO="f0008",
                    ErrType="",
                    ErrNO="",
                    Advisory="",
                )
            ]
        )
        self.api.OnResponse.emit(1, 1, "SendStockOrder", None, result)
        self.drain()

        report = Obj(
            Account="S12341234567",
            RptType=50,
            OrderNo="f0008",
            CompanyNo="3605",
            BS="B",
            Price=171.5,
            BeforeQty=1000,
            OrderQty=800,
            TradeKind=3,
            APCode=0,
            BasketNo=stored.basket_no,
            OrderStatus=4,
            SeqNo=0,
            StkErrorNo="00000",
            OrderErrorNo="",
        )
        self.api.OnResponse.emit(2, 2, "RR_RealReport", None, report)
        self.drain()
        updated = self.store.get(stored.client_order_id)
        self.assertEqual(updated.quantity, 800)
        self.assertEqual(updated.remaining_quantity, 800)

    def test_filled_order_status_before_fill_does_not_false_halt(self):
        stored = self.adapter.submit(self.intent(quantity=1000))
        self.store.bind_broker_order(stored.client_order_id, "f0010")
        self.store.acknowledge(stored.client_order_id)
        order_status = Obj(
            Account="S12341234567", RptType=50, OrderNo="f0010",
            CompanyNo="3605", BS="B", Price=171.5, BeforeQty=1000,
            OrderQty=1000, TradeKind=1, APCode=0, BasketNo=stored.basket_no,
            OrderStatus=8, SeqNo=0, StkErrorNo="00000", OrderErrorNo="",
        )
        self.api.OnResponse.emit(2, 2, "RR_RealReport", None, order_status)
        self.drain()
        self.assertFalse(self.store.control_state()["halted"])
        fill = Obj(
            Account="S12341234567", RptType=51, OrderNo="f0010",
            CompanyNo="3605", BS="B", Price=171.5, BeforeQty=0,
            OrderQty=1000, TradeKind=1, APCode=0, BasketNo=stored.basket_no,
            OrderStatus=8, SeqNo=101, StkErrorNo="00000", OrderErrorNo="",
        )
        self.api.OnResponse.emit(2, 2, "RR_RealReport", None, fill)
        self.drain()
        self.assertEqual(
            self.store.get(stored.client_order_id).status, BrokerOrderStatus.FILLED
        )

    def test_reconciliation_replays_missed_fill_from_get_real_report(self):
        stored = self.adapter.submit(self.intent())
        self.store.bind_broker_order(stored.client_order_id, "f0009")
        self.store.acknowledge(stored.client_order_id)

        def get_real_report(account, language):
            fill = Obj(
                Account=account,
                RptType=51,
                OrderNo="f0009",
                CompanyNo="3605",
                BS="B",
                Price=171.5,
                BeforeQty=0,
                OrderQty=1000,
                TradeKind=1,
                APCode=0,
                BasketNo=stored.basket_no,
                OrderStatus=8,
                SeqNo=99,
                StkErrorNo="00000",
                OrderErrorNo="",
            )
            self.api.OnResponse.emit(
                1, 0, "GetRealReport", None, Obj(RealReportList=[fill])
            )
            return True

        self.api.GetRealReport = get_real_report
        self.api.merge_rows = [
            {
                "Account": "S12341234567",
                "RptType": 1,
                "OrderNo": "f0009",
                "CompanyNo": "3605",
                "BS": "B",
                "Price": 171.5,
                "LastDealPrice": 171.5,
                "AvgDealPrice": 171.5,
                "BeforeQty": 0,
                "OrderQty": 1000,
                "OkQty": 1000,
                "APCode": 0,
                "OrderStatus": 20,
                "LastOrderStatus": 8,
                "BasketNo": stored.basket_no,
                "StkErrorNo": "",
            }
        ]
        self.api.positions = {"3605": 1000}
        result = self.adapter.reconcile(timeout=1)
        self.assertEqual(result.status, "MATCH")
        self.assertEqual(self.store.get(stored.client_order_id).filled_quantity, 1000)

    def test_short_inventory_uses_trade_kind_sign(self):
        stored = self.adapter.submit(
            self.intent(
                intent_id="short-1",
                side=Side.SELL,
                order_type=StockOrderType.SHORT_SELL,
            )
        )
        self.store.bind_broker_order(stored.client_order_id, "s0001")
        self.store.acknowledge(stored.client_order_id)
        self.store.record_fill(
            stored.client_order_id,
            fill_id="s0001:1",
            quantity=1000,
            price="171.5",
            broker_order_no="s0001",
            seq_no="1",
        )
        self.api.merge_rows = [{
            "Account": "S12341234567", "RptType": 1, "OrderNo": "s0001",
            "CompanyNo": "3605", "BS": "S", "Price": 171.5,
            "LastDealPrice": 171.5, "AvgDealPrice": 171.5, "BeforeQty": 0,
            "OrderQty": 1000, "OkQty": 1000, "APCode": 0,
            "OrderStatus": 20, "LastOrderStatus": 8,
            "BasketNo": stored.basket_no, "StkErrorNo": "",
        }]
        self.api.positions = {"3605": (1000, 4)}
        result = self.adapter.reconcile(timeout=1)
        self.assertEqual(result.broker_positions, {"3605|4": -1000})
        self.assertEqual(result.expected_broker_positions, {"3605|4": -1000})

    def test_reviewed_position_baseline_allows_unmanaged_inventory(self):
        api = FakeApi()
        api.positions = {"2330": (1000, 0)}
        store = LiveOrderStore(Path(self.temp.name) / "baseline.sqlite")
        adapter = YuantaSparkExecutionAdapter(
            api=api,
            api_types=api_types(),
            account="S12341234567",
            store=store,
            live_gate=self.live_gate(),
            position_baseline={"2330|0": 1000},
        )
        try:
            result = adapter.reconcile(timeout=1)
            self.assertEqual(result.status, "MATCH")
            self.assertEqual(result.position_baseline, {"2330|0": 1000})
        finally:
            adapter.close()
            store.close()

    def test_cash_and_short_inventory_do_not_net_across_trade_kinds(self):
        api = FakeApi()
        api.positions = {"3605|0": 1000, "3605|4": 1000}
        store = LiveOrderStore(Path(self.temp.name) / "mixed-inventory.sqlite")
        adapter = YuantaSparkExecutionAdapter(
            api=api,
            api_types=api_types(),
            account="S12341234567",
            store=store,
            live_gate=self.live_gate(),
            position_baseline={"3605|0": 1000, "3605|4": -1000},
        )
        try:
            result = adapter.reconcile(timeout=1)
            self.assertEqual(
                result.broker_positions,
                {"3605|0": 1000, "3605|4": -1000},
            )
        finally:
            adapter.close()
            store.close()

    def test_close_drains_callback_queue(self):
        stored = self.adapter.submit(self.intent(intent_id="close-drain"))
        value = Obj(ResultList=[Obj(
            Identify=stored.identify, ReplyCode=0, OrderNO="f-close",
            ErrType="", ErrNO="", Advisory="",
        )])
        self.api.OnResponse.emit(1, 1, "SendStockOrder", None, value)
        self.adapter.close()
        updated = self.store.get(stored.client_order_id)
        self.assertEqual(updated.broker_order_no, "f-close")
        self.assertEqual(updated.status, BrokerOrderStatus.ACKNOWLEDGED)


if __name__ == "__main__":
    unittest.main()
