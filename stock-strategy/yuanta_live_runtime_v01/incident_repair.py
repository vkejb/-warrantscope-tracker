"""Offline, broker-evidence-backed correction of one misattributed entry.

This module has no broker/session imports and no automatic runtime path. Plans
contain private financial evidence and MUST NOT be committed or shared. A plan
is not authentication of its evidence: the caller must obtain account-validated
broker rows through an independently reviewed read-only query and stop every
controller before applying it. Runtime/account locks are the caller's duty.

An unconfirmed manual sell remainder is imported as PARTIALLY_FILLED, never
invented cancelled/expired. HALT remains active and no LIVE readiness is claimed.
No baseline, MFE checkpoint, intent identity, or next-identify is rewritten.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
SCHEMA_VERSION = 1
TIME_BINDING_SECONDS = Decimal("5")
APPLY_EVIDENCE_MAX_AGE_SECONDS = 300
REPAIR_EVENTS = {"INCIDENT_REPAIR_BEFORE", "INCIDENT_REPAIR_APPLIED"}
REQUIRED_TABLES = {"live_orders", "live_fills", "broker_requests", "live_events", "live_control"}
TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}


class IncidentRepairError(RuntimeError):
    """A stable error code; never include raw private evidence in errors."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str):
    raise IncidentRepairError(code)


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        _fail("NON_JSON_OR_NONFINITE_EVIDENCE")


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _decimal(value: Any, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool):
        _fail("INVALID_DECIMAL")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        _fail("INVALID_DECIMAL")
    if not number.is_finite() or (positive and number <= 0):
        _fail("INVALID_DECIMAL")
    return number


def _integer(value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        _fail("INVALID_INTEGER")
    number = _decimal(value, positive=False)
    if number != number.to_integral_value() or number < minimum or number > 2_000_000_000:
        _fail("INVALID_INTEGER")
    return int(number)


def _aware(value: Any) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError()
        return stamp.astimezone(TAIPEI)
    except (ValueError, TypeError, OverflowError):
        _fail("UNPROVEN_TIMESTAMP")


def _native_time(row: dict) -> datetime:
    if row.get("trade_date_source") not in {"OrderDate", "TradeDate", "TradeDate+OrderDate"}:
        _fail("BROKER_DATE_SOURCE_MISSING")
    day = str(row.get("trade_date", "")).replace("-", "").replace("/", "")
    clock = str(row.get("order_time", ""))
    if not re.fullmatch(r"[0-9]{8}", day) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?", clock):
        _fail("BROKER_NATIVE_DATE_TIME_MISSING")
    try:
        parsed = datetime.fromisoformat(f"{day[:4]}-{day[4:6]}-{day[6:]}T{clock}")
        return parsed.replace(tzinfo=TAIPEI)
    except ValueError:
        _fail("BROKER_NATIVE_DATE_TIME_INVALID")


def _side(row: dict) -> str:
    value = str(row.get("side", "")).upper()
    if value not in {"B", "BUY", "S", "SELL"}:
        _fail("BROKER_SIDE_UNPROVEN")
    return "BUY" if value in {"B", "BUY"} else "SELL"


def _positions(rows: dict) -> dict[str, int]:
    if not isinstance(rows, dict):
        _fail("POSITIONS_MISSING")
    result = {}
    for key, raw in rows.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z0-9]+\|[0346]", key):
            _fail("POSITION_BUCKET_INVALID")
        quantity = _decimal(raw, positive=False)
        if quantity != quantity.to_integral_value() or abs(quantity) > 2_000_000_000:
            _fail("POSITION_QUANTITY_INVALID")
        kind = key.rsplit("|", 1)[1]
        if (kind in {"0", "3"} and quantity < 0) or (kind in {"4", "6"} and quantity > 0):
            _fail("POSITION_SIGN_INVALID")
        if quantity:
            result[key] = int(quantity)
    return result


def _no_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and sidecar.stat().st_size):
            _fail("ACTIVE_OR_UNSAFE_SQLITE_SIDECAR")


def _safe_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        _fail("DATABASE_UNAVAILABLE")
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        _fail("DATABASE_NOT_OWNED_REGULAR_FILE")


def _snapshot(db: sqlite3.Connection) -> dict:
    db.row_factory = sqlite3.Row
    schema = {row[0]: row[1] for row in db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )}
    if not REQUIRED_TABLES <= schema.keys():
        _fail("UNSUPPORTED_STORE_SCHEMA")
    tables = {}
    for name in schema:
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            _fail("UNSUPPORTED_TABLE_NAME")
        rows = [dict(row) for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
        # Only our own audit rows are excluded from the idempotency digest.
        if name == "live_events":
            rows = [row for row in rows if row["event_type"] not in REPAIR_EVENTS]
        tables[name] = rows
    return {"schema": schema, "tables": tables}


def _ledger_positions(snapshot: dict) -> dict[str, int]:
    orders = {row["client_order_id"]: row for row in snapshot["tables"]["live_orders"]}
    kinds = {"0": "0", "9": "0", "3": "3", "4": "4", "5": "6", "6": "6"}
    totals = {}
    for fill in snapshot["tables"]["live_fills"]:
        order = orders.get(fill["client_order_id"])
        if order is None or str(order["order_type"]) not in kinds:
            _fail("LOCAL_FILL_OWNERSHIP_INVALID")
        key = f'{order["symbol"]}|{kinds[str(order["order_type"])]}'
        amount = _integer(fill["quantity"], minimum=1)
        totals[key] = totals.get(key, 0) + amount * (1 if order["side"] == "BUY" else -1)
    return {key: value for key, value in totals.items() if value}


def _terminal_or_partial(row: dict) -> str:
    quantity = _integer(row.get("order_qty"), minimum=1)
    filled = _integer(row.get("ok_qty"))
    if filled > quantity:
        _fail("BROKER_OVERFILL")
    status = _integer(row.get("order_status"))
    last = _integer(row.get("last_order_status"))
    if status == 30 or last == 2:
        return "CANCELED"
    if status in {24, 25} or last in {24, 25}:
        return "EXPIRED"
    if status == 10 or last == 1:
        return "REJECTED"
    if filled == quantity:
        return "FILLED"
    return "PARTIALLY_FILLED" if filled else "ACKNOWLEDGED"


def _mean(fills: list[dict]) -> tuple[int, str]:
    quantity = sum(_integer(row["order_qty"], minimum=1) for row in fills)
    if not quantity:
        _fail("FILL_PROOF_MISSING")
    total = sum((_decimal(row["price"]) * _integer(row["order_qty"], minimum=1) for row in fills), Decimal(0))
    return quantity, str(total / quantity)


def _fingerprint(order: dict) -> str:
    price = None if order["price"] is None else str(_decimal(order["price"]).normalize())
    payload = {key: order[key] for key in ("intent_id", "symbol", "side", "quantity", "price_type", "time_in_force", "ap_code", "order_type", "purpose")}
    payload["price"] = price
    return _digest(payload)


def _prepare(snapshot: dict, evidence: dict, old_id: str, current_id: str, baseline: dict) -> dict:
    # Copy through strict JSON to avoid references to mutable caller objects.
    evidence = json.loads(_json(evidence))
    mapping = evidence.get("incident_mapping")
    if (not isinstance(mapping, dict) or mapping.get("human_confirmed") is not True
            or mapping.get("confirmation_source") != "USER_CONFIRMED_INCIDENT"
            or mapping.get("misbound_entry_id") != old_id
            or mapping.get("current_entry_id") != current_id):
        _fail("EXPLICIT_INCIDENT_MAPPING_REQUIRED")
    if evidence.get("schema_version") != SCHEMA_VERSION or evidence.get("account_rows_validated") is not True:
        _fail("ACCOUNT_VALIDATED_EVIDENCE_REQUIRED")
    account_hash = evidence.get("account_fingerprint", "")
    if not isinstance(account_hash, str) or not re.fullmatch(r"[0-9a-f]{12}", account_hash):
        _fail("ACCOUNT_FINGERPRINT_INVALID")
    query_time = _aware(evidence.get("queried_at"))
    if query_time > datetime.now(TAIPEI).replace(microsecond=0) and (query_time - datetime.now(TAIPEI)).total_seconds() > 60:
        _fail("QUERY_TIME_IN_FUTURE")
    actual, baseline = _positions(evidence.get("positions")), _positions(baseline)
    orders, details = evidence.get("orders"), evidence.get("details")
    if not isinstance(orders, list) or not isinstance(details, list) or not orders or not details:
        _fail("COMPLETE_ORDER_AND_DETAIL_EVIDENCE_REQUIRED")
    for row in orders + details:
        if not isinstance(row, dict) or any(key in row for key in ("account", "password", "token", "api_key")):
            _fail("UNSANITIZED_OR_INVALID_EVIDENCE_ROW")
        if not str(row.get("order_no", "")).strip() or not str(row.get("symbol", "")).strip():
            _fail("BROKER_ORDER_IDENTITY_MISSING")
        _side(row)
        if _native_time(row) > query_time:
            _fail("BROKER_ROW_AFTER_QUERY")
    local_orders = {row["client_order_id"]: row for row in snapshot["tables"]["live_orders"]}
    old, current = local_orders.get(old_id), local_orders.get(current_id)
    if old is None or current is None or old_id == current_id:
        _fail("ENTRY_IDS_INVALID")
    if any(row["purpose"] != "ENTRY" or row["side"] != "BUY" or str(row["order_type"]) not in {"0", "9"} for row in (old, current)):
        _fail("UNSUPPORTED_ENTRY_CATEGORY")
    if old["symbol"] == current["symbol"] or _aware(old["created_at"]).date() >= _aware(current["created_at"]).date():
        _fail("HISTORICAL_DISTINCT_ENTRY_REQUIRED")
    if baseline.get(f'{current["symbol"]}|0', 0):
        _fail("CURRENT_STOCK_BASELINE_MUST_BE_ZERO")
    if any(key not in {f'{old["symbol"]}|0', f'{current["symbol"]}|0'} for key in _ledger_positions(snapshot)):
        _fail("OTHER_STRATEGY_POSITION_UNSUPPORTED")
    local_fills = snapshot["tables"]["live_fills"]
    wrong = [row for row in local_fills if row["client_order_id"] == old_id]
    if not wrong or any(row["client_order_id"] == current_id for row in local_fills) or _integer(current["filled_quantity"]) != 0:
        _fail("EXPECTED_EXACT_MISBOUND_FILL_STATE_MISSING")
    events = snapshot["tables"]["live_events"]
    reject_events = [row for row in events if row["client_order_id"] == old_id and row["event_type"] == "ORDER_REJECTED" and _aware(row["created_at"]).date() == _aware(old["created_at"]).date()]
    if not reject_events:
        _fail("ORIGINAL_REJECTION_PROOF_MISSING")
    rejected_requests = set()
    for row in events:
        if row["client_order_id"] == old_id and row["event_type"] == "BROKER_REQUEST_RESULT" and _aware(row["created_at"]).date() == _aware(old["created_at"]).date():
            payload = json.loads(row["payload"])
            if payload.get("operation") == "NEW" and payload.get("success") is False:
                rejected_requests.add(payload.get("identify"))
    requests = snapshot["tables"]["broker_requests"]
    old_requests = [row for row in requests if row["client_order_id"] == old_id and row["operation"] == "NEW"]
    current_requests = [row for row in requests if row["client_order_id"] == current_id and row["operation"] == "NEW"]
    if not old_requests or any(row["identify"] not in rejected_requests for row in old_requests) or len(current_requests) != 1:
        _fail("ORIGINAL_REQUEST_REJECTION_PROOF_MISSING")
    if not any(row["client_order_id"] == current_id and row["event_type"] == "ORDER_SEND_REQUESTED" for row in events):
        _fail("CURRENT_ENTRY_SEND_EVIDENCE_MISSING")
    candidates = [row for row in orders if row["symbol"] == current["symbol"] and _side(row) == "BUY" and _native_time(row).date() == _aware(current["created_at"]).date() and _integer(row.get("order_qty"), minimum=1) == _integer(current["quantity"], minimum=1) and abs(Decimal(str((_native_time(row) - _aware(current["created_at"])).total_seconds()))) <= TIME_BINDING_SECONDS]
    if len(candidates) != 1:
        _fail("UNIQUE_NATIVE_ENTRY_IDENTITY_UNPROVEN")
    broker_entry = candidates[0]
    broker_no = str(broker_entry["order_no"])
    broker_basket = str(broker_entry.get("basket_no", ""))
    if not broker_basket or not re.fullmatch(r"[A-Za-z0-9]{1,128}", broker_basket):
        _fail("BROKER_BASKET_BINDING_UNPROVEN")
    if _decimal(broker_entry["price"]) != _decimal(current["price"]) or _integer(broker_entry.get("ap_code")) != _integer(current["ap_code"]) or str(broker_entry.get("order_type")) != str(current["order_type"]):
        _fail("ENTRY_PRICE_OR_TYPE_IDENTITY_UNPROVEN")
    matching_local = [row for row in local_orders.values() if row["purpose"] == "ENTRY" and row["symbol"] == current["symbol"] and row["side"] == current["side"] and row["quantity"] == current["quantity"] and abs(Decimal(str((_native_time(broker_entry) - _aware(row["created_at"])).total_seconds()))) <= TIME_BINDING_SECONDS]
    if len(matching_local) != 1 or matching_local[0]["client_order_id"] != current_id:
        _fail("LOCAL_ENTRY_TIME_IDENTITY_AMBIGUOUS")
    expected_mapping = {"human_confirmed": True, "confirmation_source": "USER_CONFIRMED_INCIDENT", "misbound_entry_id": old_id, "current_entry_id": current_id, "broker_order_no": broker_no, "broker_basket_no": broker_basket, "misbound_fill_ids": sorted(row["fill_id"] for row in wrong)}
    if mapping != expected_mapping:
        _fail("EXPLICIT_INCIDENT_MAPPING_REQUIRED")
    if any(row["broker_order_no"] != broker_no for row in wrong) or old["broker_order_no"] != broker_no or current["broker_order_no"] not in (None, broker_no):
        _fail("MISBOUND_ORDER_NUMBER_PROOF_CONFLICT")
    fills_by_order = {}
    seen = {}
    for row in details:
        if _integer(row.get("rpt_type")) != 51:
            continue
        if _integer(row.get("order_status")) != 8 or str(row.get("seq_no", "")) in {"", "0"}:
            _fail("ACTUAL_FILL_PROOF_INVALID")
        key = (_native_time(row).date().isoformat(), row["order_no"], str(row["seq_no"]))
        if key in seen and seen[key] != row:
            _fail("DUPLICATE_FILL_PROOF_CONFLICT")
        if key in seen:
            continue
        seen[key] = row
        fills_by_order.setdefault(row["order_no"], []).append(row)
    buy_fills = fills_by_order.get(broker_no, [])
    if any(row["symbol"] != current["symbol"] or _side(row) != "BUY" or _native_time(row).date() != _aware(current["created_at"]).date() or _native_time(row) < _native_time(broker_entry) or row.get("basket_no") != broker_basket or _integer(row.get("ap_code")) != _integer(broker_entry.get("ap_code")) or str(row.get("order_type")) != str(current["order_type"]) for row in buy_fills):
        _fail("ENTRY_FILL_IDENTITY_CONFLICT")
    bought, average = _mean(buy_fills)
    if bought != current["quantity"] or _integer(broker_entry.get("ok_qty")) != bought or _terminal_or_partial(broker_entry) != "FILLED" or _decimal(broker_entry.get("avg_deal_price")) != _decimal(average):
        _fail("FULL_ENTRY_FILL_PROOF_REQUIRED")
    proof = {str(row["seq_no"]): row for row in buy_fills}
    if len(proof) != len(wrong) or len({str(row["seq_no"]) for row in wrong}) != len(wrong):
        _fail("EXACT_MISBOUND_FILL_SET_REQUIRED")
    for row in wrong:
        actual_fill = proof.get(str(row["seq_no"]))
        if actual_fill is None or _integer(row["quantity"], minimum=1) != _integer(actual_fill["order_qty"], minimum=1) or _decimal(row["price"]) != _decimal(actual_fill["price"]):
            _fail("MISBOUND_FILL_PAYLOAD_CONFLICT")
        # The adapter's compatibility replay recognizes only this legacy key.
        # Reparenting an old client-scoped key would allow a second receipt
        # under current_client:SeqNo. Do not silently widen migration scope.
        if row["fill_id"] != broker_no + ":" + str(row["seq_no"]):
            _fail("UNSUPPORTED_MISBOUND_FILL_ID_FORMAT")
    if _integer(old["filled_quantity"]) != bought or sum(row["quantity"] for row in wrong) != bought:
        _fail("MISBOUND_AGGREGATE_CONFLICT")
    manual_orders, manual_fills, blockers = [], [], []
    entry_stamp = _native_time(broker_entry)
    used_broker_orders = {broker_no}
    for row in orders:
        if row["order_no"] == broker_no:
            continue
        status = _terminal_or_partial(row)
        if row["symbol"] != current["symbol"] or _side(row) != "SELL" or _native_time(row) < entry_stamp:
            if status not in TERMINAL:
                _fail("UNRELATED_OPEN_BROKER_ORDER")
            continue
        if row["order_no"] in used_broker_orders:
            _fail("DUPLICATE_BROKER_ORDER_EVIDENCE")
        used_broker_orders.add(row["order_no"])
        if str(row.get("order_type")) != "0" or _integer(row.get("ap_code")) not in {0, 2, 4, 7}:
            _fail("MANUAL_CASH_SALE_TYPE_UNPROVEN")
        if row.get("price_type") not in {"LIMIT", "MARKET", "LIMIT_UP", "LIMIT_DOWN", "FLAT"} or row.get("time_in_force") not in {"ROD", "IOC", "FOK"}:
            _fail("MANUAL_PRICE_TYPE_OR_TIF_UNPROVEN")
        group = fills_by_order.get(row["order_no"], [])
        if any(item["symbol"] != current["symbol"] or _side(item) != "SELL" or _native_time(item).date() != _native_time(row).date() or _native_time(item) < _native_time(row) or str(item.get("basket_no", "")) not in {"", str(row.get("basket_no", ""))} or str(item.get("order_type")) != "0" or _integer(item.get("ap_code")) != _integer(row.get("ap_code")) for item in group):
            _fail("MANUAL_FILL_IDENTITY_CONFLICT")
        sold, mean = _mean(group)
        if sold != _integer(row.get("ok_qty")) or _decimal(row.get("avg_deal_price")) != _decimal(mean):
            _fail("MANUAL_FILL_AGGREGATE_CONFLICT")
        token = _digest({"account": account_hash, "date": _native_time(row).date().isoformat(), "order_no": row["order_no"]})
        identity = "manual-" + token[:32]
        if any(item["broker_order_no"] == row["order_no"] or item["client_order_id"] == identity for item in local_orders.values()):
            _fail("MANUAL_ORDER_ALREADY_PRESENT")
        stamp = _native_time(row).isoformat()
        new = {"client_order_id": identity, "intent_id": "INCIDENT-MANUAL-" + token, "basket_no": str(row.get("basket_no", "")) or "WS" + token[:30], "broker_order_no": row["order_no"], "symbol": current["symbol"], "side": "SELL", "quantity": _integer(row["order_qty"], minimum=1), "price": str(_decimal(row["price"])), "price_type": row["price_type"], "time_in_force": row["time_in_force"], "ap_code": _integer(row["ap_code"]), "order_type": "0", "purpose": "EXIT", "status": status, "filled_quantity": sold, "average_fill_price": mean, "last_error": "MANUAL_ACTUAL_FILL_IMPORTED_NO_BROKER_SUBMISSION", "created_at": stamp, "updated_at": stamp}
        new["fingerprint"] = _fingerprint(new)
        manual_orders.append(new)
        for item in group:
            manual_fills.append({"fill_id": identity + ":" + str(item["seq_no"]), "client_order_id": identity, "broker_order_no": row["order_no"], "seq_no": str(item["seq_no"]), "quantity": _integer(item["order_qty"], minimum=1), "price": str(_decimal(item["price"])), "filled_at": _native_time(item).isoformat()})
        if status not in TERMINAL:
            blockers.append({"code": "UNCONFIRMED_MANUAL_REMAINDER", "broker_order_no": row["order_no"], "remaining_quantity": new["quantity"] - sold, "status": status})
    if not manual_orders:
        _fail("MANUAL_SALE_PROOF_REQUIRED")
    if sum(row["quantity"] for row in manual_fills) > bought:
        _fail("MANUAL_SALE_EXCEEDS_PROVEN_ENTRY")
    proposed = json.loads(_json(snapshot))
    rows = {row["client_order_id"]: row for row in proposed["tables"]["live_orders"]}
    rejection_payload = json.loads(reject_events[-1]["payload"])
    rows[old_id].update(broker_order_no=None, status="REJECTED", filled_quantity=0, average_fill_price=None, last_error=str(rejection_payload.get("error") or "BROKER_PROVEN_ORIGINAL_REJECTION"))
    rows[current_id].update(broker_order_no=broker_no, basket_no=broker_basket, status="FILLED", filled_quantity=bought, average_fill_price=average, last_error=None)
    for row in proposed["tables"]["live_fills"]:
        if row["client_order_id"] == old_id:
            row["client_order_id"] = current_id
    proposed["tables"]["live_orders"].extend(manual_orders)
    proposed["tables"]["live_fills"].extend(manual_fills)
    for row in proposed["tables"]["broker_requests"]:
        if row["identify"] in rejected_requests:
            row["request_status"] = "REJECTED"
        if row["identify"] == current_requests[0]["identify"]:
            row["request_status"] = "CONFIRMED"
    controls = proposed["tables"]["live_control"]
    if len(controls) != 1 or _integer(controls[0].get("halted")) != 1:
        _fail("HALT_MUST_ALREADY_BE_ACTIVE")
    after_positions = _ledger_positions(proposed)
    if (any(key != f'{current["symbol"]}|0' for key in after_positions)
            or not 0 <= after_positions.get(f'{current["symbol"]}|0', 0) <= bought):
        _fail("REPAIRED_POSITION_OUT_OF_SCOPE")
    expected = dict(baseline)
    for key, quantity in after_positions.items():
        expected[key] = expected.get(key, 0) + quantity
        if expected[key] == 0:
            expected.pop(key)
    if expected != actual:
        _fail("BROKER_BASELINE_LEDGER_QUANTITY_MISMATCH")
    if after_positions:
        blockers.append({"code": "STRATEGY_POSITION_REMAINS", "positions": after_positions})
    blockers.append({"code": "HALT_PRESERVED"})
    plan = {"schema_version": SCHEMA_VERSION, "status": "REPAIRABLE_TRADING_BLOCKED", "apply_safe": True, "normal_start_ready": False, "evidence": evidence, "baseline": baseline, "misbound_entry_id": old_id, "current_entry_id": current_id, "before": snapshot, "before_digest": _digest(snapshot), "after": proposed, "after_digest": _digest(proposed), "broker_verified_as_of": evidence["queried_at"], "blockers": blockers, "after_positions": after_positions, "manual_orders": manual_orders, "manual_fills": manual_fills, "misbound_fill_ids": sorted(row["fill_id"] for row in wrong)}
    plan["plan_id"] = _digest({"before": plan["before_digest"], "evidence": evidence, "baseline": baseline, "old": old_id, "current": current_id})
    plan["plan_hash"] = _digest(plan)
    return plan


def build_plan(snapshotDB: str | Path, evidence: dict, misbound_entry_id: str, current_entry_id: str, baseline: dict) -> dict:
    """Read a stable SQLite snapshot and produce a private deterministic plan.

    Evidence rows use the existing normalizer names plus ``order_type``. All
    rows require broker ``trade_date_source`` and native ``order_time``. An
    explicit ``incident_mapping`` acknowledges the differing wire basket; this
    exception applies only to the listed historical fills, not future orders.
    """
    path = Path(snapshotDB).absolute()
    _safe_file(path)
    _no_sidecars(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            _fail("DATABASE_INTEGRITY_FAILURE")
        snapshot = _snapshot(db)
    return _prepare(snapshot, evidence, misbound_entry_id, current_entry_id, baseline)


def _backup(path: Path, directory: Path, plan_id: str) -> tuple[str, str]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        _fail("BACKUP_DIRECTORY_NOT_PRIVATE")
    target = directory / (plan_id + ".pre-repair.sqlite")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    try:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        _safe_file(target)
        if target.stat().st_mode & 0o077 or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            _fail("BACKUP_COLLISION")
    else:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    return str(target), digest


def _write_repair(db: sqlite3.Connection, plan: dict) -> None:
    """Write only the proven rows. Intents, checkpoints, control stay untouched."""
    after_orders = {row["client_order_id"]: row for row in plan["after"]["tables"]["live_orders"]}
    for identity in (plan["misbound_entry_id"], plan["current_entry_id"]):
        row = after_orders[identity]
        db.execute("UPDATE live_orders SET broker_order_no=?,basket_no=?,status=?,filled_quantity=?,average_fill_price=?,last_error=? WHERE client_order_id=?", (row["broker_order_no"], row["basket_no"], row["status"], row["filled_quantity"], row["average_fill_price"], row["last_error"], identity))
    for fill_id in plan["misbound_fill_ids"]:
        db.execute("UPDATE live_fills SET client_order_id=? WHERE fill_id=? AND client_order_id=?", (plan["current_entry_id"], fill_id, plan["misbound_entry_id"]))
    for row in plan["manual_orders"]:
        columns = tuple(row)
        db.execute(f'INSERT INTO live_orders ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})', tuple(row[key] for key in columns))
    for row in plan["manual_fills"]:
        columns = tuple(row)
        db.execute(f'INSERT INTO live_fills ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})', tuple(row[key] for key in columns))
    for row in plan["after"]["tables"]["broker_requests"]:
        if row["client_order_id"] in {plan["misbound_entry_id"], plan["current_entry_id"]}:
            db.execute("UPDATE broker_requests SET request_status=? WHERE identify=?", (row["request_status"], row["identify"]))


def apply_plan(DB: str | Path, plan: dict, backup_dir: str | Path) -> dict:
    """Apply one validated offline plan atomically; never contact a broker.

    Caller MUST hold runtime/account exclusivity and preserve private backups.
    An active manual remainder remains open in SQLite and in returned blockers.
    """
    plan = json.loads(_json(plan))
    recorded_hash = plan.pop("plan_hash", None)
    if recorded_hash != _digest(plan):
        _fail("PLAN_HASH_MISMATCH")
    plan["plan_hash"] = recorded_hash
    if plan.get("apply_safe") is not True or plan.get("normal_start_ready") is not False:
        _fail("UNSAFE_PLAN_FLAGS")
    def require_fresh_query():
        age = (datetime.now(TAIPEI) - _aware(plan["broker_verified_as_of"])).total_seconds()
        if age < -60 or age > APPLY_EVIDENCE_MAX_AGE_SECONDS:
            _fail("APPLY_BROKER_EVIDENCE_STALE")
    require_fresh_query()
    path = Path(DB).absolute()
    _safe_file(path)
    _no_sidecars(path)
    db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        current = _snapshot(db)
        markers = db.execute("SELECT payload FROM live_events WHERE event_type='INCIDENT_REPAIR_APPLIED'").fetchall()
        for marker in markers:
            value = json.loads(marker[0])
            if value.get("plan_id") == plan["plan_id"]:
                if value.get("plan_hash") != recorded_hash or _digest(current) != plan["after_digest"]:
                    _fail("APPLIED_PLAN_STATE_CHANGED")
                db.rollback()
                return {"status": "ALREADY_APPLIED", "plan_id": plan["plan_id"], "normal_start_ready": False, "blockers": plan["blockers"], "positions": plan["after_positions"], "broker_submission_calls": 0}
        if _digest(current) != plan["before_digest"]:
            _fail("DATABASE_PRECONDITION_CHANGED")
        rebuilt = _prepare(current, plan["evidence"], plan["misbound_entry_id"], plan["current_entry_id"], plan["baseline"])
        if rebuilt != plan:
            _fail("PLAN_DERIVATION_MISMATCH")
        backup_path, backup_hash = _backup(path, Path(backup_dir).absolute(), plan["plan_id"])
        stamp = datetime.now(timezone.utc).isoformat()
        base = {"plan_id": plan["plan_id"], "plan_hash": recorded_hash, "backup_path": backup_path, "backup_sha256": backup_hash, "broker_verified_as_of": plan["broker_verified_as_of"], "account_fingerprint": plan["evidence"]["account_fingerprint"]}
        db.execute("INSERT INTO live_events(event_type,client_order_id,payload,created_at) VALUES(?,NULL,?,?)", ("INCIDENT_REPAIR_BEFORE", _json({**base, "before_digest": plan["before_digest"], "misbound_entry_id": plan["misbound_entry_id"], "current_entry_id": plan["current_entry_id"], "misbound_fill_ids": plan["misbound_fill_ids"]}), stamp))
        _write_repair(db, plan)
        if db.execute("PRAGMA foreign_key_check").fetchall() or db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or _digest(_snapshot(db)) != plan["after_digest"]:
            _fail("REPAIR_POSTCONDITION_FAILED")
        db.execute("INSERT INTO live_events(event_type,client_order_id,payload,created_at) VALUES(?,NULL,?,?)", ("INCIDENT_REPAIR_APPLIED", _json({**base, "after_digest": plan["after_digest"], "normal_start_ready": False, "blockers": plan["blockers"], "positions": plan["after_positions"], "wire_basket_before": next(row["basket_no"] for row in plan["before"]["tables"]["live_orders"] if row["client_order_id"] == plan["current_entry_id"]), "wire_basket_after": next(row["basket_no"] for row in plan["after"]["tables"]["live_orders"] if row["client_order_id"] == plan["current_entry_id"])}), stamp))
        require_fresh_query()
        db.commit()
        return {"status": "APPLIED_TRADING_BLOCKED", "plan_id": plan["plan_id"], "backup_path": backup_path, "backup_sha256": backup_hash, "normal_start_ready": False, "blockers": plan["blockers"], "positions": plan["after_positions"], "broker_submission_calls": 0}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
