from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
import copy
import hashlib
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from yuanta_broker_execution_v01 import ExecutionIntent, IntentPurpose, LiveOrderStore, Side
from yuanta_live_runtime_v01 import carryover_manual_close as close


class CarryoverManualCloseTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="carryover-close-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "store.sqlite"
        self.backups = self.root / "backups"
        now = datetime.now(close.base.TAIPEI).replace(microsecond=0)
        prior = now - timedelta(days=3)
        store = LiveOrderStore(self.db)
        entry, _ = store.reserve(ExecutionIntent("ENTRY", "3094", Side.BUY, 2000, Decimal("70")))
        exit_order, _ = store.reserve(ExecutionIntent(
            "OLD-EXIT", "3094", Side.SELL, 2000, Decimal("70.3"), purpose=IntentPurpose.EXIT
        ))
        for order, order_no in ((entry, "BUY-1"), (exit_order, "SELL-OLD")):
            store.bind_broker_order(order.client_order_id, order_no)
        store.record_fill(entry.client_order_id, fill_id="BUY-1:A", quantity=1000,
                          price="70", broker_order_no="BUY-1", seq_no="A")
        store.record_fill(entry.client_order_id, fill_id="BUY-1:B", quantity=1000,
                          price="70.1", broker_order_no="BUY-1", seq_no="B")
        store.record_fill(exit_order.client_order_id, fill_id="SELL-OLD:C", quantity=1000,
                          price="70.3", broker_order_no="SELL-OLD", seq_no="C")
        store.halt("TEST")
        store.close()
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE live_orders SET created_at=?,updated_at=?",
                       (prior.isoformat(), prior.isoformat()))
        with sqlite3.connect(self.db) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.entry_id, self.old_exit_id = entry.client_order_id, exit_order.client_order_id
        day = now.strftime("%Y%m%d")
        order_no = "SELL-TODAY"
        common = {"symbol": "3094", "side": "S", "order_no": order_no,
                  "trade_date": day, "trade_date_source": "TradeDate", "order_type": "0"}
        merge = {**common, "rpt_type": 1, "order_qty": 1000, "ok_qty": 1000,
                 "order_status": 20, "last_order_status": 8, "price": "70.4",
                 "avg_deal_price": "70.4", "order_time": "09:00:47.877"}
        detail = {**common, "rpt_type": 51, "order_qty": 1000, "order_status": 8,
                  "price": "70.4", "order_time": "09:00:47.877", "seq_no": "D"}
        history_order = {"source": "GetOrderTradeReport.StkOrderList", "account_verified": True,
                         **common, "original_qty": 1000, "ok_qty": 1000, "price": "70.4",
                         "price_type": "LIMIT", "time_in_force": "ROD", "ap_code": 0,
                         "accept_date": day, "accept_time": "09:00:33.491"}
        history_trade = {"source": "GetOrderTradeReport.StkTradeList", "account_verified": True,
                         **common, "ok_qty": 1000, "fill_price": "70.4", "fill_time": "09:00:47.877"}
        self.evidence = {
            "source": "YUANTA_PROD_READONLY", "captured_at": now.isoformat(),
            "account_fingerprint": "0123456789ab", "account_rows_validated": True,
            "positions": {"0050|0": 1000}, "orders": [merge], "details": [detail],
            "history": {"source": "GetOrderTradeReport", "account_verified": True,
                        "orders": [history_order], "trades": [history_trade]},
        }
        self.baseline = {"0050|0": 1000}

    def test_plan_and_apply_flatten_without_clearing_halt(self):
        before = self.db.read_bytes()
        plan = close.build_plan(self.db, self.evidence, self.baseline)
        self.assertEqual(plan["after_positions"], {})
        result = close.apply_plan(self.db, plan, self.backups)
        self.assertEqual(result["status"], "APPLIED_HALT_PRESERVED")
        self.assertEqual(Path(result["backup_path"]).read_bytes(), before)
        self.assertEqual(result["backup_sha256"], hashlib.sha256(before).hexdigest())
        store = LiveOrderStore(self.db)
        try:
            self.assertEqual(store.positions(), {})
            self.assertEqual(store.orders(open_only=True), [])
            self.assertTrue(store.control_state()["halted"])
        finally:
            store.close()

    def test_refuses_position_or_quantity_mismatch(self):
        evidence = copy.deepcopy(self.evidence)
        evidence["positions"]["3094|0"] = 1000
        with self.assertRaisesRegex(close.base.IncidentRepairError, "BROKER_NOT_AT_REVIEWED_BASELINE"):
            close.build_plan(self.db, evidence, self.baseline)
        evidence = copy.deepcopy(self.evidence)
        evidence["orders"][0]["ok_qty"] = 500
        with self.assertRaisesRegex(close.base.IncidentRepairError, "BROKER_OPEN_ORDER_PRESENT"):
            close.build_plan(self.db, evidence, self.baseline)

    def test_refuses_ambiguous_or_stale_evidence(self):
        evidence = copy.deepcopy(self.evidence)
        evidence["history"]["trades"].append(copy.deepcopy(evidence["history"]["trades"][0]))
        with self.assertRaisesRegex(close.base.IncidentRepairError, "MANUAL_SALE_IDENTITY_AMBIGUOUS"):
            close.build_plan(self.db, evidence, self.baseline)
        evidence = copy.deepcopy(self.evidence)
        evidence["captured_at"] = (datetime.now(close.base.TAIPEI) - timedelta(hours=1)).isoformat()
        with self.assertRaisesRegex(close.base.IncidentRepairError, "APPLY_BROKER_EVIDENCE_STALE"):
            close.build_plan(self.db, evidence, self.baseline)

    def test_apply_is_atomic_on_changed_database(self):
        plan = close.build_plan(self.db, self.evidence, self.baseline)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE live_control SET next_identify=999")
        with sqlite3.connect(self.db) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with self.assertRaisesRegex(close.base.IncidentRepairError, "DATABASE_PRECONDITION_CHANGED"):
            close.apply_plan(self.db, plan, self.backups)
        self.assertFalse(self.backups.exists())


if __name__ == "__main__":
    unittest.main()
