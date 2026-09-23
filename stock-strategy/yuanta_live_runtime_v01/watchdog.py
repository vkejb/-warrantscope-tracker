"""Heartbeat writer and independent process watchdog."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from .notifications import RuntimeNotifier


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Heartbeat:
    def __init__(self, runtime_dir: Path):
        self.path = Path(runtime_dir) / "heartbeat.json"

    def beat(self, state: str, **details) -> None:
        payload = {"at": utc_now(), "pid": os.getpid(), "state": state, **details}
        tmp = self.path.with_suffix(".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, sort_keys=True, default=str) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def stopped(self, clean: bool, **details) -> None:
        self.beat("STOPPED_CLEAN" if clean else "STOPPED_UNSAFE", **details)


def check_once(runtime_dir: Path, *, stale_seconds: float = 15.0) -> bool:
    runtime_dir = Path(runtime_dir)
    path = runtime_dir / "heartbeat.json"
    notifier = RuntimeNotifier(runtime_dir)
    if not path.is_file():
        notifier.critical("WATCHDOG_HEARTBEAT_MISSING", "runtime heartbeat is missing")
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(str(payload["at"]).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
    except Exception as exc:
        notifier.critical("WATCHDOG_HEARTBEAT_INVALID", "runtime heartbeat is unreadable", error=str(exc))
        return False
    state = str(payload.get("state", ""))
    if state == "STOPPED_CLEAN":
        return True
    if state == "STOPPED_UNSAFE" or age > stale_seconds:
        notifier.critical(
            "WATCHDOG_RUNTIME_UNHEALTHY",
            "runtime exited abnormally or stopped updating its heartbeat",
            state=state,
            age_seconds=round(age, 3),
            pid=payload.get("pid"),
        )
        return False
    return True


def monitor(runtime_dir: Path, *, stale_seconds: float = 15.0, interval_seconds: float = 5.0) -> int:
    while True:
        if not check_once(runtime_dir, stale_seconds=stale_seconds):
            return 2
        time.sleep(interval_seconds)
