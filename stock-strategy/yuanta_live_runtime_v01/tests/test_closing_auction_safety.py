"""Closing-auction safety: pure contracts/mocks, never SDK/login/orders.

Primary sources reviewed 2026-10-03:
https://shl.twse.com.tw/page/trading/1.html (table: closing accepts limit ROD)
https://www.tpex.org.tw/zh-tw/mainboard/trading/rules/continuous.html
https://www.yuanta.com.tw/file-repository/content/sparkapi_docs/交易/國內證券下單/index.html
SPARK PriceFlag H=limit-up,L=limit-down,Price=0; Time_in_force 0=ROD.
"""
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from yuanta_broker_execution_v01 import (
    APCode, BrokerOrderStatus, ExecutionIntent, IntentPurpose, PriceType,
    Side, StockOrderType, TimeInForce, LiveOrderStore, YuantaSparkExecutionAdapter,
)
from yuanta_broker_execution_v01.models import PRICE_FLAG, TIF_CODE
from yuanta_live_runtime_v01 import main as runtime
from yuanta_live_runtime_v01.tests import test_runtime_safety_regressions as runtime_fixtures


class ClosingAuctionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target in ("socket.socket", "socket.create_connection", "subprocess.run",
                       "subprocess.Popen", "yuanta_live_runtime_v01.main.load_credentials",
                       "yuanta_live_runtime_v01.main.load_api_types"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("MOCK_ONLY")))
        self.position = SimpleNamespace(side="LONG", stock_id="3094", entry_order_id="MOCK-ENTRY")
        self.engine = Mock()
        self.engine.safe_exit_quote.return_value = SimpleNamespace(price=70)

    def at(self, minute, second=0, microsecond=0):
        return datetime(2026, 10, 2, 13, minute, second, microsecond, tzinfo=runtime.TAIPEI)

    def intent(self, side=Side.SELL, quantity=1000, **kwargs):
        return ExecutionIntent("MOCK-EXIT", "3094", side, quantity,
                               Decimal("70"), purpose=IntentPurpose.EXIT, **kwargs)

    def guard(self, intent, when):
        return runtime._exit_pre_send_guard(
            intent, position=self.position, engine=self.engine,
            quote_time=self.at(22), max_age_seconds=3, now=when,
        )

    def test_exact_phase_boundaries(self):
        for moment, phase in ((self.at(19,59),"LIMIT"), (self.at(20),"LIMIT"),
                              (self.at(22,59,999999),"LIMIT"), (self.at(23),"MARKET"),
                              (self.at(24,59,999999),"MARKET"), (self.at(25),"CLOSING"),
                              (self.at(29,49,999999),"CLOSING"), (self.at(29,50),"CLOSED"),
                              (self.at(30),"CLOSED")):
            with self.subTest(moment=moment):
                self.assertEqual(runtime._force_flat_market_phase(moment),phase)

    def test_phase_uses_taipei_even_with_utc_input(self):
        from datetime import timezone
        self.assertEqual(runtime._force_flat_market_phase(self.at(25).astimezone(timezone.utc)),"CLOSING")

    def test_closing_sell_uses_documented_limit_down_rod(self):
        old=self.intent(quantity=2000)
        new=runtime._as_closing_fallback(old)
        self.assertEqual((new.price_type,new.time_in_force,new.ap_code,new.price),
                         (PriceType.LIMIT_DOWN,TimeInForce.ROD,APCode.REGULAR,None))
        self.assertEqual((PRICE_FLAG[new.price_type],TIF_CODE[new.time_in_force]),("L","0"))
        for key in ("intent_id","symbol","side","quantity","purpose","order_type"):
            self.assertEqual(getattr(old,key),getattr(new,key))

    def test_closing_short_cover_uses_limit_up_not_new_short_entry(self):
        old=self.intent(side=Side.BUY,order_type=StockOrderType.SHORT_SELL)
        new=runtime._as_closing_fallback(old)
        self.assertEqual((PRICE_FLAG[new.price_type],TIF_CODE[new.time_in_force]),("H","0"))
        self.assertEqual(new.order_type,old.order_type)
        self.position.side="SHORT"
        self.guard(new,self.at(25))

    def test_closing_fallback_does_not_require_stale_numeric_quote(self):
        self.engine.safe_exit_quote.return_value=None
        self.guard(runtime._as_closing_fallback(self.intent()),self.at(25))
        self.engine.safe_exit_quote.assert_not_called()

    def test_crossing_1325_rejects_market_ioc_before_any_send(self):
        market=runtime._as_market_fallback(self.intent())
        self.guard(market,self.at(24,59,999999))
        with self.assertRaisesRegex(RuntimeError,"CLOSING_LIMIT_ROD"):
            self.guard(market,self.at(25))

    def test_market_ioc_remains_allowed_1323_not_earlier(self):
        market=runtime._as_market_fallback(self.intent())
        self.guard(market,self.at(23))
        with self.assertRaises(RuntimeError):
            self.guard(market,self.at(22,59))

    def test_numeric_limit_cannot_slip_into_closing_from_earlier_phase(self):
        with self.assertRaisesRegex(RuntimeError,"CLOSING_LIMIT_ROD"):
            self.guard(self.intent(),self.at(25))

    def test_closing_ioc_fok_and_market_rod_are_forbidden(self):
        from dataclasses import replace
        closing=runtime._as_closing_fallback(self.intent())
        for bad in (replace(closing,time_in_force=TimeInForce.IOC),
                    replace(closing,time_in_force=TimeInForce.FOK),
                    replace(closing,price_type=PriceType.MARKET)):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                self.guard(bad,self.at(25))

    def test_cutoff_never_creates_after_close_order(self):
        for value in (runtime._as_closing_fallback(self.intent()),runtime._as_market_fallback(self.intent())):
            with self.assertRaisesRegex(RuntimeError,"CUTOFF"):
                self.guard(value,self.at(29,50))

    def test_wrong_closing_direction_and_entry_intents_rejected(self):
        from dataclasses import replace
        for bad in (replace(self.intent(),purpose=IntentPurpose.ENTRY), self.intent(side=Side.BUY)):
            with self.assertRaisesRegex(RuntimeError,"DIRECTION"):
                self.guard(runtime._as_closing_fallback(bad) if bad.purpose==IntentPurpose.EXIT else bad,self.at(25))
        for adapt in (runtime._as_closing_fallback,runtime._as_market_fallback):
            with self.assertRaises(RuntimeError):
                adapt(replace(self.intent(),purpose=IntentPurpose.ENTRY))

    def test_all_odd_and_mixed_quantities_fail_closed(self):
        for quantity in (1,500,999,1001,1500,2500):
            for adapt in (runtime._as_market_fallback,runtime._as_closing_fallback):
                with self.subTest(quantity=quantity,adapt=adapt.__name__),self.assertRaisesRegex(RuntimeError,"ODD_LOT_UNSUPPORTED"):
                    adapt(self.intent(quantity=quantity))

    def test_even_whole_lot_quantity_cannot_use_unverified_odd_apcode(self):
        for ap in (APCode.ODD_LOT,APCode.INTRADAY_ODD_LOT,APCode.AFTER_HOURS):
            for adapt in (runtime._as_market_fallback,runtime._as_closing_fallback):
                with self.assertRaisesRegex(RuntimeError,"ODD_LOT_UNSUPPORTED"):
                    adapt(self.intent(ap_code=ap))

    def test_closing_order_is_preserved_not_cancelled_every_loop(self):
        closing=runtime._as_closing_fallback(self.intent())
        for _ in range(100):
            self.assertFalse(runtime._fallback_exit_replacement_required(closing,"CLOSING"))
        self.assertTrue(runtime._fallback_exit_replacement_required(self.intent(),"CLOSING"))
        self.assertTrue(runtime._fallback_exit_replacement_required(runtime._as_market_fallback(self.intent()),"CLOSING"))

    def test_reconciled_old_market_must_be_terminal_before_reissue(self):
        for state in runtime.ACTIVE_EXIT_STATUSES:
            self.assertEqual(runtime._classify_reconciled_exit(state,1000),"TRACK_EXISTING")
        self.assertEqual(runtime._classify_reconciled_exit(BrokerOrderStatus.UNKNOWN,1000),"UNKNOWN")
        for state in runtime.TERMINAL:
            self.assertEqual(runtime._classify_reconciled_exit(state,1000),"RETRY_RESCUE")

    def order(self, *, closing=False, status=BrokerOrderStatus.ACKNOWLEDGED):
        intent=(runtime._as_closing_fallback if closing else runtime._as_market_fallback)(self.intent(quantity=2000))
        from dataclasses import asdict
        return SimpleNamespace(**asdict(intent),client_order_id="MOCK-OLD-EXIT",broker_order_no="MOCK-BROKER",status=status)

    def test_market_to_closing_cancels_once_and_never_submits_new_before_terminal(self):
        adapter,store=Mock(),Mock()
        store.pending_mutation.return_value=None
        order=self.order()
        self.assertTrue(runtime._cancel_incompatible_fallback_exit(adapter,store,order,"CLOSING"))
        order.status=BrokerOrderStatus.CANCEL_PENDING
        for _ in range(100):
            self.assertFalse(runtime._cancel_incompatible_fallback_exit(adapter,store,order,"CLOSING"))
        adapter.cancel.assert_called_once_with("MOCK-OLD-EXIT","13:25 closing auction fallback",emergency=True)
        adapter.submit.assert_not_called()
        adapter.submit_rescue.assert_not_called()
        store.get.return_value=order
        store.positions.return_value={"3094":1000}
        action,_,remaining=runtime._reconcile_failed_exit(adapter=adapter,store=store,
            exit_order_id=order.client_order_id,position=self.position,reconcile_timeout=2)
        self.assertEqual((action,remaining),("TRACK_EXISTING",1000))
        order.status=BrokerOrderStatus.CANCELED
        action,_,remaining=runtime._reconcile_failed_exit(adapter=adapter,store=store,
            exit_order_id=order.client_order_id,position=self.position,reconcile_timeout=2)
        self.assertEqual((action,remaining),("RETRY_RESCUE",1000))
        self.assertEqual(runtime._as_closing_fallback(self.intent(quantity=remaining)).quantity,1000)

    def test_closing_live_order_and_inflight_mutation_are_not_repeatedly_cancelled(self):
        adapter,store=Mock(),Mock()
        store.pending_mutation.return_value=None
        self.assertFalse(runtime._cancel_incompatible_fallback_exit(adapter,store,self.order(closing=True),"CLOSING"))
        store.pending_mutation.return_value={"operation":"CANCEL","status":"SEND_PENDING"}
        self.assertFalse(runtime._cancel_incompatible_fallback_exit(adapter,store,self.order(),"CLOSING"))
        adapter.cancel.assert_not_called()

    def test_unknown_order_and_cancel_failure_do_not_authorize_second_order(self):
        adapter,store=Mock(),Mock()
        store.pending_mutation.return_value=None
        self.assertFalse(runtime._cancel_incompatible_fallback_exit(adapter,store,self.order(status=BrokerOrderStatus.UNKNOWN),"CLOSING"))
        adapter.cancel.side_effect=RuntimeError("MOCK cancel failure")
        with self.assertRaisesRegex(RuntimeError,"cancel failure"):
            runtime._cancel_incompatible_fallback_exit(adapter,store,self.order(),"CLOSING")
        adapter.submit.assert_not_called()
        adapter.submit_rescue.assert_not_called()

    def test_missing_active_order_identity_refuses_guessed_cancel(self):
        adapter,store=Mock(),Mock()
        store.pending_mutation.return_value=None
        order=self.order(); order.broker_order_no=None
        with self.assertRaisesRegex(RuntimeError,"IDENTITY_UNPROVEN"):
            runtime._cancel_incompatible_fallback_exit(adapter,store,order,"CLOSING")
        adapter.cancel.assert_not_called()

    def test_remaining_whole_lot_after_partial_fill_preserves_actual_size(self):
        adapter,store=Mock(),Mock()
        adapter.reconcile.return_value=SimpleNamespace(broker_positions={"3094|0":2000})
        store.position_buckets.return_value={"3094|0":1000}
        quantity=runtime._authoritative_fallback_delta(adapter,store,baseline={"3094|0":1000},position=self.position,timeout=2)
        new=runtime._as_closing_fallback(self.intent(quantity=quantity))
        self.assertEqual(new.quantity,1000)
        adapter.reconcile.assert_called_once_with(timeout=2,strict_positions=True)

    def test_short_cover_quantity_uses_signed_proven_bucket(self):
        self.position.side="SHORT"
        adapter,store=Mock(),Mock()
        store.get.return_value=SimpleNamespace(symbol="3094",side=Side.SELL,order_type=StockOrderType.SHORT_SELL)
        adapter.reconcile.return_value=SimpleNamespace(broker_positions={"3094|4":-2000})
        store.position_buckets.return_value={"3094|4":-1000}
        self.assertEqual(runtime._authoritative_fallback_delta(adapter,store,baseline={"3094|4":-1000},position=self.position,timeout=2),1000)

    def test_short_broker_local_mismatch_does_not_invent_cover_quantity(self):
        self.position.side="SHORT"
        adapter,store=Mock(),Mock()
        store.get.return_value=SimpleNamespace(symbol="3094",side=Side.SELL,order_type=StockOrderType.SHORT_SELL)
        adapter.reconcile.return_value=SimpleNamespace(broker_positions={"3094|4":-2000})
        store.position_buckets.return_value={"3094|4":-1000}
        with self.assertRaisesRegex(RuntimeError,"DELTA_MISMATCH"):
            runtime._authoritative_fallback_delta(adapter,store,baseline={},position=self.position,timeout=2)

    def test_actual_adapter_builds_closing_wire_contract_without_sdk(self):
        class FakeStockOrder:
            def __init__(self):
                self.Identify=0
        adapter=YuantaSparkExecutionAdapter.__new__(YuantaSparkExecutionAdapter)
        adapter.api_types={"StockOrder":FakeStockOrder}
        adapter.account="MOCK_ACCOUNT"
        for side,flag in ((Side.SELL,"L"),(Side.BUY,"H")):
            intent=runtime._as_closing_fallback(self.intent(side=side,quantity=2000))
            from dataclasses import asdict
            order=SimpleNamespace(**asdict(intent),basket_no="MOCK_BASKET")
            stock=adapter._construct_stock_order(order,identify=7,trade_kind=0)
            self.assertEqual((stock.PriceFlag,stock.Price,stock.Time_in_force,stock.OrderQty,stock.APCode),
                             (flag,0.0,"0",2,0))
            self.assertEqual(stock.BuySell,"S" if side==Side.SELL else "B")
            self.assertEqual(intent.quantity,2000)


class ClosingTransitionHarness(runtime_fixtures.RuntimeHarness):
    """Reuse the complete controller harness; emulate late fills at cancellation."""
    def __init__(self,directory,*,fill_on_cancel=1000):
        super().__init__(directory,minute=24,partial=True)
        self.current=self.current.replace(second=59,microsecond=900000)
        self.fill_on_cancel=fill_on_cancel
        self.cancellation_started=False
        self.cancel_snapshot_count=0
        self.transition_events=[]
        self.old_exit_id=None

    def prepare(self):
        super().prepare()
        with LiveOrderStore(self.directory/"live-orders.sqlite") as store:
            store.record_fill(self.entry_id,fill_id="MOCK_SECOND_ENTRY_FILL",quantity=1000,price=100)
            out,_=store.reserve(ExecutionIntent("MOCK_EXISTING_MARKET_EXIT","TEST",Side.SELL,2000,
                price_type=PriceType.MARKET,time_in_force=TimeInForce.IOC,purpose=IntentPurpose.EXIT))
            store.bind_broker_order(out.client_order_id,"MOCK_EXISTING_MARKET")
            store.acknowledge(out.client_order_id)
            self.old_exit_id=out.client_order_id

    def wrap_adapter(self,base):
        harness=self
        class Adapter(base):
            def cancel(self,identity,reason,*,emergency=False):
                if identity!=harness.old_exit_id:
                    return super().cancel(identity,reason,emergency=emergency)
                harness.cancel_calls+=1
                harness.cancellation_started=True
                harness.transition_events.append("CANCEL_PENDING")
                self.store.request_cancel(identity,reason)
                request=self.store.create_request(identity,"CANCEL")
                self.store.complete_request(request,success=True)

            def reconcile(self,**kwargs):
                if harness.cancellation_started:
                    harness.cancel_snapshot_count+=1
                    if harness.cancel_snapshot_count==1:
                        harness.transition_events.append("PENDING_SNAPSHOT")
                    elif self.store.get(harness.old_exit_id).status==BrokerOrderStatus.CANCEL_PENDING:
                        self.store.record_fill(harness.old_exit_id,fill_id="MOCK_FILL_DURING_CANCEL",
                            quantity=harness.fill_on_cancel,price=100)
                        self.store.finalize_latest_request(harness.old_exit_id,"CANCEL",success=True)
                        self.store.canceled(harness.old_exit_id)
                        harness.transition_events.append("TERMINAL_AFTER_ACTUAL_FILL")
                return super().reconcile(**kwargs)

            def submit_rescue(self,intent,**kwargs):
                if harness.old_exit_id and self.store.get(harness.old_exit_id).status not in runtime.TERMINAL:
                    raise AssertionError("NEW EXIT before old broker terminal")
                harness.transition_events.append("NEW_CLOSING_EXIT")
                return super().submit_rescue(intent,**kwargs)
        return Adapter

    def run(self):
        # The reusable harness defines its adapter inside run(). Instrument
        # only that mocked class via its patch factory, never the real adapter.
        harness=self
        real_patch=runtime_fixtures.patch
        class InstrumentedPatch:
            def __call__(self,*args,**kwargs):
                return real_patch(*args,**kwargs)
            def object(self,target,name,value=unittest.mock.DEFAULT,**kwargs):
                if target is runtime and name=="YuantaSparkExecutionAdapter":
                    value=harness.wrap_adapter(value)
                return real_patch.object(target,name,value,**kwargs)
        with real_patch.object(runtime_fixtures,"patch",InstrumentedPatch()):
            return super().run()


class FullClosingRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        for target in ("socket.socket","socket.create_connection","subprocess.run","subprocess.Popen"):
            self.stack.enter_context(patch(target,side_effect=AssertionError("MOCK_ONLY")))

    def assert_flat(self,harness,result,heartbeat):
        self.assertEqual(result,0)
        self.assertEqual(heartbeat["state"],"STOPPED_CLEAN")
        self.assertTrue(heartbeat["broker_flat_confirmed"])
        self.assertFalse(harness.runtime_closed_with_exposure)
        with LiveOrderStore(harness.directory/"live-orders.sqlite") as store:
            self.assertEqual(store.positions(),{})
            self.assertEqual(store.orders(open_only=True),[])

    def test_full_market_transition_waits_cancel_and_uses_partial_actual_remainder(self):
        with TemporaryDirectory(prefix="closing-transition-mock-") as directory:
            harness=ClosingTransitionHarness(directory)
            result,heartbeat=harness.run()
            self.assert_flat(harness,result,heartbeat)
            self.assertEqual(harness.cancel_calls,1)
            self.assertEqual(harness.exit_quantities,[1000])
            self.assertEqual(harness.exit_price_types,["LIMIT_DOWN"])
            self.assertEqual(harness.transition_events[:3],["CANCEL_PENDING","PENDING_SNAPSHOT","TERMINAL_AFTER_ACTUAL_FILL"])
            self.assertEqual(harness.transition_events[-1],"NEW_CLOSING_EXIT")

    def test_full_transition_that_fills_all_during_cancel_never_sends_zero_order(self):
        with TemporaryDirectory(prefix="closing-zero-remainder-mock-") as directory:
            harness=ClosingTransitionHarness(directory,fill_on_cancel=2000)
            result,heartbeat=harness.run()
            self.assert_flat(harness,result,heartbeat)
            self.assertEqual(harness.exit_quantities,[])
            self.assertNotIn("NEW_CLOSING_EXIT",harness.transition_events)
            self.assertIn("TERMINAL_AFTER_ACTUAL_FILL",harness.transition_events)

    def test_full_initial_closing_submit_branch_is_limit_rod(self):
        with TemporaryDirectory(prefix="closing-initial-mock-") as directory:
            harness=runtime_fixtures.RuntimeHarness(directory,minute=25)
            result,heartbeat=harness.run()
            self.assert_flat(harness,result,heartbeat)
            self.assertEqual(harness.cancel_calls,0)
            self.assertEqual(harness.exit_quantities,[1000])
            self.assertEqual(harness.exit_price_types,["LIMIT_DOWN"])

    def test_full_deferred_entry_cancel_branch_submits_closing_only_after_terminal(self):
        with TemporaryDirectory(prefix="closing-entry-cancel-mock-") as directory:
            harness=runtime_fixtures.RuntimeHarness(directory,minute=25,partial=True)
            result,heartbeat=harness.run()
            self.assert_flat(harness,result,heartbeat)
            self.assertEqual(harness.cancel_calls,1)
            self.assertEqual(harness.exit_quantities,[1000])
            self.assertEqual(harness.exit_price_types,["LIMIT_DOWN"])

    def test_zero_delta_never_claims_flat_when_actual_broker_has_exposure(self):
        class StopSimulation(BaseException):
            pass
        class UnconfirmedHarness(runtime_fixtures.RuntimeHarness):
            def sleep(self,seconds):
                raise StopSimulation()
        with TemporaryDirectory(prefix="closing-unconfirmed-zero-mock-") as directory:
            harness=UnconfirmedHarness(directory,minute=25)
            with patch.object(runtime,"_authoritative_fallback_delta",return_value=0),self.assertRaises(StopSimulation):
                harness.run()
            self.assertEqual(harness.exit_quantities,[])
            heartbeat=json.loads((Path(directory)/"heartbeat.json").read_text())
            self.assertEqual(heartbeat["state"],"STOPPED_UNSAFE")
            self.assertNotEqual(heartbeat.get("broker_flat_confirmed"),True)


if __name__=="__main__":
    unittest.main()
