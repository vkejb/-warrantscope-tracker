from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any


CONFIG_FILENAME = "live_data_sync_targets.json"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_immutable(path: Path, payload: bytes) -> str:
    if path.is_file():
        existing = path.read_bytes()
        if existing != payload:
            raise RuntimeError(f"refusing to replace different immutable file: {path}")
        return "ALREADY_SYNCED"
    _atomic_write(path, payload)
    return "SYNCED"


def _validated_seal(path: Path, target_date: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("signal_date") != target_date:
        raise RuntimeError("Stage A seal date differs from requested sync date")
    content = {
        key: value[key]
        for key in (
            "schema_version",
            "signal_date",
            "setup",
            "mode",
            "stocks",
            "model_hash",
            "model_spec_hash",
            "config_hash",
            "input_hash",
            "eligible_stock_count",
        )
    }
    if _sha256(_canonical_bytes(content)) != value.get("seal_hash"):
        raise RuntimeError("Stage A seal hash validation failed")
    stocks = value.get("stocks", [])
    if len(stocks) != 30 or len({str(row["stock_id"]) for row in stocks}) != 30:
        raise RuntimeError("Stage A seal must contain 30 unique stocks")
    return value


def _copy_target_sources(
    audit: dict[str, Any],
    *,
    source_root: Path,
    destination_root: Path,
    target_date: str,
) -> list[dict[str, str]]:
    copied: list[dict[str, str]] = []
    markets: set[str] = set()
    for archive in audit.get("archives", []):
        for source in archive.get("sources", []):
            if str(source.get("response_date")) != target_date:
                continue
            name = str(source.get("source", ""))
            if name == f"twse_eod_{target_date}":
                markets.add("TWSE")
            elif name == f"tpex_eod_{target_date}":
                markets.add("TPEX")
            else:
                continue
            source_path = Path(str(source["path"])).resolve()
            try:
                relative = source_path.relative_to(source_root)
            except ValueError as exc:
                raise RuntimeError("official source path escapes authoritative root") from exc
            payload = source_path.read_bytes()
            expected = str(source.get("sha256", ""))
            if _sha256(payload) != expected or source_path.stem != expected:
                raise RuntimeError(f"official source hash mismatch: {source_path.name}")
            destination = destination_root / relative
            state = _write_immutable(destination, payload)
            source["path"] = str(destination)
            copied.append(
                {
                    "source": name,
                    "path": str(destination),
                    "sha256": expected,
                    "status": state,
                }
            )
    if markets != {"TWSE", "TPEX"}:
        raise RuntimeError("official TWSE/TPEx target-day sources are incomplete")
    return copied


def sync_sealed_day(
    source_strategy_root: Path,
    destination_strategy_root: Path,
    target_date: str,
) -> dict[str, Any]:
    if len(target_date) != 8 or not target_date.isdigit():
        raise ValueError("target_date must be YYYYMMDD")
    source_root = Path(source_strategy_root).resolve()
    destination_root = Path(destination_strategy_root).resolve()
    if source_root == destination_root:
        return {
            "status": "SOURCE_IS_DESTINATION",
            "target_date": target_date,
            "destination": str(destination_root),
        }

    seal_relative = Path("stage_a_prospective_watchlist_v01/runtime/seals") / f"{target_date}.json"
    audit_relative = Path("shadow_daily_runner/runtime/audit") / f"official_eod_through_{target_date}.json"
    calendar_relatives = (
        Path("shadow_daily_runner/runtime/trading_calendar.csv"),
        Path("shadow_daily_runner/runtime/trading_calendar.metadata.json"),
    )

    source_seal = source_root / seal_relative
    source_audit = source_root / audit_relative
    if not source_seal.is_file() or not source_audit.is_file():
        raise RuntimeError("authoritative Stage A seal or official EOD audit is missing")
    seal = _validated_seal(source_seal, target_date)
    audit = json.loads(source_audit.read_text(encoding="utf-8"))
    copied_sources = _copy_target_sources(
        audit,
        source_root=source_root,
        destination_root=destination_root,
        target_date=target_date,
    )

    seal_status = _write_immutable(destination_root / seal_relative, source_seal.read_bytes())
    rendered_audit = (json.dumps(audit, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    audit_status = _write_immutable(destination_root / audit_relative, rendered_audit)
    calendar_status: dict[str, str] = {}
    for relative in calendar_relatives:
        source = source_root / relative
        if not source.is_file():
            raise RuntimeError(f"authoritative calendar artifact is missing: {relative.name}")
        _atomic_write(destination_root / relative, source.read_bytes())
        calendar_status[relative.name] = "SYNCED"

    return {
        "status": "SYNCED",
        "target_date": target_date,
        "destination": str(destination_root),
        "seal_hash": seal["seal_hash"],
        "seal": seal_status,
        "audit": audit_status,
        "calendar": calendar_status,
        "official_sources": copied_sources,
    }


def configured_targets(runtime_dir: Path) -> list[Path]:
    path = Path(runtime_dir) / CONFIG_FILENAME
    if not path.is_file():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != "1" or not isinstance(value.get("targets"), list):
        raise RuntimeError("live data sync configuration is malformed")
    targets = [Path(str(item)).expanduser().resolve() for item in value["targets"]]
    if len(targets) != len(set(targets)):
        raise RuntimeError("live data sync configuration contains duplicate targets")
    return targets


def sync_configured_live_data(
    source_strategy_root: Path,
    runtime_dir: Path,
    target_date: str,
) -> dict[str, Any]:
    targets = configured_targets(runtime_dir)
    if not targets:
        return {"status": "DISABLED", "target_date": target_date, "targets": []}
    results = [
        sync_sealed_day(source_strategy_root, destination, target_date)
        for destination in targets
    ]
    return {"status": "SYNCED", "target_date": target_date, "targets": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize verified live-readiness data")
    parser.add_argument("--source-strategy-root", type=Path, required=True)
    parser.add_argument("--destination-strategy-root", type=Path, required=True)
    parser.add_argument("--target-date", required=True)
    args = parser.parse_args(argv)
    result = sync_sealed_day(
        args.source_strategy_root,
        args.destination_strategy_root,
        args.target_date,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
