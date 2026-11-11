"""Notification-only handoff for a user-entered manual order."""

from __future__ import annotations

import hashlib

from prospective_notifications_v01.keychain import load_into_environment
from prospective_notifications_v01.notifier import notify
from yuanta_intraday_shadow_v01.collector import canonical_bytes


def manual_review_message(signal: dict) -> str:
    if signal.get("mode") != "NOTIFICATION_ONLY_MANUAL_ORDER":
        raise ValueError("manual review message requires notification-only mode")
    decision = str(signal["signal_decision_time"])
    review = str(signal["review_decision_time"])
    expiry = str(signal["manual_review_expires_at"])
    side = "做多" if signal.get("side") == "LONG" else "做空"
    lines = [
        "【WarrantScope｜人工下單審核】",
        f"{signal['stock_id']} {signal['stock_name']}｜方向：{side}",
        f"原訊號：{decision}｜原參考價 {float(signal['entry_price']):.2f}",
        f"人工審核確認：{review}",
        f"目前人工進場參考價：{float(signal['manual_entry_reference_price']):.2f}",
        f"研究參考股數：{int(signal['manual_reference_quantity']):,} 股",
        f"參考停損價：{float(signal['manual_reference_stop_price']):.2f}（成本後約 -5,000 元）",
        f"相對強度：{float(signal['relative_strength_5m']):+.2%}",
        f"三分鐘保守估值：{float(signal['conservative_net_pnl_twd']):+.0f} 元",
        f"訊號失效：{expiry}",
        "請自行查看券商即時價格與庫存後手動下單；逾時不要追價。",
        "系統沒有送單、沒有確認成交，也不會自動平倉。",
        "NOTIFICATION_ONLY｜actual_orders=0｜broker_order_calls=0",
    ]
    message = "\n".join(lines)
    if len(message) > 4096:
        raise ValueError("manual review notification exceeds Telegram limit")
    return message


def deliver_manual_review(signal: dict) -> dict:
    message = manual_review_message(signal)
    identity = {
        "stock_id": signal["stock_id"],
        "signal_decision_time": signal["signal_decision_time"],
        "review_decision_time": signal["review_decision_time"],
        "policy_hash": signal.get("policy_hash"),
        "mode": signal["mode"],
    }
    notification_hash = hashlib.sha256(canonical_bytes(identity)).hexdigest()
    day = str(signal["review_decision_time"])[:10].replace("-", "")
    keychain = load_into_environment()
    delivery = notify(
        "EXPANDED_REALTIME_MANUAL_REVIEW", day, notification_hash,
        "MANUAL_ORDER_REVIEW", message,
    )
    return {
        "credential_status": keychain, "providers": delivery,
        "notification_hash": notification_hash,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
    }
