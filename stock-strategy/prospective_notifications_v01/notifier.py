from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

from shadow_daily_runner.io_utils import process_lock


RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
LEDGER = RUNTIME_DIR / "notification_ledger.jsonl"


def _digest(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _records(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _telegram(message: str) -> tuple[str, int | None]:
    token = os.environ.get("WARRANTSCOPE_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("WARRANTSCOPE_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return "NOT_CONFIGURED", None
    payload = json.dumps({"chat_id": chat_id, "text": message}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read(4096))
            return ("SUCCESS" if response.status == 200 and body.get("ok") is True else "FAILED"), response.status
    except urllib.error.HTTPError as exc:
        return "FAILED", exc.code
    except (urllib.error.URLError, TimeoutError, ValueError):
        return "FAILED", None


def _macos(message: str) -> tuple[str, int | None]:
    if not Path("/usr/bin/osascript").is_file():
        return "NOT_CONFIGURED", None
    script = 'on run argv\n display notification (item 1 of argv) with title "WarrantScope"\nend run'
    result = subprocess.run(["/usr/bin/osascript", "-e", script, message], capture_output=True, timeout=10, check=False)
    return ("SUCCESS" if result.returncode == 0 else "FAILED"), result.returncode


def notify(module: str, signal_date: str, seal_hash: str, notification_type: str, message: str, *, ledger: Path = LEDGER, providers: tuple[str, ...] = ("TELEGRAM", "MACOS_LOCAL_NOTIFICATION")) -> dict:
    if len(message) > 4096:
        raise ValueError("Telegram message exceeds Bot API limit")
    results: dict[str, str] = {}
    with process_lock(ledger.parent / ".notification.lock"):
        previous = _records(ledger)
        for provider in providers:
            key = (module, signal_date, seal_hash, notification_type, provider)
            if any((row.get("module"), row.get("signal_date"), row.get("seal_hash"), row.get("notification_type"), row.get("provider")) == key and row.get("status") == "SUCCESS" for row in previous):
                results[provider] = "ALREADY_SENT"
                continue
            try:
                status, response_code = _telegram(message) if provider == "TELEGRAM" else _macos(message)
            except Exception:
                status, response_code = "FAILED", None
            record = {
                "module": module, "signal_date": signal_date, "seal_hash": seal_hash,
                "notification_type": notification_type, "provider": provider,
                "attempted_at": datetime.now(timezone.utc).isoformat(),
                "status": status, "response_code": response_code,
                "message_digest": _digest(message),
            }
            _append(ledger, record)
            results[provider] = status
    return results


def daily_message(date: str, scan: dict, stage: dict) -> str:
    if stage.get("count") != 30 or stage.get("status") not in ("SEALED", "ALREADY_SEALED"):
        raise ValueError("Stage A Top30 must be sealed with exactly 30 rows")
    label = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    raw = scan.get("raw_n_retest_count", scan.get("raw_n_count", 0))
    compact = scan.get("compact_count", scan.get("compact_signal_count", 0))
    lines = [f"【WarrantScope Daily｜{label}】", "今日封存完成", f"N Compact：Raw N {raw}｜Compact {compact}｜seal {str(scan.get('record_hash', ''))[:12]}", f"Stage A：Top30 SEALED｜seal {stage['seal_hash'][:12]}", "Top10："]
    for row in stage["stocks"][:10]:
        lines.append(f"{row['rank']}. {row['stock_id']} {row['stock_name']} {row['score']:.4f}")
    if int(compact) > 0:
        lines.append("N Compact candidates：")
        for row in scan.get("compact_candidates", []):
            lines.append(
                f"{row.get('stock_id')} {row.get('stock_name')}｜Close {row.get('signal_close')}"
                f"｜Pivot {row.get('first_pivot_date')}/{row.get('second_pivot_date')}"
                f"｜間距 {row.get('pivot_separation_sessions')}｜底差 {row.get('bottom_difference')}"
                f"｜中間反彈 {row.get('intervening_bounce')}｜確認反彈 {row.get('confirmation_rebound')}"
            )
    else:
        lines.append("N Compact status：NO_SIGNAL")
    lines.append("SHADOW_ONLY｜無下單")
    return "\n".join(lines)


def warning_message(date: str, module: str, error_type: str, detail: str = "") -> str:
    clean = " ".join(str(detail).split())[:180]
    for marker in ("/Users/", "bot", "token", "chat_id", "Traceback"):
        if marker.lower() in clean.lower():
            clean = "詳情請見本機 log"
            break
    return f"【WarrantScope WARNING】\n日期：{date[:4]}-{date[4:6]}-{date[6:]}\n模組：{module}\n錯誤：{error_type}\n{clean}".strip()


def latest_status(ledger: Path = LEDGER) -> dict:
    records = _records(ledger)
    recent = records[-1] if records else None
    warning = next((row for row in reversed(records) if row.get("notification_type") == "WARNING"), None)
    success = next((row for row in reversed(records) if row.get("status") == "SUCCESS" and row.get("notification_type") == "SEALED_DAILY"), None)
    return {"last_notification": recent, "last_warning": warning, "last_successful_run": success, "records": len(records), "recent_records": records[-20:]}
