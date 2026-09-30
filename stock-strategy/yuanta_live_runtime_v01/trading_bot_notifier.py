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
    "SIGNAL_DETECTED",
    "SIGNAL_SKIPPED",
    "ENTRY_SUBMITTED",
    "ENTRY_CANCEL_SENT",
    "ENTRY_NOT_FILLED",
    "POSITION_OPENED",
    "EXIT_SUBMITTED",
    "POSITION_CLOSED",
    "POSITION_CLOSED_AFTER_RECONCILIATION",
    "LIVE_TRADE_COMPLETE_CONTINUE_ARCHIVE",
    "SESSION_COMPLETE",
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

    def side_text(value: Any) -> str:
        return {
            "LONG": "做多",
            "SHORT": "做空",
            "BUY": "買進",
            "SELL": "賣出",
        }.get(str(value), str(value if value is not None else "-"))

    def reason_text(value: Any) -> str:
        raw = str(value or "-")
        return {
            "LIVE_TRADE_LIMIT_CONSUMED": "今日實盤交易額度已使用，後續僅記錄研究訊號",
            "OBSERVE_ONLY": "目前為觀察模式，只記錄、不下單",
            "RISK_REJECTED": "風控條件未通過",
            "MFE_PROFIT_PROTECTION": "MFE 獲利保護",
            "MAX_DAILY_LOSS": "當日最大虧損保護",
            "EMERGENCY_STOP": "緊急停止",
            "GRACEFUL_STOP": "正常停止",
            "REVERSAL_SIGNAL": "反向訊號出場",
            "EOD_FORCE_EXIT": "收盤前強制平倉",
            "HARD_STOP": "固定停損",
            "STOP_LOSS": "停損",
            "LOSS_RECOVERY_TO_PROFIT": "虧損回復轉正出場",
            "BENCHMARK_MISSING_OR_STALE": "大盤基準行情缺失或過期",
            "RELATIVE_STRENGTH_BELOW_THRESHOLD": "相對大盤強度不足",
            "ANTI_CHASE_OPENING_EXTENSION": "距離開盤價漲幅過大，防追高條件未通過",
            "ANTI_CHASE_VWAP_EXTENSION": "偏離成交均價過大，防追高條件未通過",
            "CONFIRMATIONS_INCOMPLETE": "訊號確認次數不足",
            "STOCK_5M_HISTORY_MISSING": "個股五分鐘歷史行情不足",
            "BASE_SIGNAL_CONFIRMED": "基礎訊號成立",
            "MARKET_GATE_PASSED": "市場條件通過",
        }.get(raw, f"其他原因（原始代碼：{raw}）")

    def status_text(value: Any) -> str:
        raw = str(value or "-")
        return {
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
        }.get(raw, raw)

    if event == "RUNTIME_STARTED":
        mode = "實盤" if row.get("submit_live") else "觀察"
        environment = {
            "PROD": "正式環境",
            "UAT": "測試環境",
        }.get(str(row.get("environment")), str(row.get("environment", "-")))
        return (
            f"【WarrantScope｜{mode}監控已啟動】\n"
            f"環境：{environment}\n"
            f"訊號資料日：{row.get('signal_date', '-')}\n"
            "監控範圍：Stage A Top30"
        )

    if event == "QUOTE_RECONNECT_BEGIN":
        return (
            "【WarrantScope｜行情連線異常，正在重新連線】\n"
            f"行情已逾時：{row.get('stale_seconds', '-')} 秒"
        )

    if event == "QUOTE_RECONNECT_PASSED":
        return "【WarrantScope｜行情重新連線成功】\n行情訂閱與券商對帳均已恢復正常。"

    if event == "QUOTE_RECONNECT_FAILED":
        return (
            "【WarrantScope｜行情重新連線失敗】\n"
            f"原因：{row.get('error', '-')}"
        )

    if event == "SIGNAL_DETECTED":
        candidate = row.get("candidate")
        if not isinstance(candidate, dict):
            return None

        return (
            "【WarrantScope｜偵測到交易訊號】\n"
            f"{candidate.get('stock_id', '-')} "
            f"{candidate.get('stock_name', '')}\n"
            f"方向：{side_text(candidate.get('side'))}\n"
            f"預計進場價：{candidate.get('entry_price', '-')}\n"
            f"預計數量：{candidate.get('quantity', '-')} 股\n"
            f"訊號強度：{_fmt_float(candidate.get('score'))}"
        )

    if event == "SIGNAL_SKIPPED":
        candidate = row.get("candidate")
        if not isinstance(candidate, dict):
            return None

        text = (
            "【WarrantScope｜訊號未執行下單】\n"
            f"{candidate.get('stock_id', '-')} "
            f"{candidate.get('stock_name', '')}\n"
            f"方向：{side_text(candidate.get('side'))}\n"
            f"原因：{reason_text(row.get('reason'))}"
        )

        reasons = row.get("reasons")
        if isinstance(reasons, (list, tuple)) and reasons:
            text += "\n風控原因：" + "、".join(
                reason_text(value) for value in reasons
            )

        return text

    if event == "ENTRY_SUBMITTED":
        return (
            "【WarrantScope｜進場委託已送出】\n"
            f"股票：{row.get('stock_id', '-')}\n"
            f"方向：{side_text(row.get('side'))}\n"
            f"數量：{row.get('quantity', '-')} 股\n"
            f"委託價：{row.get('price', '-')}"
        )

    if event == "ENTRY_CANCEL_SENT":
        return (
            "【WarrantScope｜正在撤銷進場委託】\n"
            f"原因：{reason_text(row.get('reason'))}"
        )

    if event == "ENTRY_NOT_FILLED":
        text = (
            "【WarrantScope｜進場委託未成交】\n"
            f"股票：{row.get('stock_id', '-')}\n"
            f"狀態：{status_text(row.get('status'))}"
        )
        if row.get("last_error"):
            text += f"\n原因：{row.get('last_error')}"
        return text

    if event == "POSITION_OPENED":
        return (
            "【WarrantScope｜進場成交】\n"
            f"股票：{row.get('stock_id', '-')}\n"
            f"方向：{side_text(row.get('side'))}\n"
            f"數量：{row.get('quantity', '-')} 股\n"
            f"成交均價：{row.get('average_fill_price', '-')}"
        )

    if event == "EXIT_SUBMITTED":
        return (
            "【WarrantScope｜出場委託已送出】\n"
            f"原因：{reason_text(row.get('reason'))}\n"
            f"數量：{row.get('quantity', '-')} 股\n"
            f"委託價：{row.get('price', '-')}\n"
            f"預估淨損益：{row.get('projected_net_pnl', '-')} 元"
        )

    if event == "POSITION_CLOSED":
        return (
            "【WarrantScope｜部位已平倉】\n"
            f"成交數量：{row.get('quantity', '-')} 股\n"
            f"成交均價：{row.get('average_fill_price', '-')}"
        )

    if event == "POSITION_CLOSED_AFTER_RECONCILIATION":
        return (
            "【WarrantScope｜部位已確認平倉】\n"
            "經券商重新對帳確認，策略部位目前已歸零。"
        )

    if event == "LIVE_TRADE_COMPLETE_CONTINUE_ARCHIVE":
        return (
            "【WarrantScope｜今日實盤交易已完成】\n"
            "今日不會再送出第二筆實盤交易；"
            "行情與研究訊號會繼續記錄至 13:25。"
        )

    if event == "SESSION_COMPLETE":
        attempted = bool(row.get("trade_attempted"))
        return (
            "【WarrantScope｜今日交易系統正常結束】\n"
            + (
                "今日已使用／嘗試一筆實盤交易，"
                if attempted
                else "今日沒有使用實盤交易額度，"
            )
            + "行情資料已正常封存。"
        )

    if event == "NO_TRADE_SESSION_COMPLETE":
        return (
            "【WarrantScope｜今日交易結束】\n"
            "今日沒有建立策略部位，系統已正常結束。"
        )

    if event == "EMERGENCY_STOP_REQUESTED":
        return (
            "【WarrantScope｜已收到緊急停止要求】\n"
            "正在依安全流程處理現有委託與部位。"
        )

    if event == "EMERGENCY_STOP_COMPLETE":
        exposure = {
            "NONE": "無策略曝險",
        }.get(str(row.get("exposure")), str(row.get("exposure", "-")))

        return (
            "【WarrantScope｜交易系統已安全停止】\n"
            f"策略曝險：{exposure}"
        )

    return None

def format_critical(event: str, message: str) -> str:
    event_text = {
        "MAX_DAILY_LOSS": "當日最大虧損保護已觸發",
        "RUNTIME_STOPPED_UNSAFE": "交易系統異常停止",
        "STALE_RUNTIME_DURING_KILL": "交易系統心跳逾時",
        "FLAT_CONFIRMATION_FAILED": "平倉狀態確認失敗",
        "QUOTE_STALE": "行情資料過期",
        "RECONCILIATION_MISMATCH": "券商對帳不一致",
    }.get(
        str(event),
        f"系統重大警示（原始代碼：{event}）",
    )

    known_message = {
        "MAX_DAILY_LOSS":
            "已達當日最大虧損限制，系統正在依安全機制處理。",
        "RUNTIME_STOPPED_UNSAFE":
            "交易程式未以正常安全狀態結束，請立即查看 /status。",
        "STALE_RUNTIME_DURING_KILL":
            "交易程式仍存在，但系統心跳已逾時；為避免第二個交易程序接管，已拒絕額外控制。",
        "FLAT_CONFIRMATION_FAILED":
            "券商或本機仍偵測到未確認的部位／委託，無法確認完全平倉。",
        "QUOTE_STALE":
            "即時行情已超過允許延遲，交易安全機制已介入。",
        "RECONCILIATION_MISMATCH":
            "券商實際狀態與本機交易基準不一致，交易已暫停。",
    }.get(str(event))

    if known_message is None:
        clean = " ".join(str(message).split())[:300]
        known_message = f"詳細資訊：{clean}"

    return (
        "【WarrantScope｜重大警示】\n"
        f"事件：{event_text}\n"
        f"{known_message}"
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
