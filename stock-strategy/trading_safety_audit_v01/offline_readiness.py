"""Sanitized, offline evidence inspection; never a LIVE authorization gate.

Only explicit paths are read. No runtime/SDK/store/notification imports, broker
connection, credentials, subprocess, lock manipulation or runtime writes occur.
SQLite is queried only from a private temporary copy of a stable main file with
no pending WAL/journal. WAL presence means UNVERIFIED, never "flat". Even a clean
local snapshot cannot establish actual broker inventory, cash or order state.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import stat
import tempfile
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
SEAL_FIELDS = ("schema_version", "signal_date", "setup", "mode", "stocks",
               "model_hash", "model_spec_hash", "config_hash", "input_hash",
               "eligible_stock_count")
ORDER_STATES = {"RESERVED", "SEND_PENDING", "ACKNOWLEDGED", "PARTIALLY_FILLED",
                "CANCEL_PENDING", "FILLED", "CANCELED", "EXPIRED", "REJECTED", "UNKNOWN"}
TERMINAL_STATES = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
HEARTBEAT_STATES = {"STARTING", "RUNNING", "STOPPING", "EMERGENCY_EXIT", "FORCE_FLAT_EXIT",
                    "EXIT_ONLY_RECOVERY", "STOPPED_CLEAN", "STOPPED_UNSAFE"}
TRADE_KINDS = {"0": 0, "9": 0, "3": 3, "4": 4, "5": 6, "6": 6}
SCHEMA = {
    "live_control": {"singleton", "halted", "reason", "next_identify", "updated_at"},
    "live_orders": {"client_order_id", "intent_id", "symbol", "side", "quantity", "order_type",
                    "status", "filled_quantity", "created_at", "updated_at"},
    "live_fills": {"fill_id", "client_order_id", "quantity", "price", "filled_at"},
}


class EvidenceError(Exception):
    """Code-only errors; never include file content or OS/SQLite exception text."""


def _result(code, **safe_values):
    return {"code": code, **safe_values}


def _read(path: Path, *, limit=4 * 1024 * 1024) -> bytes:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise EvidenceError("INPUT_UNSAFE_OR_TOO_LARGE")
        # Bound reading even if a concurrently growing file changes size.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise EvidenceError("INPUT_UNSAFE_OR_TOO_LARGE")
            content = handle.read(limit + 1)
        if len(content) > limit:
            raise EvidenceError("INPUT_UNSAFE_OR_TOO_LARGE")
        return content
    except FileNotFoundError:
        raise EvidenceError("INPUT_MISSING") from None
    except OSError:
        raise EvidenceError("INPUT_UNREADABLE") from None


def _reject_constant(_):
    raise ValueError()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _require_bounded_json_nesting(content: bytes, *, maximum=64):
    """Bound structural depth before JSON parsing, independent of Python limits.

    Brackets inside JSON strings, including escaped quotes/backslashes, are not
    structural. Syntax validation remains the JSON decoder's responsibility.
    """
    depth = 0
    in_string = escaped = False
    for character in content:
        if in_string:
            if escaped:
                escaped = False
            elif character == 92:  # backslash
                escaped = True
            elif character == 34:  # double quote
                in_string = False
        elif character == 34:
            in_string = True
        elif character in (91, 123):  # array/object open
            depth += 1
            if depth > maximum:
                raise EvidenceError("INPUT_MALFORMED")
        elif character in (93, 125):
            depth -= 1


def _json(path: Path):
    try:
        content = _read(path)
        _require_bounded_json_nesting(content)
        # Existing runtime/seal files use UTF-8. Do not let the JSON decoder's
        # implicit UTF-16/32 support bypass the byte-level structural bound.
        value = json.loads(content.decode("utf-8-sig"), parse_constant=_reject_constant,
                           object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise EvidenceError("INPUT_MALFORMED") from None


def _day(value) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{8}", value):
        raise ValueError()
    return date.fromisoformat(value if "-" in value else f"{value[:4]}-{value[4:6]}-{value[6:]}")


def _stamp(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError()
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError()
    return result


def inspect_calendar(path: Path, target: date):
    try:
        rows = list(csv.DictReader(io.StringIO(_read(path).decode("utf-8-sig"))))
        days = [_day(row["date"]) for row in rows]
        if not days or days != sorted(set(days)):
            raise ValueError()
        if target not in days:
            return _result("TARGET_NOT_IN_CALENDAR"), None
        index = days.index(target)
        if index == 0:
            return _result("PRIOR_SESSION_MISSING"), None
        previous = days[index - 1]
        return _result("CALENDAR_RELATION_VALID", expected_signal_date=previous.isoformat()), previous
    except EvidenceError as error:
        return _result("CALENDAR_" + str(error)), None
    except (KeyError, ValueError, UnicodeError, csv.Error):
        return _result("CALENDAR_MALFORMED"), None


def inspect_seal(directory: Path, previous: date | None):
    try:
        paths = sorted(directory.glob("*.json"))
        if not paths:
            return _result("SEAL_MISSING")
        value = _json(paths[-1])
        content = {key: value[key] for key in SEAL_FIELDS}
        digest = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        if digest != value.get("seal_hash"):
            return _result("SEAL_HASH_INVALID")
        stocks = value["stocks"]
        if not isinstance(stocks, list) or len(stocks) != 30:
            return _result("SEAL_COUNT_INVALID")
        identities = [row["stock_id"] for row in stocks]
        if any(not isinstance(symbol, str) or not re.fullmatch(r"[0-9A-Z]{4,6}", symbol)
               for symbol in identities) or len(set(identities)) != 30:
            return _result("SEAL_IDENTITIES_INVALID")
        if (value["schema_version"] != "1" or value["setup"] != "FROZEN_STAGE_A_TOP30"
                or value["mode"] != "SHADOW_ONLY"):
            return _result("SEAL_SCHEMA_INVALID")
        day = _day(value["signal_date"])
        if paths[-1].stem != day.strftime("%Y%m%d"):
            return _result("SEAL_FILENAME_DATE_INVALID")
        if previous is None:
            return _result("SEAL_CALENDAR_UNVERIFIED", seal_signal_date=day.isoformat())
        if day != previous:
            return _result("SEAL_PREVIOUS_SESSION_MISMATCH", seal_signal_date=day.isoformat())
        return _result("SEAL_LOCAL_HASH_AND_DATE_VALID", seal_signal_date=day.isoformat(), stock_count=30)
    except EvidenceError as error:
        return _result("SEAL_" + str(error))
    except (OSError, KeyError, ValueError, TypeError, RecursionError, OverflowError):
        return _result("SEAL_MALFORMED")


def inspect_baseline(runtime: Path, target: date, now: datetime):
    try:
        baseline = _json(runtime / "position_baseline.json")
        if any(not isinstance(key, str) or not re.fullmatch(r"[0-9A-Z]{4,6}\|[0346]", key)
               or not isinstance(quantity, int) or isinstance(quantity, bool)
               for key, quantity in baseline.items()):
            return _result("BASELINE_MALFORMED")
        meta = _json(runtime / "position_baseline.meta.json")
        captured = _stamp(meta["captured_at"])
        captured_day = _day(meta["trading_date"])
        if (type(meta.get("version")) is not int or meta["version"] != 1
                or not isinstance(meta.get("account_fingerprint"), str)
                or not re.fullmatch(r"[0-9a-f]{12}", meta["account_fingerprint"])
                or captured.astimezone(TAIPEI).date() != captured_day):
            return _result("BASELINE_PROVENANCE_MALFORMED")
        if captured > now:
            return _result("BASELINE_CAPTURE_FUTURE")
        if captured_day != target:
            return _result("BASELINE_TARGET_DATE_MISMATCH", baseline_date=captured_day.isoformat())
        return _result("BASELINE_LOCAL_PROVENANCE_VALID_BROKER_UNVERIFIED", baseline_date=captured_day.isoformat())
    except EvidenceError as error:
        return _result("BASELINE_" + str(error))
    except (KeyError, ValueError, TypeError, OverflowError):
        return _result("BASELINE_PROVENANCE_MALFORMED")


def inspect_heartbeat(runtime: Path, now: datetime, stale_seconds: float):
    try:
        value = _json(runtime / "heartbeat.json")
        stamp = _stamp(value["at"])
        age = (now - stamp).total_seconds()
        state = value.get("state")
        if state not in HEARTBEAT_STATES:
            return _result("HEARTBEAT_STATE_UNKNOWN")
        if type(value.get("pid")) is not int or not 0 < value["pid"] < 2**31:
            return _result("HEARTBEAT_PID_INVALID")
        if age < 0:
            return _result("HEARTBEAT_FUTURE", state=state)
        if age > stale_seconds:
            return _result("HEARTBEAT_STALE", state=state)
        if state in {"STOPPED_CLEAN", "STOPPED_UNSAFE"}:
            return _result("HEARTBEAT_STOPPED_METADATA", state=state)
        return _result("HEARTBEAT_FRESH_METADATA_ONLY", state=state,
                       process_and_lock_health="UNVERIFIED")
    except EvidenceError as error:
        return _result("HEARTBEAT_" + str(error))
    except (KeyError, ValueError, TypeError, OverflowError):
        return _result("HEARTBEAT_MALFORMED")


def inspect_markers(runtime: Path):
    result = {}
    for name in ("EMERGENCY_STOP", "STOP_REQUEST", "FORCE_FLAT_REQUEST"):
        # Contents can contain identifiers and user text; never read or echo.
        try:
            present = (runtime / name).lstat() is not None
        except FileNotFoundError:
            present = False
        except OSError:
            present = None
        result[name] = present
    return _result("PERSISTENT_MARKERS_PRESENT" if any(result.values()) else
                   "PERSISTENT_MARKERS_ABSENT" if all(v is False for v in result.values()) else
                   "PERSISTENT_MARKERS_UNVERIFIED", markers=result)


def _signature(path: Path):
    try:
        value = path.lstat()
        if not stat.S_ISREG(value.st_mode):
            raise EvidenceError("STORE_ARTIFACT_UNSAFE")
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    except FileNotFoundError:
        return None
    except OSError:
        raise EvidenceError("STORE_ARTIFACT_UNREADABLE") from None


def _private_store_summary(connection):
    for table, required in SCHEMA.items():
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not required <= columns:
            raise EvidenceError("STORE_SCHEMA_UNRECOGNIZED")
    control = connection.execute("SELECT singleton,halted FROM live_control").fetchall()
    if len(control) != 1 or control[0][0] != 1 or type(control[0][1]) is not int or control[0][1] not in (0, 1):
        raise EvidenceError("STORE_CONTROL_INVALID")
    orders = {}
    active = unknown = 0
    for row in connection.execute("SELECT client_order_id,symbol,side,quantity,order_type,status,filled_quantity FROM live_orders"):
        identifier, symbol, side, quantity, kind, state, filled = row
        if (not isinstance(identifier, str) or not identifier or identifier in orders
                or not isinstance(symbol, str) or not re.fullmatch(r"[0-9A-Z]{4,6}", symbol)
                or side not in {"BUY", "SELL"} or kind not in TRADE_KINDS
                or state not in ORDER_STATES or type(quantity) is not int or quantity <= 0
                or type(filled) is not int or not 0 <= filled <= quantity
                or (state == "FILLED" and filled != quantity)
                or (state == "PARTIALLY_FILLED" and not 0 < filled < quantity)):
            raise EvidenceError("STORE_ORDER_DATA_INVALID")
        orders[identifier] = (symbol, side, kind, filled)
        active += state not in TERMINAL_STATES
        unknown += state == "UNKNOWN"
    positions, filled_totals, fill_ids = {}, {}, set()
    for identifier, order_id, quantity, price, stamp in connection.execute(
            "SELECT fill_id,client_order_id,quantity,price,filled_at FROM live_fills"):
        if (not isinstance(identifier, str) or not identifier or identifier in fill_ids
                or order_id not in orders or type(quantity) is not int or quantity <= 0):
            raise EvidenceError("STORE_FILL_DATA_INVALID")
        try:
            price_value = Decimal(str(price))
            _stamp(stamp)
            if not price_value.is_finite() or price_value <= 0:
                raise ValueError()
        except (ValueError, InvalidOperation, TypeError):
            raise EvidenceError("STORE_FILL_DATA_INVALID") from None
        fill_ids.add(identifier)
        symbol, side, kind, _ = orders[order_id]
        key = (symbol, TRADE_KINDS[kind])
        positions[key] = positions.get(key, 0) + quantity * (1 if side == "BUY" else -1)
        filled_totals[order_id] = filled_totals.get(order_id, 0) + quantity
    if any(filled_totals.get(key, 0) != value[3] for key, value in orders.items()):
        raise EvidenceError("STORE_FILL_TOTAL_MISMATCH")
    exposure = any(positions.values())
    halted = bool(control[0][1])
    return _result("STORE_LOCAL_BLOCKERS_PRESENT" if halted or active or exposure else
                   "STORE_LOCAL_SNAPSHOT_NO_EXPOSURE_BROKER_UNVERIFIED",
                   halted=halted, open_order_count=active, unknown_order_count=unknown,
                   has_local_exposure=exposure, evidence="STABLE_MAIN_FILE_LOCAL_SNAPSHOT_ONLY")


def inspect_store(runtime: Path):
    database = runtime / "live-orders.sqlite"
    paths = [database, Path(str(database) + "-wal"), Path(str(database) + "-shm"), Path(str(database) + "-journal")]
    try:
        before = [_signature(path) for path in paths]
        if before[0] is None:
            return _result("STORE_MISSING")
        if before[1] is not None and before[1][2] > 0:
            return _result("STORE_WAL_PRESENT_UNVERIFIED", halted=None, open_order_count=None,
                           has_local_exposure=None)
        if before[3] is not None and before[3][2] > 0:
            return _result("STORE_JOURNAL_PRESENT_UNVERIFIED")
        content = _read(database, limit=64 * 1024 * 1024)
        # Never connect SQLite to the original: even mode=ro can create a SHM
        # sidecar. A private immutable copy cannot modify original runtime.
        # Explicit trusted OS temporary root: ambient TMPDIR/gettempdir may be
        # configured inside the very runtime that must never receive writes.
        temporary_root = Path("/tmp").resolve()
        runtime_root = runtime.resolve()
        if temporary_root == runtime_root or temporary_root.is_relative_to(runtime_root):
            raise EvidenceError("STORE_SAFE_TEMP_LOCATION_UNAVAILABLE")
        with tempfile.TemporaryDirectory(prefix="warrantscope-offline-", dir=temporary_root) as directory:
            copy = Path(directory) / "snapshot.sqlite"
            copy.write_bytes(content)
            connection = sqlite3.connect(copy.as_uri() + "?mode=ro&immutable=1", uri=True)
            try:
                if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise EvidenceError("STORE_CORRUPT")
                summary = _private_store_summary(connection)
            finally:
                connection.close()
        # Any observed writer/checkpoint/sidecar change invalidates this local
        # snapshot. This does not prove the absence of an unobserved broker or
        # controller change and never confers readiness/flat certification.
        reread = _read(database, limit=64 * 1024 * 1024)
        if before != [_signature(path) for path in paths] or hashlib.sha256(content).digest() != hashlib.sha256(reread).digest():
            return _result("STORE_CHANGED_DURING_INSPECTION_UNVERIFIED")
        return summary
    except EvidenceError as error:
        return _result(str(error) if str(error).startswith("STORE_") else "STORE_" + str(error))
    except sqlite3.Error:
        return _result("STORE_CORRUPT_OR_UNREADABLE")
    except (OSError, ValueError, TypeError, OverflowError, RuntimeError):
        return _result("STORE_INSPECTION_UNVERIFIED")


def inspect_plist(path: Path, repo: Path, runtime: Path):
    try:
        value = plistlib.loads(_read(path))
        args = value["ProgramArguments"]
        if (not isinstance(args, list) or not all(isinstance(item, str) for item in args)
                or "-m" not in args or "--runtime-dir" not in args
                or args.count("-m") != 1 or args.count("--runtime-dir") != 1
                or args[args.index("-m") + 1] != "yuanta_live_runtime_v01.trading_bot_service"
                or "serve" not in args):
            return _result("PLIST_COMMAND_UNRECOGNIZED")
        cwd = Path(value["WorkingDirectory"])
        configured_runtime = Path(args[args.index("--runtime-dir") + 1])
        if not cwd.is_absolute() or not configured_runtime.is_absolute():
            return _result("PLIST_PATHS_INVALID")
        matches_source = cwd.resolve() == (repo / "stock-strategy").resolve()
        matches_runtime = configured_runtime.resolve() == runtime.resolve()
        source_runtime_split = runtime.resolve().parent.parent != cwd.resolve()
        return _result("PLIST_CONFIGURATION_ONLY", source_matches_requested_repo=matches_source,
                       runtime_matches_requested_path=matches_runtime,
                       source_and_runtime_different_checkout=source_runtime_split,
                       service_running="UNVERIFIED", environment_and_credentials="NOT_INSPECTED")
    except EvidenceError as error:
        return _result("PLIST_" + str(error))
    except (KeyError, ValueError, TypeError, IndexError, OSError, plistlib.InvalidFileException,
            RecursionError, OverflowError, RuntimeError):
        return _result("PLIST_MALFORMED")


def build_report(*, repo_dir: Path, runtime_dir: Path, plist: Path, calendar: Path,
                 seal_dir: Path, trading_date: date, now: datetime | None = None,
                 stale_seconds: float = 15.0):
    current = datetime.now(timezone.utc) if now is None else now
    if (not isinstance(current, datetime) or current.tzinfo is None
            or not isinstance(trading_date, date) or isinstance(trading_date, datetime)
            or not math.isfinite(stale_seconds) or stale_seconds <= 0):
        raise ValueError("Invalid offline inspection time/configuration")
    calendar_result, previous = inspect_calendar(calendar, trading_date)
    return {
        "schema_version": 1, "scope": "OFFLINE_READ_ONLY_DIAGNOSTIC",
        "overall": "NOT_LIVE_CERTIFIED", "target_trading_date": trading_date.isoformat(),
        "calendar": calendar_result, "seal": inspect_seal(seal_dir, previous),
        "baseline": inspect_baseline(runtime_dir, trading_date, current),
        "heartbeat": inspect_heartbeat(runtime_dir, current, stale_seconds),
        "markers": inspect_markers(runtime_dir), "store": inspect_store(runtime_dir),
        "plist": inspect_plist(plist, repo_dir, runtime_dir),
        "not_verified": ["BROKER_LOGIN", "ACTUAL_POSITIONS_AND_OPEN_ORDERS", "AVAILABLE_FUNDS",
                         "QUOTE_CONNECTION", "PROCESS_AND_ACCOUNT_LOCK_OWNERSHIP", "NOTIFICATION_DELIVERY",
                         "VENDOR_SDK_CONTRACT", "DEPLOYMENT_AND_RUNNING_SOURCE_VERSION"],
        "actions_performed": ["READ_EXPLICIT_LOCAL_EVIDENCE_ONLY"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo-dir", "runtime-dir", "plist", "calendar", "seal-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--trading-date", required=True)
    args = parser.parse_args(argv)
    try:
        report = build_report(repo_dir=args.repo_dir, runtime_dir=args.runtime_dir, plist=args.plist,
                              calendar=args.calendar, seal_dir=args.seal_dir, trading_date=_day(args.trading_date))
    except (ValueError, TypeError, OSError):
        print(json.dumps(_result("OFFLINE_INPUT_CONFIGURATION_INVALID", overall="NOT_LIVE_CERTIFIED")))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    # Deliberately nonzero: this diagnostic cannot approve LIVE, even if every
    # local component looks consistent. It is not used by any production gate.
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
