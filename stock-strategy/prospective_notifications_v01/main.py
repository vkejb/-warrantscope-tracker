from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
import secrets
from zoneinfo import ZoneInfo

from shadow_daily_runner.config import CFG as RUNNER_CFG
from stage_a_prospective_watchlist_v01.seal_store import latest_seal

from .chip_watch import prepare_chip_watch
from .notifier import chip_not_ready_message, chip_watch_message, entry_state_message, latest_status, load_entry_state, notify
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
    parser.add_argument("command", choices=("test", "status", "configure-keychain", "send-entry-state", "send-chip-watch", "attempt-chip-watch"))
    parser.add_argument("--date", help="YYYYMMDD; required for send-entry-state/send-chip-watch")
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
    elif args.command in {"send-entry-state", "send-chip-watch", "attempt-chip-watch"}:
        target_date = args.date
        if args.command == "attempt-chip-watch":
            target_date = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y%m%d")
        if not target_date or len(target_date) != 8 or not target_date.isdigit():
            parser.error(f"{args.command} requires --date YYYYMMDD")
        stage = latest_seal()
        if stage is None or stage["signal_date"] != target_date:
            if args.command == "attempt-chip-watch":
                result = {"status": "NO_CURRENT_DAY_STAGE_A_SEAL", "signal_date": target_date}
                print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
                return 0
            raise RuntimeError("requested date is not the latest verified Stage A seal")
        entry_state = load_entry_state(target_date, stage["seal_hash"])
        if args.command == "send-entry-state":
            credential_status = load_into_environment()
            delivery = notify(
                "STAGE_A_ENTRY_STATE", target_date, entry_state["seal_hash"],
                "SEALED_ENTRY_STATE", entry_state_message(target_date, entry_state),
            )
            result = {"credential_status": credential_status, "signal_date": target_date, "seal_hash": entry_state["seal_hash"], "delivery": delivery}
            code = 0 if delivery.get("TELEGRAM") in {"SUCCESS", "ALREADY_SENT"} else 3
        else:
            snapshot = prepare_chip_watch(target_date, stage, entry_state)
            if snapshot["status"] != "COMPLETE":
                result = snapshot
                local_time = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%H:%M")
                if args.command == "attempt-chip-watch" and local_time >= "22:15":
                    credential_status = load_into_environment()
                    delivery = notify(
                        "STAGE_A_CHIP_WATCH", target_date,
                        f"{entry_state['seal_hash']}:NOT_READY",
                        "CHIP_WATCH_NOT_READY",
                        chip_not_ready_message(target_date, snapshot.get("reason", "")),
                    )
                    result = {**result, "credential_status": credential_status, "delivery": delivery}
                code = 0 if args.command == "attempt-chip-watch" else 3
            else:
                credential_status = load_into_environment()
                delivery = notify(
                    "STAGE_A_CHIP_WATCH", target_date, snapshot["source_hash"],
                    "CHIP_WATCH_READY", chip_watch_message(target_date, snapshot["candidates"], snapshot["source_hash"]),
                )
                result = {"credential_status": credential_status, **snapshot, "delivery": delivery}
                code = 0 if delivery.get("TELEGRAM") in {"SUCCESS", "ALREADY_SENT"} else 3
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
