from __future__ import annotations

import csv
from datetime import datetime, time, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Callable
from zoneinfo import ZoneInfo

from .config import CFG, RunnerConfig
from .io_utils import append_jsonl, atomic_write_json, utc_timestamp


TITLE = "WarrantScope Shadow"
TEST_MESSAGE = "通知測試成功"
OSASCRIPT_PATH = Path("/usr/bin/osascript")
MAX_DISPLAYED_STOCKS = 5

SEALED_WITH_SIGNALS = "SEALED_WITH_SIGNALS"
SEALED_ZERO_SIGNAL = "SEALED_ZERO_SIGNAL"
FINAL_READINESS_FAILED = "FINAL_READINESS_FAILED"
HISTORICAL_PREFLIGHT_FAILED = "HISTORICAL_PREFLIGHT_FAILED"
TEST_NOTIFICATION = "TEST_NOTIFICATION"


def _notification_dir(cfg: RunnerConfig) -> Path:
    return cfg.runtime_dir / "notifications"


def _state_path(cfg: RunnerConfig) -> Path:
    return _notification_dir(cfg) / "notification_state.json"


def _log_path(cfg: RunnerConfig) -> Path:
    return cfg.logs_dir / "notification.log"


def _read_json(path: Path, default: object) -> object:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _load_notification_state(cfg: RunnerConfig) -> dict:
    state = _read_json(
        _state_path(cfg),
        {"schema_version": "1", "notifications": []},
    )
    if not isinstance(state, dict) or not isinstance(state.get("notifications"), list):
        raise RuntimeError("notification state is malformed")
    return state


def _compact_date(value: object) -> str:
    compact = str(value or "").replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError("signal_date must be YYYYMMDD")
    datetime.strptime(compact, "%Y%m%d")
    return compact


def _date_label(signal_date: str) -> str:
    compact = _compact_date(signal_date)
    return f"{int(compact[4:6])}/{int(compact[6:8])}"


def _payload_hash(title: str, message: str) -> str:
    payload = json.dumps(
        {"message": message, "title": title},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _signal_label(row: dict[str, str]) -> str:
    code = str(row.get("stock_id", "")).strip()
    name = str(row.get("stock_name", "")).strip()
    if not code:
        raise RuntimeError("sealed prospective signal is missing stock_id")
    return f"{code} {name}" if name else code


def build_sealed_payload(
    signal_date: str,
    signal_rows: list[dict[str, str]],
) -> dict[str, str]:
    compact = _compact_date(signal_date)
    labels = [_signal_label(row) for row in signal_rows]
    count = len(labels)
    if not labels:
        message = f"{_date_label(compact)} 已封存｜N Compact 0 檔"
        notification_type = SEALED_ZERO_SIGNAL
    else:
        displayed = labels[:MAX_DISPLAYED_STOCKS]
        suffix = (
            f"，另 {count - MAX_DISPLAYED_STOCKS} 檔"
            if count > MAX_DISPLAYED_STOCKS
            else ""
        )
        message = (
            f"{_date_label(compact)} 已封存｜N Compact {count} 檔："
            f"{'、'.join(displayed)}{suffix}"
        )
        notification_type = SEALED_WITH_SIGNALS
    return {
        "signal_date": compact,
        "notification_type": notification_type,
        "title": TITLE,
        "message": message,
    }


def _failure_classification(reason: object) -> tuple[str, str]:
    text = str(reason or "").strip()
    lower = text.lower()
    if "historical preflight" in lower:
        return HISTORICAL_PREFLIGHT_FAILED, "Historical preflight"
    if "twse" in lower:
        return FINAL_READINESS_FAILED, "TWSE EOD incomplete"
    if "tpex" in lower:
        return FINAL_READINESS_FAILED, "TPEx EOD incomplete"
    if "official eod" in lower:
        return FINAL_READINESS_FAILED, "EOD incomplete"
    return FINAL_READINESS_FAILED, "Readiness FAIL"


def build_failure_payload(signal_date: str, reason: object = None) -> dict[str, str]:
    compact = _compact_date(signal_date)
    notification_type, summary = _failure_classification(reason)
    if notification_type == HISTORICAL_PREFLIGHT_FAILED:
        message = f"{_date_label(compact)} Shadow FAIL｜Historical preflight"
    else:
        message = f"{_date_label(compact)} 尚未封存｜{summary}"
    return {
        "signal_date": compact,
        "notification_type": notification_type,
        "title": TITLE,
        "message": message,
    }


def is_final_scheduled_attempt(local: datetime, cfg: RunnerConfig = CFG) -> bool:
    current = local.time().replace(tzinfo=None)
    final_attempt = max(time.fromisoformat(value) for value in cfg.attempt_times)
    latest = time.fromisoformat(cfg.latest_attempt_time)
    return final_attempt <= current <= latest


def _osascript_error(completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or completed.stdout or "unknown osascript error").strip()
    return " ".join(detail.split())[:500]


def _send_macos_notification(title: str, message: str) -> None:
    script = (
        "on run argv\n"
        "display notification (item 2 of argv) with title (item 1 of argv)\n"
        "end run"
    )
    completed = subprocess.run(
        [str(OSASCRIPT_PATH), "-e", script, "--", title, message],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=10,
    )
    if completed.returncode:
        raise RuntimeError(
            f"osascript exited with {completed.returncode}: {_osascript_error(completed)}"
        )


def _log(
    cfg: RunnerConfig,
    *,
    signal_date: str | None,
    notification_type: str,
    title: str,
    message: str,
    result: str,
    error: str | None,
) -> None:
    append_jsonl(
        _log_path(cfg),
        {
            "timestamp": utc_timestamp(),
            "signal_date": signal_date,
            "type": notification_type,
            "title": title,
            "message": message,
            "result": result,
            "error": error,
        },
    )


def _best_effort_log(cfg: RunnerConfig, **values: object) -> None:
    try:
        _log(cfg, **values)  # type: ignore[arg-type]
    except Exception:
        # A notification/logging failure is intentionally isolated from the
        # shadow pipeline.  There is no second-layer notification.
        pass


def _delivery_record(
    payload: dict[str, str],
    payload_hash: str,
    *,
    success: bool,
    error: str | None,
) -> dict:
    return {
        "signal_date": payload["signal_date"],
        "notification_type": payload["notification_type"],
        "payload_hash": payload_hash,
        "sent_at": utc_timestamp(),
        "success": success,
        "title": payload["title"],
        "message": payload["message"],
        "error": error,
    }


def deliver_notification(
    payload: dict[str, str],
    *,
    cfg: RunnerConfig = CFG,
    sender: Callable[[str, str], None] | None = None,
) -> dict:
    compact = _compact_date(payload.get("signal_date"))
    notification_type = str(payload.get("notification_type", "")).strip()
    title = str(payload.get("title", "")).strip()
    message = str(payload.get("message", "")).strip()
    if not notification_type or not title or not message:
        raise ValueError("notification payload is incomplete")
    normalized = {
        "signal_date": compact,
        "notification_type": notification_type,
        "title": title,
        "message": message,
    }
    digest = _payload_hash(title, message)
    state = _load_notification_state(cfg)
    duplicate = any(
        isinstance(record, dict)
        and record.get("signal_date") == compact
        and record.get("notification_type") == notification_type
        and record.get("payload_hash") == digest
        and record.get("success") is True
        for record in state["notifications"]
    )
    if duplicate:
        _best_effort_log(
            cfg,
            signal_date=compact,
            notification_type=notification_type,
            title=title,
            message=message,
            result="SUPPRESSED_DUPLICATE",
            error=None,
        )
        return {
            "status": "SUPPRESSED_DUPLICATE",
            "signal_date": compact,
            "notification_type": notification_type,
            "payload_hash": digest,
            "success": True,
        }

    send = sender or _send_macos_notification
    try:
        send(title, message)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        record = _delivery_record(normalized, digest, success=False, error=error)
        try:
            state["notifications"].append(record)
            state.update(
                {
                    "schema_version": "1",
                    "updated_at": record["sent_at"],
                    "last_notification": record,
                }
            )
            atomic_write_json(_state_path(cfg), state)
        except Exception as state_exc:
            error = f"{error}; state error: {type(state_exc).__name__}: {state_exc}"
        _best_effort_log(
            cfg,
            signal_date=compact,
            notification_type=notification_type,
            title=title,
            message=message,
            result="FAILED",
            error=error,
        )
        return {
            "status": "FAILED",
            "signal_date": compact,
            "notification_type": notification_type,
            "payload_hash": digest,
            "success": False,
            "error": error,
        }

    record = _delivery_record(normalized, digest, success=True, error=None)
    try:
        state["notifications"].append(record)
        state.update(
            {
                "schema_version": "1",
                "updated_at": record["sent_at"],
                "last_notification": record,
            }
        )
        atomic_write_json(_state_path(cfg), state)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _best_effort_log(
            cfg,
            signal_date=compact,
            notification_type=notification_type,
            title=title,
            message=message,
            result="SENT_STATE_FAILED",
            error=error,
        )
        return {
            "status": "SENT_STATE_FAILED",
            "signal_date": compact,
            "notification_type": notification_type,
            "payload_hash": digest,
            "success": False,
            "error": error,
        }

    _best_effort_log(
        cfg,
        signal_date=compact,
        notification_type=notification_type,
        title=title,
        message=message,
        result="SENT",
        error=None,
    )
    return {
        "status": "SENT",
        "signal_date": compact,
        "notification_type": notification_type,
        "payload_hash": digest,
        "sent_at": record["sent_at"],
        "success": True,
    }


def _read_signal_rows(cfg: RunnerConfig, signal_date: str) -> list[dict[str, str]]:
    path = cfg.shadow_store_dir / "prospective_signals.csv"
    if not path.is_file():
        raise RuntimeError("prospective signal ledger is missing")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [
            row
            for row in csv.DictReader(handle)
            if row.get("signal_date") == signal_date
        ]


def _verified_completed_target(cfg: RunnerConfig, signal_date: str) -> dict:
    state = _read_json(
        cfg.runner_state_path,
        {"schema_version": "1", "completed_targets": {}},
    )
    if not isinstance(state, dict) or not isinstance(state.get("completed_targets"), dict):
        raise RuntimeError("runner state is malformed")
    completed = state["completed_targets"].get(signal_date)
    if not isinstance(completed, dict):
        raise RuntimeError("target does not have a completed runner marker")
    verified = completed.get("ledger_status")
    if not isinstance(verified, dict):
        raise RuntimeError("completed target is missing verified ledger status")
    live_status = _read_json(cfg.shadow_store_dir / "shadow_status.json", {})
    if not isinstance(live_status, dict):
        raise RuntimeError("prospective shadow status is malformed")
    for name in (
        "last_successful_signal_date",
        "signals_sha256",
        "outcomes_sha256",
        "scans_sha256",
        "actual_orders",
        "actual_fills",
        "broker_connections",
    ):
        if live_status.get(name) != verified.get(name):
            raise RuntimeError(f"live ledger status no longer matches verified status: {name}")
    if live_status.get("last_successful_signal_date") != signal_date:
        raise RuntimeError("prospective status has not sealed the notification date")
    for name in ("actual_orders", "actual_fills", "broker_connections"):
        if live_status.get(name) != 0:
            raise RuntimeError(f"shadow safety counter is not zero: {name}")
    return completed


def notify_sealed_target(signal_date: str, cfg: RunnerConfig = CFG) -> dict:
    compact = _compact_date(signal_date)
    completed = _verified_completed_target(cfg, compact)
    rows = _read_signal_rows(cfg, compact)
    expected_count = completed.get("signal_count")
    if isinstance(expected_count, bool) or not isinstance(expected_count, int):
        raise RuntimeError("completed target signal_count is invalid")
    if len(rows) != expected_count:
        raise RuntimeError("sealed signal rows do not match completed signal_count")
    return deliver_notification(build_sealed_payload(compact, rows), cfg=cfg)


def _target_is_completed(cfg: RunnerConfig, signal_date: str) -> bool:
    state = _read_json(
        cfg.runner_state_path,
        {"schema_version": "1", "completed_targets": {}},
    )
    if not isinstance(state, dict) or not isinstance(state.get("completed_targets"), dict):
        raise RuntimeError("runner state is malformed")
    return signal_date in state["completed_targets"]


def notify_final_failure(
    signal_date: str,
    reason: object,
    cfg: RunnerConfig = CFG,
) -> dict:
    compact = _compact_date(signal_date)
    if _target_is_completed(cfg, compact):
        return {
            "status": "SKIPPED_ALREADY_SEALED",
            "signal_date": compact,
            "success": True,
        }
    return deliver_notification(build_failure_payload(compact, reason), cfg=cfg)


def notify_attempt_result(
    result: dict,
    local: datetime,
    cfg: RunnerConfig = CFG,
) -> dict:
    status = result.get("status")
    target = _compact_date(result.get("target_date"))
    if status in {"SUCCESS", "ALREADY_SUCCEEDED_NO_OP"}:
        return notify_sealed_target(target, cfg)
    if status == "READINESS_FAILED_NO_LEDGER_WRITE":
        if not is_final_scheduled_attempt(local, cfg):
            return {
                "status": "SKIPPED_BEFORE_FINAL_ATTEMPT",
                "signal_date": target,
                "success": True,
            }
        readiness = result.get("readiness")
        reason = _readiness_failure_reason(readiness)
        return notify_final_failure(target, reason, cfg)
    return {
        "status": "NOT_APPLICABLE",
        "signal_date": target,
        "success": True,
    }


def _readiness_failure_reason(readiness: object) -> object:
    if not isinstance(readiness, dict):
        return None
    audit_path = readiness.get("audit_path")
    if audit_path:
        try:
            audit = _read_json(Path(str(audit_path)), {})
            if isinstance(audit, dict):
                for name in (
                    "historical_preflight_before_target_download",
                    "historical_preflight",
                ):
                    section = audit.get(name)
                    if isinstance(section, dict) and section.get("status") in {
                        "FAIL",
                        "FAIL_CLOSED",
                    }:
                        return "historical preflight"
        except Exception:
            # The already-returned, bounded readiness reason remains a safe
            # fallback; notification diagnostics never change runner status.
            pass
    return readiness.get("failure_reason")


def _safe_failure_result(
    cfg: RunnerConfig,
    *,
    signal_date: str | None,
    notification_type: str,
    title: str,
    message: str,
    exc: Exception,
) -> dict:
    error = f"{type(exc).__name__}: {exc}"
    _best_effort_log(
        cfg,
        signal_date=signal_date,
        notification_type=notification_type,
        title=title,
        message=message,
        result="FAILED",
        error=error,
    )
    return {"status": "FAILED", "success": False, "error": error}


def safe_notify_attempt_result(
    result: dict,
    local: datetime,
    cfg: RunnerConfig = CFG,
) -> dict:
    try:
        return notify_attempt_result(result, local, cfg)
    except Exception as exc:
        return _safe_failure_result(
            cfg,
            signal_date=str(result.get("target_date") or "") or None,
            notification_type="ATTEMPT_NOTIFICATION_ERROR",
            title=TITLE,
            message="",
            exc=exc,
        )


def safe_notify_unhandled_attempt_failure(
    *,
    local: datetime,
    reason: object,
    cfg: RunnerConfig = CFG,
) -> dict:
    target = local.strftime("%Y%m%d")
    if not is_final_scheduled_attempt(local, cfg):
        return {
            "status": "SKIPPED_BEFORE_FINAL_ATTEMPT",
            "signal_date": target,
            "success": True,
        }
    try:
        return notify_final_failure(target, reason, cfg)
    except Exception as exc:
        return _safe_failure_result(
            cfg,
            signal_date=target,
            notification_type="ATTEMPT_NOTIFICATION_ERROR",
            title=TITLE,
            message="",
            exc=exc,
        )


def send_test_notification(
    cfg: RunnerConfig = CFG,
    *,
    sender: Callable[[str, str], None] | None = None,
) -> dict:
    signal_date = datetime.now(timezone.utc).astimezone(
        ZoneInfo(cfg.timezone)
    ).strftime("%Y%m%d")
    send = sender or _send_macos_notification
    try:
        send(TITLE, TEST_MESSAGE)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _best_effort_log(
            cfg,
            signal_date=signal_date,
            notification_type=TEST_NOTIFICATION,
            title=TITLE,
            message=TEST_MESSAGE,
            result="FAILED",
            error=error,
        )
        return {
            "status": "NOTIFICATION_TEST_FAILED",
            "title": TITLE,
            "message": TEST_MESSAGE,
            "osascript_path": str(OSASCRIPT_PATH),
            "diagnostic": error,
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
        }
    _best_effort_log(
        cfg,
        signal_date=signal_date,
        notification_type=TEST_NOTIFICATION,
        title=TITLE,
        message=TEST_MESSAGE,
        result="SENT",
        error=None,
    )
    return {
        "status": "NOTIFICATION_TEST_SENT",
        "title": TITLE,
        "message": TEST_MESSAGE,
        "osascript_path": str(OSASCRIPT_PATH),
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }


def notification_status(cfg: RunnerConfig = CFG) -> dict:
    try:
        state = _load_notification_state(cfg)
        last = state.get("last_notification")
        if not isinstance(last, dict):
            records = [row for row in state["notifications"] if isinstance(row, dict)]
            last = records[-1] if records else {}
        return {
            "last_signal_date": last.get("signal_date"),
            "last_type": last.get("notification_type"),
            "last_sent_at": last.get("sent_at"),
            "last_success": last.get("success"),
        }
    except Exception as exc:
        return {
            "last_signal_date": None,
            "last_type": None,
            "last_sent_at": None,
            "last_success": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
