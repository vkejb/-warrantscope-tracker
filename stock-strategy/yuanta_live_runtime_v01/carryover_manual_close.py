"""Offline reconciliation for a proved manual sale of a strategy carryover.

The broker evidence is captured by a separately reviewed read-only session.
This module never imports an SDK, logs in, clears HALT, or submits an order.
It preserves the original cross-day order and fills, records the newly proved
sale as a separate order, and closes the prior ROD remainder as expired only
after a later trading-day broker snapshot proves no current order or exposure.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from . import incident_repair as base
from .account_lock import acquire_account_lock, release_account_lock
from .main import _acquire_runtime_instance_lock, _release_runtime_instance_lock
from yuanta_intraday_shadow_v01.yuanta_keychain import load_credentials


def _fail(code: str):
    raise base.IncidentRepairError(code)


def _remote_terminal(row: dict) -> bool:
    qty = base._integer(row.get("order_qty"), minimum=1)
    filled = base._integer(row.get("ok_qty"))
    status = base._integer(row.get("order_status"))
    last = base._integer(row.get("last_order_status"))
    return status in {10, 24, 25, 30} or last in {1, 2, 24, 25} or filled >= qty


def _prepare(snapshot: dict, evidence: dict, baseline: dict, *, symbol: str) -> dict:
    evidence = json.loads(base._json(evidence))
    baseline = base._positions(baseline)
    if evidence.get("source") != "YUANTA_PROD_READONLY" or evidence.get("account_rows_validated") is not True:
        _fail("ACCOUNT_VALIDATED_EVIDENCE_REQUIRED")
    fingerprint = str(evidence.get("account_fingerprint", ""))
    if len(fingerprint) != 12 or any(ch not in "0123456789abcdef" for ch in fingerprint):
        _fail("ACCOUNT_FINGERPRINT_INVALID")
    captured = base._aware(evidence.get("captured_at"))
    if abs((datetime.now(base.TAIPEI) - captured).total_seconds()) > base.APPLY_EVIDENCE_MAX_AGE_SECONDS:
        _fail("APPLY_BROKER_EVIDENCE_STALE")
    if base._positions(evidence.get("positions")) != baseline:
        _fail("BROKER_NOT_AT_REVIEWED_BASELINE")
    if baseline.get(f"{symbol}|0", 0):
        _fail("TARGET_PRESENT_IN_BASELINE")
    orders = evidence.get("orders")
    details = evidence.get("details")
    history = evidence.get("history")
    if not isinstance(orders, list) or not isinstance(details, list) or not isinstance(history, dict):
        _fail("COMPLETE_BROKER_EVIDENCE_REQUIRED")
    if history.get("source") != "GetOrderTradeReport" or history.get("account_verified") is not True:
        _fail("NATIVE_HISTORY_ACCOUNT_UNPROVEN")
    if any(not _remote_terminal(row) for row in orders):
        _fail("BROKER_OPEN_ORDER_PRESENT")

    local = {row["client_order_id"]: row for row in snapshot["tables"]["live_orders"]}
    target_orders = [row for row in local.values() if row["symbol"] == symbol]
    entries = [row for row in target_orders if row["side"] == "BUY" and row["purpose"] == "ENTRY"]
    exits = [row for row in target_orders if row["side"] == "SELL" and row["purpose"] == "EXIT"]
    if len(entries) != 1 or len(exits) != 1:
        _fail("EXPECTED_SINGLE_CARRYOVER_PATH_MISSING")
    entry, prior_exit = entries[0], exits[0]
    if entry["status"] != "FILLED" or base._integer(entry["filled_quantity"]) <= 0:
        _fail("ENTRY_NOT_PROVEN_FILLED")
    if prior_exit["status"] != "PARTIALLY_FILLED" or prior_exit["time_in_force"] != "ROD":
        _fail("PRIOR_EXIT_NOT_PARTIAL_ROD")
    entry_day = base._aware(entry["created_at"]).date()
    prior_day = base._aware(prior_exit["created_at"]).date()
    if prior_day != entry_day or captured.date() <= prior_day:
        _fail("CROSS_DAY_EVIDENCE_REQUIRED")

    local_fills = snapshot["tables"]["live_fills"]
    entry_qty = sum(base._integer(row["quantity"], minimum=1) for row in local_fills
                    if row["client_order_id"] == entry["client_order_id"])
    prior_sold = sum(base._integer(row["quantity"], minimum=1) for row in local_fills
                     if row["client_order_id"] == prior_exit["client_order_id"])
    if entry_qty != base._integer(entry["filled_quantity"]) or prior_sold != base._integer(prior_exit["filled_quantity"]):
        _fail("LOCAL_FILL_AGGREGATE_CONFLICT")
    remaining = entry_qty - prior_sold
    if remaining <= 0:
        _fail("NO_CARRYOVER_REMAINDER")

    day = captured.strftime("%Y%m%d")
    merge = [row for row in orders if row.get("symbol") == symbol and base._side(row) == "SELL"
             and str(row.get("trade_date", "")).replace("-", "") == day]
    hist_orders = [row for row in history.get("orders", []) if row.get("symbol") == symbol
                   and base._side(row) == "SELL" and str(row.get("trade_date", "")).replace("-", "") == day]
    hist_trades = [row for row in history.get("trades", []) if row.get("symbol") == symbol
                   and base._side(row) == "SELL" and str(row.get("trade_date", "")).replace("-", "") == day]
    fill_details = [row for row in details if row.get("symbol") == symbol and base._side(row) == "SELL"
                    and str(row.get("trade_date", "")).replace("-", "") == day
                    and base._integer(row.get("rpt_type")) == 51 and base._integer(row.get("order_status")) == 8]
    if not (len(merge) == len(hist_orders) == len(hist_trades) == len(fill_details) == 1):
        _fail("MANUAL_SALE_IDENTITY_AMBIGUOUS")
    remote, native_order, native_trade, detail = merge[0], hist_orders[0], hist_trades[0], fill_details[0]
    keys = {(str(row.get("order_no", "")), row.get("symbol"), base._side(row))
            for row in (remote, native_order, native_trade, detail)}
    if len(keys) != 1:
        _fail("MANUAL_SALE_IDENTITY_CONFLICT")
    sold = base._integer(remote.get("ok_qty"), minimum=1)
    if (sold != remaining or sold != base._integer(remote.get("order_qty"), minimum=1)
            or sold != base._integer(native_order.get("ok_qty"), minimum=1)
            or sold != base._integer(native_order.get("original_qty"), minimum=1)
            or sold != base._integer(native_trade.get("ok_qty"), minimum=1)
            or sold != base._integer(detail.get("order_qty"), minimum=1)):
        _fail("MANUAL_SALE_QUANTITY_CONFLICT")
    if (base._decimal(native_trade.get("fill_price")) != base._decimal(detail.get("price"))
            or base._decimal(remote.get("avg_deal_price")) != base._decimal(detail.get("price"))):
        _fail("MANUAL_SALE_PRICE_CONFLICT")
    fill_at = base._history_native_time(native_trade, "trade_date", "fill_time")
    if fill_at != base._native_time(detail):
        _fail("MANUAL_SALE_TIME_CONFLICT")

    proposed = json.loads(base._json(snapshot))
    proposed_orders = {row["client_order_id"]: row for row in proposed["tables"]["live_orders"]}
    old = proposed_orders[prior_exit["client_order_id"]]
    old["status"] = "EXPIRED"
    old["last_error"] = "HISTORICAL_ROD_REMAINDER_EXPIRED_BEFORE_PROVEN_CARRYOVER_CLOSE"
    old["updated_at"] = captured.astimezone(timezone.utc).isoformat()

    token = base._digest({"account": fingerprint, "date": day, "order_no": remote["order_no"], "symbol": symbol})
    identity = "manual-close-" + token[:28]
    if identity in local or any(row.get("broker_order_no") == remote["order_no"] for row in local.values()):
        _fail("MANUAL_SALE_ALREADY_PRESENT")
    stamp = base._history_native_time(native_order, "accept_date", "accept_time").isoformat()
    new = {
        "client_order_id": identity, "intent_id": "CARRYOVER-MANUAL-CLOSE-" + token,
        "basket_no": "WS" + token[:30], "broker_order_no": remote["order_no"],
        "symbol": symbol, "side": "SELL", "quantity": sold,
        "price": str(base._decimal(remote["price"])), "price_type": native_order["price_type"],
        "time_in_force": native_order["time_in_force"], "ap_code": base._integer(native_order["ap_code"]),
        "order_type": str(native_order["order_type"]), "purpose": "EXIT", "status": "FILLED",
        "filled_quantity": sold, "average_fill_price": str(base._decimal(native_trade["fill_price"])),
        "last_error": "MANUAL_CARRYOVER_CLOSE_IMPORTED_NO_BROKER_SUBMISSION",
        "created_at": stamp, "updated_at": fill_at.isoformat(),
    }
    new["fingerprint"] = base._fingerprint(new)
    proposed["tables"]["live_orders"].append(new)
    proposed["tables"]["live_fills"].append({
        "fill_id": identity + ":" + str(detail["seq_no"]), "client_order_id": identity,
        "broker_order_no": remote["order_no"], "seq_no": str(detail["seq_no"]),
        "quantity": sold, "price": str(base._decimal(detail["price"])), "filled_at": fill_at.isoformat(),
    })
    if base._ledger_positions(proposed):
        _fail("REPAIR_DOES_NOT_FLATTEN_LOCAL_POSITION")
    plan = {
        "schema_version": 1, "status": "REPAIRABLE_HALT_PRESERVED", "apply_safe": True,
        "normal_start_ready": False, "symbol": symbol, "evidence": evidence,
        "broker_verified_as_of": evidence["captured_at"], "baseline": baseline,
        "prior_exit_id": prior_exit["client_order_id"], "manual_order": new,
        "manual_fill": proposed["tables"]["live_fills"][-1], "before": snapshot,
        "before_digest": base._digest(snapshot), "after": proposed, "after_digest": base._digest(proposed),
        "after_positions": {}, "blockers": [{"code": "HALT_REQUIRES_SEPARATE_BROKER_BACKED_CLEAR"}],
    }
    plan["plan_id"] = base._digest({"before": plan["before_digest"], "after": plan["after_digest"]})
    plan["plan_hash"] = base._digest(plan)
    return plan


def build_plan(db_path: str | Path, evidence: dict, baseline: dict, *, symbol: str = "3094") -> dict:
    path = Path(db_path).absolute()
    base._safe_file(path)
    base._no_sidecars(path)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            _fail("DATABASE_INTEGRITY_FAILURE")
        snapshot = base._snapshot(db)
    return _prepare(snapshot, evidence, baseline, symbol=symbol)


def apply_plan(db_path: str | Path, plan: dict, backup_dir: str | Path) -> dict:
    plan = json.loads(base._json(plan))
    recorded = plan.pop("plan_hash", None)
    if recorded != base._digest(plan):
        _fail("PLAN_HASH_MISMATCH")
    plan["plan_hash"] = recorded
    path = Path(db_path).absolute()
    base._safe_file(path)
    base._no_sidecars(path)
    db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        current = base._snapshot(db)
        if base._digest(current) != plan["before_digest"]:
            _fail("DATABASE_PRECONDITION_CHANGED")
        rebuilt = _prepare(current, plan["evidence"], plan["baseline"], symbol=plan["symbol"])
        if rebuilt != plan:
            _fail("PLAN_DERIVATION_MISMATCH")
        backup_path, backup_hash = base._backup(path, Path(backup_dir).absolute(), plan["plan_id"])
        stamp = datetime.now(timezone.utc).isoformat()
        audit = {"plan_id": plan["plan_id"], "plan_hash": recorded, "backup_path": backup_path,
                 "backup_sha256": backup_hash, "broker_verified_as_of": plan["broker_verified_as_of"],
                 "account_fingerprint": plan["evidence"]["account_fingerprint"]}
        old_after = next(row for row in plan["after"]["tables"]["live_orders"]
                         if row["client_order_id"] == plan["prior_exit_id"])
        db.execute("UPDATE live_orders SET status=?,last_error=?,updated_at=? WHERE client_order_id=?",
                   (old_after["status"], old_after["last_error"], old_after["updated_at"],
                    plan["prior_exit_id"]))
        row = plan["manual_order"]
        columns = tuple(row)
        db.execute(f'INSERT INTO live_orders ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                   tuple(row[key] for key in columns))
        fill = plan["manual_fill"]
        columns = tuple(fill)
        db.execute(f'INSERT INTO live_fills ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                   tuple(fill[key] for key in columns))
        if db.execute("PRAGMA foreign_key_check").fetchall() or db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            _fail("REPAIR_POSTCONDITION_FAILED")
        after = base._snapshot(db)
        if base._digest(after) != plan["after_digest"] or base._ledger_positions(after):
            _fail("REPAIR_POSTCONDITION_FAILED")
        db.execute("INSERT INTO live_events(event_type,client_order_id,payload,created_at) VALUES(?,NULL,?,?)",
                   ("CARRYOVER_CLOSE_REPAIR_BEFORE", base._json({**audit, "before_digest": plan["before_digest"]}), stamp))
        db.execute("INSERT INTO live_events(event_type,client_order_id,payload,created_at) VALUES(?,NULL,?,?)",
                   ("CARRYOVER_CLOSE_REPAIR_APPLIED", base._json({**audit, "after_digest": plan["after_digest"]}), stamp))
        db.commit()
        return {"status": "APPLIED_HALT_PRESERVED", "plan_id": plan["plan_id"],
                "backup_path": backup_path, "backup_sha256": backup_hash,
                "positions": {}, "broker_submission_calls": 0}
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
    parser.add_argument("--symbol", default="3094")
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
        db_path = runtime / "live-orders.sqlite"
        plan = build_plan(db_path, evidence, baseline, symbol=args.symbol)
        result = apply_plan(db_path, plan, runtime / "incident_backups")
        print(base._json({"status": result["status"], "positions": result["positions"],
                          "broker_submission_calls": result["broker_submission_calls"],
                          "halt_preserved": True, "plan_id": result["plan_id"]}))
        return 0
    finally:
        if credentials is not None:
            credentials.update({"pfx_password": "", "trading_password": ""})
        release_account_lock(account_lock)
        _release_runtime_instance_lock(runtime_lock)


if __name__ == "__main__":
    raise SystemExit(main())
