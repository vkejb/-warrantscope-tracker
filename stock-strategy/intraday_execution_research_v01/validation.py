from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from .storage import immutable_json, sha256_file


def seal_day(
    runtime: Path, trading_date: str, watchlist_hash: str, raw_manifest: dict,
    one_minute_path: Path, five_minute_path: Path, snapshots_path: Path,
) -> tuple[Path, dict]:
    if raw_manifest["watchlist_hash"] != watchlist_hash:
        raise RuntimeError("watchlist hash mismatch at daily seal")
    seal = {
        "schema_version": 1, "trading_date": trading_date, "state": "SEALED",
        "watchlist_hash": watchlist_hash,
        "raw_files": raw_manifest["raw_files"],
        "raw_tick_count": raw_manifest["tick_count"],
        "duplicate_count": raw_manifest["duplicate_count"],
        "out_of_order_count": raw_manifest["out_of_order_count"],
        "cumulative_volume_regressions": raw_manifest["cumulative_volume_regressions"],
        "gap_diagnostics": raw_manifest["gap_diagnostics"],
        "missing_symbols": raw_manifest["missing_symbols"],
        "interrupted_symbols": raw_manifest["interrupted_symbols"],
        "one_minute_bars": {"path": str(one_minute_path.relative_to(runtime)), "sha256": sha256_file(one_minute_path)},
        "five_minute_bars": {"path": str(five_minute_path.relative_to(runtime)), "sha256": sha256_file(five_minute_path)},
        "feature_snapshots": {"path": str(snapshots_path.relative_to(runtime)), "sha256": sha256_file(snapshots_path)},
        "seal_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "classification": "INFRASTRUCTURE_READY_NO_REAL_INTRADAY_EVIDENCE",
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
    }
    path = runtime / "intraday_seals" / f"{trading_date.replace('-', '')}.json"
    immutable_json(path, seal)
    return path, seal


def validate_seal(runtime: Path, seal_path: Path) -> dict:
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    errors = []
    for key in ("one_minute_bars", "five_minute_bars", "feature_snapshots"):
        item = seal[key]
        path = runtime / item["path"]
        if not path.exists() or sha256_file(path) != item["sha256"]:
            errors.append(f"{key}_HASH_MISMATCH")
    for item in seal["raw_files"]:
        path = runtime / item["path"]
        if not path.exists() or sha256_file(path) != item["sha256"]:
            errors.append(f"RAW_HASH_MISMATCH:{item['stock_code']}")
    return {"pass": not errors, "errors": errors, "state": seal.get("state")}
