from __future__ import annotations

import argparse
from datetime import datetime, time
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from yuanta_intraday_shadow_v01.collector import (
    DEFAULT_RUNTIME_DIR as COLLECTOR_RUNTIME_DIR,
    canonical_bytes,
)

from .runner import DEFAULT_RUNTIME_DIR, _verify_published


TAIPEI = ZoneInfo("Asia/Taipei")


def _latest_source_preflight(collector_runtime_dir: Path) -> dict:
    manifests = sorted((collector_runtime_dir / "runs").glob("*/run_manifest.json"))
    if not manifests:
        return {"status": "NO_SOURCE_RUN", "paper_eligible_prerequisites": False}
    path = manifests[-1]
    manifest = json.loads(path.read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != manifest.get("manifest_hash"):
        raise RuntimeError("latest source run manifest hash mismatch")
    errors = int(manifest.get("event_counts", {}).get("callback_errors", 0))
    artifacts = manifest.get("artifacts", {})
    context_present = any(
        name in artifacts
        for name in ("market_context_ticks.jsonl", "market_context_ticks.jsonl.gz")
    ) and any(
        name in artifacts
        for name in ("market_context_books.jsonl", "market_context_books.jsonl.gz")
    )
    started = datetime.fromisoformat(str(manifest["started_at"]).replace("Z", "+00:00")).astimezone(TAIPEI)
    ended = datetime.fromisoformat(str(manifest["ended_at"]).replace("Z", "+00:00")).astimezone(TAIPEI)
    blockers = []
    if manifest.get("status") != "COMPLETE":
        blockers.append("SOURCE_NOT_COMPLETE")
    if errors:
        blockers.append("CALLBACK_ERRORS_PRESENT")
    if started.time() > time(9, 0, 30):
        blockers.append("LATE_START")
    if ended.time() < time(13, 30):
        blockers.append("EARLY_END")
    if not context_present:
        blockers.append("MARKET_CONTEXT_MISSING")
    return {
        "status": "PREREQUISITES_PASS" if not blockers else "PREREQUISITES_BLOCKED",
        "run_id": manifest["run_id"],
        "source_status": manifest.get("status"),
        "started_at_taipei": started.isoformat(),
        "ended_at_taipei": ended.isoformat(),
        "callback_errors": errors,
        "callback_error_types": manifest.get("callback_error_types", {}),
        "market_context_present": context_present,
        "blocking_reasons": blockers,
        "paper_eligible_prerequisites": not blockers,
        "note": "Final stream-coverage validation still occurs during paper publication.",
    }


def _comparison_status(runtime_dir: Path) -> dict:
    path = runtime_dir / "comparison" / "latest.json"
    if not path.is_file():
        return {"status": "NOT_BUILT", "verified_session_count": 0}
    report = json.loads(path.read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in report.items() if key != "report_hash"}
    if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != report.get("report_hash"):
        raise RuntimeError("paper comparison hash mismatch")
    return {
        "status": report["status"],
        "verified_session_count": report["verified_session_count"],
        "paired_comparisons": report["paired_comparisons"],
        "report_hash": report["report_hash"],
    }


def latest_status(
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    collector_runtime_dir: Path = COLLECTOR_RUNTIME_DIR,
) -> dict:
    source_preflight = _latest_source_preflight(collector_runtime_dir)
    manifests = sorted((runtime_dir / "days").glob("*/*/manifest.json"))
    if not manifests:
        return {
            "status": "NO_PAPER_DAY_YET",
            "comparison": _comparison_status(runtime_dir),
            "latest_source_preflight": source_preflight,
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
        }
    path = manifests[-1].parent
    manifest = _verify_published(path)
    return {
        "status": manifest["status"],
        "session_date": manifest["session_date"],
        "paper_run_id": manifest["paper_run_id"],
        "paper_trade_count": manifest["paper_trade_count"],
        "net_pnl": manifest["net_pnl"],
        "variant_results": manifest.get("variant_results", {}),
        "comparison": _comparison_status(runtime_dir),
        "latest_source_preflight": source_preflight,
        "evaluable_checkpoints": manifest["evaluable_checkpoints"],
        "decision_windows": manifest["decision_windows"],
        "manifest_hash": manifest["manifest_hash"],
        "actual_orders": manifest["actual_orders"],
        "actual_fills": manifest["actual_fills"],
        "broker_connections": manifest["broker_connections"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Show latest background paper day")
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    args = parser.parse_args()
    print(json.dumps(latest_status(args.runtime_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
