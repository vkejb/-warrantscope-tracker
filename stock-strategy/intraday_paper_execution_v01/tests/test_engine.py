from pathlib import Path
import tempfile
import unittest

from intraday_paper_execution_v01.engine import (
    DuplicateOrderConflict,
    EmergencyStopActive,
    ExecutionDisabled,
    InvalidTransition,
    OrderStatus,
    PaperExecutionEngine,
    ReconciliationError,
)


class PaperExecutionEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "orders.sqlite"
        self.engine = PaperExecutionEngine(self.db)
        self.engine.set_execution_mode("PAPER_ONLY", "test setup")

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def _entry(self, key="signal-1", quantity=1000):
        return self.engine.submit_order(
            idempotency_key=key,
            symbol="3605",
            side="BUY",
            quantity=quantity,
            limit_price="171.5",
        )

    def test_state_machine_and_partial_fills(self):
        order = self._entry()
        self.assertEqual(order.status, OrderStatus.SUBMITTED)
        self.engine.acknowledge(order.order_id)
        partial = self.engine.record_fill(
            order.order_id, fill_id="fill-1", quantity=400, price="171.5"
        )
        self.assertEqual(partial.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(partial.remaining_quantity, 600)
        done = self.engine.record_fill(
            order.order_id, fill_id="fill-2", quantity=600, price="172"
        )
        self.assertEqual(done.status, OrderStatus.FILLED)
        self.assertEqual(str(done.average_fill_price), "171.8")
        self.assertEqual(self.engine.position_quantities(), {"3605": 1000})

    def test_fill_id_is_idempotent_and_overfill_is_rejected(self):
        order = self._entry(quantity=100)
        first = self.engine.record_fill(
            order.order_id, fill_id="same-fill", quantity=40, price="171"
        )
        second = self.engine.record_fill(
            order.order_id, fill_id="same-fill", quantity=40, price="171"
        )
        self.assertEqual(first.filled_quantity, second.filled_quantity)
        with self.assertRaises(DuplicateOrderConflict):
            self.engine.record_fill(
                order.order_id, fill_id="same-fill", quantity=41, price="171"
            )
        with self.assertRaises(InvalidTransition):
            self.engine.record_fill(
                order.order_id, fill_id="overfill", quantity=61, price="171"
            )

    def test_order_idempotency_suppresses_duplicate(self):
        first = self._entry()
        second = self._entry()
        self.assertEqual(first.order_id, second.order_id)
        with self.assertRaises(DuplicateOrderConflict):
            self.engine.submit_order(
                idempotency_key="signal-1",
                symbol="3605",
                side="SELL",
                quantity=1000,
                limit_price="171.5",
            )

    def test_restart_recovers_orders_positions_and_stop(self):
        order = self._entry()
        self.engine.record_fill(order.order_id, fill_id="fill-1", quantity=250, price="171")
        self.engine.emergency_stop("test restart")
        self.engine.close()
        self.engine = PaperExecutionEngine(self.db)
        recovered = self.engine.get_order(order.order_id)
        self.assertEqual(recovered.filled_quantity, 250)
        self.assertEqual(recovered.status, OrderStatus.CANCEL_PENDING)
        self.assertTrue(self.engine.control_state()["emergency_stop"])
        self.assertEqual(self.engine.position_quantities(), {"3605": 250})

    def test_emergency_stop_cancel_then_flatten_and_reset(self):
        order = self._entry()
        self.engine.record_fill(order.order_id, fill_id="entry-fill", quantity=400, price="171")
        canceled = self.engine.emergency_stop("manual kill")
        self.assertEqual(canceled[0].status, OrderStatus.CANCEL_PENDING)
        with self.assertRaises(EmergencyStopActive):
            self._entry(key="new-entry")
        with self.assertRaises(InvalidTransition):
            self.engine.force_flatten({"3605": "170"})
        self.engine.confirm_cancel(order.order_id)
        exits = self.engine.force_flatten({"3605": "170"})
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].intent, "EXIT")
        same = self.engine.force_flatten({"3605": "170"})
        self.assertEqual(same[0].order_id, exits[0].order_id)
        self.engine.acknowledge(exits[0].order_id)
        self.engine.record_fill(
            exits[0].order_id, fill_id="exit-fill-1", quantity=100, price="170"
        )
        after_partial = self.engine.force_flatten({"3605": "170"})
        self.assertEqual(after_partial[0].order_id, exits[0].order_id)
        self.engine.record_fill(
            exits[0].order_id, fill_id="exit-fill-2", quantity=300, price="170"
        )
        self.assertEqual(self.engine.position_quantities(), {})
        self.engine.reset_emergency_stop("review completed")
        self.assertFalse(self.engine.control_state()["emergency_stop"])

    def test_reconciliation_mismatch_fails_closed(self):
        order = self._entry()
        with self.assertRaises(ReconciliationError):
            self.engine.reconcile(
                authoritative_orders={order.idempotency_key: ("FILLED", 1000)},
                authoritative_positions={"3605": 1000},
            )
        self.assertTrue(self.engine.control_state()["emergency_stop"])
        self.assertEqual(self.engine.get_order(order.order_id).status, OrderStatus.CANCEL_PENDING)

    def test_matching_reconciliation_passes(self):
        order = self._entry()
        result = self.engine.reconcile(
            authoritative_orders={order.idempotency_key: ("SUBMITTED", 0)},
            authoritative_positions={},
        )
        self.assertEqual(result["status"], "MATCH")

    def test_snapshot_proves_zero_live_execution(self):
        snapshot = self.engine.snapshot()
        self.assertEqual(snapshot["mode"], "PAPER_ONLY")
        self.assertFalse(snapshot["live_send_available"])
        self.assertEqual(snapshot["actual_orders"], 0)
        self.assertEqual(snapshot["actual_fills"], 0)
        self.assertEqual(snapshot["broker_connections"], 0)

    def test_default_is_disabled_live_is_rejected_and_mode_persists(self):
        other_db = Path(self.temp.name) / "mode.sqlite"
        with PaperExecutionEngine(other_db) as engine:
            self.assertEqual(engine.snapshot()["mode"], "DISABLED")
            with self.assertRaises(ExecutionDisabled):
                engine.submit_order(
                    idempotency_key="blocked",
                    symbol="3605",
                    side="BUY",
                    quantity=1000,
                    limit_price="171",
                )
            with self.assertRaises(ValueError):
                engine.set_execution_mode("LIVE", "must reject")
            engine.set_execution_mode("PAPER_ONLY", "approved paper session")
        with PaperExecutionEngine(other_db) as recovered:
            self.assertEqual(recovered.snapshot()["mode"], "PAPER_ONLY")

    def test_disabling_requests_cancel_and_blocks_new_entries(self):
        order = self._entry()
        changed = self.engine.set_execution_mode("DISABLED", "operator off")
        self.assertEqual(changed[0].status, OrderStatus.CANCEL_PENDING)
        with self.assertRaises(ExecutionDisabled):
            self._entry(key="blocked-after-off")


if __name__ == "__main__":
    unittest.main()
