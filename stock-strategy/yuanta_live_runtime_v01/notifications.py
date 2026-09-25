from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .trading_bot_notifier import send_critical_async


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
            handle.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

        send_critical_async(event, message)
        return {
            "LOCAL_LEDGER": "SUCCESS",
            "TRADING_BOT": "QUEUED",
        }
