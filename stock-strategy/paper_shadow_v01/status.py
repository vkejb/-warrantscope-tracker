from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runner import DEFAULT_RUNTIME_DIR, _verify_published


def latest_status(runtime_dir: Path = DEFAULT_RUNTIME_DIR) -> dict:
    manifests = sorted((runtime_dir / "days").glob("*/*/manifest.json"))
    if not manifests:
        return {
            "status": "NO_PAPER_DAY_YET",
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
