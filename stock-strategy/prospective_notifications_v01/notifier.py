from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

from shadow_daily_runner.io_utils import process_lock


RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
LEDGER = RUNTIME_DIR / "notification_ledger.jsonl"
ENTRY_STATE_RUNTIME = Path(__file__).resolve().parents[1] / "stage_a_t1_extreme_upside_study_v01" / "runtime" / "seals"
ENTRY_STATE_ORDER = ("READY", "WATCH", "COOLING_BUT_WEAK", "OVERHEATED")
TELEGRAM_MAX_ATTEMPTS = 3
TELEGRAM_RETRY_DELAYS_SECONDS = (1.0, 3.0)


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


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float:
    """Return a bounded Telegram retry delay without logging response content."""

    raw = exc.headers.get("Retry-After") if exc.headers is not None else None
    try:
        return min(10.0, max(1.0, float(raw))) if raw is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


def _telegram(message: str) -> tuple[str, int | None, str | None, int]:
    token = os.environ.get("WARRANTSCOPE_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("WARRANTSCOPE_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return "NOT_CONFIGURED", None, "MISSING_CREDENTIALS", 0
    payload = json.dumps({"chat_id": chat_id, "text": message}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    last_code: int | None = None
    last_error: str | None = None
    for attempt in range(1, TELEGRAM_MAX_ATTEMPTS + 1):
        retry_delay: float | None = None
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read(4096))
                if response.status == 200 and body.get("ok") is True:
                    return "SUCCESS", response.status, None, attempt
                last_code = response.status
                last_error = "INVALID_API_RESPONSE"
                retry_delay = TELEGRAM_RETRY_DELAYS_SECONDS[min(attempt - 1, len(TELEGRAM_RETRY_DELAYS_SECONDS) - 1)]
        except urllib.error.HTTPError as exc:
            last_code = exc.code
            if exc.code == 429:
                last_error = "RATE_LIMITED"
                retry_delay = _retry_after_seconds(exc)
            elif 500 <= exc.code <= 599:
                last_error = "HTTP_SERVER_ERROR"
                retry_delay = TELEGRAM_RETRY_DELAYS_SECONDS[min(attempt - 1, len(TELEGRAM_RETRY_DELAYS_SECONDS) - 1)]
            else:
                return "FAILED", exc.code, "HTTP_CLIENT_ERROR", attempt
        except urllib.error.URLError:
            last_error = "NETWORK_ERROR"
            retry_delay = TELEGRAM_RETRY_DELAYS_SECONDS[min(attempt - 1, len(TELEGRAM_RETRY_DELAYS_SECONDS) - 1)]
        except TimeoutError:
            last_error = "TIMEOUT"
            retry_delay = TELEGRAM_RETRY_DELAYS_SECONDS[min(attempt - 1, len(TELEGRAM_RETRY_DELAYS_SECONDS) - 1)]
        except (ValueError, OSError):
            last_error = "INVALID_RESPONSE_OR_IO_ERROR"
            retry_delay = TELEGRAM_RETRY_DELAYS_SECONDS[min(attempt - 1, len(TELEGRAM_RETRY_DELAYS_SECONDS) - 1)]

        if attempt < TELEGRAM_MAX_ATTEMPTS and retry_delay is not None:
            time.sleep(retry_delay)
    return "FAILED", last_code, last_error, TELEGRAM_MAX_ATTEMPTS


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
                delivery = _telegram(message) if provider == "TELEGRAM" else _macos(message)
                status, response_code = delivery[:2]
                error_category = delivery[2] if len(delivery) >= 3 else None
                attempt_count = delivery[3] if len(delivery) >= 4 else 1
            except Exception:
                status, response_code = "FAILED", None
                error_category, attempt_count = "UNEXPECTED_DELIVERY_ERROR", 1
            record = {
                "module": module, "signal_date": signal_date, "seal_hash": seal_hash,
                "notification_type": notification_type, "provider": provider,
                "attempted_at": datetime.now(timezone.utc).isoformat(),
                "status": status, "response_code": response_code,
                "error_category": error_category,
                "attempt_count": attempt_count,
                "message_digest": _digest(message),
            }
            _append(ledger, record)
            results[provider] = status
    return results


def load_entry_state(date: str, stage_seal_hash: str, *, runtime: Path = ENTRY_STATE_RUNTIME) -> dict:
    from stage_a_t1_extreme_upside_study_v01.entry_state import digest

    path = runtime / f"{date}.json"
    if not path.is_file():
        raise FileNotFoundError(f"entry-state seal absent for {date}")
    value = json.loads(path.read_text(encoding="utf-8"))
    fields = (
        "schema_version", "signal_date", "mode", "evidence_label", "rules",
        "config_hash", "input_hash", "stage_a_seal_hash", "stocks",
        "actual_orders", "actual_fills", "broker_connections",
    )
    content = {key: value.get(key) for key in fields}
    if value.get("seal_hash") != digest(content):
        raise RuntimeError("entry-state seal hash mismatch")
    if value.get("signal_date") != date or value.get("stage_a_seal_hash") != stage_seal_hash:
        raise RuntimeError("entry-state seal does not match Stage A signal seal")
    if len(value.get("stocks", [])) != 30:
        raise RuntimeError("entry-state seal must contain exactly 30 stocks")
    return value


def _classification_lines(entry_state: dict) -> list[str]:
    groups = {label: [] for label in ENTRY_STATE_ORDER}
    for row in entry_state["stocks"]:
        label = row.get("classification")
        if label not in groups:
            raise RuntimeError(f"unknown frozen entry-state classification: {label}")
        groups[label].append(f"{row['stock_id']} {row['stock_name']}")
    lines = ["固定 Entry State（不受籌碼影響）："]
    for label in ENTRY_STATE_ORDER:
        names = "、".join(groups[label]) if groups[label] else "無"
        lines.append(f"{label}（{len(groups[label])}）：{names}")
    return lines


def entry_state_message(date: str, entry_state: dict) -> str:
    label = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    lines = [
        f"【Stage A Entry State｜{label}】",
        "分類封存完成",
        "Stage A Top30 排行：",
    ]
    for row in sorted(entry_state["stocks"], key=lambda item: int(item["stage_a_rank"])):
        lines.append(
            f"{int(row['stage_a_rank'])}. {row['stock_id']} {row['stock_name']} "
            f"{float(row['stage_a_score']):.4f}"
        )
    lines.extend(_classification_lines(entry_state))
    lines.extend([
        f"classification seal：{entry_state['seal_hash'][:12]}",
        "分類只由封存價量／ATR／均線決定；籌碼更新不會回寫分類。",
        "SHADOW_ONLY｜觀察分類，不是買進或放空訊號",
    ])
    return "\n".join(lines)


def daily_message(date: str, scan: dict, stage: dict, entry_state: dict | None = None) -> str:
    if stage.get("count") != 30 or stage.get("status") not in ("SEALED", "ALREADY_SEALED"):
        raise ValueError("Stage A Top30 must be sealed with exactly 30 rows")
    label = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    raw = scan.get("raw_n_retest_count", scan.get("raw_n_count", 0))
    compact = scan.get("compact_count", scan.get("compact_signal_count", 0))
    lines = [
        f"【WarrantScope Daily｜{label}】",
        "今日封存完成",
        f"N 結構掃描：Raw N_RETEST {raw}｜N Compact {compact}｜seal {str(scan.get('record_hash', ''))[:12]}",
        f"Stage A：完整 Top30 已封存｜seal {stage['seal_hash'][:12]}",
        "Stage A Top30 完整名單：",
    ]
    for row in stage["stocks"]:
        lines.append(f"{row['rank']}. {row['stock_id']} {row['stock_name']} {row['score']:.4f}")
    if entry_state is not None:
        if entry_state.get("stage_a_seal_hash") != stage["seal_hash"]:
            raise ValueError("entry-state classification does not match Stage A seal")
        lines.extend(_classification_lines(entry_state))
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
    lines.append("觀察名單，不是買進訊號｜SHADOW_ONLY｜無下單")
    return "\n".join(lines)


def chip_watch_message(
    date: str,
    candidates: list[dict],
    source_hash: str,
    *,
    ready_sources: list[str] | None = None,
    missing_sources: list[str] | None = None,
) -> str:
    """Clearly-labelled research watchlist; never call chip data a validated limit predictor."""
    label = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    display = {
        "TWSE_INSTITUTIONAL": "TWSE 法人", "TPEX_INSTITUTIONAL": "TPEx 法人",
        "TWSE_MARGIN": "TWSE 融資融券", "TPEX_MARGIN": "TPEx 融資融券",
    }
    ready_sources = ready_sources or []
    missing_sources = missing_sources or []
    coverage = "完整四來源" if not missing_sources else "部分來源"
    lines = [
        f"【籌碼更新｜{label}】",
        f"資料狀態：{coverage}",
        "已取得：" + ("、".join(display.get(value, value) for value in ready_sources) or "未標示"),
        "缺少：" + ("、".join(display.get(value, value) for value in missing_sources) or "無"),
        "明日漲停觀察候選（未驗證、非交易訊號）：",
        "固定規則：三大法人合計買超的 Stage A 股票中，依原排名取前 5 檔。",
    ]
    if candidates:
        for index, row in enumerate(candidates, 1):
            tags = "、".join(row.get("chip_tags", [])) or "籌碼中性"
            lines.append(f"{index}. {row['stock_id']} {row['stock_name']}｜{row['classification']}｜{tags}")
    else:
        lines.append("無符合事前固定觀察規則的候選")
    lines.extend([
        f"chip source hash：{source_hash[:12]}",
        "缺少來源不以舊資料補值；Entry State 分類只顯示、不決定入選。",
        "歷史研究未證明籌碼可穩定預測隔日漲停；請以開盤價差與盤中量價再確認。",
    ])
    return "\n".join(lines)


def chip_not_ready_message(date: str, reason: str) -> str:
    label = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    source = "官方四來源尚未全部就緒"
    for name, display in (
        ("TWSE_INSTITUTIONAL", "TWSE 法人"),
        ("TPEX_INSTITUTIONAL", "TPEx 法人"),
        ("TWSE_MARGIN", "TWSE 融資融券"),
        ("TPEX_MARGIN", "TPEx 融資融券"),
    ):
        if name in reason:
            source = display
            break
    return "\n".join([
        f"【籌碼更新尚未完成｜{label}】",
        "晚間有限次排程已執行。",
        f"未完成來源：{source}",
        "基於 fail-closed，未產生或發送明日漲停觀察候選。",
        "沒有使用舊資料，也沒有修改 Stage A／Entry State 封存。",
    ])


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
