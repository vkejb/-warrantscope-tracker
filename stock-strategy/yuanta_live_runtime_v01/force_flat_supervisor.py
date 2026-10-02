"""Independent 13:20/13:23 force-flat supervisor.

This process never creates entry intents.  It writes a durable force-flat
request at 13:20 and, when the primary runtime is absent, starts the same
guarded runtime in exit-only recovery mode.  The runtime removes the request
only after fresh broker reconciliation proves positions equal the frozen daily
baseline and no broker/local orders remain.
"""
from __future__ import annotations

import argparse
from datetime import datetime, time as time_cls
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from yuanta_broker_execution_v01 import LiveTradingGate

from .notifications import RuntimeNotifier


TAIPEI = ZoneInfo("Asia/Taipei")
MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_RUNTIME_DIR = MODULE_DIR / "runtime"
REQUEST = "FORCE_FLAT_REQUEST"
META = "position_baseline.meta.json"
LEDGER = "force_flat_supervisor.jsonl"
SCHEDULE_GATE = "ENABLE_SCHEDULED_FORCE_FLAT"
TRIGGER_START = time_cls(13, 20)
TRIGGER_END = time_cls(13, 29, 30)
MARKET_CUTOFF = time_cls(13, 29, 50)


def _append(path: Path, event: str, **details: Any) -> None:
    row = {
        "at": datetime.now(TAIPEI).isoformat(),
        "event": event,
        **details,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _baseline_ready(runtime_dir: Path, now: datetime) -> bool:
    baseline = runtime_dir / "position_baseline.json"
    meta = _read_json(runtime_dir / META)
    return bool(
        baseline.is_file()
        and meta is not None
        and str(meta.get("trading_date", "")) == now.astimezone(TAIPEI).date().isoformat()
    )


def _runtime_active(runtime_dir: Path, now: datetime, *, stale_seconds: float = 15.0) -> bool:
    heartbeat = _read_json(runtime_dir / "heartbeat.json")
    if heartbeat is None:
        return False
    if str(heartbeat.get("state", "")) not in {
        "STARTING", "RUNNING", "STOPPING", "EMERGENCY_EXIT", "FORCE_FLAT_EXIT",
    }:
        return False
    try:
        stamp = datetime.fromisoformat(str(heartbeat["at"]).replace("Z", "+00:00"))
        age = (now.astimezone(TAIPEI) - stamp.astimezone(TAIPEI)).total_seconds()
        pid = int(heartbeat["pid"])
        if age < 0 or age > stale_seconds or pid <= 0:
            return False
        os.kill(pid, 0)
    except (OSError, ValueError, TypeError, KeyError):
        return False
    return True


def _write_request(runtime_dir: Path, now: datetime) -> Path:
    request = runtime_dir / REQUEST
    runtime_dir.mkdir(parents=True, exist_ok=True)
    tmp = request.with_suffix(".tmp")
    tmp.write_text(
        f"{now.astimezone(TAIPEI).isoformat()} SCHEDULED_FORCE_FLAT_1320\n",
        encoding="utf-8",
    )
    os.replace(tmp, request)
    return request


def _within_trigger_window(now: datetime) -> bool:
    local_time = now.astimezone(TAIPEI).time().replace(tzinfo=None)
    return TRIGGER_START <= local_time < TRIGGER_END


def _seconds_until_market_cutoff(now: datetime) -> float:
    local = now.astimezone(TAIPEI)
    cutoff = datetime.combine(local.date(), MARKET_CUTOFF, tzinfo=TAIPEI)
    return max(0.0, (cutoff - local).total_seconds())


def _launch_exit_only(runtime_dir: Path) -> subprocess.Popen:
    if os.environ.get(SCHEDULE_GATE, "").strip().upper() != "YES":
        raise RuntimeError("scheduled force-flat gate is not enabled")
    command = [
        sys.executable,
        "-m",
        "yuanta_live_runtime_v01.main",
        "start-prod",
        "--live",
        "--recover-force-flat",
        "--runtime-dir",
        str(runtime_dir),
        "--baseline",
        str(runtime_dir / "position_baseline.json"),
    ]
    child_env = os.environ.copy()
    child_env["EXECUTION_MODE"] = "LIVE"
    child_env["ENABLE_LIVE_TRADING"] = "YES"
    return subprocess.Popen(
        command,
        cwd=str(MODULE_DIR.parent),
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def trigger_once(
    runtime_dir: Path,
    *,
    now: datetime | None = None,
    wait_seconds: float = 600.0,
    poll_seconds: float = 1.0,
    launch_retry_seconds: float = 30.0,
    max_launch_attempts: int = 3,
    launcher: Callable[[Path], Any] = _launch_exit_only,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    runtime_dir = Path(runtime_dir).resolve()
    current = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    ledger = runtime_dir / LEDGER
    notifier = RuntimeNotifier(runtime_dir)
    try:
        if not _baseline_ready(runtime_dir, current):
            _append(ledger, "FORCE_FLAT_BLOCKED", reason="DAILY_BASELINE_NOT_READY")
            notifier.critical(
                "FORCE_FLAT_BASELINE_NOT_READY",
                "13:20 force-flat refused because today's broker baseline is unavailable",
            )
            return False

        request = _write_request(runtime_dir, current)
        _append(ledger, "FORCE_FLAT_REQUESTED", request=str(request))
        launch_attempts = 0
        last_launch_at: float | None = None
        effective_wait = min(
            max(0.0, wait_seconds),
            _seconds_until_market_cutoff(current),
        )
        deadline = time.monotonic() + effective_wait

        while time.monotonic() <= deadline:
            if not request.exists():
                heartbeat = _read_json(runtime_dir / "heartbeat.json") or {}
                if str(heartbeat.get("state", "")) == "STOPPED_CLEAN":
                    _append(ledger, "FORCE_FLAT_CONFIRMED_BASELINE_ONLY")
                    return True

            fresh_now = datetime.now(TAIPEI)
            monotonic_now = time.monotonic()
            retry_due = (
                last_launch_at is None
                or monotonic_now - last_launch_at >= max(1.0, launch_retry_seconds)
            )
            if (
                not _runtime_active(runtime_dir, fresh_now)
                and retry_due
                and launch_attempts < max(1, max_launch_attempts)
            ):
                launch_attempts += 1
                last_launch_at = monotonic_now
                try:
                    launcher(runtime_dir)
                    _append(
                        ledger,
                        "FORCE_FLAT_EXIT_ONLY_RUNTIME_LAUNCHED",
                        attempt=launch_attempts,
                    )
                except Exception as exc:
                    _append(
                        ledger,
                        "FORCE_FLAT_EXIT_ONLY_LAUNCH_FAILED",
                        attempt=launch_attempts,
                        error_type=type(exc).__name__,
                    )

            sleeper(max(0.05, poll_seconds))

        _append(ledger, "FORCE_FLAT_UNCONFIRMED", request_present=request.exists())
        notifier.critical(
            "FORCE_FLAT_UNCONFIRMED",
            "13:20/13:23 force-flat did not obtain broker-confirmed baseline-only inventory",
        )
        return False
    finally:
        notifier.close(timeout=5.0)


def scheduler_loop(runtime_dir: Path, *, interval_seconds: float = 1.0) -> int:
    """Run the time scheduler; broker submission remains gated in its child."""
    if os.environ.get(SCHEDULE_GATE, "").strip().upper() != "YES":
        raise RuntimeError("scheduled force-flat gate is not enabled")
    runtime_dir = Path(runtime_dir).resolve()
    _append(
        runtime_dir / LEDGER,
        "SUPERVISOR_STARTED",
        pid=os.getpid(),
        trigger_start=TRIGGER_START.isoformat(timespec="seconds"),
        trigger_end=TRIGGER_END.isoformat(timespec="seconds"),
    )
    last_attempt_date = None
    while True:
        now = datetime.now(TAIPEI)
        today = now.date().isoformat()
        if (
            now.weekday() < 5
            and _within_trigger_window(now)
            and last_attempt_date != today
        ):
            last_attempt_date = today
            trigger_once(runtime_dir, now=now)
        time.sleep(max(0.25, interval_seconds))


def serve(runtime_dir: Path, *, live: bool, interval_seconds: float = 1.0) -> int:
    gate = LiveTradingGate.from_environment(cli_live=live)
    if not gate.authorized:
        raise RuntimeError(
            "force-flat supervisor requires EXECUTION_MODE=LIVE, "
            "ENABLE_LIVE_TRADING=YES and --live"
        )
    return scheduler_loop(runtime_dir, interval_seconds=interval_seconds)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Independent scheduled force-flat supervisor")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("once", "serve"):
        command = sub.add_parser(name)
        command.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
        command.add_argument("--live", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        gate = LiveTradingGate.from_environment(cli_live=bool(args.live))
        if not gate.authorized:
            raise RuntimeError(
                "force-flat supervisor requires EXECUTION_MODE=LIVE, "
                "ENABLE_LIVE_TRADING=YES and --live"
            )
        if args.command == "once":
            return 0 if trigger_once(args.runtime_dir) else 2
        return serve(args.runtime_dir, live=args.live)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
