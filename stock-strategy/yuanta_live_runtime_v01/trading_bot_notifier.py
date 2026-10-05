from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable
import uuid

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
    "EXTERNAL_MANUAL_INVENTORY_ADOPTED",
}


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    outcome: str
    retry_after_seconds: float = 0.0


def _telegram_send(token: str, chat_id: str, message: str) -> DeliveryResult:
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
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        # Exception text may contain the bot-token URL. Persist only a class.
        return DeliveryResult(False, f"DELIVERY_UNKNOWN_{type(exc).__name__}")
    if not isinstance(value, dict) or value.get("ok") is not True:
        retry_after = 0.0
        if isinstance(value, dict):
            try:
                retry_after = max(0.0, float(value.get("parameters", {}).get("retry_after", 0)))
            except (TypeError, ValueError, AttributeError):
                pass
        return DeliveryResult(False, "API_REJECTED", retry_after)
    return DeliveryResult(True, "API_CONFIRMED")


class NotificationOutbox:
    """Durable at-least-once delivery, with a cross-process SQLite lease.

    An API confirmation is not proof the person read the alert. A timeout may
    already have delivered a message; retry can duplicate it. Credentials and
    HTTP exception/response bodies are never persisted.
    """
    def __init__(self, runtime_dir: Path):
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.runtime_dir / "notification_outbox.sqlite"
        self.ledger = self.runtime_dir / "notification_delivery.jsonl"
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS notifications (
                notification_id TEXT PRIMARY KEY, event TEXT NOT NULL,
                message TEXT NOT NULL, created_at REAL NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0,
                lease_owner TEXT, outcome TEXT, updated_at REAL NOT NULL)""")
        os.chmod(self.path, 0o600)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def enqueue(self, event: str, message: str, *, key: str | None = None) -> str:
        now = time.time()
        identifier = hashlib.sha256(key.encode()).hexdigest() if key else uuid.uuid4().hex
        with self._connect() as db:
            db.execute("""INSERT OR IGNORE INTO notifications
                (notification_id,event,message,created_at,status,next_attempt_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""", (identifier, str(event), str(message), now, "PENDING", now, now))
        return identifier

    def _claim(self, now: float) -> dict | None:
        owner = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM notifications WHERE
                (status IN ('PENDING','RETRY') AND next_attempt_at<=?) OR
                (status='SENDING' AND lease_until<=?)
                ORDER BY created_at LIMIT 1""", (now, now)).fetchone()
            if row is None:
                return None
            db.execute("""UPDATE notifications SET status='SENDING', attempts=attempts+1,
                lease_owner=?,lease_until=?,updated_at=? WHERE notification_id=?""",
                (owner, now + 60, now, row["notification_id"]))
            claimed = dict(row)
            claimed.update(lease_owner=owner, attempts=int(row["attempts"]) + 1)
            return claimed

    def deliver_once(self, *, now: float | None = None,
                     credential_loader: Callable | None = None,
                     sender: Callable | None = None) -> DeliveryResult | None:
        credential_loader = credential_loader or load_trading_bot_credentials
        sender = sender or _telegram_send
        current = time.time() if now is None else float(now)
        row = self._claim(current)
        if row is None:
            return None
        try:
            # Reload every attempt: a temporarily missing Keychain item must
            # not make this process discard its entire future notification feed.
            token, chat_id = credential_loader()
        except Exception as exc:
            result = DeliveryResult(False, f"CREDENTIALS_UNAVAILABLE_{type(exc).__name__}")
        else:
            try:
                result = sender(token, chat_id, row["message"])
                if not isinstance(result, DeliveryResult):
                    result = DeliveryResult(False, "DELIVERY_UNCONFIRMED")
            except Exception as exc:
                result = DeliveryResult(False, f"DELIVERY_UNKNOWN_{type(exc).__name__}")
        retry = max(result.retry_after_seconds, min(300.0, 2 ** min(row["attempts"], 8)))
        status = "SENT" if result.delivered else "RETRY"
        finished = time.time() if now is None else current
        with self._connect() as db:
            changed = db.execute("""UPDATE notifications SET status=?,outcome=?,
                next_attempt_at=?,lease_until=0,lease_owner=NULL,updated_at=?
                WHERE notification_id=? AND lease_owner=?""",
                (status, result.outcome, finished + retry, finished,
                 row["notification_id"], row["lease_owner"])).rowcount
        if changed:
            self._record({"at": datetime.now(timezone.utc).isoformat(),
                          "notification_id": row["notification_id"], "event": row["event"],
                          "status": status, "outcome": result.outcome,
                          "attempt": row["attempts"], "retry_seconds": 0 if result.delivered else retry})
        return result

    def _record(self, row: dict) -> None:
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def snapshot(self) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT notification_id,event,status,attempts,outcome,next_attempt_at FROM notifications ORDER BY created_at")]


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
            "EXTERNAL_MANUAL_ORDER_CONFLICT": "人工委託與策略候選為同一股票",
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

    if event == "EXTERNAL_MANUAL_INVENTORY_ADOPTED":
        side = "買進" if str(row.get("side")) == "B" else "賣出"
        delta = int(row.get("delta_quantity", 0) or 0)
        return (
            "【WarrantScope｜人工成交已自動納入庫存基準】\n"
            f"股票：{row.get('symbol', '-')}\n"
            f"方向：{side}\n"
            f"本次庫存變動：{abs(delta)} 股\n"
            "策略持倉與強制平倉範圍未改變。"
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
    def __init__(self, runtime_dir: Path, *, start_worker: bool = True):
        self.runtime_dir = Path(runtime_dir)
        self.outbox = NotificationOutbox(self.runtime_dir)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._worker,
            name="warrantscope-trading-notifier",
            daemon=True,
        )
        if start_worker:
            self._thread.start()

    def emit(self, event: str, row: dict[str, Any]) -> None:
        message = format_runtime_event(event, row)
        if message:
            self.outbox.enqueue(event, message, key=f"{event}:{row.get('at')}:{message}")
            self._wake.set()

    def critical(self, event: str, message: str, *, key: str | None = None) -> str:
        identifier = self.outbox.enqueue(event, format_critical(event, message), key=key)
        self._wake.set()
        return identifier

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.outbox.deliver_once(
                    credential_loader=load_trading_bot_credentials, sender=_telegram_send)
                if result is not None:
                    continue
            except Exception as exc:
                # Disk/SQLite failure must be visible, but must not terminate
                # the primary trading or exit loop. Durable leases recover on
                # restart if a process dies after claiming an alert.
                print(f"Notification outbox warning: {type(exc).__name__}", flush=True)
            self._wake.wait(1.0)
            self._wake.clear()

    def close(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=max(0.0, timeout))


def send_critical_async(event: str, message: str, *, runtime_dir: Path | None = None) -> threading.Thread:
    """Compatibility helper; durable callers should own AsyncTradingNotifier."""
    def worker() -> None:
        try:
            outbox = NotificationOutbox(runtime_dir or Path(__file__).resolve().parent / "runtime")
            outbox.enqueue(event, format_critical(event, message))
            outbox.deliver_once(credential_loader=load_trading_bot_credentials, sender=_telegram_send)
        except Exception as exc:
            print(f"Critical notification warning: {type(exc).__name__}", flush=True)

    thread = threading.Thread(
        target=worker,
        name="warrantscope-trading-critical",
        # Critical shutdown alerts must survive the runtime process leaving its
        # main loop.  The caller retains and joins this bounded network task.
        daemon=False,
    )
    thread.start()
    return thread
