from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from yuanta_broker_execution_v01 import (
    ExecutionIntent, IntentPurpose, LiveOrderStore, Side, StockOrderType,
)
from yuanta_live_runtime_v01.accounting import AccountingError, execution_pnl
from yuanta_live_runtime_v01.main import (
    _checkpoint_position, _confirm_owned_exposure_clear_for_stop,
    _confirm_strategy_flat, _exchange_tick_time,
    _inventory_is_baseline_reduction_only, _realized_pnl_today,
    _recover_runtime_state, _recovery_notification_due, _Session,
)
from yuanta_live_runtime_v01.risk_manager import RiskLimits, RiskManager
from yuanta_live_runtime_v01.strategy import LiveDirectionEngine, ManagedPosition, SPEC, TAIPEI


class StoreCase(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "test.sqlite"
        self.store = LiveOrderStore(self.path)
        self.now = datetime.now(TAIPEI)
        self.engine = LiveDirectionEngine({"TEST": "Test"})

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def order(self, key, quantity, price, *, exit=False, side=None, order_type=StockOrderType.CASH):
        order, _ = self.store.reserve(ExecutionIntent(
            key, "TEST", side or (Side.SELL if exit else Side.BUY), quantity,
            Decimal(str(price)), purpose=IntentPurpose.EXIT if exit else IntentPurpose.ENTRY,
            order_type=order_type,
        ))
        return order

    def fill(self, order, quantity, price, key=None):
        self.store.record_fill(order.client_order_id, fill_id=key or order.intent_id,
                               quantity=quantity, price=str(price))


class AccountingTests(StoreCase):
    def test_partial_entry_cancel_then_exit_counts_loss_and_costs(self):
        entry = self.order("entry", 2000, 90)
        self.fill(entry, 1000, 90)
        self.store.canceled(entry.client_order_id)
        exit_order = self.order("exit", 1000, 85, exit=True)
        self.fill(exit_order, 1000, 85)
        result = _realized_pnl_today(self.store, self.engine, self.now)
        self.assertEqual(result, Decimal("-5278"))
        self.assertTrue(RiskManager(RiskLimits()).loss_kill_required(realized=result, unrealized=Decimal(0)))

    def test_split_exits_all_count(self):
        entry = self.order("entry", 2000, 90)
        self.fill(entry, 2000, 90)
        for key, price in (("exit1", 85), ("exit2", 84)):
            order = self.order(key, 1000, price, exit=True)
            self.fill(order, 1000, price)
        self.assertEqual(execution_pnl(self.store, self.now, SPEC).realized, Decimal("-11553"))

    def test_partial_exit_mark_only_remaining_shares(self):
        entry = self.order("entry", 2000, 90)
        self.fill(entry, 2000, 90)
        out = self.order("exit", 2000, 85, exit=True)
        self.fill(out, 1000, 85)
        pnl = execution_pnl(self.store, self.now, SPEC)
        self.assertEqual(sum(lot.quantity for lot in pnl.open_lots), 1000)
        self.assertEqual(pnl.realized + pnl.unrealized({"TEST": Decimal(84)}, SPEC), Decimal("-11553"))

    def test_duplicate_fill_and_restart_do_not_duplicate_pnl(self):
        entry = self.order("entry", 1000, 90)
        self.fill(entry, 1000, 90)
        out = self.order("exit", 1000, 85, exit=True)
        self.fill(out, 1000, 85)
        self.fill(out, 1000, 85)
        self.store.close()
        self.store = LiveOrderStore(self.path)
        self.assertEqual(execution_pnl(self.store, self.now, SPEC).realized, Decimal("-5278"))

    def test_fee_minimum_once_per_order_not_per_callback(self):
        spec = dict(SPEC, commission_rate_each_side=0, day_trade_sell_tax_rate=0)
        entry = self.order("entry", 1000, 10)
        self.fill(entry, 500, 10, "a")
        self.fill(entry, 500, 10, "b")
        out = self.order("exit", 1000, 10, exit=True)
        self.fill(out, 250, 10, "c")
        self.fill(out, 750, 10, "d")
        self.assertEqual(execution_pnl(self.store, self.now, spec).realized, Decimal(-40))

    def test_short_fill_accounting(self):
        entry = self.order("entry", 1000, 90, side=Side.SELL)
        self.fill(entry, 1000, 90)
        out = self.order("exit", 1000, 85, exit=True, side=Side.BUY)
        self.fill(out, 1000, 85)
        self.assertEqual(execution_pnl(self.store, self.now, SPEC).realized, Decimal(4715))

    def test_financing_categories_never_silently_net(self):
        entry = self.order("entry", 1000, 90, order_type=StockOrderType.MARGIN_BUY)
        self.fill(entry, 1000, 90)
        out = self.order("exit", 1000, 85, exit=True)
        self.fill(out, 1000, 85)
        with self.assertRaises(AccountingError):
            execution_pnl(self.store, self.now, SPEC)

    def test_replayed_receipt_timestamp_does_not_shift_session_pnl(self):
        entry = self.order("entry", 1000, 90)
        self.fill(entry, 1000, 90)
        out = self.order("exit", 1000, 85, exit=True)
        self.fill(out, 1000, 85)
        with self.store.connection:
            self.store.connection.execute("UPDATE live_fills SET filled_at=?", ((self.now+timedelta(days=1)).isoformat(),))
        self.assertEqual(execution_pnl(self.store, self.now, SPEC).realized, Decimal(-5278))
        self.assertEqual(execution_pnl(self.store, self.now+timedelta(days=1), SPEC).realized, Decimal(0))

    def test_missing_mark_refuses_to_assume_zero_unrealized(self):
        entry = self.order("entry", 1000, 90)
        self.fill(entry, 1000, 90)
        with self.assertRaises(AccountingError):
            execution_pnl(self.store, self.now, SPEC).unrealized({}, SPEC)


class PositionRecoveryTests(StoreCase):
    def create_position(self):
        entry = self.order("entry", 1000, 100)
        self.fill(entry, 1000, 100)
        return ManagedPosition("TEST", "Test", "LONG", 1000, 100, entry.client_order_id, self.now)

    def test_restart_preserves_mfe_state_and_exit_reason(self):
        position = self.create_position()
        position.peak_return = .05
        position.worst_return = -.025
        position.reversal_streak = 1
        position.last_reversal_decision = self.now
        self.engine._refresh_mfe_basis(position)
        risk = position.entry_price - position.initial_stop_price
        peak = position.entry_price + 3 * risk
        self.engine._observe_mfe(
            position, price=peak,
            projected_net_pnl=self.engine.projected_net(position, peak),
            at=self.now,
        )
        _checkpoint_position(self.store, position, "MFE_PROFIT_PROTECTION")
        self.store.close()
        self.store = LiveOrderStore(self.path)
        recovered = _recover_runtime_state(self.store, {"TEST": "Test"}, self.now)
        restored = recovered["position"]
        self.assertEqual(restored.peak_return, .05)
        self.assertEqual(restored.worst_return, -.025)
        self.assertEqual(restored.reversal_streak, 1)
        self.assertEqual(restored.last_reversal_decision, self.now)
        self.assertTrue(restored.mfe_protection_armed)
        self.assertAlmostEqual(restored.mfe_r, 3.0)
        self.assertAlmostEqual(restored.locked_profit_r, 2.25)
        self.assertEqual(recovered["pending_exit_reason"], "MFE_PROFIT_PROTECTION")
        floor = restored.locked_profit_price
        executable_floor = (floor // 0.5) * 0.5
        self.engine.record_tick(
            "TEST", at=self.now, price=floor, bid=executable_floor, ask=executable_floor + .5, volume=1,
        )
        self.assertEqual(
            self.engine.evaluate_exit(restored, self.now).reason,
            "MFE_PROFIT_PROTECTION",
        )

    def test_legacy_checkpoint_requests_exit_instead_of_resetting_mfe(self):
        position = self.create_position()
        _checkpoint_position(self.store, position, None)
        payload = self.store.position_checkpoint(position.entry_order_id)
        payload["version"] = 1
        for key in list(payload):
            if key not in {
                "version", "stock_id", "side", "peak_return", "worst_return",
                "reversal_streak", "last_reversal_decision", "pending_exit_reason",
            }:
                payload.pop(key)
        self.store.save_position_checkpoint(position.entry_order_id, payload)
        recovered = _recover_runtime_state(self.store, {"TEST": "Test"}, self.now)
        self.assertEqual(
            recovered["pending_exit_reason"],
            "MFE_STATE_UNAVAILABLE_AFTER_UPGRADE",
        )

    def test_missing_checkpoint_requests_exit_not_reset_and_hold(self):
        self.create_position()
        recovered = _recover_runtime_state(self.store, {"TEST": "Test"}, self.now)
        self.assertEqual(recovered["pending_exit_reason"], "MISSING_POSITION_CHECKPOINT")

    def test_wrong_checkpoint_identity_halts(self):
        position = self.create_position()
        _checkpoint_position(self.store, position, None)
        payload = self.store.position_checkpoint(position.entry_order_id)
        payload["stock_id"] = "OTHER"
        self.store.save_position_checkpoint(position.entry_order_id, payload)
        with self.assertRaises(RuntimeError):
            _recover_runtime_state(self.store, {"TEST": "Test"}, self.now)
        self.assertTrue(self.store.control_state()["halted"])


class FlatConfirmationTests(StoreCase):
    def adapter(self, positions=None, open_orders=None):
        adapter = Mock()
        adapter.position_baseline = {"UNMANAGED|0": 1000}
        adapter.inspect_broker_state.return_value = SimpleNamespace(
            positions=adapter.position_baseline if positions is None else positions,
            open_orders=[] if open_orders is None else open_orders,
        )
        return adapter

    def test_success_requires_fresh_remote_inventory_and_orders(self):
        adapter = self.adapter()
        _confirm_strategy_flat(adapter, self.store, 1)
        adapter.reconcile.assert_called_once_with(timeout=1, strict_positions=True)
        adapter.inspect_broker_state.assert_called_once_with(timeout=1)
        adapter.submit.assert_not_called()

    def test_filled_exit_does_not_override_broker_remaining_inventory(self):
        adapter = self.adapter(positions={"UNMANAGED|0": 1000, "TEST|0": 1000})
        with self.assertRaises(RuntimeError):
            _confirm_strategy_flat(adapter, self.store, 1)
        self.assertTrue(self.store.control_state()["halted"])

    def test_broker_open_order_blocks_close_success(self):
        adapter = self.adapter(open_orders=[{"order_no": "external"}])
        with self.assertRaises(RuntimeError):
            _confirm_strategy_flat(adapter, self.store, 1)

    def test_late_local_fill_blocks_close_success(self):
        entry = self.order("late", 1000, 90)
        self.fill(entry, 1000, 90)
        with self.assertRaises(RuntimeError):
            _confirm_strategy_flat(self.adapter(), self.store, 1)

    def test_timeout_never_confirms_flat(self):
        adapter = self.adapter()
        adapter.reconcile.side_effect = TimeoutError("mock")
        with self.assertRaises(TimeoutError):
            _confirm_strategy_flat(adapter, self.store, 1)
        adapter.inspect_broker_state.assert_not_called()


class ShutdownOwnedExposureProofTests(StoreCase):
    def adapter(self, *, baseline, positions, open_orders=None):
        adapter = Mock()
        adapter.position_baseline = dict(baseline)
        adapter.inspect_broker_state.return_value = SimpleNamespace(
            positions=dict(positions),
            open_orders=[] if open_orders is None else list(open_orders),
        )
        return adapter

    def test_manual_reduction_of_preexisting_inventory_can_confirm_stop(self):
        baseline = {"0050|0": 1030, "078302|0": 16000}
        adapter = self.adapter(baseline=baseline, positions={"0050|0": 1030})

        mode = _confirm_owned_exposure_clear_for_stop(adapter, self.store, 1)

        self.assertEqual(mode, "BASELINE_REDUCTION_ONLY")
        adapter.inspect_broker_state.assert_called_once_with(timeout=1)
        adapter.reconcile.assert_not_called()
        self.assertFalse(self.store.control_state()["halted"])

    def test_exact_baseline_can_confirm_stop(self):
        baseline = {"0050|0": 1030}
        adapter = self.adapter(baseline=baseline, positions=baseline)
        self.assertEqual(
            _confirm_owned_exposure_clear_for_stop(adapter, self.store, 1),
            "BASELINE_MATCH",
        )

    def test_new_or_increased_broker_inventory_cannot_confirm_stop(self):
        for positions in (
            {"0050|0": 1031},
            {"0050|0": 1030, "NEW|0": 1},
            {"0050|0": -1},
        ):
            with self.subTest(positions=positions):
                store = LiveOrderStore(Path(self.directory.name) / f"unsafe-{len(positions)}-{abs(sum(positions.values()))}.sqlite")
                try:
                    adapter = self.adapter(baseline={"0050|0": 1030}, positions=positions)
                    with self.assertRaisesRegex(RuntimeError, "reduction-only"):
                        _confirm_owned_exposure_clear_for_stop(adapter, store, 1)
                    self.assertTrue(store.control_state()["halted"])
                finally:
                    store.close()

    def test_broker_order_or_local_position_cannot_confirm_stop(self):
        adapter = self.adapter(
            baseline={"0050|0": 1030},
            positions={"0050|0": 1030},
            open_orders=[{"order_no": "external"}],
        )
        with self.assertRaisesRegex(RuntimeError, "exposure or unresolved order"):
            _confirm_owned_exposure_clear_for_stop(adapter, self.store, 1)

        other_store = LiveOrderStore(Path(self.directory.name) / "owned.sqlite")
        try:
            order, _ = other_store.reserve(ExecutionIntent(
                "owned", "TEST", Side.BUY, 1000, Decimal("100"),
            ))
            other_store.bind_broker_order(order.client_order_id, "BROKER")
            other_store.acknowledge(order.client_order_id)
            other_store.record_fill(
                order.client_order_id,
                fill_id="owned-fill",
                quantity=1000,
                price="100",
            )
            adapter = self.adapter(baseline={}, positions={"TEST|0": 1000})
            with self.assertRaisesRegex(RuntimeError, "exposure or unresolved order"):
                _confirm_owned_exposure_clear_for_stop(adapter, other_store, 1)
        finally:
            other_store.close()

    def test_reduction_only_inventory_predicate_supports_long_and_short(self):
        self.assertTrue(_inventory_is_baseline_reduction_only(
            {"LONG|0": 1000, "SHORT|4": -1000},
            {"LONG|0": 500, "SHORT|4": -500},
        ))
        self.assertFalse(_inventory_is_baseline_reduction_only(
            {"SHORT|4": -1000}, {"SHORT|4": -1500},
        ))


class RecoveryNotificationThrottleTests(TestCase):
    def test_first_changed_and_elapsed_failures_notify(self):
        self.assertTrue(_recovery_notification_due(
            signature="A", previous_signature=None, previous_at=None, now=10,
        ))
        self.assertFalse(_recovery_notification_due(
            signature="A", previous_signature="A", previous_at=10, now=309,
        ))
        self.assertTrue(_recovery_notification_due(
            signature="A", previous_signature="A", previous_at=10, now=310,
        ))
        self.assertTrue(_recovery_notification_due(
            signature="B", previous_signature="A", previous_at=10, now=11,
        ))


class QuoteIntegrityTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 24, 9, 10, tzinfo=TAIPEI)
        self.engine = LiveDirectionEngine({"TEST": "Test"})

    def tick(self, serial, at=None, received_at=None, **kwargs):
        payload = dict(price=100, bid=99.9, ask=100, volume=10)
        payload.update(kwargs)
        return self.engine.record_tick("TEST", serial=serial, at=at or self.now,
                                       received_at=received_at, **payload)

    def test_duplicate_serial_does_not_inflate_vwap_or_volume(self):
        self.assertTrue(self.tick(1))
        self.assertFalse(self.tick(1, at=self.now+timedelta(seconds=1), price=200))
        self.assertEqual(self.engine._states["TEST"].cumulative_volume, 10)
        self.assertEqual(self.engine._states["TEST"].cumulative_pv, 1000)

    def test_delayed_and_future_quotes_do_not_refresh_price(self):
        self.assertTrue(self.tick(1))
        self.assertFalse(self.tick(2, received_at=self.now+timedelta(seconds=6)))
        self.assertFalse(self.tick(3, at=self.now+timedelta(seconds=1), received_at=self.now))
        self.assertEqual(len(self.engine._states["TEST"].ticks), 1)

    def test_subsecond_exchange_clock_lead_is_accepted(self):
        self.assertTrue(self.tick(
            1,
            at=self.now + timedelta(milliseconds=200),
            received_at=self.now,
        ))
        self.assertEqual(self.engine.quote_age_seconds("TEST", self.now), 0.0)

    def test_material_future_quote_is_still_rejected(self):
        self.assertFalse(self.tick(
            1,
            at=self.now + timedelta(milliseconds=600),
            received_at=self.now,
        ))

    def test_out_of_order_quote_does_not_replace_newer_quote(self):
        self.assertTrue(self.tick(5))
        self.assertFalse(self.tick(6, at=self.now-timedelta(seconds=1)))
        self.assertTrue(self.tick(6, at=self.now+timedelta(seconds=1)))

    def test_new_session_resets_sequence_and_vwap(self):
        self.assertTrue(self.tick(100))
        self.assertTrue(self.tick(1, at=self.now+timedelta(days=1), price=110))
        self.assertEqual(self.engine._states["TEST"].cumulative_pv, 1100)

    def test_crossed_quote_rejected(self):
        self.assertFalse(self.tick(1, bid=101, ask=100))

    def test_exchange_time_is_used_instead_of_callback_time(self):
        value = SimpleNamespace(bytHour=9, bytMin=9, bytSec=59, ushtMSec=500)
        self.assertEqual(_exchange_tick_time(value, self.now), self.now-timedelta(milliseconds=500))
        self.assertIsNone(_exchange_tick_time(None, self.now))

    def test_actual_callback_rejects_invalid_tick_time_but_archives_it(self):
        engine = self.engine
        session = _Session(api_types={}, environment="UAT", credentials={"account": "mock"},
                           engine=engine, logger=Mock())
        session._archive_quote = Mock()
        # This quote has an invalid hour and must never become a current tick.
        value = SimpleNamespace(StkCode="TEST", Time=SimpleNamespace(
            bytHour=99, bytMin=0, bytSec=0, ushtMSec=0), SerialNo=1,
            BuyPrice=99, SellPrice=100, DealPrice=100, DealVol=1, InOutFlag="1")
        session._on_response(2, 0, "SubscribeStockTick", None, value)
        self.assertIsNone(session.last_quote_at)
        self.assertEqual(len(engine._states["TEST"].ticks), 0)
        session._archive_quote.assert_called_once()
