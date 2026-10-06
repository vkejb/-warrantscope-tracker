"""Explicit read-only Yuanta evidence capture for an offline incident review.

This module never constructs an execution adapter or a live order store. Its
four allowlisted API calls only query broker reports and inventory. Output is
private evidence, never a production readiness or order authorization signal.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

from yuanta_broker_execution_v01.adapter import (
    _normalise_merge_report,
    _normalise_order_trade_report,
    _normalise_positions,
    _normalise_real_report,
    _required_collection,
    _validate_snapshot_order,
)
from yuanta_broker_execution_v01.sdk import load_api_types
from yuanta_intraday_shadow_v01.yuanta_keychain import load_credentials
from yuanta_live_runtime_v01.account_lock import acquire_account_lock, release_account_lock
from yuanta_live_runtime_v01.main import (
    DEFAULT_VENDOR_DIR,
    _Session,
    _acquire_runtime_instance_lock,
    _release_runtime_instance_lock,
)

QUERIES = ("GetRealReport", "GetRealReportMerge", "GetStoreSummary", "GetOrderTradeReport")


def _private_rows(rows: object) -> list[dict]:
    """Remove raw account identity after callback-level validation."""
    if not isinstance(rows, list):
        raise RuntimeError("broker evidence rows are not a list")
    return [
        {key: value for key, value in dict(row).items() if key != "account"}
        for row in rows
    ]


def _normalise(name: str, value: object, account: str) -> object:
    if name == "GetRealReport":
        rows = _required_collection(value, "RealReportList")
        for row in rows:
            _validate_snapshot_order(row, merge=False, account=account)
        return [_normalise_real_report(row) for row in rows]
    if name == "GetRealReportMerge":
        rows = _required_collection(value, "RealReportMergeList")
        for row in rows:
            _validate_snapshot_order(row, merge=True, account=account)
        return [_normalise_merge_report(row) for row in rows]
    if name == "GetStoreSummary":
        return _normalise_positions(value)
    if name == "GetOrderTradeReport":
        return _normalise_order_trade_report(value, account=account)
    raise ValueError("query not allowlisted")


def _query(session: _Session, language: object, *, timeout: float) -> dict:
    """Capture one complete callback per explicitly dispatched query."""
    assert session.api is not None
    result: dict[str, object] = {}
    errors: dict[str, str] = {}
    events = {name: threading.Event() for name in QUERIES}
    active = {"name": ""}

    def on_response(mark, _index, response_name, _handle, value):
        name = str(response_name)
        if name not in events or name != active["name"]:
            return
        try:
            if int(mark) != 1:
                raise RuntimeError("broker query callback failed")
            result[name] = _normalise(name, value, session.account)
        except Exception as exc:
            errors[name] = type(exc).__name__ + ": " + str(exc)
        finally:
            events[name].set()

    session.api.OnResponse += on_response
    try:
        for name in QUERIES:
            active["name"] = name
            method = getattr(session.api, name)
            if name == "GetOrderTradeReport":
                accepted = method(False, session.account, language)
            else:
                accepted = method(session.account, language)
            if accepted is False:
                raise RuntimeError(name + " request rejected")
            if not events[name].wait(timeout):
                raise TimeoutError(name + " callback timed out")
            if name in errors:
                raise RuntimeError(name + ": " + errors[name])
            active["name"] = ""
        return result
    finally:
        session.api.OnResponse -= on_response


def _save_private(payload: dict) -> Path:
    date_token = datetime.now(timezone.utc).strftime("%Y%m%d")
    fd, name = tempfile.mkstemp(prefix=f"yuanta-readonly-{date_token}-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        os.unlink(name)
        raise
    return Path(name)


def capture(*, runtime_dir: Path, vendor_dir: Path, timeout: float = 20.0) -> dict:
    runtime_lock = _acquire_runtime_instance_lock(runtime_dir)
    account_lock = None
    session = None
    credentials = None
    try:
        credentials = load_credentials()
        api_types = load_api_types(vendor_dir)
        session = _Session(api_types=api_types, environment="PROD", credentials=credentials,
                           engine=None, logger=lambda *_args, **_kwargs: None)
        account_lock = acquire_account_lock(session.account, "PROD", runtime_dir=runtime_dir)
        session.connect()
        data = _query(session, api_types["Language"].UTF8, timeout=timeout)
        payload = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "source": "YUANTA_PROD_READONLY",
            "account_fingerprint": hashlib.sha256(session.account.encode()).hexdigest()[:12],
            "account_rows_validated": True,
            "details": _private_rows(data["GetRealReport"]),
            "orders": _private_rows(data["GetRealReportMerge"]),
            "positions": data["GetStoreSummary"],
            "history": data["GetOrderTradeReport"],
        }
        path = _save_private(payload)
        return {"evidence_path": str(path), "captured_at": payload["captured_at"],
                "current_order_count": len(payload["orders"]),
                "position_bucket_count": len(payload["positions"]),
                "history_order_count": len(payload["history"]["orders"]),
                "history_trade_count": len(payload["history"]["trades"])}
    finally:
        if session is not None:
            session.close()
        if credentials is not None:
            credentials.update({"pfx_password": "", "trading_password": ""})
        release_account_lock(account_lock)
        _release_runtime_instance_lock(runtime_lock)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--vendor-dir", type=Path, default=DEFAULT_VENDOR_DIR)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    print(json.dumps(capture(runtime_dir=args.runtime_dir.resolve(),
                             vendor_dir=args.vendor_dir.resolve(), timeout=args.timeout),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
