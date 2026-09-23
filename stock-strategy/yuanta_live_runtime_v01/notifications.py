"""Durable critical notifications with local fallback."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path


class RuntimeNotifier:
    def __init__(self, runtime_dir: Path):
        self.runtime_dir = Path(runtime_dir)
        self.local_ledger = self.runtime_dir / "critical_notifications.jsonl"

    def critical(self, event: str, message: str, **details) -> dict[str, str]:
        row = {
            "at": datetime.now(timezone.utc).isoformat(),
            "severity": "CRITICAL",
            "event": event,
            "message": message,
            "details": details,
        }
        self.local_ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.local_ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        results = {"LOCAL_LEDGER": "SUCCESS"}
        try:
            from prospective_notifications_v01.keychain import load_into_environment
            from prospective_notifications_v01.notifier import notify

            load_into_environment()
            key = hashlib.sha256(
                f"{event}|{message}|{json.dumps(details, sort_keys=True, default=str)}".encode("utf-8")
            ).hexdigest()
            results.update(
                notify(
                    "YUANTA_LIVE_RUNTIME_V01",
                    datetime.now().strftime("%Y%m%d"),
                    key,
                    event,
                    f"[CRITICAL] {event}\n{message}",
                    ledger=self.runtime_dir / "notifications" / "notification_ledger.jsonl",
                )
            )
        except Exception as exc:
            results["EXTERNAL"] = f"FAILED:{type(exc).__name__}"
        return results
