from __future__ import annotations

import json
from pathlib import Path
from queue import Queue
import threading
import urllib.error
import urllib.request
from typing import Any

from .trading_bot_keychain import load_trading_bot_credentials

NORMAL_EVENTS = {
    "RUNTIME_STARTED",
    "QUOTE_RECONNECT_BEGIN",
    "QUOTE_RECONNECT_PASSED",
    "QUOTE_RECONNECT_FAILED",
    "RISK_APPROVED_CANDIDATE",
    "ENTRY_SUBMITTED",
    "ENTRY_CANCEL_SENT",
    "ENTRY_NOT_FILLED",
    "POSITION_OPENED",
    "EXIT_SUBMITTED",
    "POSITION_CLOSED",
    "POSITION_CLOSED_AFTER_RECONCILIATION",
    "NO_TRADE_SESSION_COMPLETE",
    "EMERGENCY_STOP_REQUESTED",
    "EMERGENCY_STOP_COMPLETE",
}


def _telegram_send(token: str, chat_id: str, message: str) -> None:
    payload = json.dumps(
        {"chat_id": chat_id, "text": message},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            value = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return
    if not isinstance(value, dict) or value.get("ok") is not True:
        return


def _fmt_float(value: Any, digits: int = 3) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value if value is not None else "-")


def format_runtime_event(event: str, row: dict[str, Any]) -> str | None:
    if event not in NORMAL_EVENTS:
        return None

    if event == "RUNTIME_STARTED":
        mode = "LIVE" if row.get("submit_live") else "OBSERVE"
        return (
            f"【WarrantScope Trading｜{mode} 啟動】\n"
            f"環境：{row.get('environment', '-')}\n"
            f"訊號日：{row.get('signal_date', '-')}\n"
            "監控：Stage A Top30"
        )

    if event == "QUOTE_RECONNECT_BEGIN":
        return (
            "【Trading｜行情重連中】\n"
            f"行情已逾時：{row.get('stale_seconds', '-')} 秒"
        )

    if event == "QUOTE_RECONNECT_PASSED":
        return "【Trading｜行情重連成功】\n行情訂閱與 reconciliation 已恢復。"

    if event == "QUOTE_RECONNECT_FAILED":
        return (
            "【Trading｜行情重連失敗】\n"
            f"原因：{row.get('error', '-')}"
        )

    if event == "RISK_APPROVED_CANDIDATE":
        candidate = row.get("candidate")
        if not isinstance(candidate, dict):
            return None
        return (
            "【Trading｜交易訊號成立】\n"
            f"{candidate.get('stock_id', '-')} {candidate.get('stock_name', '')}"
            f"｜{candidate.get('side', '-')}\n"
            f"預計價格：{candidate.get('entry_price', '-')}"
            f"｜數量：{candidate.get('quantity', '-')} 股\n"
            f"score：{_fmt_float(candidate.get('score'))}"
        )

    if event == "ENTRY_SUBMITTED":
        return (
            "【Trading｜進場委託已送出】\n"
            f"{row.get('stock_id', '-')}｜{row.get('side', '-')}\n"
            f"{row.get('quantity', '-')} 股 @ {row.get('price', '-')}"
        )

    if event == "ENTRY_CANCEL_SENT":
        return (
            "【Trading｜進場委託撤單中】\n"
            f"原因：{row.get('reason', '-')}"
        )

    if event == "ENTRY_NOT_FILLED":
        text = (
            "【Trading｜進場未成交】\n"
            f"{row.get('stock_id', '-')}｜狀態：{row.get('status', '-')}"
        )
        if row.get("last_error"):
            text += f"\n原因：{row.get('last_error')}"
        return text

    if event == "POSITION_OPENED":
        return (
            "【Trading｜已成交建倉】\n"
            f"{row.get('stock_id', '-')}｜{row.get('side', '-')}\n"
            f"{row.get('quantity', '-')} 股"
            f"｜均價 {row.get('average_fill_price', '-')}"
        )

    if event == "EXIT_SUBMITTED":
        return (
            "【Trading｜出場委託已送出】\n"
            f"原因：{row.get('reason', '-')}\n"
            f"{row.get('quantity', '-')} 股 @ {row.get('price', '-')}\n"
            f"預估淨損益：{row.get('projected_net_pnl', '-')}"
        )

    if event == "POSITION_CLOSED":
        return (
            "【Trading｜部位已平倉】\n"
            f"成交數量：{row.get('quantity', '-')}\n"
            f"成交均價：{row.get('average_fill_price', '-')}"
        )

    if event == "POSITION_CLOSED_AFTER_RECONCILIATION":
        return "【Trading｜部位已確認平倉】\n經 reconciliation 確認目前策略部位已歸零。"

    if event == "NO_TRADE_SESSION_COMPLETE":
        return "【Trading｜今日交易結束】\n今日未建立策略部位，runtime 正常結束。"

    if event == "EMERGENCY_STOP_REQUESTED":
        return "【Trading｜停止要求已收到】\n正在依安全流程處理。"

    if event == "EMERGENCY_STOP_COMPLETE":
        return (
            "【Trading｜Runtime 已安全停止】\n"
            f"策略曝險：{row.get('exposure', '-')}"
        )

    return None


def format_critical(event: str, message: str) -> str:
    clean = " ".join(str(message).split())[:300]
    return (
        "【WarrantScope Trading｜CRITICAL】\n"
        f"事件：{event}\n"
        f"{clean}"
    )


class AsyncTradingNotifier:
    def __init__(self, runtime_dir: Path):
        self.runtime_dir = Path(runtime_dir)
        self._queue: Queue[tuple[str, dict[str, Any]] | None] = Queue()
        self._thread = threading.Thread(
            target=self._worker,
            name="warrantscope-trading-notifier",
            daemon=True,
        )
        self._thread.start()

    def emit(self, event: str, row: dict[str, Any]) -> None:
        if event in NORMAL_EVENTS:
            self._queue.put((event, dict(row)))

    def _worker(self) -> None:
        try:
            token, chat_id = load_trading_bot_credentials()
        except Exception:
            while True:
                item = self._queue.get()
                self._queue.task_done()
                if item is None:
                    return

        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                event, row = item
                message = format_runtime_event(event, row)
                if message:
                    _telegram_send(token, chat_id, message)
            finally:
                self._queue.task_done()

    def close(self, timeout: float = 3.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout=timeout)


def send_critical_async(event: str, message: str) -> None:
    def worker() -> None:
        try:
            token, chat_id = load_trading_bot_credentials()
            _telegram_send(token, chat_id, format_critical(event, message))
        except Exception:
            return

    threading.Thread(
        target=worker,
        name="warrantscope-trading-critical",
        daemon=True,
    ).start()
