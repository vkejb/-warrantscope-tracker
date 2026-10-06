"""Offline repair for one same-day strategy entry closed manually at Yuanta.

This module deliberately has no broker SDK imports and cannot submit, cancel,
or replace an order.  It accepts only account-validated read-only evidence,
requires the broker inventory to equal the frozen opening baseline, and keeps
HALT active.  Its only mutation is to bind the proven entry fill and import the
proven manual closing fill into the durable local execution ledger.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from . import incident_repair as base
from .account_lock import acquire_account_lock, release_account_lock
from .main import _acquire_runtime_instance_lock, _release_runtime_instance_lock
from yuanta_intraday_shadow_v01.yuanta_keychain import load_credentials


AUDIT_EVENTS = {
    "SAME_DAY_MANUAL_CLOSE_REPAIR_BEFORE",
    "SAME_DAY_MANUAL_CLOSE_REPAIR_APPLIED",
}
TERMINAL_REMOTE_STATUSES = {10, 24, 25, 30}


def _fail(code: str):
    raise base.IncidentRepairError(code)


def _snapshot(db: sqlite3.Connection) -> dict:
    result = base._snapshot(db)
    result["tables"]["live_events"] = [
        row for row in result["tables"]["live_events"]
        if row["event_type"] not in AUDIT_EVENTS
    ]
    return result


def _basket_matches(local: object, broker: object) -> bool:
    local_text = str(local or "").strip()
    broker_text = str(broker or "").strip()
    if broker_text == local_text and bool(local_text):
        return True
    return bool(
        local_text.startswith("WS")
        and len(local_text) == 32
        and len(broker_text) == 32
        and broker_text[5:] == local_text[:27]
    )


def _remote_terminal(row: dict) -> bool:
    quantity = base._integer(row.get("order_qty"), minimum=1)
    filled = base._integer(row.get("ok_qty"))
    status = base._integer(row.get("order_status"))
    last = base._integer(row.get("last_order_status"))
    return (
        filled == quantity
        or status in TERMINAL_REMOTE_STATUSES
        or last in {1, 2, 24, 25}
    )


def _one(rows: list[dict], code: str) -> dict:
    if len(rows) != 1:
        _fail(code)
    return rows[0]


def _fill_rows(evidence: dict, *, order_no: str, symbol: str, side: str) -> list[dict]:
    rows = []
    seen: dict[str, dict] = {}
    for row in evidence["details"]:
        if (
            row.get("order_no") != order_no
            or row.get("symbol") != symbol
            or base._side(row) != side
            or base._integer(row.get("rpt_type")) != 51
        ):
            continue
        if base._integer(row.get("order_status")) != 8:
            _fail("ACTUAL_FILL_PROOF_INVALID")
        sequence = str(row.get("seq_no", ""))
        if sequence in {"", "0"}:
            _fail("ACTUAL_FILL_SEQUENCE_MISSING")
        if sequence in seen and seen[sequence] != row:
            _fail("DUPLICATE_FILL_PROOF_CONFLICT")
        seen[sequence] = row
    rows.extend(seen.values())
    if not rows:
        _fail("ACTUAL_FILL_PROOF_MISSING")
    return sorted(rows, key=lambda row: str(row["seq_no"]))


def _history_pair(evidence: dict, *, order_no: str, symbol: str, side: str) -> tuple[dict, dict]:
    history = evidence["history"]
    order = _one([
        row for row in history["orders"]
        if row.get("order_no") == order_no
        and row.get("symbol") == symbol
        and base._side(row) == side
    ], "NATIVE_HISTORY_ORDER_IDENTITY_AMBIGUOUS")
    trade = _one([
        row for row in history["trades"]
        if row.get("order_no") == order_no
        and row.get("symbol") == symbol
        and base._side(row) == side
    ], "NATIVE_HISTORY_TRADE_IDENTITY_AMBIGUOUS")
    if (
        order.get("source") != "GetOrderTradeReport.StkOrderList"
        or trade.get("source") != "GetOrderTradeReport.StkTradeList"
        or order.get("account_verified") is not True
        or trade.get("account_verified") is not True
    ):
        _fail("NATIVE_HISTORY_SOURCE_UNPROVEN")
    return order, trade


def _prepare(snapshot: dict, evidence: dict, baseline: dict, *, entry_id: str) -> dict:
    evidence = json.loads(base._json(evidence))
    baseline = base._positions(baseline)
    if (
        evidence.get("source") != "YUANTA_PROD_READONLY"
        or evidence.get("account_rows_validated") is not True
    ):
        _fail("ACCOUNT_VALIDATED_EVIDENCE_REQUIRED")
    fingerprint = str(evidence.get("account_fingerprint", ""))
    if not re.fullmatch(r"[0-9a-f]{12}", fingerprint):
        _fail("ACCOUNT_FINGERPRINT_INVALID")
    captured = base._aware(evidence.get("captured_at"))
    if captured > datetime.now(base.TAIPEI) and (captured - datetime.now(base.TAIPEI)).total_seconds() > 60:
        _fail("QUERY_TIME_IN_FUTURE")
    if base._positions(evidence.get("positions")) != baseline:
        _fail("BROKER_NOT_AT_FROZEN_BASELINE")

    orders = evidence.get("orders")
    details = evidence.get("details")
    history = evidence.get("history")
    if not isinstance(orders, list) or not isinstance(details, list) or not isinstance(history, dict):
        _fail("COMPLETE_BROKER_EVIDENCE_REQUIRED")
    if (
        history.get("source") != "GetOrderTradeReport"
        or history.get("account_verified") is not True
        or not isinstance(history.get("orders"), list)
        or not isinstance(history.get("trades"), list)
    ):
        _fail("NATIVE_HISTORY_ACCOUNT_UNPROVEN")
    for row in orders + details:
        if not isinstance(row, dict) or any(key in row for key in ("account", "password", "token", "api_key")):
            _fail("UNSANITIZED_OR_INVALID_EVIDENCE_ROW")
    if any(not _remote_terminal(row) for row in orders):
        _fail("BROKER_OPEN_ORDER_PRESENT")

    local_orders = {row["client_order_id"]: row for row in snapshot["tables"]["live_orders"]}
    entry = local_orders.get(entry_id)
    if entry is None:
        _fail("LOCAL_ENTRY_NOT_FOUND")
    if (
        entry["purpose"] != "ENTRY"
        or entry["side"] != "BUY"
        or str(entry["order_type"]) not in {"0", "9"}
        or entry["status"] not in {"SEND_PENDING", "ACKNOWLEDGED", "UNKNOWN"}
        or base._integer(entry["filled_quantity"]) != 0
        or entry["broker_order_no"] not in (None, "")
    ):
        _fail("LOCAL_ENTRY_STATE_UNSUPPORTED")
    entry_at = base._aware(entry["created_at"])
    symbol = str(entry["symbol"])
    if baseline.get(f"{symbol}|0", 0):
        _fail("TARGET_PRESENT_IN_FROZEN_BASELINE")
    if any(row["client_order_id"] == entry_id for row in snapshot["tables"]["live_fills"]):
        _fail("LOCAL_ENTRY_ALREADY_HAS_FILLS")
    requests = [
        row for row in snapshot["tables"]["broker_requests"]
        if row["client_order_id"] == entry_id and row["operation"] == "NEW"
    ]
    request = _one(requests, "EXPECTED_SINGLE_NEW_REQUEST_MISSING")
    if request["request_status"] not in {"SEND_PENDING", "ACCEPTED"}:
        _fail("LOCAL_NEW_REQUEST_STATE_UNSUPPORTED")
    day = entry_at.strftime("%Y%m%d")
    if any(
        row.get("symbol") == symbol
        and str(row.get("trading_date", "")).replace("-", "") == day
        for row in snapshot["tables"].get("external_inventory_adjustments", [])
    ):
        _fail("TARGET_EXTERNAL_ADJUSTMENT_ALREADY_PRESENT")

    broker_entry = _one([
        row for row in orders
        if row.get("symbol") == symbol
        and base._side(row) == "BUY"
        and str(row.get("trade_date", "")).replace("-", "") == day
        and base._integer(row.get("order_qty"), minimum=1) == base._integer(entry["quantity"], minimum=1)
        and abs((base._native_time(row) - entry_at).total_seconds()) <= 5
    ], "UNIQUE_NATIVE_ENTRY_IDENTITY_UNPROVEN")
    if (
        not _basket_matches(entry["basket_no"], broker_entry.get("basket_no"))
        or base._decimal(broker_entry.get("price")) != base._decimal(entry["price"])
        or base._integer(broker_entry.get("ap_code")) != base._integer(entry["ap_code"])
        or str(broker_entry.get("order_type")) != str(entry["order_type"])
        or not _remote_terminal(broker_entry)
    ):
        _fail("ENTRY_BROKER_IDENTITY_CONFLICT")
    broker_entry_no = str(broker_entry.get("order_no", ""))
    if not broker_entry_no:
        _fail("BROKER_ORDER_IDENTITY_MISSING")
    buy_fills = _fill_rows(evidence, order_no=broker_entry_no, symbol=symbol, side="BUY")
    buy_history, buy_trade = _history_pair(
        evidence, order_no=broker_entry_no, symbol=symbol, side="BUY",
    )
    bought, buy_average = base._mean(buy_fills)
    quantity = base._integer(entry["quantity"], minimum=1)
    if (
        bought != quantity
        or base._integer(broker_entry.get("ok_qty")) != quantity
        or base._integer(buy_history.get("ok_qty")) != quantity
        or base._integer(buy_trade.get("ok_qty")) != quantity
        or base._decimal(broker_entry.get("avg_deal_price")) != base._decimal(buy_average)
        or base._decimal(buy_trade.get("fill_price")) != base._decimal(buy_average)
    ):
        _fail("FULL_ENTRY_FILL_PROOF_REQUIRED")

    entry_fill_time = max(base._native_time(row) for row in buy_fills)
    if base._history_native_time(buy_trade, "trade_date", "fill_time") != entry_fill_time:
        _fail("ENTRY_FILL_TIME_CONFLICT")

    manual = _one([
        row for row in orders
        if row.get("symbol") == symbol
        and base._side(row) == "SELL"
        and str(row.get("trade_date", "")).replace("-", "") == day
        and base._native_time(row) > base._native_time(broker_entry)
        and base._integer(row.get("ok_qty")) == quantity
        and base._integer(row.get("order_qty"), minimum=1) == quantity
    ], "UNIQUE_MANUAL_CLOSE_IDENTITY_UNPROVEN")
    if str(manual.get("basket_no", "")):
        _fail("MANUAL_CLOSE_MUST_NOT_CLAIM_STRATEGY_BASKET")
    if str(manual.get("order_type")) != "0" or not _remote_terminal(manual):
        _fail("MANUAL_CLOSE_TYPE_OR_STATUS_UNPROVEN")
    manual_no = str(manual.get("order_no", ""))
    if not manual_no or manual_no == broker_entry_no:
        _fail("MANUAL_CLOSE_ORDER_IDENTITY_INVALID")
    sell_fills = _fill_rows(evidence, order_no=manual_no, symbol=symbol, side="SELL")
    sell_history, sell_trade = _history_pair(
        evidence, order_no=manual_no, symbol=symbol, side="SELL",
    )
    sold, sell_average = base._mean(sell_fills)
    if (
        sold != quantity
        or base._integer(sell_history.get("ok_qty")) != quantity
        or base._integer(sell_trade.get("ok_qty")) != quantity
        or base._decimal(manual.get("avg_deal_price")) != base._decimal(sell_average)
        or base._decimal(sell_trade.get("fill_price")) != base._decimal(sell_average)
    ):
        _fail("FULL_MANUAL_CLOSE_FILL_PROOF_REQUIRED")
    manual_fill_time = max(base._native_time(row) for row in sell_fills)
    if (
        manual_fill_time <= entry_fill_time
        or base._history_native_time(sell_trade, "trade_date", "fill_time") != manual_fill_time
    ):
        _fail("MANUAL_CLOSE_FILL_TIME_CONFLICT")
    if any(row.get("broker_order_no") == manual_no for row in local_orders.values()):
        _fail("MANUAL_CLOSE_ALREADY_IMPORTED")

    proposed = json.loads(base._json(snapshot))
    target = next(row for row in proposed["tables"]["live_orders"] if row["client_order_id"] == entry_id)
    target.update({
        "broker_order_no": broker_entry_no,
        "status": "FILLED",
        "filled_quantity": quantity,
        "average_fill_price": str(base._decimal(buy_average)),
        "last_error": None,
        "updated_at": entry_fill_time.astimezone(timezone.utc).isoformat(),
    })
    for row in buy_fills:
        proposed["tables"]["live_fills"].append({
            "fill_id": f'{entry_id}:{row["seq_no"]}',
            "client_order_id": entry_id,
            "broker_order_no": broker_entry_no,
            "seq_no": str(row["seq_no"]),
            "quantity": base._integer(row["order_qty"], minimum=1),
            "price": str(base._decimal(row["price"])),
            "filled_at": base._native_time(row).isoformat(),
        })
    for row in proposed["tables"]["broker_requests"]:
        if row["identify"] == request["identify"]:
            row["request_status"] = "CONFIRMED"
            row["updated_at"] = entry_fill_time.astimezone(timezone.utc).isoformat()

    token = base._digest({
        "account": fingerprint, "date": day,
        "order_no": manual_no, "symbol": symbol,
    })
    manual_id = "manual-same-day-close-" + token[:24]
    accepted = base._history_native_time(sell_history, "accept_date", "accept_time")
    imported = {
        "client_order_id": manual_id,
        "intent_id": "SAME-DAY-MANUAL-CLOSE-" + token,
        "basket_no": "WS" + token[:30],
        "broker_order_no": manual_no,
        "symbol": symbol,
        "side": "SELL",
        "quantity": quantity,
        "price": str(base._decimal(manual["price"])),
        "price_type": str(sell_history["price_type"]),
        "time_in_force": str(sell_history["time_in_force"]),
        "ap_code": base._integer(sell_history["ap_code"]),
        "order_type": str(sell_history["order_type"]),
        "purpose": "EXIT",
        "status": "FILLED",
        "filled_quantity": quantity,
        "average_fill_price": str(base._decimal(sell_average)),
        "last_error": "MANUAL_SAME_DAY_CLOSE_IMPORTED_NO_BROKER_SUBMISSION",
        "created_at": accepted.isoformat(),
        "updated_at": manual_fill_time.isoformat(),
    }
    imported["fingerprint"] = base._fingerprint(imported)
    proposed["tables"]["live_orders"].append(imported)
    for row in sell_fills:
        proposed["tables"]["live_fills"].append({
            "fill_id": f'{manual_id}:{row["seq_no"]}',
            "client_order_id": manual_id,
            "broker_order_no": manual_no,
            "seq_no": str(row["seq_no"]),
            "quantity": base._integer(row["order_qty"], minimum=1),
            "price": str(base._decimal(row["price"])),
            "filled_at": base._native_time(row).isoformat(),
        })
    if base._ledger_positions(proposed):
        _fail("REPAIR_DOES_NOT_FLATTEN_LOCAL_POSITION")
    controls = proposed["tables"]["live_control"]
    if len(controls) != 1 or base._integer(controls[0].get("halted")) != 1:
        _fail("HALT_MUST_ALREADY_BE_ACTIVE")

    plan = {
        "schema_version": 1,
        "status": "REPAIRABLE_HALT_PRESERVED",
        "apply_safe": True,
        "normal_start_ready": False,
        "entry_id": entry_id,
        "manual_order": imported,
        "buy_fills": proposed["tables"]["live_fills"][-len(sell_fills)-len(buy_fills):-len(sell_fills)],
        "manual_fills": proposed["tables"]["live_fills"][-len(sell_fills):],
        "request_identify": request["identify"],
        "evidence": evidence,
        "broker_verified_as_of": evidence["captured_at"],
        "baseline": baseline,
        "before": snapshot,
        "before_digest": base._digest(snapshot),
        "after": proposed,
        "after_digest": base._digest(proposed),
        "after_positions": {},
        "blockers": [{"code": "HALT_REQUIRES_SEPARATE_BROKER_BACKED_CLEAR"}],
    }
    plan["plan_id"] = base._digest({
        "before": plan["before_digest"], "after": plan["after_digest"],
        "evidence": evidence,
    })
    plan["plan_hash"] = base._digest(plan)
    return plan


def build_plan(db_path: str | Path, evidence: dict, baseline: dict, *, entry_id: str) -> dict:
    path = Path(db_path).absolute()
    base._safe_file(path)
    base._no_sidecars(path)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            _fail("DATABASE_INTEGRITY_FAILURE")
        snapshot = _snapshot(db)
    return _prepare(snapshot, evidence, baseline, entry_id=entry_id)


def checkpoint_offline_database(db_path: str | Path) -> None:
    """Durably merge a dead controller's WAL while exclusive locks are held."""
    path = Path(db_path).absolute()
    base._safe_file(path)
    journal = Path(str(path) + "-journal")
    if journal.exists() and journal.stat().st_size:
        _fail("ACTIVE_OR_UNSAFE_SQLITE_JOURNAL")
    wal = Path(str(path) + "-wal")
    if wal.exists():
        info = wal.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            _fail("UNSAFE_SQLITE_WAL")
    with sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=2) as db:
        db.execute("PRAGMA busy_timeout=2000")
        checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            _fail("SQLITE_WAL_CHECKPOINT_BUSY")
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            _fail("DATABASE_INTEGRITY_FAILURE")
    base._no_sidecars(path)


def apply_plan(db_path: str | Path, plan: dict, backup_dir: str | Path) -> dict:
    plan = json.loads(base._json(plan))
    recorded = plan.pop("plan_hash", None)
    if recorded != base._digest(plan):
        _fail("PLAN_HASH_MISMATCH")
    plan["plan_hash"] = recorded
    if plan.get("apply_safe") is not True or plan.get("normal_start_ready") is not False:
        _fail("UNSAFE_PLAN_FLAGS")
    path = Path(db_path).absolute()
    base._safe_file(path)
    base._no_sidecars(path)
    db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        current = _snapshot(db)
        markers = db.execute(
            "SELECT payload FROM live_events WHERE event_type='SAME_DAY_MANUAL_CLOSE_REPAIR_APPLIED'"
        ).fetchall()
        for marker in markers:
            payload = json.loads(marker[0])
            if payload.get("plan_id") == plan["plan_id"]:
                if payload.get("plan_hash") != recorded or base._digest(current) != plan["after_digest"]:
                    _fail("APPLIED_PLAN_STATE_CHANGED")
                db.rollback()
                return {
                    "status": "ALREADY_APPLIED", "plan_id": plan["plan_id"],
                    "positions": {}, "broker_submission_calls": 0,
                }
        if base._digest(current) != plan["before_digest"]:
            _fail("DATABASE_PRECONDITION_CHANGED")
        rebuilt = _prepare(current, plan["evidence"], plan["baseline"], entry_id=plan["entry_id"])
        if rebuilt != plan:
            _fail("PLAN_DERIVATION_MISMATCH")
        age = (datetime.now(base.TAIPEI) - base._aware(plan["broker_verified_as_of"])).total_seconds()
        if age < -60 or age > base.APPLY_EVIDENCE_MAX_AGE_SECONDS:
            _fail("APPLY_BROKER_EVIDENCE_STALE")
        backup_path, backup_hash = base._backup(path, Path(backup_dir).absolute(), plan["plan_id"])
        stamp = datetime.now(timezone.utc).isoformat()
        audit = {
            "plan_id": plan["plan_id"], "plan_hash": recorded,
            "backup_path": backup_path, "backup_sha256": backup_hash,
            "broker_verified_as_of": plan["broker_verified_as_of"],
            "account_fingerprint": plan["evidence"]["account_fingerprint"],
        }
        db.execute(
            "INSERT INTO live_events(event_type,client_order_id,payload,created_at) VALUES(?,?,?,?)",
            ("SAME_DAY_MANUAL_CLOSE_REPAIR_BEFORE", plan["entry_id"],
             base._json({**audit, "before_digest": plan["before_digest"]}), stamp),
        )
        entry = next(row for row in plan["after"]["tables"]["live_orders"] if row["client_order_id"] == plan["entry_id"])
        db.execute(
            "UPDATE live_orders SET broker_order_no=?,status=?,filled_quantity=?,average_fill_price=?,last_error=?,updated_at=? WHERE client_order_id=?",
            (entry["broker_order_no"], entry["status"], entry["filled_quantity"],
             entry["average_fill_price"], entry["last_error"], entry["updated_at"], plan["entry_id"]),
        )
        db.execute(
            "UPDATE broker_requests SET request_status='CONFIRMED',updated_at=? WHERE identify=?",
            (entry["updated_at"], plan["request_identify"]),
        )
        for row in plan["buy_fills"]:
            columns = tuple(row)
            db.execute(
                f'INSERT INTO live_fills ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                tuple(row[key] for key in columns),
            )
        row = plan["manual_order"]
        columns = tuple(row)
        db.execute(
            f'INSERT INTO live_orders ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
            tuple(row[key] for key in columns),
        )
        for row in plan["manual_fills"]:
            columns = tuple(row)
            db.execute(
                f'INSERT INTO live_fills ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                tuple(row[key] for key in columns),
            )
        if db.execute("PRAGMA foreign_key_check").fetchall() or db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            _fail("REPAIR_POSTCONDITION_FAILED")
        if base._digest(_snapshot(db)) != plan["after_digest"] or base._ledger_positions(_snapshot(db)):
            _fail("REPAIR_POSTCONDITION_FAILED")
        db.execute(
            "INSERT INTO live_events(event_type,client_order_id,payload,created_at) VALUES(?,?,?,?)",
            ("SAME_DAY_MANUAL_CLOSE_REPAIR_APPLIED", plan["entry_id"],
             base._json({**audit, "after_digest": plan["after_digest"],
                         "normal_start_ready": False, "halt_preserved": True}), stamp),
        )
        db.commit()
        return {
            "status": "APPLIED_HALT_PRESERVED", "plan_id": plan["plan_id"],
            "backup_path": backup_path, "backup_sha256": backup_hash,
            "positions": {}, "broker_submission_calls": 0,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--entry-id", required=True)
    args = parser.parse_args()
    runtime = args.runtime_dir.resolve()
    runtime_lock = _acquire_runtime_instance_lock(runtime)
    credentials = None
    account_lock = None
    try:
        credentials = load_credentials()
        account_lock = acquire_account_lock(credentials["account"], "PROD", runtime_dir=runtime)
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        checkpoint_offline_database(runtime / "live-orders.sqlite")
        plan = build_plan(
            runtime / "live-orders.sqlite", evidence, baseline, entry_id=args.entry_id,
        )
        result = apply_plan(runtime / "live-orders.sqlite", plan, runtime / "incident_backups")
        print(base._json({
            "status": result["status"], "positions": result["positions"],
            "broker_submission_calls": 0, "halt_preserved": True,
            "plan_id": result["plan_id"],
        }))
        return 0
    finally:
        if credentials is not None:
            credentials.update({"pfx_password": "", "trading_password": ""})
        release_account_lock(account_lock)
        _release_runtime_instance_lock(runtime_lock)


if __name__ == "__main__":
    raise SystemExit(main())
