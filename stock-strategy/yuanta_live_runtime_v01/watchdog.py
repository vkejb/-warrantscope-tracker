"""Heartbeat writer and independent process watchdog."""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import time

from .notifications import RuntimeNotifier

ACTIVE_STATES = {"STARTING", "RUNNING", "STOPPING", "EMERGENCY_EXIT", "FORCE_FLAT_EXIT", "EXIT_ONLY_RECOVERY"}


@dataclass(frozen=True)
class RuntimeHealth:
    healthy: bool
    reason: str
    pid_alive: bool = False
    lock_owned: bool = False
    age_seconds: float | None = None
    state: str = "UNKNOWN"
    controller_present: bool = False


def runtime_lock_owned(runtime_dir: Path, expected_pid: int) -> bool:
    """Read the owner identity and probe kernel flock, never remove a lock."""
    path = Path(runtime_dir) / "runtime.lock"
    try:
        with path.open("r+", encoding="utf-8") as handle:
            owner = json.load(handle)
            if not isinstance(owner, dict):
                return False
            if int(owner.get("pid", 0)) != expected_pid:
                return False
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return False
    except (OSError, ValueError, TypeError, OverflowError):
        return False


def runtime_health(runtime_dir: Path, *, now: datetime | None = None,
                   stale_seconds: float = 15.0, require_lock: bool = True) -> RuntimeHealth:
    try:
        payload = json.loads((Path(runtime_dir) / "heartbeat.json").read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(str(payload["at"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("naive heartbeat")
        age = ((now or datetime.now(timezone.utc)).astimezone(timezone.utc)
               - stamp.astimezone(timezone.utc)).total_seconds()
        state = str(payload.get("state", "UNKNOWN"))
        pid = int(payload["pid"])
    except (OSError, KeyError, ValueError, TypeError, OverflowError):
        return RuntimeHealth(False, "HEARTBEAT_INVALID_OR_MISSING")
    try:
        if pid <= 0:
            raise ValueError("invalid PID")
        os.kill(pid, 0)
    except (OSError, ValueError, OverflowError):
        return RuntimeHealth(False, "PID_NOT_ALIVE", age_seconds=age, state=state)
    local_owned = runtime_lock_owned(runtime_dir, pid)
    owned = local_owned
    account_path = payload.get("account_lock_path")
    account_instance = payload.get("account_lock_instance")
    if account_path:
        try:
            from .account_lock import account_lock_health
            owned = owned and account_lock_health(
                Path(account_path), expected_pid=pid,
                expected_instance=account_instance)
        except (OSError, ValueError, TypeError, RuntimeError, OverflowError):
            owned = False
    elif bool(payload.get("submit_live")):
        # A legacy heartbeat cannot prove which account its controller owns.
        owned = False
    if bool(payload.get("submit_live")) and not (
            isinstance(account_instance, str) and account_instance.strip()):
        # A path/PID alone cannot correlate this particular LIVE controller.
        owned = False
    if age < 0 or age > stale_seconds:
        return RuntimeHealth(False, "HEARTBEAT_FUTURE" if age < 0 else "HEARTBEAT_STALE",
                             pid_alive=True, lock_owned=owned, age_seconds=age,
                             state=state, controller_present=local_owned)
    if state not in ACTIVE_STATES:
        return RuntimeHealth(False, "RUNTIME_NOT_ACTIVE", pid_alive=True,
                             lock_owned=owned, age_seconds=age, state=state,
                             controller_present=local_owned)
    if require_lock and not owned:
        return RuntimeHealth(False, "LOCK_OWNERSHIP_UNPROVEN", pid_alive=True,
                             age_seconds=age, state=state, controller_present=local_owned)
    return RuntimeHealth(True, "HEALTHY", pid_alive=True, lock_owned=owned,
                         age_seconds=age, state=state, controller_present=local_owned)


def broker_flat_proof(runtime_dir: Path, *, since: datetime | None = None,
                      now: datetime | None = None) -> bool:
    """An absent marker/STOPPED_CLEAN alone is never broker-flat proof."""
    try:
        payload = json.loads((Path(runtime_dir) / "heartbeat.json").read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(str(payload["broker_flat_confirmed_at"]).replace("Z", "+00:00"))
        if (stamp.tzinfo is None or payload.get("state") != "STOPPED_CLEAN"
                or payload.get("broker_flat_confirmed") is not True):
            return False
        current = now or datetime.now(timezone.utc)
        reference = since or current
        if current.tzinfo is None or reference.tzinfo is None:
            return False
        if stamp.astimezone(timezone.utc) > current.astimezone(timezone.utc):
            return False
        return (stamp.astimezone(reference.tzinfo).date() == reference.date()
                and (since is None or stamp >= since))
    except (OSError, ValueError, TypeError, KeyError):
        return False


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
    notifier = RuntimeNotifier(runtime_dir)
    try:
        health = runtime_health(runtime_dir, stale_seconds=stale_seconds)
        if health.healthy or broker_flat_proof(runtime_dir):
            return True
        notifier.critical("WATCHDOG_RUNTIME_UNHEALTHY",
                          "runtime liveness or broker-flat proof is unavailable",
                          reason=health.reason, state=health.state,
                          age_seconds=health.age_seconds)
        return False
    finally:
        notifier.close(timeout=0.25)


def monitor(runtime_dir: Path, *, stale_seconds: float = 15.0, interval_seconds: float = 5.0) -> int:
    while True:
        if not check_once(runtime_dir, stale_seconds=stale_seconds):
            return 2
        time.sleep(interval_seconds)
