"""Private temporary SQLite + fabricated broker evidence only; never a login."""
from contextlib import ExitStack, closing
import copy
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from yuanta_broker_execution_v01 import ExecutionIntent, LiveOrderStore, Side
from yuanta_live_runtime_v01 import incident_repair as repair


class IncidentRepairTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        blocked = RuntimeError("MOCK_ONLY_EXTERNAL_ACCESS_BLOCKED")
        for target in ("socket.socket", "socket.create_connection", "subprocess.run", "subprocess.Popen", "yuanta_broker_execution_v01.sdk.load_api_types"):
            self.stack.enter_context(patch(target, side_effect=blocked))
        self.root = Path(self.stack.enter_context(TemporaryDirectory(prefix="incident-repair-mock-")))
        self.path = self.root / "fake.sqlite"
        self.backups = self.root / "private-backups"
        today = datetime.now(repair.TAIPEI)
        self.old_stamp = (today - timedelta(days=9)).replace(hour=9, minute=0, second=0, microsecond=0)
        self.entry_stamp = (today - timedelta(days=1)).replace(hour=9, minute=13, second=30, microsecond=209000)
        self.day = self.entry_stamp.strftime("%Y%m%d")
        store = LiveOrderStore(self.path)
        try:
            with patch("yuanta_broker_execution_v01.store.utc_now", return_value=self.old_stamp.isoformat()):
                old, _ = store.reserve(ExecutionIntent("MOCK-OLD", "3605", Side.BUY, 2000, Decimal("100")))
                request = store.create_request(old.client_order_id, "NEW")
                store.mark_send_pending(old.client_order_id)
                store.complete_request(request, success=False)
                store.reject(old.client_order_id, "MOCK documented original rejection")
            with patch("yuanta_broker_execution_v01.store.utc_now", return_value=self.entry_stamp.isoformat()):
                current, _ = store.reserve(ExecutionIntent("MOCK-CURRENT", "3094", Side.BUY, 2000, Decimal("70.1")))
                store.create_request(current.client_order_id, "NEW")
                store.mark_send_pending(current.client_order_id)
                store.bind_broker_order(old.client_order_id, "MOCK_BUY")
                for seq, price in (("MOCK_A", "70.0"), ("MOCK_B", "70.1")):
                    store.record_fill(old.client_order_id, fill_id="MOCK_BUY:" + seq,
                                      quantity=1000, price=price,
                                      broker_order_no="MOCK_BUY", seq_no=seq)
                store.halt("MOCK original identity mismatch")
            store.save_position_checkpoint(old.client_order_id, {"private_mock_checkpoint": "MUST_NOT_CHANGE"})
            self.old_id, self.current_id = old.client_order_id, current.client_order_id
            self.old_before = store.get(self.old_id)
            self.current_before = store.get(self.current_id)
            self.control_before = store.control_state()
        finally:
            store.close()
        self.basket = "MOCKSERVER01234567890123456789012"
        common = {"symbol": "3094", "trade_date": self.day, "trade_date_source": "OrderDate", "order_type": "0", "price_type": "LIMIT", "time_in_force": "ROD", "trade_kind": 0, "stk_error_no": "", "order_error_no": ""}
        buy = {**common, "rpt_type": 1, "order_no": "MOCK_BUY", "side": "B", "order_qty": 2000, "ok_qty": 2000, "price": "70.1", "avg_deal_price": "70.05", "order_status": 20, "last_order_status": 8, "basket_no": self.basket, "ap_code": 0, "order_time": "09:13:30.209"}
        sell = {**common, "rpt_type": 1, "order_no": "MOCK_MANUAL", "side": "S", "order_qty": 2000, "ok_qty": 1000, "price": "70.9", "avg_deal_price": "70.9", "order_status": 20, "last_order_status": 8, "basket_no": "", "ap_code": 7, "order_time": "14:00:03.084"}
        details = [
            {**common, "rpt_type": 51, "order_status": 8, "order_no": "MOCK_BUY", "side": "B", "order_qty": 1000, "price": price, "seq_no": seq, "basket_no": self.basket, "ap_code": 0, "order_time": "09:13:30.977"}
            for seq, price in (("MOCK_A", "70.0"), ("MOCK_B", "70.1"))
        ]
        details.append({**common, "rpt_type": 51, "order_status": 8, "order_no": "MOCK_MANUAL", "side": "S", "order_qty": 1000, "price": "70.9", "seq_no": "MOCK_SALE", "basket_no": "", "ap_code": 7, "order_time": "14:30:00.000"})
        self.evidence = {"schema_version": 1, "queried_at": today.isoformat(), "account_fingerprint": "000000000000", "account_rows_validated": True, "positions": {"3094|0": 1000}, "orders": [buy, sell], "details": details, "incident_mapping": {"human_confirmed": True, "confirmation_source": "USER_CONFIRMED_INCIDENT", "misbound_entry_id": self.old_id, "current_entry_id": self.current_id, "broker_order_no": "MOCK_BUY", "broker_basket_no": self.basket, "misbound_fill_ids": ["MOCK_BUY:MOCK_A", "MOCK_BUY:MOCK_B"]}}

    def plan(self, evidence=None, baseline=None):
        return repair.build_plan(self.path, self.evidence if evidence is None else evidence,
                                 self.old_id, self.current_id,
                                 {} if baseline is None else baseline)

    def row_state(self):
        with closing(sqlite3.connect(self.path)) as db:
            return repair._snapshot(db)

    def test_build_is_read_only_deterministic_and_keeps_explicit_blockers(self):
        before = self.path.read_bytes()
        plan = self.plan()
        self.assertEqual(plan, self.plan())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(plan["after_positions"], {"3094|0": 1000})
        self.assertFalse(plan["normal_start_ready"])
        self.assertIn("UNCONFIRMED_MANUAL_REMAINDER", {row["code"] for row in plan["blockers"]})
        order = plan["manual_orders"][0]
        self.assertEqual((order["quantity"], order["filled_quantity"], order["status"]), (2000, 1000, "PARTIALLY_FILLED"))
        self.assertEqual(order["created_at"], self.entry_stamp.replace(hour=14, minute=0, second=3, microsecond=84000).isoformat())

    def test_atomic_repair_reparents_actual_fills_and_preserves_halt_ids_and_checkpoint(self):
        plan = self.plan()
        before = self.row_state()
        before_bytes = self.path.read_bytes()
        result = repair.apply_plan(self.path, plan, self.backups)
        self.assertEqual(result["status"], "APPLIED_TRADING_BLOCKED")
        self.assertFalse(result["normal_start_ready"])
        self.assertEqual(result["broker_submission_calls"], 0)
        self.assertEqual(Path(result["backup_path"]).read_bytes(), before_bytes)
        self.assertEqual(result["backup_sha256"], hashlib.sha256(before_bytes).hexdigest())
        after = self.row_state()
        self.assertEqual(repair._ledger_positions(after), {"3094|0": 1000})
        self.assertEqual(after["tables"]["live_control"], before["tables"]["live_control"])
        self.assertEqual(after["tables"]["position_checkpoints"], before["tables"]["position_checkpoints"])
        rows = {row["client_order_id"]: row for row in after["tables"]["live_orders"]}
        self.assertEqual((rows[self.old_id]["status"], rows[self.old_id]["filled_quantity"], rows[self.old_id]["broker_order_no"]), ("REJECTED", 0, None))
        self.assertEqual((rows[self.current_id]["status"], rows[self.current_id]["filled_quantity"], rows[self.current_id]["average_fill_price"]), ("FILLED", 2000, "70.05"))
        self.assertEqual(rows[self.current_id]["basket_no"], self.basket)
        for identity in (self.old_id, self.current_id):
            original = next(row for row in before["tables"]["live_orders"] if row["client_order_id"] == identity)
            for key in ("intent_id", "fingerprint", "symbol", "side", "quantity", "created_at", "updated_at"):
                self.assertEqual(rows[identity][key], original[key])
        original_fills = {row["fill_id"]: row for row in before["tables"]["live_fills"]}
        for row in after["tables"]["live_fills"]:
            if row["fill_id"] in original_fills:
                expected = dict(original_fills[row["fill_id"]], client_order_id=self.current_id)
                self.assertEqual(row, expected)
        self.assertEqual(len(after["tables"]["broker_requests"]), len(before["tables"]["broker_requests"]))
        self.assertEqual(len(after["tables"]["live_events"]), len(before["tables"]["live_events"]))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM live_events WHERE event_type LIKE 'INCIDENT_REPAIR_%'").fetchone()[0], 2)
        self.assertEqual(repair.apply_plan(self.path, plan, self.backups)["status"], "ALREADY_APPLIED")

    def test_rollback_restores_every_logical_row_after_mid_transaction_failure(self):
        plan, before = self.plan(), self.row_state()
        original = repair._write_repair
        def crash(db, proposed):
            original(db, proposed)
            raise RuntimeError("MOCK crash after updates before audit/commit")
        with patch.object(repair, "_write_repair", side_effect=crash):
            with self.assertRaises(RuntimeError):
                repair.apply_plan(self.path, plan, self.backups)
        self.assertEqual(self.row_state(), before)
        self.assertTrue(list(self.backups.glob("*.sqlite")))
        self.assertEqual(repair.apply_plan(self.path, plan, self.backups)["status"], "APPLIED_TRADING_BLOCKED")

    def test_original_rejection_and_failed_request_both_required(self):
        for event in ("ORDER_REJECTED", "BROKER_REQUEST_RESULT"):
            with self.subTest(event=event):
                copied = self.root / (event + ".sqlite")
                copied.write_bytes(self.path.read_bytes())
                with closing(sqlite3.connect(copied, isolation_level=None)) as db:
                    db.execute("DELETE FROM live_events WHERE client_order_id=? AND event_type=?", (self.old_id, event))
                with self.assertRaises(repair.IncidentRepairError):
                    repair.build_plan(copied, self.evidence, self.old_id, self.current_id, {})

    def test_exact_fill_set_payload_and_broker_identity_required(self):
        alterations = [
            lambda e: e["details"][0].update(price="70.2"),
            lambda e: e["details"][0].update(seq_no="OTHER"),
            lambda e: e["details"][0].update(symbol="3605"),
            lambda e: e["details"][0].update(side="S"),
            lambda e: e["details"].pop(0),
            lambda e: e["incident_mapping"].update(human_confirmed=False),
            lambda e: e["incident_mapping"]["misbound_fill_ids"].pop(),
            lambda e: e["incident_mapping"].update(broker_basket_no="OTHER"),
        ]
        for alteration in alterations:
            with self.subTest(alteration=alteration):
                evidence = copy.deepcopy(self.evidence)
                alteration(evidence)
                with self.assertRaises(repair.IncidentRepairError):
                    self.plan(evidence)

    def test_native_dates_and_time_binding_cannot_use_receipt_or_query_time(self):
        for fields in ({"trade_date_source": "RECEIPT_TIME"}, {"trade_date": ""}, {"order_time": ""}, {"order_time": "09:14:30.209"}):
            evidence = copy.deepcopy(self.evidence)
            evidence["orders"][0].update(fields)
            with self.subTest(fields=fields), self.assertRaises(repair.IncidentRepairError):
                self.plan(evidence)

    def test_fully_filled_or_official_cancel_proof_only_changes_manual_terminal_state(self):
        for fields, expected in (({"order_qty": 1000}, "FILLED"), ({"order_status": 30, "last_order_status": 2}, "CANCELED"), ({"order_status": 24, "last_order_status": 24}, "EXPIRED")):
            evidence = copy.deepcopy(self.evidence)
            evidence["orders"][1].update(fields)
            plan = self.plan(evidence)
            self.assertEqual(plan["manual_orders"][0]["status"], expected)
            self.assertNotIn("UNCONFIRMED_MANUAL_REMAINDER", {row["code"] for row in plan["blockers"]})
            self.assertFalse(plan["normal_start_ready"])

    def test_actual_position_and_unchanged_baseline_must_reconcile_exactly(self):
        with self.assertRaisesRegex(repair.IncidentRepairError, "CURRENT_STOCK_BASELINE_MUST_BE_ZERO"):
            self.plan(baseline={"3094|0": 1000})
        evidence = copy.deepcopy(self.evidence)
        evidence["positions"] = {"3094|0": 2000}
        with self.assertRaisesRegex(repair.IncidentRepairError, "BROKER_BASELINE_LEDGER_QUANTITY_MISMATCH"):
            self.plan(evidence)

    def test_account_unvalidated_or_secret_fields_are_rejected(self):
        for alteration in (lambda e: e.update(account_rows_validated=False), lambda e: e.update(account_fingerprint="plain account"), lambda e: e["orders"][0].update(account="MOCK_ACCOUNT")):
            evidence = copy.deepcopy(self.evidence)
            alteration(evidence)
            with self.assertRaises(repair.IncidentRepairError):
                self.plan(evidence)

    def test_halt_cannot_be_cleared_by_repair(self):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("UPDATE live_control SET halted=0")
        with self.assertRaisesRegex(repair.IncidentRepairError, "HALT_MUST_ALREADY_BE_ACTIVE"):
            self.plan()

    def test_database_precondition_and_plan_derivation_are_exact(self):
        plan = self.plan()
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("UPDATE live_control SET next_identify=99")
        with self.assertRaisesRegex(repair.IncidentRepairError, "DATABASE_PRECONDITION_CHANGED"):
            repair.apply_plan(self.path, plan, self.backups)
        self.assertFalse(self.backups.exists())

    def test_tampered_plan_is_rejected_even_if_outer_hash_is_recomputed(self):
        plan = self.plan()
        plan["after"]["tables"]["live_control"][0]["halted"] = 0
        with self.assertRaisesRegex(repair.IncidentRepairError, "PLAN_HASH_MISMATCH"):
            repair.apply_plan(self.path, plan, self.backups)
        del plan["plan_hash"]
        plan["plan_hash"] = repair._digest(plan)
        with self.assertRaisesRegex(repair.IncidentRepairError, "PLAN_DERIVATION_MISMATCH"):
            repair.apply_plan(self.path, plan, self.backups)

    def test_duplicate_callback_evidence_is_idempotent_but_conflicts_refuse(self):
        evidence = copy.deepcopy(self.evidence)
        evidence["details"].append(copy.deepcopy(evidence["details"][0]))
        plan = self.plan(evidence)
        self.assertEqual(plan["after_positions"], {"3094|0": 1000})
        evidence["details"][-1]["price"] = "1"
        with self.assertRaisesRegex(repair.IncidentRepairError, "DUPLICATE_FILL_PROOF_CONFLICT"):
            self.plan(evidence)

    def test_unrelated_active_broker_order_blocks_any_apply(self):
        evidence = copy.deepcopy(self.evidence)
        other = dict(evidence["orders"][1], symbol="2330", order_no="MOCK_OTHER")
        evidence["orders"].append(other)
        with self.assertRaisesRegex(repair.IncidentRepairError, "UNRELATED_OPEN_BROKER_ORDER"):
            self.plan(evidence)

    def test_nonempty_sidecars_and_symlink_database_are_rejected(self):
        wal = Path(str(self.path) + "-wal")
        wal.write_bytes(b"MOCK uncheckpointed WAL")
        with self.assertRaisesRegex(repair.IncidentRepairError, "ACTIVE_OR_UNSAFE_SQLITE_SIDECAR"):
            self.plan()
        wal.unlink()
        alias = self.root / "unsafe.sqlite"
        alias.symlink_to(self.path)
        with self.assertRaisesRegex(repair.IncidentRepairError, "DATABASE_NOT_OWNED_REGULAR_FILE"):
            repair.build_plan(alias, self.evidence, self.old_id, self.current_id, {})

    def test_backup_directory_must_be_private_and_collision_cannot_be_overwritten(self):
        plan = self.plan()
        self.backups.mkdir(mode=0o755)
        with self.assertRaisesRegex(repair.IncidentRepairError, "BACKUP_DIRECTORY_NOT_PRIVATE"):
            repair.apply_plan(self.path, plan, self.backups)
        self.backups.chmod(0o700)
        (self.backups / (plan["plan_id"] + ".pre-repair.sqlite")).write_bytes(b"unrelated backup")
        with self.assertRaisesRegex(repair.IncidentRepairError, "BACKUP_COLLISION"):
            repair.apply_plan(self.path, plan, self.backups)

    def test_replay_after_later_state_changes_never_reapplies(self):
        plan = self.plan()
        repair.apply_plan(self.path, plan, self.backups)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("UPDATE live_control SET reason='MOCK later legitimate state'")
        with self.assertRaisesRegex(repair.IncidentRepairError, "APPLIED_PLAN_STATE_CHANGED"):
            repair.apply_plan(self.path, plan, self.backups)

    def test_corrected_legacy_receipts_remain_idempotent_under_current_adapter_replay(self):
        repair.apply_plan(self.path, self.plan(), self.backups)
        with LiveOrderStore(self.path) as store:
            for sequence, price in (("MOCK_A", "70.0"), ("MOCK_B", "70.1")):
                store.record_fill(self.current_id,
                                  fill_id=self.current_id + ":" + sequence,
                                  legacy_fill_id="MOCK_BUY:" + sequence,
                                  broker_order_no="MOCK_BUY", seq_no=sequence,
                                  quantity=1000, price=price)
            self.assertEqual(store.get(self.current_id).filled_quantity, 2000)
            self.assertEqual(store.positions(), {"3094": 1000})
            self.assertEqual(len(store.fills()), 3)

    def test_old_client_scoped_fill_keys_are_rejected_not_reparented_blindly(self):
        old_key, new_key = "MOCK_BUY:MOCK_A", self.old_id + ":MOCK_A"
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("UPDATE live_fills SET fill_id=? WHERE fill_id=?", (new_key, old_key))
        evidence = copy.deepcopy(self.evidence)
        evidence["incident_mapping"]["misbound_fill_ids"] = sorted([new_key, "MOCK_BUY:MOCK_B"])
        with self.assertRaisesRegex(repair.IncidentRepairError, "UNSUPPORTED_MISBOUND_FILL_ID_FORMAT"):
            self.plan(evidence)


    def test_missing_mapping_is_first_blocker_even_when_type_proof_is_missing(self):
        evidence = copy.deepcopy(self.evidence)
        evidence.pop("incident_mapping")
        evidence["orders"][0].pop("order_type")
        with self.assertRaisesRegex(repair.IncidentRepairError, "EXPLICIT_INCIDENT_MAPPING_REQUIRED"):
            self.plan(evidence)
        evidence["incident_mapping"] = copy.deepcopy(self.evidence["incident_mapping"])
        with self.assertRaisesRegex(repair.IncidentRepairError, "ENTRY_PRICE_OR_TYPE_IDENTITY_UNPROVEN"):
            self.plan(evidence)

    def test_manual_total_cannot_sell_more_than_proven_entry(self):
        evidence = copy.deepcopy(self.evidence)
        evidence["orders"][1].update(order_qty=3000, ok_qty=2500)
        evidence["details"][-1]["order_qty"] = 2500
        evidence["positions"] = {}
        with self.assertRaisesRegex(repair.IncidentRepairError, "MANUAL_SALE_EXCEEDS_PROVEN_ENTRY"):
            self.plan(evidence)

    def test_other_strategy_stock_or_financing_position_is_not_allocated(self):
        with LiveOrderStore(self.path) as store:
            order, _ = store.reserve(ExecutionIntent("MOCK-OTHER", "2330", Side.BUY,
                                                    1000, Decimal("100")), allow_halted=True)
            store.record_fill(order.client_order_id, fill_id="MOCK-OTHER-FILL", quantity=1000, price="100")
        with self.assertRaisesRegex(repair.IncidentRepairError, "OTHER_STRATEGY_POSITION_UNSUPPORTED"):
            self.plan()

    def test_buy_fill_basket_apcode_and_cash_type_must_match(self):
        for fields in ({"basket_no": "MOCK_OTHER_BASKET"}, {"ap_code": 7}, {"order_type": "3"}):
            evidence = copy.deepcopy(self.evidence)
            evidence["details"][0].update(fields)
            with self.assertRaisesRegex(repair.IncidentRepairError, "ENTRY_FILL_IDENTITY_CONFLICT"):
                self.plan(evidence)

    def test_manual_price_type_and_tif_cannot_be_invented(self):
        for field in ("price_type", "time_in_force"):
            evidence = copy.deepcopy(self.evidence)
            evidence["orders"][1].pop(field)
            with self.assertRaisesRegex(repair.IncidentRepairError, "MANUAL_PRICE_TYPE_OR_TIF_UNPROVEN"):
                self.plan(evidence)

    def test_stale_plan_can_be_inspected_but_apply_refuses_before_backup_or_write(self):
        evidence = copy.deepcopy(self.evidence)
        evidence["queried_at"] = (datetime.now(repair.TAIPEI) - timedelta(minutes=6)).isoformat()
        plan = self.plan(evidence)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(repair.IncidentRepairError, "APPLY_BROKER_EVIDENCE_STALE"):
            repair.apply_plan(self.path, plan, self.backups)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.backups.exists())

    def native_history(self, evidence=None):
        evidence = self.evidence if evidence is None else evidence
        day = self.entry_stamp.date().isoformat()
        rows = []
        for row in evidence["orders"]:
            rows.append({"source": "GetOrderTradeReport.StkOrderList", "account_verified": True,
                **{key: row[key] for key in ("symbol", "side", "order_no", "price", "ap_code",
                    "order_type", "price_type", "time_in_force", "basket_no", "ok_qty")},
                "trade_date": day, "trade_date_source": "TradeDate", "accept_date": day,
                "accept_date_source": "AcceptDate", "accept_time": row["order_time"],
                "accept_time_source": "AcceptTime", "original_qty": row["order_qty"],
                "after_qty": row["order_qty"], "cancel_qty": 0,
                "order_status": 20, "terminal_status": "UNKNOWN"})
        return {"source": "GetOrderTradeReport", "account_verified": True,
            "account_fingerprint": evidence["account_fingerprint"],
            "queried_at": evidence["queried_at"], "orders": rows, "trades": []}

    def test_native_history_terminal_proof_preserves_original_partial_fill_quantity(self):
        for status, expected in ((30, "CANCELED"), (24, "EXPIRED"), (25, "EXPIRED")):
            history = self.native_history()
            history["orders"][1].update(order_status=status, terminal_status=expected,
                                        after_qty=0, cancel_qty=1000)
            evidence = repair.bind_history_order_evidence(self.evidence, history)
            plan = self.plan(evidence)
            manual = plan["manual_orders"][0]
            self.assertEqual((manual["quantity"], manual["filled_quantity"], manual["status"]),
                             (2000, 1000, expected))
            self.assertEqual(plan["after_positions"], {"3094|0": 1000})
            self.assertNotIn("UNCONFIRMED_MANUAL_REMAINDER", {x["code"] for x in plan["blockers"]})
            self.assertFalse(plan["normal_start_ready"])

    def test_native_history_quantities_and_yesterday_rod_never_invent_expiry(self):
        history = self.native_history()
        history["orders"][1].update(after_qty=0, cancel_qty=1000)
        evidence = repair.bind_history_order_evidence(self.evidence, history)
        plan = self.plan(evidence)
        self.assertEqual(plan["manual_orders"][0]["status"], "PARTIALLY_FILLED")
        self.assertIn("UNCONFIRMED_MANUAL_REMAINDER", {x["code"] for x in plan["blockers"]})

    def test_native_history_exact_identity_account_and_type_conflicts_refuse(self):
        changes = [{"trade_date": "2000-01-01"}, {"original_qty": 1000},
                   {"accept_date": "2000-01-01"}, {"side": "B"}, {"symbol": "3605"},
                   {"price": "1"}, {"ap_code": 0}, {"order_type": "3"},
                   {"terminal_status": "EXPIRED"}, {"cancel_qty": 2000}]
        for fields in changes:
            history = self.native_history()
            history["orders"][1].update(fields)
            with self.subTest(fields=fields), self.assertRaises(repair.IncidentRepairError):
                repair.bind_history_order_evidence(self.evidence, history)
        history = self.native_history()
        history["account_fingerprint"] = "111111111111"
        with self.assertRaisesRegex(repair.IncidentRepairError, "NATIVE_HISTORY_ACCOUNT_UNPROVEN"):
            repair.bind_history_order_evidence(self.evidence, history)

    def test_native_history_supplies_only_proven_order_type_not_guessed_financing(self):
        history = self.native_history()
        evidence = copy.deepcopy(self.evidence)
        for row in evidence["orders"] + evidence["details"]:
            row.pop("order_type")
        merged = repair.bind_history_order_evidence(evidence, history)
        self.assertEqual(self.plan(merged)["after_positions"], {"3094|0": 1000})
        history["orders"][0]["order_type"] = ""
        with self.assertRaises(repair.IncidentRepairError):
            repair.bind_history_order_evidence(evidence, history)

    def test_distinct_native_accept_and_merge_times_preserve_semantics(self):
        history = self.native_history()
        history["orders"][1]["accept_time"] = "13:42:40.000"
        merged = repair.bind_history_order_evidence(self.evidence, history)
        plan = self.plan(merged)
        self.assertEqual(plan["manual_orders"][0]["created_at"],
                         self.entry_stamp.replace(hour=13, minute=42, second=40, microsecond=0).isoformat())
        self.assertEqual(plan["evidence"]["orders"][1]["order_time"], "14:00:03.084")
        self.assertEqual(plan["manual_fills"][0]["filled_at"],
                         self.entry_stamp.replace(hour=14, minute=30, second=0, microsecond=0).isoformat())

    def test_normalizer_native_basic_dates_work_on_python310_without_iso_assumptions(self):
        evidence, history = self.manual_ap_history()
        for order in history["orders"]:
            order["trade_date"] = order["trade_date"].replace("-", "")
            order["accept_date"] = order["accept_date"].replace("-", "")
        history["trades"][0]["trade_date"] = history["trades"][0]["trade_date"].replace("-", "")
        merged = repair.bind_history_order_evidence(evidence, history)
        plan = self.plan(merged)
        self.assertEqual(plan["manual_orders"][0]["status"], "PARTIALLY_FILLED")
        self.assertEqual(plan["manual_orders"][0]["created_at"],
                         self.entry_stamp.replace(hour=13, minute=42, second=40, microsecond=0).isoformat())

    def manual_ap_history(self):
        evidence, history = copy.deepcopy(self.evidence), self.native_history()
        evidence["details"][-1]["ap_code"] = 0
        history["orders"][1]["accept_time"] = "13:42:40.000"
        history["ap_code_contract"] = {"source": "YUANTA_NATIVE_IO_FIELD_DESCRIPTION",
                                      "verified": True, "exchange_code": 4, "ap_code": 7}
        history["trades"] = [{"source": "GetOrderTradeReport.StkTradeList", "account_verified": True,
            "order_no": "MOCK_MANUAL", "symbol": "3094", "side": "S", "order_type": "0",
            "trade_date": self.entry_stamp.date().isoformat(), "trade_date_source": "DateTime",
            "fill_time": "14:30:00.000", "fill_time_source": "DateTime", "exchange_code": 4,
            "ok_qty": 1000, "fill_price": "70.9", "order_price": "70.9"}]
        return evidence, history

    def test_manual_ap_discrepancy_requires_native_trade_and_preserves_raw_ap(self):
        evidence, history = self.manual_ap_history()
        merged = repair.bind_history_order_evidence(evidence, history)
        plan = self.plan(merged)
        self.assertEqual(plan["evidence"]["details"][-1]["ap_code"], 0)
        self.assertEqual(plan["manual_orders"][0]["ap_code"], 7)
        self.assertEqual(plan["manual_orders"][0]["status"], "PARTIALLY_FILLED")
        self.assertIn("manual_history_ap_discrepancy_proof", plan["evidence"]["details"][-1])

    def test_manual_ap_discrepancy_conflicting_or_nonunique_history_refuses(self):
        for fields in ({"symbol": "3605"}, {"side": "B"}, {"order_type": "3"},
                       {"exchange_code": 0}, {"ok_qty": 2000}, {"fill_price": "1"},
                       {"fill_time": "14:30:01.000"}, {"trade_date": "2000-01-01"}):
            evidence, history = self.manual_ap_history()
            history["trades"][0].update(fields)
            with self.subTest(fields=fields), self.assertRaises(repair.IncidentRepairError):
                repair.bind_history_order_evidence(evidence, history)
        evidence, history = self.manual_ap_history()
        history["trades"].append(copy.deepcopy(history["trades"][0]))
        with self.assertRaisesRegex(repair.IncidentRepairError, "MANUAL_HISTORY_TRADE_IDENTITY_AMBIGUOUS"):
            repair.bind_history_order_evidence(evidence, history)
        evidence, history = self.manual_ap_history()
        history.pop("ap_code_contract")
        with self.assertRaisesRegex(repair.IncidentRepairError, "MANUAL_AP_NATIVE_CONTRACT_UNPROVEN"):
            repair.bind_history_order_evidence(evidence, history)

    def test_manual_ap0_ack_is_not_a_fill_or_a_history_trade_candidate(self):
        evidence, history = self.manual_ap_history()
        ack = {**evidence["details"][-1], "rpt_type": 50, "order_status": 20,
               "order_qty": 2000, "order_time": "14:00:03.340", "seq_no": "MOCK_ACK"}
        evidence["details"].insert(2, ack)
        merged = repair.bind_history_order_evidence(evidence, history)
        plan = self.plan(merged)
        self.assertEqual(merged["details"][2]["ap_code"], 0)
        self.assertNotIn("manual_history_ap_discrepancy_proof", merged["details"][2])
        self.assertEqual(len(plan["manual_fills"]), 1)
        self.assertEqual(plan["manual_fills"][0]["quantity"], 1000)
        self.assertEqual(plan["after_positions"], {"3094|0": 1000})

    def test_manual_history_fill_timestamp_representation_is_exact_not_tolerant(self):
        evidence, history = self.manual_ap_history()
        history["trades"][0]["fill_time"] = "14:30:00.000000"
        merged = repair.bind_history_order_evidence(evidence, history)
        self.assertEqual(self.plan(merged)["manual_orders"][0]["filled_quantity"], 1000)
        self.assertEqual(merged["details"][-1]["order_time"], "14:30:00.000")
        for changed in ("14:30:00.001", "14:30:00.000001"):
            with self.subTest(changed=changed):
                history["trades"][0]["fill_time"] = changed
                with self.assertRaisesRegex(repair.IncidentRepairError, "MANUAL_HISTORY_TRADE_IDENTITY_AMBIGUOUS"):
                    repair.bind_history_order_evidence(evidence, history)

    def provenance(self):
        plan = self.plan()
        repair.apply_plan(self.path, plan, self.backups)
        baseline = self.root / "position_baseline.json"
        baseline.write_bytes(b"{}\n")
        evidence = {"schema_version": 1, "account_rows_validated": True,
                    "account_fingerprint": self.evidence["account_fingerprint"],
                    "queried_at": datetime.now(repair.TAIPEI).isoformat(),
                    "positions": {"3094|0": 1000}, "open_orders_validated": True,
                    "open_orders": [{"order_no": "MOCK_MANUAL", "remaining_quantity": 1000}]}
        review = {"human_confirmed": True, "confirmation_source": "USER_REVIEWED_ORIGINAL_CAPTURE",
                  "account_fingerprint": evidence["account_fingerprint"],
                  "baseline_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
                  "capture_evidence_verified": True, "capture_evidence_sha256": "a" * 64,
                  "capture_evidence_source": "ORIGINAL_CAPTURE_LOG",
                  "captured_at": self.entry_stamp.replace(hour=8, minute=30).isoformat(),
                  "trading_date": self.entry_stamp.date().isoformat(),
                  "repair_plan_id": plan["plan_id"], "repaired_database_digest": plan["after_digest"]}
        return baseline, evidence, review

    def test_provenance_only_restores_missing_metadata_not_baseline_halt_or_checkpoint(self):
        baseline, evidence, review = self.provenance()
        before_db, before_baseline = self.path.read_bytes(), baseline.read_bytes()
        plan = repair.build_baseline_provenance_plan(self.path, baseline, evidence, review)
        self.assertFalse((self.root / "position_baseline.meta.json").exists())
        result = repair.apply_baseline_provenance_plan(self.path, baseline, plan)
        self.assertEqual(result["status"], "PROVENANCE_REPAIRED_TRADING_BLOCKED")
        self.assertFalse(result["normal_start_ready"])
        self.assertEqual(result["broker_submission_calls"], 0)
        self.assertEqual(self.path.read_bytes(), before_db)
        self.assertEqual(baseline.read_bytes(), before_baseline)
        meta = self.root / "position_baseline.meta.json"
        payload = json.loads(meta.read_text())
        self.assertEqual(payload["captured_at"], review["captured_at"])
        self.assertEqual(payload["trading_date"], self.entry_stamp.date().isoformat())
        self.assertNotEqual(payload["trading_date"], datetime.now(repair.TAIPEI).date().isoformat())
        self.assertEqual(meta.stat().st_mode & 0o777, 0o600)
        self.assertEqual(repair.apply_baseline_provenance_plan(self.path, baseline, plan)["status"],
                         "ALREADY_APPLIED_TRADING_BLOCKED")

    def test_provenance_rejects_unreviewed_original_capture_wrong_account_or_hash(self):
        baseline, evidence, review = self.provenance()
        for fields in ({"capture_evidence_verified": False}, {"capture_evidence_source": "FILE_MTIME"},
                       {"baseline_sha256": "0" * 64}, {"account_fingerprint": "111111111111"},
                       {"captured_at": self.entry_stamp.replace(hour=10).isoformat()},
                       {"repaired_database_digest": "0" * 64}):
            modified = {**review, **fields}
            with self.subTest(fields=fields), self.assertRaises(repair.IncidentRepairError):
                repair.build_baseline_provenance_plan(self.path, baseline, evidence, modified)

    def test_provenance_never_absorbs_residual_or_hides_changed_broker_position(self):
        baseline, evidence, review = self.provenance()
        evidence["positions"] = {}
        with self.assertRaisesRegex(repair.IncidentRepairError, "BROKER_BASELINE_LEDGER_QUANTITY_MISMATCH"):
            repair.build_baseline_provenance_plan(self.path, baseline, evidence, review)
        baseline.write_text('{"3094|0":1000}')
        review["baseline_sha256"] = hashlib.sha256(baseline.read_bytes()).hexdigest()
        evidence["positions"] = {"3094|0": 2000}
        with self.assertRaisesRegex(repair.IncidentRepairError, "STRATEGY_EXPOSURE_ABSORBED_IN_BASELINE"):
            repair.build_baseline_provenance_plan(self.path, baseline, evidence, review)

    def test_provenance_stale_evidence_and_existing_metadata_cannot_overwrite(self):
        baseline, evidence, review = self.provenance()
        evidence["queried_at"] = (datetime.now(repair.TAIPEI) - timedelta(minutes=6)).isoformat()
        plan = repair.build_baseline_provenance_plan(self.path, baseline, evidence, review)
        with self.assertRaisesRegex(repair.IncidentRepairError, "APPLY_BROKER_EVIDENCE_STALE"):
            repair.apply_baseline_provenance_plan(self.path, baseline, plan)
        meta = self.root / "position_baseline.meta.json"
        meta.write_bytes(b"MOCK unrelated original provenance")
        evidence["queried_at"] = datetime.now(repair.TAIPEI).isoformat()
        with self.assertRaisesRegex(repair.IncidentRepairError, "BASELINE_METADATA_ALREADY_EXISTS"):
            repair.build_baseline_provenance_plan(self.path, baseline, evidence, review)
        self.assertEqual(meta.read_bytes(), b"MOCK unrelated original provenance")

    def test_provenance_failed_write_and_concurrent_publish_leave_no_partial_or_overwritten_meta(self):
        baseline, evidence, review = self.provenance()
        plan = repair.build_baseline_provenance_plan(self.path, baseline, evidence, review)
        meta = self.root / "position_baseline.meta.json"
        with patch.object(repair.os, "fsync", side_effect=OSError("MOCK disk failure")):
            with self.assertRaises(OSError):
                repair.apply_baseline_provenance_plan(self.path, baseline, plan)
        self.assertFalse(meta.exists())
        self.assertFalse(list(self.root.glob(".position_baseline.meta.json.*")))
        real_link = repair.os.link
        def create_conflict(source, target):
            meta.write_bytes(b"MOCK concurrently established provenance")
            return real_link(source, target)
        with patch.object(repair.os, "link", side_effect=create_conflict):
            with self.assertRaisesRegex(repair.IncidentRepairError, "BASELINE_METADATA_CONFLICT"):
                repair.apply_baseline_provenance_plan(self.path, baseline, plan)
        self.assertEqual(meta.read_bytes(), b"MOCK concurrently established provenance")
        self.assertFalse(list(self.root.glob(".position_baseline.meta.json.*")))


if __name__ == "__main__":
    unittest.main()
