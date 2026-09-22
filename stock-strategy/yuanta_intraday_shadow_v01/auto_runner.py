#!/usr/bin/env python3
"""Trading-day launchd entrypoint for read-only full-session collection."""

from __future__ import annotations

import csv
from datetime import datetime, time
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

from prospective_notifications_v01.keychain import load_into_environment
from prospective_notifications_v01.notifier import notify

from .collector import DEFAULT_RUNTIME_DIR, DEFAULT_STAGE_A_RUNTIME, utc_now
from .collector_main import run
from .main import DEFAULT_VENDOR_DIR, _load_api
from .postprocess import process_run
from .yuanta_keychain import load_credentials
from stage_a_prospective_watchlist_v01.seal_store import latest_seal


TAIPEI = ZoneInfo("Asia/Taipei")
CALENDAR = Path(__file__).resolve().parents[1] / "shadow_daily_runner" / "runtime" / "trading_calendar.csv"
LOG = DEFAULT_RUNTIME_DIR / "automatic_runs.jsonl"
LOCK = DEFAULT_RUNTIME_DIR / ".automatic_runner.lock"


def _append(record: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush(); os.fsync(handle.fileno())


def _calendar() -> list[str]:
    with CALENDAR.open(encoding="utf-8", newline="") as handle:
        dates = [row["date"].replace("-", "") for row in csv.DictReader(handle)]
    if dates != sorted(set(dates)):
        raise RuntimeError("trading calendar is not sorted and unique")
    return dates


def _warning(day: str, reason: str) -> None:
    message = "\n".join([f"【WarrantScope WARNING｜{day}】", "模組：Yuanta intraday shadow", f"原因：{reason}", "沒有下單；未建立假資料。"])
    try:
        load_into_environment()
        notify("YUANTA_INTRADAY_SHADOW", day, __import__("hashlib").sha256(reason.encode()).hexdigest(), "WARNING", message)
    except Exception:
        pass


def _readiness(now: datetime) -> tuple[str, str]:
    dates = _calendar()
    today = now.strftime("%Y%m%d")
    if today not in dates:
        return "NON_TRADING_DAY", ""
    index = dates.index(today)
    if index == 0:
        raise RuntimeError("trading calendar has no prior session")
    if not time(8, 35) <= now.time() <= time(9, 0, 30):
        raise RuntimeError(f"automatic start outside 08:35-09:00:30: {now.time().isoformat(timespec='seconds')}")
    seal = latest_seal(DEFAULT_STAGE_A_RUNTIME)
    if seal is None:
        raise RuntimeError("Stage A seal is absent")
    expected = dates[index - 1]
    if seal.get("signal_date") != expected or len(seal.get("stocks", [])) != 30:
        raise RuntimeError(f"Stage A seal not ready: expected {expected}, got {seal.get('signal_date')}")
    return "READY", expected


def main() -> int:
    now = datetime.now(TAIPEI)
    day = now.strftime("%Y%m%d")
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _append({"at": utc_now(), "date": day, "status": "ALREADY_RUNNING"})
            return 0
        try:
            readiness, signal_date = _readiness(now)
            if readiness == "NON_TRADING_DAY":
                _append({"at": utc_now(), "date": day, "status": readiness})
                return 0
            credentials = load_credentials()
            target = datetime.combine(now.date(), time(13, 35), tzinfo=TAIPEI)
            seconds = int((target - now).total_seconds())
            if not 60 <= seconds <= 18000:
                raise RuntimeError("invalid automatic full-session duration")
            caffeinate = subprocess.Popen(["/usr/bin/caffeinate", "-dimsu", "-w", str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            holder: dict[str, str] = {}
            def progress(event: dict):
                if event.get("type") == "FINAL": holder["run_dir"] = event["run_dir"]
            try:
                code = run(_load_api(DEFAULT_VENDOR_DIR), seconds=seconds, runtime_dir=DEFAULT_RUNTIME_DIR, credentials=credentials, progress_callback=progress, compress=True)
            finally:
                credentials.clear()
                caffeinate.terminate()
            if code != 0 or "run_dir" not in holder:
                raise RuntimeError(f"collector failed with code {code}")
            result = process_run(Path(holder["run_dir"]))
            session = result["session"]
            if session["coverage_status"] != "FULL_SESSION":
                raise RuntimeError(f"coverage failed: max_gap={session.get('max_market_event_gap_seconds')}")
            _append({
                "at": utc_now(), "date": day, "signal_date": signal_date, "status": "COMPLETE",
                "run_id": session["source_run_id"], "analysis_hash": result["quality"]["analysis_hash"],
                "coverage": session["coverage_status"], "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
            })
            return 0
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            _append({"at": utc_now(), "date": day, "status": "FAILED", "reason": reason, "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0})
            _warning(day, reason)
            print(reason, file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
