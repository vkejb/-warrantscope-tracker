from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from yuanta_intraday_shadow_v01.collector import canonical_bytes

from .runner import DEFAULT_RUNTIME_DIR, _verify_published


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


def latest_status(runtime_dir: Path = DEFAULT_RUNTIME_DIR) -> dict:
    manifests = sorted((runtime_dir / "days").glob("*/*/manifest.json"))
    if not manifests:
        return {
            "status": "NO_PAPER_DAY_YET",
            "comparison": _comparison_status(runtime_dir),
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
