from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .trading_bot_keychain import load_trading_bot_credentials

MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_RUNTIME_DIR = MODULE_DIR / "runtime"


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
    return max(
        0.0,
        (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds(),
    )


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
        if state in {"RUNNING", "EMERGENCY_EXIT"}:
            if heartbeat_age is not None and heartbeat_age <= 15:
                runtime_state = (
                    "LIVE_RUNNING"
                    if bool(heartbeat.get("submit_live"))
                    else "OBSERVE_RUNNING"
                )
            else:
                runtime_state = "HEARTBEAT_STALE"
        elif state in {"STOPPED_CLEAN", "STOPPED_UNSAFE"}:
            runtime_state = state
        else:
            runtime_state = state

    if runtime_state in {"LIVE_RUNNING", "OBSERVE_RUNNING"}:
        quote_age = _age_seconds((heartbeat or {}).get("last_quote_at"))
        if quote_age is None:
            quote_health = "NO_QUOTE_YET"
        elif quote_age <= 5:
            quote_health = "FRESH"
        elif quote_age <= 30:
            quote_health = "DELAYED"
        else:
            quote_health = "STALE"
    else:
        quote_age = None
        quote_health = "N/A"

    signal_events = {"RISK_APPROVED_CANDIDATE", "RISK_REJECTED_CANDIDATE"}
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
        return f"{float(value):.1f}s"
    except Exception:
        return str(value)


def render_status(status: dict[str, Any]) -> str:
    mode = "LIVE" if status.get("submit_live") else "OBSERVE"
    lines = [
        "【WarrantScope Trading】",
        f"Runtime：{status.get('runtime_state', 'UNKNOWN')}",
        f"環境：{status.get('environment') or '-'}｜模式：{mode}",
    ]

    gate = status.get("gate_authorized")
    if gate is not None:
        lines.append(f"交易授權：{'AUTHORIZED' if gate else 'BLOCKED'}")

    lines.append(
        f"行情：{status.get('quote_health', 'UNKNOWN')}"
        f"｜Quote age：{_fmt_age(status.get('quote_age_seconds'))}"
    )
    lines.append(
        f"監控：{status.get('watchlist_count') or '-'} / 30"
        f"｜Entry：{status.get('entry_start') or '-'}"
    )
    lines.append(f"今日交易嘗試：{_yn(status.get('trade_attempted'))}")

    signal = status.get("last_signal")
    if isinstance(signal, dict):
        try:
            score_text = f"{float(signal.get('score')):.3f}"
        except Exception:
            score_text = "-"
        lines.append(
            "最後訊號："
            f"{signal.get('stock_id', '-')} {signal.get('stock_name', '')}｜"
            f"{signal.get('side', '-')}｜{signal.get('entry_price', '-')}｜"
            f"score {score_text}"
        )
    else:
        lines.append("最後訊號：NONE")

    order = status.get("last_order")
    if isinstance(order, dict):
        event = order.get("event", "-")
        status_text = order.get("status")
        lines.append(
            f"最後委託事件：{event}"
            + (f"｜{status_text}" if status_text else "")
        )
        if order.get("last_error"):
            lines.append(f"原因：{order['last_error']}")
    else:
        lines.append("最後委託事件：NONE")

    lines.append(f"Heartbeat：{_fmt_age(status.get('heartbeat_age_seconds'))} 前")
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


def serve(runtime_dir: Path, *, poll_timeout: int = 25) -> int:
    token, configured_chat_id = load_trading_bot_credentials()
    offset = _load_offset(runtime_dir)
    print("Trading Bot status service started. Commands: /status /help", flush=True)

    while True:
        try:
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

                offset = max(offset, update_id + 1)
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
                if command == "/status":
                    _send_message(
                        token,
                        configured_chat_id,
                        render_status(build_status(runtime_dir)),
                    )
                elif command == "/help":
                    _send_message(
                        token,
                        configured_chat_id,
                        "【WarrantScope Trading Bot】\n/status：查看交易 runtime 即時狀態\n/help：顯示指令",
                    )

            time.sleep(0.05)
        except KeyboardInterrupt:
            print("Trading Bot status service stopped.", flush=True)
            return 0
        except Exception as exc:
            print(
                f"Trading Bot polling warning: {type(exc).__name__}: {exc}",
                flush=True,
            )
            time.sleep(5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only WarrantScope Trading Bot status service"
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
