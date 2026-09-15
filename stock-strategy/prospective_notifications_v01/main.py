from __future__ import annotations

import argparse
import csv
import json
import secrets

from shadow_daily_runner.config import CFG as RUNNER_CFG
from stage_a_prospective_watchlist_v01.seal_store import latest_seal

from .notifier import latest_status, notify
from .keychain import configure_interactive, load_into_environment


def _scan_status(recent_notifications: list[dict] | None = None) -> dict:
    path = RUNNER_CFG.shadow_store_dir / "prospective_scan_log.csv"
    if not path.is_file():
        return {"last_sealed_date": None, "raw_n_count": None, "compact_count": None}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("scan_status") == "COMPLETE"]
    if not rows:
        return {"last_sealed_date": None, "raw_n_count": None, "compact_count": None}
    row = rows[-1]
    legacy_path = RUNNER_CFG.runtime_dir / "notifications" / "notification_state.json"
    legacy = json.loads(legacy_path.read_text(encoding="utf-8")) if legacy_path.is_file() else {}
    delivery = next((item for item in reversed(legacy.get("notifications", [])) if item.get("signal_date") == row["signal_date"] and str(item.get("notification_type", "")).startswith("SEALED")), None)
    combined = next((item for item in reversed(recent_notifications or []) if item.get("module") == "WARRANTSCOPE_DAILY" and item.get("signal_date") == row["signal_date"]), None)
    notification_status = combined.get("status") if combined else ("SUCCESS_MACOS" if delivery and delivery.get("success") else "UNKNOWN_OR_NOT_SENT")
    return {"last_sealed_date": row["signal_date"], "raw_n_count": int(row["raw_n_retest_count"]), "compact_count": int(row["compact_count"]), "seal_hash": row["record_hash"], "notification_status": notification_status}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post-seal notification test/status; never creates a signal")
    parser.add_argument("command", choices=("test", "status", "configure-keychain"))
    args = parser.parse_args(argv)
    if args.command == "configure-keychain":
        result = configure_interactive()
        code = 0 if result.get("status") == "KEYCHAIN_CONFIGURED" else 3
    elif args.command == "test":
        credential_status = load_into_environment()
        delivery = notify("NOTIFICATION_TEST", "00000000", secrets.token_hex(8), "TEST", "WarrantScope notification test")
        result = {"credential_status": credential_status, "test": delivery}
        telegram = delivery.get("TELEGRAM")
        code = 0 if (telegram == "SUCCESS" or (telegram == "NOT_CONFIGURED" and delivery.get("MACOS_LOCAL_NOTIFICATION") == "SUCCESS")) else 3
    else:
        stage = latest_seal()
        notifications = latest_status()
        stage_delivery = next((item for item in reversed(notifications.get("recent_records", [])) if item.get("module") == "WARRANTSCOPE_DAILY" and stage and item.get("signal_date") == stage["signal_date"]), None)
        result = {
            "n_compact": _scan_status(notifications.get("recent_records", [])),
            "stage_a": {"last_sealed_date": stage["signal_date"], "count": len(stage["stocks"]), "top5": stage["stocks"][:5], "seal_hash": stage["seal_hash"], "notification_status": stage_delivery.get("status") if stage_delivery else "UNKNOWN_OR_NOT_SENT"} if stage else {"last_sealed_date": None, "count": 0, "status": "NO_EXISTING_PROSPECTIVE_STAGE_A_RECORDS"},
            "notifications": notifications,
            "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
        }
        code = 0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
