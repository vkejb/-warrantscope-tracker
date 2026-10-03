from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .trading_bot_keychain import load_trading_bot_credentials
from .force_flat_supervisor import (
    SCHEDULE_GATE,
    scheduler_loop as force_flat_scheduler_loop,
)
from .watchdog import runtime_health
from .notifications import RuntimeNotifier
from .trading_bot_notifier import AsyncTradingNotifier

MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_RUNTIME_DIR = MODULE_DIR / "runtime"

CONTROL_CONFIRM_TTL_SECONDS = 120
CONTROL_CONFIRM_MAX_ATTEMPTS = 3
CONTROL_AUDIT_FILENAME = "trading_bot_audit.jsonl"
START_CONFIRM_TIMEOUT_SECONDS = 20.0
SUPERVISOR_STARTUP_GRACE_SECONDS = 15.0
SUPERVISOR_HEARTBEAT_STALE_SECONDS = 45.0


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _read_recent_events(path: Path, limit: int = 500) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _stamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _age_seconds(value: Any) -> float | None:
    stamp = _stamp(value)
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        return None
    return (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()


def _compact_signal(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    candidate = row.get("candidate")
    if not isinstance(candidate, dict):
        return None
    allowed = (
        "stock_id", "stock_name", "side", "decision_time",
        "score", "entry_price", "quantity",
    )
    return {key: candidate.get(key) for key in allowed if key in candidate}


def _compact_order(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    allowed = (
        "at", "event", "stock_id", "side", "quantity", "price",
        "status", "last_error", "reason", "average_fill_price",
        "projected_net_pnl",
    )
    return {key: row.get(key) for key in allowed if key in row}


def build_status(runtime_dir: Path) -> dict[str, Any]:
    runtime_dir = Path(runtime_dir)
    heartbeat = _read_json(runtime_dir / "heartbeat.json")
    events = _read_recent_events(runtime_dir / "session.jsonl")

    if heartbeat is None:
        runtime_state = "NOT_STARTED"
        heartbeat_age = None
    else:
        heartbeat_age = _age_seconds(heartbeat.get("at"))
        state = str(heartbeat.get("state", "UNKNOWN"))
        if state in {
            "STARTING", "RUNNING", "EMERGENCY_EXIT", "FORCE_FLAT_EXIT", "EXIT_ONLY_RECOVERY", "STOPPING",
        }:
            health = runtime_health(runtime_dir)
            if health.healthy:
                if state == "STARTING":
                    runtime_state = (
                        "LIVE_STARTING"
                        if bool(heartbeat.get("submit_live"))
                        else "OBSERVE_STARTING"
                    )
                elif state == "STOPPING":
                    runtime_state = (
                        "LIVE_STOPPING"
                        if bool(heartbeat.get("submit_live"))
                        else "OBSERVE_STOPPING"
                    )
                elif state in {"EMERGENCY_EXIT", "FORCE_FLAT_EXIT", "EXIT_ONLY_RECOVERY"}:
                    runtime_state = "LIVE_EXIT_ONLY_RECOVERY" if bool(heartbeat.get("submit_live")) else "OBSERVE_EXIT_ONLY_RECOVERY"
                else:
                    runtime_state = (
                        "LIVE_RUNNING"
                        if bool(heartbeat.get("submit_live"))
                        else "OBSERVE_RUNNING"
                    )
            else:
                runtime_state = "HEARTBEAT_STALE" if health.reason == "HEARTBEAT_STALE" else "RUNTIME_LIVENESS_UNPROVEN"
        elif state in {"STOPPED_CLEAN", "STOPPED_UNSAFE"}:
            runtime_state = state
        else:
            runtime_state = state

    if runtime_state in {
        "LIVE_STARTING",
        "OBSERVE_STARTING",
        "LIVE_RUNNING",
        "OBSERVE_RUNNING",
        "LIVE_STOPPING",
        "OBSERVE_STOPPING",
        "LIVE_EXIT_ONLY_RECOVERY",
        "OBSERVE_EXIT_ONLY_RECOVERY",
    }:
        quote_age = _age_seconds((heartbeat or {}).get("last_quote_at"))
        if quote_age is None:
            quote_health = "NO_QUOTE_YET"
        elif quote_age < 0:
            quote_health = "FUTURE_TIMESTAMP"
        elif quote_age <= 5:
            quote_health = "FRESH"
        elif quote_age <= 30:
            quote_health = "DELAYED"
        else:
            quote_health = "STALE"
    else:
        quote_age = None
        quote_health = "N/A"

    signal_events = {
        "SIGNAL_DETECTED",
        "SIGNAL_SKIPPED",
        "RISK_APPROVED_CANDIDATE",
        "RISK_REJECTED_CANDIDATE",
    }
    order_events = {
        "ENTRY_SUBMITTED",
        "ENTRY_NOT_FILLED",
        "ENTRY_CANCEL_SENT",
        "POSITION_OPENED",
        "EXIT_SUBMITTED",
        "EXIT_REPRICE_SENT",
        "POSITION_CLOSED",
        "POSITION_CLOSED_AFTER_RECONCILIATION",
    }

    last_signal_row = next(
        (row for row in reversed(events) if row.get("event") in signal_events),
        None,
    )
    last_order_row = next(
        (row for row in reversed(events) if row.get("event") in order_events),
        None,
    )

    gate = (heartbeat or {}).get("gate")
    if not isinstance(gate, dict):
        gate = None

    return {
        "runtime_state": runtime_state,
        "emergency_stop_active": (runtime_dir / "EMERGENCY_STOP").exists(),
        "heartbeat_age_seconds": None if heartbeat_age is None else round(heartbeat_age, 1),
        "environment": (heartbeat or {}).get("environment"),
        "submit_live": bool((heartbeat or {}).get("submit_live", False)),
        "gate_authorized": None if gate is None else bool(gate.get("authorized")),
        "signal_date": (heartbeat or {}).get("signal_date"),
        "watchlist_count": (heartbeat or {}).get("watchlist_count"),
        "entry_start": (heartbeat or {}).get("entry_start"),
        "quote_health": quote_health,
        "quote_age_seconds": None if quote_age is None else round(quote_age, 1),
        "trade_attempted": (heartbeat or {}).get("trade_attempted"),
        "failure_code": (heartbeat or {}).get("failure_code"),
        "failure_stage": (heartbeat or {}).get("failure_stage"),
        # A healthy controller can perform broker-only risk reduction without
        # fresh quotes. Do not present that as normal market monitoring.
        "monitoring_market": runtime_state in {"LIVE_RUNNING", "OBSERVE_RUNNING"} and quote_health == "FRESH",
        "exit_only_recovery": runtime_state in {"LIVE_EXIT_ONLY_RECOVERY", "OBSERVE_EXIT_ONLY_RECOVERY"},
        "last_signal": _compact_signal(last_signal_row),
        "last_order": _compact_order(last_order_row),
    }


def _yn(value: Any) -> str:
    if value is True:
        return "YES"
    if value is False:
        return "NO"
    return "UNKNOWN"


def _fmt_age(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.1f} 秒"
    except Exception:
        return str(value)


def render_status(status: dict[str, Any]) -> str:
    runtime_raw = str(
        status.get("runtime_state", "UNKNOWN")
    )

    runtime_text = {
        "LIVE_RUNNING": "實盤執行中",
        "OBSERVE_RUNNING": "觀察模式執行中",
        "LIVE_STARTING": "實盤啟動中",
        "OBSERVE_STARTING": "觀察模式啟動中",
        "LIVE_STOPPING": "實盤停止中",
        "OBSERVE_STOPPING": "觀察模式停止中",
        "STOPPED_CLEAN": "已正常停止",
        "STOPPED_UNSAFE": "異常停止",
        "HEARTBEAT_STALE": "系統心跳逾時",
        "NOT_STARTED": "尚未啟動",
        "RUNNING": "執行中",
        "STOPPING": "停止處理中",
        "EMERGENCY_EXIT": "緊急平倉處理中",
        "UNKNOWN": "狀態未知",
    }.get(
        runtime_raw,
        f"其他狀態（{runtime_raw}）",
    )

    environment = {
        "PROD": "正式環境",
        "UAT": "測試環境",
    }.get(
        str(status.get("environment")),
        str(status.get("environment") or "-"),
    )

    mode = (
        "實盤"
        if status.get("submit_live")
        else "觀察"
    )

    quote_raw = str(
        status.get("quote_health", "UNKNOWN")
    )

    quote_text = {
        "FRESH": "正常",
        "DELAYED": "延遲",
        "STALE": "過期",
        "NO_QUOTE_YET": "尚未收到行情",
        "N/A": "未啟用",
        "UNKNOWN": "狀態未知",
    }.get(
        quote_raw,
        f"其他狀態（{quote_raw}）",
    )

    side_map = {
        "LONG": "做多",
        "SHORT": "做空",
        "BUY": "買進",
        "SELL": "賣出",
    }

    event_map = {
        "ENTRY_SUBMITTED": "進場委託已送出",
        "ENTRY_NOT_FILLED": "進場委託未成交",
        "ENTRY_CANCEL_SENT": "進場撤單已送出",
        "POSITION_OPENED": "進場成交",
        "EXIT_SUBMITTED": "出場委託已送出",
        "EXIT_REPRICE_SENT": "出場委託已重新報價",
        "POSITION_CLOSED": "部位已平倉",
        "POSITION_CLOSED_AFTER_RECONCILIATION": "重新對帳後確認平倉",
    }

    order_status_map = {
        "NEW": "新委託",
        "PENDING": "等待中",
        "ACKNOWLEDGED": "券商已接受",
        "PARTIAL": "部分成交",
        "PARTIALLY_FILLED": "部分成交",
        "FILLED": "全部成交",
        "CANCELED": "已撤單",
        "CANCELLED": "已撤單",
        "REJECTED": "已拒絕",
        "FAILED": "失敗",
        "UNKNOWN": "狀態未確認",
    }

    lines = [
        "【WarrantScope 交易系統】",
        f"系統狀態：{runtime_text}",
        (
            "交易暫停：已啟用（緊急停止）"
            if bool(
                status.get("emergency_stop_active")
            )
            else "交易暫停：未啟用"
        ),
        f"環境：{environment}｜模式：{mode}",
    ]

    gate = status.get("gate_authorized")
    if gate is not None:
        lines.append(
            "交易授權："
            + ("已授權" if gate else "未授權")
        )

    quote_age = status.get("quote_age_seconds")
    lines.append(
        f"行情狀態：{quote_text}"
        f"｜行情延遲：{_fmt_age(quote_age)}"
    )

    lines.append(
        "目前監控市場："
        + (
            "是"
            if bool(status.get("monitoring_market"))
            else "否"
        )
    )
    if status.get("exit_only_recovery"):
        lines.append("退出恢復：進行中（禁止新倉；行情狀態另列）")

    lines.append(
        f"監控標的："
        f"{status.get('watchlist_count') or '-'} / 30"
        f"｜最早進場時間："
        f"{status.get('entry_start') or '-'}"
    )

    attempted = status.get("trade_attempted")
    attempted_text = (
        "是"
        if attempted is True
        else (
            "否"
            if attempted is False
            else "未知"
        )
    )
    lines.append(
        f"今日是否已嘗試交易：{attempted_text}"
    )

    signal = status.get("last_signal")

    if isinstance(signal, dict):
        try:
            score_text = (
                f"{float(signal.get('score')):.3f}"
            )
        except Exception:
            score_text = "-"

        side = side_map.get(
            str(signal.get("side")),
            str(signal.get("side") or "-"),
        )

        lines.append(
            "最後交易訊號："
            f"{signal.get('stock_id', '-')} "
            f"{signal.get('stock_name', '')}｜"
            f"{side}｜"
            f"預計 {signal.get('entry_price', '-')}｜"
            f"訊號強度 {score_text}"
        )
    else:
        lines.append("最後交易訊號：尚無")

    order = status.get("last_order")

    if isinstance(order, dict):
        event_raw = str(
            order.get("event", "-")
        )
        event_text = event_map.get(
            event_raw,
            f"其他事件（{event_raw}）",
        )

        status_raw = order.get("status")
        status_text = (
            order_status_map.get(
                str(status_raw),
                str(status_raw),
            )
            if status_raw
            else None
        )

        lines.append(
            f"最後委託事件：{event_text}"
            + (
                f"｜{status_text}"
                if status_text
                else ""
            )
        )

        if order.get("last_error"):
            lines.append(
                f"委託原因："
                f"{order['last_error']}"
            )
    else:
        lines.append("最後委託事件：尚無")

    heartbeat = _fmt_age(
        status.get("heartbeat_age_seconds")
    )
    lines.append(
        f"系統心跳：{heartbeat}前"
    )

    if runtime_raw == "STOPPED_UNSAFE":
        failure_code = str(
            status.get("failure_code") or "UNKNOWN"
        )
        failure_stage = str(
            status.get("failure_stage") or "UNKNOWN"
        )
        lines.append(
            f"異常原因：{failure_code}"
            f"｜階段：{failure_stage}"
        )

    return "\n".join(lines)

def _telegram_json(
    token: str,
    method: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: int = 35,
) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise RuntimeError(f"Telegram API unavailable: {type(exc).__name__}") from None
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise RuntimeError("Telegram API returned failure")
    return value


def _send_message(token: str, chat_id: str, message: str) -> None:
    _telegram_json(
        token,
        "sendMessage",
        payload={"chat_id": chat_id, "text": message},
        timeout=15,
    )


def _offset_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / "trading_bot_update_offset.json"


def _load_offset(runtime_dir: Path) -> int:
    value = _read_json(_offset_path(runtime_dir))
    try:
        return int((value or {}).get("next_update_id", 0))
    except Exception:
        return 0


def _save_offset(runtime_dir: Path, next_update_id: int) -> None:
    path = _offset_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"next_update_id": int(next_update_id)}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _command_text(message: dict[str, Any]) -> str:
    text = str(message.get("text", "")).strip()
    if not text:
        return ""
    return text.split()[0].split("@", 1)[0].lower()


@dataclass
class _PendingControl:
    action: str
    code: str
    expires_at_monotonic: float
    requested_update_id: int
    attempts_remaining: int = CONTROL_CONFIRM_MAX_ATTEMPTS


def _audit_control(
    runtime_dir: Path,
    event: str,
    *,
    action: str | None = None,
    update_id: int | None = None,
    outcome: str | None = None,
) -> None:
    """Append a deliberately minimal remote-control audit record.

    Never include Telegram token/chat-id, confirmation code, account,
    positions, balances, passwords, certificates, or broker payloads.
    """
    row: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "event": str(event),
    }
    if action is not None:
        row["action"] = str(action)
    if update_id is not None:
        row["update_id"] = int(update_id)
    if outcome is not None:
        row["outcome"] = str(outcome)

    path = Path(runtime_dir) / CONTROL_AUDIT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _runtime_process_active(runtime_dir: Path) -> bool:
    # Stale/legacy heartbeat is not healthy, but a live kernel lock owner still
    # prevents another controller. Never infer a dead child from age alone.
    health = runtime_health(runtime_dir)
    return health.controller_present


def _run_start_preflight(runtime_dir: Path) -> tuple[bool, str]:
    """Run sanitized PROD preflight before issuing a LIVE confirmation code.

    The preflight subprocess is forced to DRY_RUN/NO. Its stdout/stderr are
    never returned to Telegram because broker diagnostics may contain private
    account or position information.

    The actual LIVE runtime performs all critical checks again after the user
    confirms the start request.
    """
    runtime_dir = Path(runtime_dir).resolve()

    if _runtime_process_active(runtime_dir):
        return False, "RUNTIME_ALREADY_RUNNING"

    if (runtime_dir / "STOP_REQUEST").exists():
        return False, "STOP_REQUEST_ACTIVE"

    if (runtime_dir / "EMERGENCY_STOP").exists():
        return False, "EMERGENCY_STOP_ACTIVE"

    baseline_ok, baseline_status = _ensure_daily_position_baseline(runtime_dir)
    if not baseline_ok:
        return False, baseline_status

    baseline = runtime_dir / "position_baseline.json"

    command = [
        sys.executable,
        "-m",
        "yuanta_live_runtime_v01.main",
        "preflight-prod",
        "--runtime-dir",
        str(runtime_dir),
        "--baseline",
        str(baseline),
    ]

    child_env = os.environ.copy()
    child_env["EXECUTION_MODE"] = "DRY_RUN"
    child_env["ENABLE_LIVE_TRADING"] = "NO"

    try:
        completed = subprocess.run(
            command,
            cwd=str(MODULE_DIR.parent),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "START_PREFLIGHT_EXECUTION_ERROR"

    stdout = str(completed.stdout or "")
    if (
        '"reason": "BROKER_EXECUTION_HALTED"' in stdout
        or '"reason":"BROKER_EXECUTION_HALTED"' in stdout
    ):
        return False, "BROKER_EXECUTION_HALTED"
    ready = (
        completed.returncode == 0
        and (
            '"status": "READY"' in stdout
            or '"status":"READY"' in stdout
        )
    )

    if ready:
        return True, "START_PREFLIGHT_READY"

    return False, "START_PREFLIGHT_FAILED"


def _ensure_daily_position_baseline(runtime_dir: Path) -> tuple[bool, str]:
    """Capture or reuse today's broker-backed pre-LIVE inventory baseline.

    The baseline CLI refuses recapture after any entry order on the same day,
    while strategy exposure or local/broker orders are active, or when the
    runtime singleton is owned.  This wrapper never enables the LIVE gate.
    """
    runtime_dir = Path(runtime_dir).resolve()
    command = [
        sys.executable,
        "-m",
        "yuanta_live_runtime_v01.main",
        "baseline-prod",
        "--runtime-dir",
        str(runtime_dir),
        "--baseline",
        str(runtime_dir / "position_baseline.json"),
        "--for-live-start",
    ]
    child_env = os.environ.copy()
    child_env["EXECUTION_MODE"] = "DRY_RUN"
    child_env["ENABLE_LIVE_TRADING"] = "NO"
    try:
        completed = subprocess.run(
            command,
            cwd=str(MODULE_DIR.parent),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "AUTO_BASELINE_EXECUTION_ERROR"

    stdout = str(completed.stdout or "")
    if completed.returncode == 0 and (
        '"status": "BASELINE_CAPTURED"' in stdout
        or '"status":"BASELINE_CAPTURED"' in stdout
        or '"status": "BASELINE_REUSED"' in stdout
        or '"status":"BASELINE_REUSED"' in stdout
    ):
        return True, "AUTO_BASELINE_READY"
    return False, "AUTO_BASELINE_FAILED"


def _launch_runtime_start(runtime_dir: Path) -> tuple[bool, str]:
    """Dispatch guarded PROD LIVE runtime with child-process-only LIVE gates."""
    runtime_dir = Path(runtime_dir).resolve()

    # Friendly pre-check only. The kernel-backed runtime singleton lock remains
    # the authoritative protection against a second realtime controller.
    if _runtime_process_active(runtime_dir):
        return False, "RUNTIME_ALREADY_RUNNING"

    if (runtime_dir / "STOP_REQUEST").exists():
        return False, "STOP_REQUEST_ACTIVE"

    command = [
        sys.executable,
        "-m",
        "yuanta_live_runtime_v01.main",
        "start-prod",
        "--live",
        "--runtime-dir",
        str(runtime_dir),
        "--baseline",
        str(runtime_dir / "position_baseline.json"),
    ]

    child_env = os.environ.copy()
    child_env["EXECUTION_MODE"] = "LIVE"
    child_env["ENABLE_LIVE_TRADING"] = "YES"

    previous_heartbeat = _read_json(
        runtime_dir / "heartbeat.json"
    )
    previous_stamp = (
        None
        if previous_heartbeat is None
        else previous_heartbeat.get("at")
    )

    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        out_path = runtime_dir / "trading_runtime.out.log"
        err_path = runtime_dir / "trading_runtime.err.log"
        with out_path.open("a", encoding="utf-8") as stdout, err_path.open("a", encoding="utf-8") as stderr:
            os.chmod(out_path, 0o600)
            os.chmod(err_path, 0o600)
            process = subprocess.Popen(
                command, cwd=str(MODULE_DIR.parent), env=child_env,
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                start_new_session=True, close_fds=True,
            )
    except OSError:
        return False, "START_EXECUTION_ERROR"

    # A child surviving the first fraction of a second is not proof that LIVE
    # is monitoring. Login, reconciliation, local-state recovery, and quote
    # subscription happen later. Wait for this launch to publish a fresh
    # RUNNING or STOPPED_UNSAFE heartbeat before reporting the outcome.
    deadline = (
        time.monotonic()
        + START_CONFIRM_TIMEOUT_SECONDS
    )

    while time.monotonic() < deadline:
        heartbeat = _read_json(
            runtime_dir / "heartbeat.json"
        )

        if (
            heartbeat is not None
            and heartbeat.get("at") != previous_stamp
            and heartbeat.get("pid") == getattr(process, "pid", None)
            and heartbeat.get("pid") is not None
        ):
            state = str(
                heartbeat.get("state", "")
            )

            if state == "RUNNING" and runtime_health(runtime_dir).healthy:
                return True, "START_CONFIRMED_RUNNING"

            if state == "STOPPED_UNSAFE":
                failure = str(
                    heartbeat.get("failure_code")
                    or "UNKNOWN"
                )

                if failure == "BROKER_EXECUTION_HALTED":
                    return False, "START_FAILED_BROKER_HALTED"

                if failure == "QUOTE_SUBSCRIPTION_FAILED":
                    return False, "START_FAILED_QUOTE_SUBSCRIPTION"

                return False, "START_FAILED_UNSAFE"

        try:
            return_code = process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            continue

        if return_code == 0:
            return False, "START_EXITED_EARLY"

        return False, "START_REJECTED"

    return True, "START_DISPATCHED_PENDING"


def _sync_position_baseline(runtime_dir: Path) -> tuple[bool, str]:
    """Capture authoritative PROD inventory without enabling LIVE submission.

    The runtime CLI owns the kernel singleton lock and independently refuses
    STOP/HALT markers, broker open orders, local active orders, and local
    strategy positions. Broker/account output is captured and never returned
    through Telegram.
    """
    runtime_dir = Path(runtime_dir).resolve()
    if _runtime_process_active(runtime_dir):
        return False, "RUNTIME_ALREADY_RUNNING"
    if (runtime_dir / "STOP_REQUEST").exists():
        return False, "STOP_REQUEST_ACTIVE"
    if (runtime_dir / "EMERGENCY_STOP").exists():
        return False, "EMERGENCY_STOP_ACTIVE"

    command = [
        sys.executable,
        "-m",
        "yuanta_live_runtime_v01.main",
        "baseline-prod",
        "--runtime-dir",
        str(runtime_dir),
        "--baseline",
        str(runtime_dir / "position_baseline.json"),
        "--accept-existing-positions",
    ]
    child_env = os.environ.copy()
    child_env["EXECUTION_MODE"] = "DRY_RUN"
    child_env["ENABLE_LIVE_TRADING"] = "NO"

    try:
        completed = subprocess.run(
            command,
            cwd=str(MODULE_DIR.parent),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "BASELINE_SYNC_EXECUTION_ERROR"

    stdout = str(completed.stdout or "")
    captured = (
        completed.returncode == 0
        and (
            '"status": "BASELINE_CAPTURED"' in stdout
            or '"status":"BASELINE_CAPTURED"' in stdout
        )
    )
    if captured:
        return True, "BASELINE_SYNCED"
    return False, "BASELINE_SYNC_FAILED"


def _invoke_runtime_control(action: str, runtime_dir: Path) -> tuple[bool, str]:
    """Invoke the existing guarded runtime CLI in a separate process.

    Remote control uses only existing guarded runtime CLI entry points.
    It never calls broker methods directly. Only /start uses --live, through
    its separate launcher; this synchronous path never does.
    """
    if action not in {"stop", "kill", "clear-halt"}:
        raise ValueError(f"unsupported remote control action: {action}")

    runtime_dir = Path(runtime_dir).resolve()
    command = [
        sys.executable,
        "-m",
        "yuanta_live_runtime_v01.main",
        action,
        "--runtime-dir",
        str(runtime_dir),
        "--reason",
        f"telegram_{action}",
    ]

    if action == "clear-halt":
        command.extend([
            "--baseline",
            str(runtime_dir / "position_baseline.json"),
            "--environment",
            "PROD",
        ])

    control_timeout = 60 if action == "clear-halt" else 10

    try:
        completed = subprocess.run(
            command,
            cwd=str(MODULE_DIR.parent),
            capture_output=True,
            text=True,
            timeout=control_timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "CONTROL_EXECUTION_ERROR"

    status = "UNKNOWN"
    stdout = str(completed.stdout or "").strip()
    allowed_statuses = {
        "GRACEFUL_STOP_REQUESTED",
        "RUNTIME_NOT_RUNNING",
        "EMERGENCY_STOP_REQUESTED",
        "HALT_CLEARED",
    }

    # Control commands may emit diagnostic JSON lines before their final
    # result. Do not require stdout to contain exactly one JSON document.
    for candidate in allowed_statuses:
        if f'"status": "{candidate}"' in stdout or f'"status":"{candidate}"' in stdout:
            status = candidate
            break

    return completed.returncode == 0, status


class _RemoteControl:
    def __init__(
        self,
        runtime_dir: Path,
        *,
        confirm_ttl_seconds: int = CONTROL_CONFIRM_TTL_SECONDS,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.confirm_ttl_seconds = max(1, int(confirm_ttl_seconds))
        self.pending: _PendingControl | None = None

    def _request(self, action: str, update_id: int) -> str:
        code = f"{secrets.randbelow(10_000):04d}"
        self.pending = _PendingControl(
            action=action,
            code=code,
            expires_at_monotonic=time.monotonic() + self.confirm_ttl_seconds,
            requested_update_id=update_id,
        )
        _audit_control(
            self.runtime_dir,
            "REMOTE_CONTROL_CONFIRMATION_REQUESTED",
            action=action,
            update_id=update_id,
        )

        if action == "start":
            title = "啟動 LIVE Runtime"
            detail = (
                "PROD preflight 已通過。確認後 Bot 只會替這一次 runtime 子程序"
                "注入 EXECUTION_MODE=LIVE、ENABLE_LIVE_TRADING=YES，並呼叫 "
                "start-prod --live；不會永久修改系統環境。"
                "Runtime 仍會再次驗證交易日、Stage A、singleton lock、"
                "reconciliation、STOP/HALT 與 LIVE gate。"
            )
        elif action == "stop":
            title = "正常停止"
            detail = (
                "會停止新進場、撤銷尚未完成的進場單，"
                "若已有部位則走既有正常 EXIT 流程後停止。"
            )
        elif action == "clear-halt":
            title = "解除交易 HALT"
            detail = (
                "會連線券商重新檢查未成交單、實際部位、baseline、"
                "local orders 與 strategy positions；只有全部安全一致才會解除 HALT。"
            )
        elif action == "sync-baseline":
            title = "同步既有庫存基準"
            detail = (
                "只會在 runtime 已停止、STOP/HALT 未啟動、券商沒有未成交委託，"
                "且本機沒有策略部位或進行中委託時，讀取元大正式帳戶實際庫存並"
                "更新 position baseline。此操作不會開啟 LIVE，也不會送單。"
            )
        else:
            title = "緊急停止"
            detail = (
                "會建立 EMERGENCY_STOP；"
                "若交易 runtime 正在運作，將由既有 emergency exit 流程處理。"
            )

        return (
            f"⚠️【{title}確認】\n"
            f"{detail}\n\n"
            f"確認碼：{code}\n"
            f"請在 {self.confirm_ttl_seconds} 秒內傳送：\n"
            f"/confirm {code}\n\n"
            "未確認前不會執行。"
        )

    def _confirm(self, message: dict[str, Any], update_id: int) -> str:
        pending = self.pending
        if pending is None:
            return "目前沒有待確認的遠端控制指令。"

        if time.monotonic() > pending.expires_at_monotonic:
            action = pending.action
            self.pending = None
            _audit_control(
                self.runtime_dir,
                "REMOTE_CONTROL_CONFIRMATION_EXPIRED",
                action=action,
                update_id=update_id,
                outcome="EXPIRED",
            )
            return (
                "確認已逾時，指令沒有執行。"
                "請重新送出 /start、/stop、/kill、/clear-halt 或 /sync-baseline。"
            )

        raw = str(message.get("text", "")).strip()
        parts = raw.split()

        supplied = parts[1] if len(parts) == 2 else ""
        if (
            len(supplied) != 4
            or not supplied.isdigit()
            or supplied != pending.code
        ):
            pending.attempts_remaining -= 1
            _audit_control(
                self.runtime_dir,
                "REMOTE_CONTROL_CONFIRMATION_FAILED",
                action=pending.action,
                update_id=update_id,
                outcome="INVALID_CODE",
            )

            if pending.attempts_remaining <= 0:
                action = pending.action
                self.pending = None
                _audit_control(
                    self.runtime_dir,
                    "REMOTE_CONTROL_CONFIRMATION_CANCELED",
                    action=action,
                    update_id=update_id,
                    outcome="TOO_MANY_ATTEMPTS",
                )
                return "確認碼錯誤次數過多，這次控制要求已取消。"

            return (
                "確認碼不正確，指令尚未執行。"
                f"剩餘 {pending.attempts_remaining} 次確認機會。"
            )

        # Consume the pending action before execution so the same
        # confirmation can never execute a dangerous action twice.
        action = pending.action
        self.pending = None

        _audit_control(
            self.runtime_dir,
            "REMOTE_CONTROL_CONFIRMED",
            action=action,
            update_id=update_id,
        )

        if action == "start":
            ok, status = _launch_runtime_start(self.runtime_dir)
        elif action == "sync-baseline":
            ok, status = _sync_position_baseline(self.runtime_dir)
        else:
            ok, status = _invoke_runtime_control(action, self.runtime_dir)

        _audit_control(
            self.runtime_dir,
            "REMOTE_CONTROL_EXECUTED",
            action=action,
            update_id=update_id,
            outcome=(
                status
                if action in {"start", "sync-baseline"}
                else ("SUCCESS" if ok else "FAILED")
            ),
        )

        if action == "start":
            if status == "RUNTIME_ALREADY_RUNNING":
                return "ℹ️ Realtime runtime 已經在執行，沒有啟動第二個 instance。"
            if status == "STOP_REQUEST_ACTIVE":
                return (
                    "⚠️ 偵測到尚未解除的 STOP_REQUEST，沒有啟動 LIVE runtime。\n"
                    "請先用 /status 確認 runtime 狀態並處理停止要求。"
                )
            if status == "START_FAILED_BROKER_HALTED":
                return (
                    "⚠️ LIVE runtime 啟動失敗：券商執行狀態仍為 HALT。\n"
                    "沒有開始監控市場，也沒有送出委託。請先用 /status 檢查，"
                    "確認實際庫存與未成交委託一致後，再執行 /clear-halt。"
                )
            if status == "START_FAILED_QUOTE_SUBSCRIPTION":
                return (
                    "⚠️ LIVE runtime 啟動失敗：正式行情訂閱未完成。\n"
                    "沒有開始監控市場，也沒有送出委託。請用 /status 查看失敗階段。"
                )
            if status == "START_FAILED_UNSAFE":
                return (
                    "⚠️ LIVE runtime 啟動失敗並已安全停止。\n"
                    "沒有開始監控市場；請用 /status 查看異常原因與失敗階段。"
                )
            if not ok:
                return (
                    "⚠️ LIVE runtime 啟動要求被拒絕或立即結束。\n"
                    "既有 LIVE gate、EMERGENCY_STOP、singleton lock、"
                    "reconciliation / halt 等安全機制都沒有被繞過。"
                )
            if status == "START_CONFIRMED_RUNNING":
                return (
                    "✅ LIVE runtime 已確認啟動，正在監控市場。\n"
                    "券商登入、庫存對帳及行情訂閱均已通過；可用 /status 再次確認。"
                )
            return (
                "⏳ LIVE runtime 仍在啟動，尚未確認已開始監控市場。\n"
                "請稍後用 /status 確認；只有顯示「目前監控市場：是」才代表完成啟動。"
            )

        if not ok:
            if action == "sync-baseline":
                if status == "RUNTIME_ALREADY_RUNNING":
                    return "⚠️ Runtime 仍在執行，沒有同步庫存基準。請先正常停止。"
                if status == "STOP_REQUEST_ACTIVE":
                    return "⚠️ STOP_REQUEST 仍有效，沒有同步庫存基準。"
                if status == "EMERGENCY_STOP_ACTIVE":
                    return "⚠️ EMERGENCY_STOP / HALT 仍有效，沒有同步庫存基準。"
                return (
                    "⚠️ 庫存基準同步失敗。可能有券商未成交委託、本機策略部位／"
                    "進行中委託、PROD 登入或帳務查詢失敗。原基準未被安全更新。"
                )
            return (
                "⚠️ 控制指令執行失敗。"
                "安全機制沒有被繞過，請使用 /status 檢查 runtime 狀態。"
            )

        if action == "stop":
            if status == "RUNTIME_NOT_RUNNING":
                return "ℹ️ Runtime 目前沒有執行，因此沒有建立 STOP_REQUEST。"
            return (
                "✅ 已送出正常停止要求。\n"
                "Runtime 將停止新進場；若有曝險，會先依既有 EXIT 流程處理。"
            )

        if action == "clear-halt":
            if status == "HALT_CLEARED":
                return (
                    "✅ HALT 已解除。\n"
                    "既有券商未成交單、實際部位、baseline 與 local strategy state "
                    "已通過 clear-halt 的安全檢查。"
                )
            return (
                "⚠️ HALT 沒有解除。\n"
                "安全檢查未通過或控制程序失敗；原 HALT 狀態不應被繞過。"
            )

        if action == "sync-baseline":
            return (
                "✅ 既有庫存基準已依元大正式帳戶實際庫存更新。\n"
                "同步時已確認 runtime 停止、券商無未成交委託，且本機沒有策略部位或"
                "進行中委託。此操作沒有開啟 LIVE，也沒有送單。"
            )

        return (
            "🚨 已送出緊急停止要求。\n"
            "EMERGENCY_STOP 已建立；若 runtime 正在運作，"
            "將由既有 emergency exit 流程接手。"
        )

    def handle(self, message: dict[str, Any], update_id: int) -> str | None:
        command = _command_text(message)

        if command == "/start":
            ok, status = _run_start_preflight(self.runtime_dir)

            _audit_control(
                self.runtime_dir,
                "REMOTE_START_PREFLIGHT",
                action="start",
                update_id=update_id,
                outcome=status,
            )

            if not ok:
                if status == "RUNTIME_ALREADY_RUNNING":
                    return "ℹ️ Realtime runtime 已經在執行，沒有啟動第二個 instance。"

                if status == "STOP_REQUEST_ACTIVE":
                    return (
                        "⚠️ LIVE 啟動前置檢查未通過：STOP_REQUEST 仍有效。\n"
                        "未產生確認碼，也沒有啟動 runtime。"
                    )

                if status == "EMERGENCY_STOP_ACTIVE":
                    return (
                        "⚠️ LIVE 啟動前置檢查未通過：EMERGENCY_STOP / HALT 仍有效。\n"
                        "未產生確認碼，也沒有啟動 runtime。"
                    )

                if status == "BASELINE_MISSING":
                    return (
                        "⚠️ LIVE 啟動前置檢查未通過：position baseline 不存在。\n"
                        "未產生確認碼，也沒有啟動 runtime。"
                    )

                if status == "BROKER_EXECUTION_HALTED":
                    return (
                        "⚠️ LIVE 啟動前置檢查未通過：券商執行狀態仍為 HALT。\n"
                        "未產生確認碼，也沒有啟動 runtime。請先用 /status 檢查，"
                        "確認實際庫存與未成交委託一致後，再執行 /clear-halt。"
                    )

                return (
                    "⚠️ LIVE 啟動前置檢查未通過。\n"
                    "可能是非交易日、Stage A 日期不符、PROD 登入/行情/對帳失敗，"
                    "或其他 fail-closed 條件。未產生確認碼，也沒有啟動 runtime。"
                )

            return self._request("start", update_id)

        if command == "/stop":
            return self._request("stop", update_id)

        if command == "/kill":
            return self._request("kill", update_id)

        if command == "/clear-halt":
            return self._request("clear-halt", update_id)

        if command == "/sync-baseline":
            if _runtime_process_active(self.runtime_dir):
                return "⚠️ Runtime 仍在執行，請先正常停止後再同步庫存基準。"
            if (self.runtime_dir / "STOP_REQUEST").exists():
                return "⚠️ STOP_REQUEST 仍有效，暫不允許同步庫存基準。"
            if (self.runtime_dir / "EMERGENCY_STOP").exists():
                return "⚠️ EMERGENCY_STOP / HALT 仍有效，暫不允許同步庫存基準。"
            return self._request("sync-baseline", update_id)

        if command == "/confirm":
            return self._confirm(message, update_id)

        return None


def _supervisor_thread(runtime_dir: Path, stop_event: threading.Event) -> threading.Thread:
    def run() -> None:
        try:
            force_flat_scheduler_loop(runtime_dir, stop_event=stop_event)
        except BaseException as exc:
            print(f"CRITICAL: safety supervisor stopped: {type(exc).__name__}", flush=True)
    thread = threading.Thread(target=run, name="scheduled-force-flat-supervisor", daemon=True)
    thread.start()
    return thread


def _supervisor_thread_healthy(thread: threading.Thread, runtime_dir: Path, started_at: float) -> bool:
    if not thread.is_alive():
        return False
    elapsed = time.monotonic() - started_at
    startup_grace = 0 <= elapsed <= SUPERVISOR_STARTUP_GRACE_SECONDS
    payload = _read_json(Path(runtime_dir) / "supervisor_heartbeat.json")
    if payload is None:
        return startup_grace
    try:
        if int(payload.get("pid", 0)) != os.getpid():
            # Ordinary service restarts retain the old heartbeat. Give the
            # newly alive thread a bounded chance to publish its own identity.
            return startup_grace
        age = _age_seconds(payload.get("at"))
        return age is not None and 0 <= age <= SUPERVISOR_HEARTBEAT_STALE_SECONDS
    except (ValueError, TypeError, OverflowError):
        return False


def serve(runtime_dir: Path, *, poll_timeout: int = 25,
          stop_event: threading.Event | None = None) -> int:
    # Telegram/Keychain availability must not be a prerequisite for the
    # explicitly enabled safety supervisor. Polling reloads credentials below.
    offset = _load_offset(runtime_dir)
    controls = _RemoteControl(runtime_dir)
    force_flat_thread = None
    supervisor_stop = threading.Event()
    supervisor_started = time.monotonic()
    if os.environ.get(SCHEDULE_GATE, "").strip().upper() == "YES":
        force_flat_thread = _supervisor_thread(runtime_dir, supervisor_stop)
    else:
        print(
            "Scheduled force-flat supervisor disabled by explicit gate.",
            flush=True,
        )
    try:
        notification_worker = AsyncTradingNotifier(runtime_dir)
    except Exception as exc:
        print(f"CRITICAL: notification outbox unavailable: {type(exc).__name__}", flush=True)
        notification_worker = RuntimeNotifier(runtime_dir)
    print(
        "Trading Bot service started. Commands: "
        "/status /start /stop /kill /clear-halt /sync-baseline /confirm /help",
        flush=True,
    )

    while stop_event is None or not stop_event.is_set():
        try:
            if force_flat_thread is not None and not _supervisor_thread_healthy(force_flat_thread, runtime_dir, supervisor_started):
                notification_worker.critical(
                    "SAFETY_SUPERVISOR_UNHEALTHY",
                    "Safety supervision stopped or stalled; Bot is exiting nonzero so its existing service manager can restart it",
                )
                supervisor_stop.set()
                notification_worker.close(timeout=0.25)
                return 2
            # Long-running Telegram polls must not retain stale credentials
            # after a Keychain update. Safety supervision runs independently.
            token, configured_chat_id = load_trading_bot_credentials()
            query = urllib.parse.urlencode(
                {
                    "timeout": poll_timeout,
                    "offset": offset,
                    "allowed_updates": json.dumps(["message"]),
                }
            )
            response = _telegram_json(
                token,
                f"getUpdates?{query}",
                timeout=poll_timeout + 10,
            )
            updates = response.get("result", [])
            if not isinstance(updates, list):
                updates = []

            for update in updates:
                try:
                    update_id = int(update.get("update_id"))
                except Exception:
                    continue

                if update_id < offset:
                    continue

                offset = update_id + 1
                _save_offset(runtime_dir, offset)

                message = update.get("message")
                if not isinstance(message, dict):
                    continue
                chat = message.get("chat")
                if not isinstance(chat, dict):
                    continue
                if str(chat.get("id")) != configured_chat_id:
                    continue
                if chat.get("type") != "private":
                    continue

                command = _command_text(message)

                control_reply = controls.handle(message, update_id)
                if control_reply is not None:
                    _send_message(
                        token,
                        configured_chat_id,
                        control_reply,
                    )
                elif command == "/status":
                    _send_message(
                        token,
                        configured_chat_id,
                        render_status(build_status(runtime_dir)),
                    )
                elif command == "/help":
                    _send_message(
                        token,
                        configured_chat_id,
                        (
                            "【WarrantScope Trading Bot】\n"
                            "/status：查看交易 runtime 即時狀態\n"
                            "/start：先跑 PROD preflight，通過後才提供 LIVE 確認碼\n"
                            "/stop：要求正常停止（需要確認碼）\n"
                            "/kill：要求緊急停止（需要確認碼）\n"
                            "/clear-halt：安全檢查後解除 HALT（需要確認碼）\n"
                            "/sync-baseline：以元大實際庫存更新既有庫存基準（需要確認碼）\n"
                            "/confirm 1234：確認待執行的控制指令\n"
                            "/help：顯示指令\n\n"
                            "所有遠端控制都沿用既有 runtime 安全機制；"
                            "只有 /start 通過 PROD preflight 並完成確認後，"
                            "才會對該次 runtime 子程序暫時開啟三道 LIVE gate。"
                        ),
                    )

            time.sleep(0.05)
        except KeyboardInterrupt:
            print("Trading Bot status service stopped.", flush=True)
            supervisor_stop.set()
            notification_worker.close(timeout=0.25)
            if force_flat_thread is not None:
                force_flat_thread.join(timeout=1)
            return 0
        except Exception as exc:
            print(
                f"Trading Bot polling warning: {type(exc).__name__}",
                flush=True,
            )
            time.sleep(5)
    supervisor_stop.set()
    notification_worker.close(timeout=0.25)
    if force_flat_thread is not None:
        force_flat_thread.join(timeout=1)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Guarded WarrantScope Trading Bot status and remote-control service"
    )
    parser.add_argument("command", choices=("serve", "status-local", "send-status"))
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--poll-timeout", type=int, default=25)
    args = parser.parse_args(argv)

    runtime_dir = args.runtime_dir.resolve()

    if args.command == "status-local":
        print(render_status(build_status(runtime_dir)))
        return 0

    if args.command == "send-status":
        token, chat_id = load_trading_bot_credentials()
        _send_message(token, chat_id, render_status(build_status(runtime_dir)))
        print("TRADING_STATUS_SENT")
        return 0

    return serve(runtime_dir, poll_timeout=max(5, min(args.poll_timeout, 50)))


if __name__ == "__main__":
    raise SystemExit(main())
