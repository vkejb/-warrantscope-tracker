"""Mock-only tests: no SDK load, login, network, or broker mutation."""
from contextlib import ExitStack
from datetime import datetime, timedelta
from decimal import Decimal
import copy
import hashlib
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from yuanta_broker_execution_v01 import ExecutionIntent, LiveOrderStore, Side
from yuanta_live_runtime_v01 import incident_repair as base
from yuanta_live_runtime_v01 import same_day_manual_close_repair as repair


class SameDayManualCloseRepairTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        blocked = RuntimeError("MOCK_ONLY_EXTERNAL_ACCESS_BLOCKED")
        for target in (
            "socket.socket", "socket.create_connection", "subprocess.run",
            "subprocess.Popen", "yuanta_broker_execution_v01.sdk.load_api_types",
        ):
            self.stack.enter_context(patch(target, side_effect=blocked))
        self.root = Path(self.stack.enter_context(TemporaryDirectory(prefix="same-day-close-mock-")))
        self.db_path = self.root / "live-orders.sqlite"
        self.backup_dir = self.root / "private-backups"
        self.now = datetime.now(base.TAIPEI).replace(microsecond=0)
        self.entry_at = self.now.replace(hour=9, minute=24, second=30)
        if self.entry_at > self.now:
            self.entry_at = self.now - timedelta(minutes=4)
        self.day = self.entry_at.strftime("%Y%m%d")
        store = LiveOrderStore(self.db_path)
        try:
            with patch("yuanta_broker_execution_v01.store.utc_now", return_value=self.entry_at.astimezone().isoformat()):
                order, _ = store.reserve(ExecutionIntent(
                    "MOCK-SAME-DAY-ENTRY", "4919", Side.BUY, 1000, Decimal("155"),
                ))
                self.entry_id = order.client_order_id
                self.local_basket = order.basket_no
                store.create_request(order.client_order_id, "NEW")
                store.mark_send_pending(order.client_order_id)
            store.halt("MOCK_RECONCILIATION_MISMATCH")
        finally:
            store.close()
        self.broker_basket = "Ewodr" + self.local_basket[:27]
        common = {
            "symbol": "4919", "trade_date": self.day,
            "trade_date_source": "OrderDate", "order_type": "0",
            "ap_code": 0, "stk_error_no": "", "order_error_no": "",
        }
        buy_order = {
            **common, "rpt_type": 1, "order_no": "MOCK_BUY", "side": "B",
            "order_qty": 1000, "ok_qty": 1000, "price": "155",
            "avg_deal_price": "154.5", "order_status": 20,
            "last_order_status": 8, "basket_no": self.broker_basket,
            "order_time": self.entry_at.replace(second=31).strftime("%H:%M:%S.000"),
        }
        sell_at = self.entry_at + timedelta(hours=4, minutes=5, seconds=30)
        sell_order = {
            **common, "rpt_type": 1, "order_no": "MOCK_SELL", "side": "S",
            "order_qty": 1000, "ok_qty": 1000, "price": "150",
            "avg_deal_price": "150.5", "order_status": 20,
            "last_order_status": 8, "basket_no": "",
            "order_time": (sell_at - timedelta(minutes=1)).strftime("%H:%M:%S.000"),
        }
        buy_fill_at = self.entry_at.replace(second=31)
        self.details = [
            {
                **common, "rpt_type": 51, "order_no": "MOCK_BUY", "side": "B",
                "order_qty": 1000, "price": "154.5", "order_status": 8,
                "seq_no": "BUYSEQ", "basket_no": self.broker_basket,
                "order_time": buy_fill_at.strftime("%H:%M:%S.000"),
            },
            {
                **common, "rpt_type": 51, "order_no": "MOCK_SELL", "side": "S",
                "order_qty": 1000, "price": "150.5", "order_status": 8,
                "seq_no": "SELLSEQ", "basket_no": "",
                "order_time": sell_at.strftime("%H:%M:%S.000"),
            },
        ]

        def history_order(no, side, price, accepted):
            return {
                "source": "GetOrderTradeReport.StkOrderList", "account_verified": True,
                "order_no": no, "symbol": "4919", "side": side,
                "trade_date": self.day, "trade_date_source": "TradeDate",
                "accept_date": self.day, "accept_date_source": "AcceptDate",
                "accept_time": accepted.strftime("%H:%M:%S.000"),
                "accept_time_source": "AcceptTime", "original_qty": 1000,
                "ok_qty": 1000, "price": price, "price_type": "LIMIT",
                "time_in_force": "ROD", "ap_code": 0, "order_type": "0",
            }

        def history_trade(no, side, price, filled):
            return {
                "source": "GetOrderTradeReport.StkTradeList", "account_verified": True,
                "order_no": no, "symbol": "4919", "side": side,
                "trade_date": self.day, "trade_date_source": "DateTime",
                "fill_time": filled.strftime("%H:%M:%S.000"),
                "fill_time_source": "DateTime", "ok_qty": 1000,
                "fill_price": price, "order_type": "0",
            }

        self.evidence = {
            "source": "YUANTA_PROD_READONLY", "captured_at": self.now.isoformat(),
            "account_fingerprint": "000000000000", "account_rows_validated": True,
            "positions": {"0050|0": 1000}, "orders": [buy_order, sell_order],
            "details": self.details,
            "history": {
                "source": "GetOrderTradeReport", "account_verified": True,
                "orders": [
                    history_order("MOCK_BUY", "B", "155", buy_fill_at),
                    history_order("MOCK_SELL", "S", "150", sell_at - timedelta(minutes=2)),
                ],
                "trades": [
                    history_trade("MOCK_BUY", "B", "154.5", buy_fill_at),
                    history_trade("MOCK_SELL", "S", "150.5", sell_at),
                ],
            },
        }
        self.baseline = {"0050|0": 1000}

    def snapshot(self):
        with sqlite3.connect(self.db_path) as db:
            return repair._snapshot(db)

    def plan(self, evidence=None):
        return repair.build_plan(
            self.db_path, self.evidence if evidence is None else evidence,
            self.baseline, entry_id=self.entry_id,
        )

    def test_plan_binds_transformed_basket_and_proves_round_trip_flat(self):
        before = self.db_path.read_bytes()
        plan = self.plan()
        self.assertEqual(before, self.db_path.read_bytes())
        self.assertEqual(plan["after_positions"], {})
        self.assertEqual(base._ledger_positions(plan["after"]), {})
        entry = next(row for row in plan["after"]["tables"]["live_orders"] if row["client_order_id"] == self.entry_id)
        self.assertEqual((entry["status"], entry["broker_order_no"], entry["filled_quantity"]),
                         ("FILLED", "MOCK_BUY", 1000))
        self.assertEqual(plan["manual_order"]["status"], "FILLED")
        self.assertFalse(plan["normal_start_ready"])

    def test_apply_is_atomic_idempotent_and_preserves_halt(self):
        plan = self.plan()
        before = self.db_path.read_bytes()
        result = repair.apply_plan(self.db_path, plan, self.backup_dir)
        self.assertEqual(result["status"], "APPLIED_HALT_PRESERVED")
        self.assertEqual(result["broker_submission_calls"], 0)
        self.assertEqual(Path(result["backup_path"]).read_bytes(), before)
        self.assertEqual(result["backup_sha256"], hashlib.sha256(before).hexdigest())
        after = self.snapshot()
        self.assertEqual(base._ledger_positions(after), {})
        control = after["tables"]["live_control"][0]
        self.assertEqual((control["halted"], control["reason"]), (1, "MOCK_RECONCILIATION_MISMATCH"))
        self.assertEqual(repair.apply_plan(self.db_path, plan, self.backup_dir)["status"], "ALREADY_APPLIED")

    def test_rejects_open_order_baseline_mismatch_and_untransformed_basket(self):
        cases = []
        mismatch = copy.deepcopy(self.evidence)
        mismatch["positions"]["4919|0"] = 1000
        cases.append((mismatch, "BROKER_NOT_AT_FROZEN_BASELINE"))
        opened = copy.deepcopy(self.evidence)
        opened["orders"][1]["ok_qty"] = 0
        opened["orders"][1]["last_order_status"] = 0
        cases.append((opened, "BROKER_OPEN_ORDER_PRESENT"))
        basket = copy.deepcopy(self.evidence)
        basket["orders"][0]["basket_no"] = "X" * 32
        cases.append((basket, "ENTRY_BROKER_IDENTITY_CONFLICT"))
        for evidence, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(base.IncidentRepairError, code):
                self.plan(evidence)

    def test_rejects_unsanitized_rows_and_ambiguous_manual_close(self):
        unsafe = copy.deepcopy(self.evidence)
        unsafe["orders"][0]["account"] = "SECRET"
        with self.assertRaisesRegex(base.IncidentRepairError, "UNSANITIZED"):
            self.plan(unsafe)
        ambiguous = copy.deepcopy(self.evidence)
        ambiguous["orders"].append(copy.deepcopy(ambiguous["orders"][1]))
        ambiguous["orders"][-1]["order_no"] = "SECOND_SELL"
        with self.assertRaisesRegex(base.IncidentRepairError, "UNIQUE_MANUAL_CLOSE"):
            self.plan(ambiguous)


if __name__ == "__main__":
    unittest.main()
