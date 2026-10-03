"""Independent 13:20/13:23 force-flat supervisor.

This process never creates entry intents.  It writes a durable force-flat
request at 13:20 and, when the primary runtime is absent, starts the same
guarded runtime in exit-only recovery mode.  The runtime removes the request
only after fresh broker reconciliation proves positions equal the frozen daily
baseline and no broker/local orders remain.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, time as time_cls, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from yuanta_broker_execution_v01 import LiveTradingGate

from .notifications import RuntimeNotifier
from .watchdog import broker_flat_proof, runtime_health


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
SUPERVISOR_HEARTBEAT = "supervisor_heartbeat.json"
TRADING_CALENDAR = MODULE_DIR.parent / "shadow_daily_runner" / "runtime" / "trading_calendar.csv"


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
    return runtime_health(runtime_dir, now=now, stale_seconds=stale_seconds).healthy


def _write_request(runtime_dir: Path, now: datetime, reason: str = "SCHEDULED_FORCE_FLAT_1320") -> Path:
    request = runtime_dir / REQUEST
    runtime_dir.mkdir(parents=True, exist_ok=True)
    tmp = request.with_suffix(".tmp")
    tmp.write_text(
        f"{now.astimezone(TAIPEI).isoformat()} {reason}\n",
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
        "--recover-emergency",
        "--runtime-dir",
        str(runtime_dir),
        "--baseline",
        str(runtime_dir / "position_baseline.json"),
    ]
    child_env = os.environ.copy()
    child_env["EXECUTION_MODE"] = "LIVE"
    child_env["ENABLE_LIVE_TRADING"] = "YES"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    out_path = runtime_dir / "exit_recovery.out.log"
    err_path = runtime_dir / "exit_recovery.err.log"
    with out_path.open("a", encoding="utf-8") as stdout, err_path.open("a", encoding="utf-8") as stderr:
        os.chmod(out_path, 0o600)
        os.chmod(err_path, 0o600)
        return subprocess.Popen(
            command, cwd=str(MODULE_DIR.parent), env=child_env,
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            start_new_session=True, close_fds=True,
        )


def _supervisor_beat(runtime_dir: Path, now: datetime, state: str,
                     *, warnings: list[str] | None = None) -> None:
    path = runtime_dir / SUPERVISOR_HEARTBEAT
    if warnings is None:
        # A long recovery loop must retain an optional-guard warning published
        # by this supervisor, rather than imply that sleep prevention resumed.
        previous = _read_json(path) or {}
        previous_warnings = previous.get("warnings")
        warnings = (previous_warnings if previous.get("pid") == os.getpid()
                    and isinstance(previous_warnings, list) else [])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"at": now.isoformat(), "pid": os.getpid(), "state": state,
                               "warnings": warnings}) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _is_trading_day(now: datetime) -> bool:
    with TRADING_CALENDAR.open(encoding="utf-8", newline="") as handle:
        return now.astimezone(TAIPEI).date().isoformat() in {row["date"] for row in csv.DictReader(handle)}


def _live_day_evidence(runtime_dir: Path, now: datetime) -> bool:
    heartbeat = _read_json(runtime_dir / "heartbeat.json") or {}
    try:
        stamp = datetime.fromisoformat(str(heartbeat["at"]).replace("Z", "+00:00"))
        return (stamp.tzinfo is not None and stamp <= now
                and stamp.astimezone(TAIPEI).date() == now.date()
                and heartbeat.get("submit_live") is True and heartbeat.get("environment") == "PROD")
    except (KeyError, ValueError, TypeError):
        return False


def trigger_once(
    runtime_dir: Path,
    *,
    now: datetime | None = None,
    wait_seconds: float = 600.0,
    poll_seconds: float = 1.0,
    launch_retry_seconds: float = 30.0,
    max_launch_attempts: int | None = None,
    launcher: Callable[[Path], Any] = _launch_exit_only,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] | None = None,
    reason: str = "SCHEDULED_FORCE_FLAT_1320",
) -> bool:
    runtime_dir = Path(runtime_dir).resolve()
    wall_clock = clock or (lambda: datetime.now(TAIPEI))
    current = (now or wall_clock()).astimezone(TAIPEI)
    ledger = runtime_dir / LEDGER
    notifier = RuntimeNotifier(runtime_dir)
    try:
        if _seconds_until_market_cutoff(current) <= 0:
            _append(ledger, "FORCE_FLAT_MARKET_CLOSED_UNCONFIRMED")
            notifier.critical("FORCE_FLAT_MARKET_CLOSED_UNCONFIRMED", "Trading cutoff passed without a fresh broker-flat confirmation")
            return False
        # The current controller already owns its frozen baseline in memory.
        # Demand exit-only even if a local metadata file disappeared; a NEW
        # recovery child remains forbidden without the reviewed baseline.
        request = _write_request(runtime_dir, current, reason=reason)
        _append(ledger, "FORCE_FLAT_REQUESTED", reason=reason)
        if not _baseline_ready(runtime_dir, current):
            _append(ledger, "FORCE_FLAT_BLOCKED", reason="DAILY_BASELINE_NOT_READY")
            notifier.critical(
                "FORCE_FLAT_BASELINE_NOT_READY",
                "13:20 force-flat refused because today's broker baseline is unavailable",
            )
            return False

        launch_attempts = 0
        last_launch_at: float | None = None
        child = None
        child_started = None
        unhealthy_alerted = False
        effective_wait = min(
            max(0.0, wait_seconds),
            _seconds_until_market_cutoff(current),
        )
        deadline = time.monotonic() + effective_wait

        while time.monotonic() <= deadline:
            fresh_now = wall_clock().astimezone(TAIPEI)
            _supervisor_beat(runtime_dir, fresh_now, "EXIT_RECOVERY_SUPERVISING")
            if not request.exists() and broker_flat_proof(runtime_dir, since=current, now=fresh_now):
                _append(ledger, "FORCE_FLAT_CONFIRMED_BASELINE_ONLY")
                return True
            if _seconds_until_market_cutoff(fresh_now) <= 0:
                break
            monotonic_now = time.monotonic()
            if child is not None:
                return_code = child.poll()
                if return_code is not None:
                    _append(ledger, "FORCE_FLAT_EXIT_ONLY_CHILD_EXITED", returncode=int(return_code), attempt=launch_attempts)
                    child = None
                elif child_started is not None and monotonic_now - child_started > 90 and not _runtime_active(runtime_dir, fresh_now) and not unhealthy_alerted:
                    unhealthy_alerted = True
                    notifier.critical("FORCE_FLAT_CHILD_UNHEALTHY", "Exit-only child is alive without healthy account-lock/heartbeat evidence; no second controller was started")
            retry_due = (
                last_launch_at is None
                or monotonic_now - last_launch_at >= min(30.0, max(1.0, launch_retry_seconds) * (2 ** min(launch_attempts - 1, 5)))
            )
            if (
                not _runtime_active(runtime_dir, fresh_now)
                and retry_due
                and child is None
                and not runtime_health(runtime_dir, now=fresh_now).controller_present
            ):
                launch_attempts += 1
                last_launch_at = monotonic_now
                try:
                    child = launcher(runtime_dir)
                    child_started = monotonic_now
                    unhealthy_alerted = False
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

            sleeper(min(max(0.05, poll_seconds), max(0.05, _seconds_until_market_cutoff(fresh_now))))

        _append(ledger, "FORCE_FLAT_UNCONFIRMED", request_present=request.exists())
        notifier.critical(
            "FORCE_FLAT_UNCONFIRMED",
            "13:20/13:23 force-flat did not obtain broker-confirmed baseline-only inventory",
        )
        return False
    finally:
        notifier.close(timeout=0.25)


class _SleepInhibitor:
    """Future lifecycle only: protect an authorized trading day until cutoff."""
    def __init__(self):
        self.process = None

    def ensure(self, runtime_dir: Path, now: datetime) -> None:
        if sys.platform != "darwin" or _seconds_until_market_cutoff(now) <= 0:
            return
        if self.process is not None and self.process.poll() is None:
            return
        seconds = max(1, int(_seconds_until_market_cutoff(now)))
        self.process = subprocess.Popen(
            ["/usr/bin/caffeinate", "-d", "-i", "-t", str(seconds)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        _append(runtime_dir / LEDGER, "TRADING_SLEEP_INHIBITOR_STARTED", seconds=seconds)

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def scheduler_loop(runtime_dir: Path, *, interval_seconds: float = 1.0,
                   stop_event: threading.Event | None = None,
                   clock: Callable[[], datetime] | None = None,
                   sleeper: Callable[[float], None] | None = None) -> int:
    """Supervise both the scheduler and previously authorized LIVE children.

    Recovery creates ONLY exit-only children. It never opens a new position,
    clears a halt, or changes the frozen baseline. A live lock owner is never
    killed/replaced merely because its heartbeat is stale.
    """
    if os.environ.get(SCHEDULE_GATE, "").strip().upper() != "YES":
        raise RuntimeError("scheduled force-flat gate is not enabled")
    runtime_dir = Path(runtime_dir).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    wall_clock = clock or (lambda: datetime.now(TAIPEI))
    sleep = sleeper or time.sleep
    _append(
        runtime_dir / LEDGER,
        "SUPERVISOR_STARTED",
        pid=os.getpid(),
        trigger_start=TRIGGER_START.isoformat(timespec="seconds"),
        trigger_end=TRIGGER_END.isoformat(timespec="seconds"),
    )
    cutoff_alert_date = None
    history_alert_key = None
    last_warning_at = -float("inf")
    last_inhibitor_warning_at = -float("inf")
    inhibitor = _SleepInhibitor()
    notifier = RuntimeNotifier(runtime_dir)

    def inhibitor_warning(exc: Exception, now: datetime, *, cleanup: bool = False) -> None:
        nonlocal last_inhibitor_warning_at
        event = "TRADING_SLEEP_INHIBITOR_CLEANUP_FAILED" if cleanup else "TRADING_SLEEP_INHIBITOR_UNAVAILABLE"
        try:
            _supervisor_beat(runtime_dir, now, "SUPERVISING_WITH_WARNING", warnings=[event])
        except Exception:
            print("CRITICAL: sleep-inhibitor warning heartbeat unavailable", flush=True)
        if time.monotonic() - last_inhibitor_warning_at >= 30:
            last_inhibitor_warning_at = time.monotonic()
            try:
                _append(runtime_dir / LEDGER, event, error_type=type(exc).__name__)
                notifier.critical(event,
                                  "Optional sleep-prevention lifecycle failed; mandatory runtime supervision and cleanup continue",
                                  error_type=type(exc).__name__)
            except Exception:
                print("CRITICAL: sleep-inhibitor failure logging/notification unavailable", flush=True)

    def close_inhibitor(now: datetime) -> None:
        try:
            inhibitor.close()
        except Exception as exc:
            # A process disappearing between poll/terminate, or a failed wait,
            # must not terminate supervision or skip notifier cleanup.
            inhibitor_warning(exc, now, cleanup=True)

    try:
        while stop_event is None or not stop_event.is_set():
            now = wall_clock().astimezone(TAIPEI)
            today = now.date().isoformat()
            try:
                _supervisor_beat(runtime_dir, now, "SUPERVISING", warnings=[])
                live_evidence = _live_day_evidence(runtime_dir, now)
                request_present = (runtime_dir / REQUEST).exists()
                flat_proved = broker_flat_proof(runtime_dir, now=now)
                prior = _read_json(runtime_dir / "heartbeat.json") or {}
                try:
                    prior_stamp = datetime.fromisoformat(str(prior["at"]).replace("Z", "+00:00"))
                    unresolved_history = (prior_stamp.tzinfo is not None and prior_stamp.astimezone(TAIPEI).date() < now.date()
                        and prior.get("state") == "STOPPED_UNSAFE" and prior.get("submit_live") is True
                        and prior.get("environment") == "PROD")
                except (KeyError, TypeError, ValueError):
                    unresolved_history = False
                if unresolved_history and history_alert_key != prior.get("at"):
                    history_alert_key = prior.get("at")
                    notifier.critical("SUPERVISOR_PRIOR_SESSION_UNRESOLVED",
                                      "A prior LIVE session ended unsafe; current broker positions are unknown and no baseline/halts were reset")
                if _seconds_until_market_cutoff(now) <= 0:
                    if not flat_proved and (live_evidence or request_present) and cutoff_alert_date != today:
                        cutoff_alert_date = today
                        _append(runtime_dir / LEDGER, "SUPERVISOR_CUTOFF_EXPOSURE_UNCONFIRMED")
                        notifier.critical("SUPERVISOR_CUTOFF_EXPOSURE_UNCONFIRMED",
                                          "Market window was missed or ended without broker-flat proof; orders are not assumed filled")
                    close_inhibitor(now)
                elif now.time().replace(tzinfo=None) >= time_cls(8, 50) and (live_evidence or _is_trading_day(now)):
                    if live_evidence:
                        try:
                            inhibitor.ensure(runtime_dir, now)
                        except Exception as exc:
                            # Sleep prevention is optional: failure must not
                            # suppress mandatory liveness and force-flat work.
                            inhibitor_warning(exc, now)
                    health = runtime_health(runtime_dir, now=now)
                    recovery_due = live_evidence and not flat_proved and not health.healthy
                    scheduled_due = (now.time().replace(tzinfo=None) >= TRIGGER_START and not flat_proved
                                     and (live_evidence or _baseline_ready(runtime_dir, now)))
                    if recovery_due and health.controller_present:
                        if time.monotonic() - last_warning_at >= 30:
                            last_warning_at = time.monotonic()
                            _write_request(runtime_dir, now, reason="UNHEALTHY_CONTROLLER_EXIT_ONLY")
                            notifier.critical("RUNTIME_CONTROLLER_UNHEALTHY",
                                              "Controller still owns its lock but heartbeat is unhealthy; exit-only requested, no duplicate controller started",
                                              reason=health.reason)
                    elif scheduled_due or recovery_due:
                        if recovery_due:
                            notifier.critical("RUNTIME_CHILD_RECOVERY_REQUIRED",
                                              "Authorized LIVE runtime is unavailable; starting broker-backed exit-only recovery")
                        trigger_once(
                            runtime_dir, now=now, clock=wall_clock,
                            reason="RUNTIME_CHILD_EXIT_ONLY_RECOVERY" if recovery_due else "SCHEDULED_FORCE_FLAT_1320",
                            wait_seconds=_seconds_until_market_cutoff(now),
                        )
            except Exception as exc:
                # A disk/notification/scheduler failure must not silently kill
                # a daemon thread while the Bot keeps presenting itself alive.
                print(f"Force-flat supervision warning: {type(exc).__name__}", flush=True)
                if time.monotonic() - last_warning_at >= 30:
                    last_warning_at = time.monotonic()
                    try:
                        _append(runtime_dir / LEDGER, "SUPERVISOR_CYCLE_FAILED", error_type=type(exc).__name__)
                        notifier.critical("SUPERVISOR_CYCLE_FAILED", "Safety supervision cycle failed and will retry; market safety is not certified")
                    except Exception:
                        print("CRITICAL: safety-supervision durable logging/notification failed", flush=True)
            sleep(max(0.25, interval_seconds))
        return 0
    finally:
        close_inhibitor(datetime.now(TAIPEI))
        notifier.close(timeout=0.25)


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
