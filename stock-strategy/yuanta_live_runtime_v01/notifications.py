from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import hashlib
import sys

from .trading_bot_notifier import AsyncTradingNotifier


class RuntimeNotifier:
    def __init__(self, runtime_dir: Path):
        self.runtime_dir = Path(runtime_dir)
        self.local_ledger = self.runtime_dir / "critical_notifications.jsonl"
        self._notifier = None
        try:
            self._notifier = AsyncTradingNotifier(self.runtime_dir)
        except Exception as exc:
            print(f"CRITICAL: durable notification outbox unavailable: {type(exc).__name__}", file=sys.stderr, flush=True)

    def critical(self, event: str, message: str, **details) -> dict[str, str]:
        row = {
            "at": datetime.now(timezone.utc).isoformat(),
            "severity": "CRITICAL",
            "event": event,
            "message": message,
            "details": details,
        }
        local_status = "SUCCESS"
        try:
            self.local_ledger.parent.mkdir(parents=True, exist_ok=True)
            with self.local_ledger.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception as exc:
            local_status = "FAILED"
            print(f"CRITICAL {event}: local alert ledger unavailable ({type(exc).__name__})", file=sys.stderr, flush=True)
        try:
            if self._notifier is None:
                self._notifier = AsyncTradingNotifier(self.runtime_dir)
            identifier = self._notifier.critical(
                event, message,
                key=f"critical:{row['at'][:16]}:{event}:{message}:" + hashlib.sha256(
                    json.dumps(details, sort_keys=True, default=str).encode()).hexdigest(),
            )
        except Exception as exc:
            print(f"CRITICAL {event}: outbox persistence failed ({type(exc).__name__}); delivery NOT confirmed", file=sys.stderr, flush=True)
            return {"LOCAL_LEDGER": local_status, "TRADING_BOT": "PERSISTENCE_FAILED_NOT_DELIVERED"}
        return {
            "LOCAL_LEDGER": local_status,
            "TRADING_BOT": "DURABLY_QUEUED_NOT_DELIVERED",
            "notification_id": identifier,
        }

    def close(self, timeout: float = 12.0) -> None:
        """Give queued critical alerts a bounded chance to leave the process."""
        if self._notifier is not None:
            self._notifier.close(timeout=timeout)
