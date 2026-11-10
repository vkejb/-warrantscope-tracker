"""Sanitised, mutation-free preflight for broker UAT evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import plistlib
import sqlite3
from typing import Any

from trading_safety_audit_v01.readonly_broker_probe import QUERIES
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file


EXPECTED_READONLY_QUERIES = {
    "GetRealReport", "GetRealReportMerge", "GetStoreSummary",
    "GetOrderTradeReport",
}
ORDER_MUTATION_METHODS = {
    "SendOrder", "SendOrderByBasket", "ChangeOrder", "CancelOrder",
    "DeleteOrder", "ReplaceOrder",
}


def _json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _runtime_from_arguments(arguments: list[str]) -> Path | None:
    try:
        index = arguments.index("--runtime-dir")
        return Path(arguments[index + 1]).expanduser().resolve()
    except (ValueError, IndexError):
        return None


def _sqlite_inventory(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "MISSING", "path": str(path)}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        counts = {}
        for table in tables:
            if not table.replace("_", "").isalnum():
                continue
            counts[table] = int(connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        return {
            "status": "READ_ONLY_OK" if integrity == "ok" else "INTEGRITY_FAILED",
            "path": str(path), "sha256": sha256_file(path),
            "integrity_check": integrity, "table_row_counts": counts,
        }
    finally:
        connection.close()


def build_preflight(*, launch_agent: Path) -> dict[str, Any]:
    raw = launch_agent.read_bytes()
    plist = plistlib.loads(raw)
    arguments = [str(item) for item in plist.get("ProgramArguments", [])]
    runtime_dir = _runtime_from_arguments(arguments)
    working_dir = Path(str(plist.get("WorkingDirectory", ""))).expanduser().resolve()
    if runtime_dir is None:
        runtime_dir = working_dir / "yuanta_live_runtime_v01" / "runtime"
    baseline = runtime_dir / "position_baseline.json"
    baseline_meta = runtime_dir / "position_baseline.meta.json"
    heartbeat = _json(runtime_dir / "heartbeat.json")
    probe_source = Path(__file__).with_name("readonly_broker_probe.py")
    source = probe_source.read_text(encoding="utf-8")
    mutations_present = sorted(name for name in ORDER_MUTATION_METHODS if name in source)
    query_allowlist_ok = set(QUERIES) == EXPECTED_READONLY_QUERIES
    broker_query_boundary = (
        "READ_ONLY_ALLOWLIST_VERIFIED"
        if query_allowlist_ok and not mutations_present
        else "READ_ONLY_BOUNDARY_FAILED"
    )
    binding = {
        "launch_agent": str(launch_agent.resolve()),
        "launch_agent_sha256": hashlib.sha256(raw).hexdigest(),
        "label": plist.get("Label"), "program_arguments": arguments,
        "working_directory": str(working_dir),
        "runtime_directory": str(runtime_dir),
        "baseline_path": str(baseline),
        "baseline_sha256": sha256_file(baseline) if baseline.is_file() else None,
        "baseline_meta_sha256": (
            sha256_file(baseline_meta) if baseline_meta.is_file() else None
        ),
    }
    status = (
        "READY_FOR_EXPLICIT_BROKER_READONLY_PROBE"
        if broker_query_boundary == "READ_ONLY_ALLOWLIST_VERIFIED"
        and baseline.is_file() and runtime_dir.is_dir()
        else "UAT_PREFLIGHT_INCOMPLETE"
    )
    report = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "classification": "UAT_READONLY_AND_MOCK_ONLY_NOT_LIVE_APPROVAL",
        "binding": binding,
        "runtime_heartbeat": heartbeat,
        "order_store": _sqlite_inventory(runtime_dir / "live-orders.sqlite"),
        "broker_query_boundary": broker_query_boundary,
        "readonly_queries": sorted(QUERIES),
        "mutation_method_names_present_in_probe": mutations_present,
        "broker_login_performed": False,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
        "unresolved": [
            "BROKER_BACKED_PARTIAL_FILL_NOT_TESTED_WITH_REAL_ORDER",
            "BROKER_BACKED_CANCEL_REPLACE_NOT_TESTED_WITH_REAL_ORDER",
            "BROKER_BACKED_FORCE_FLAT_NOT_TESTED_WITH_REAL_POSITION",
        ],
    }
    report["report_hash"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-agent", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = build_preflight(launch_agent=args.launch_agent)
    payload = canonical_bytes(report) + b"\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(payload)
    print(payload.decode("utf-8"), end="")
    return 0 if report["status"] == "READY_FOR_EXPLICIT_BROKER_READONLY_PROBE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
