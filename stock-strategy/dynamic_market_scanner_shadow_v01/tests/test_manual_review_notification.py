from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.manual_review_notification import (
    deliver_manual_review, manual_review_message,
)
from dynamic_market_scanner_shadow_v01.realtime_expanded_shadow import (
    SUMMARY_NAME, RealtimeExpandedShadow, _reference_stop_price,
)


TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class Item:
    stock_id: str = "1234"
    stock_name: str = "測試股票"


def _payload() -> dict:
    return {
        "mode": "NOTIFICATION_ONLY_MANUAL_ORDER",
        "stock_id": "1234", "stock_name": "測試股票", "side": "LONG",
        "decision_time": "2026-11-11T09:11:00+08:00",
        "signal_decision_time": "2026-11-11T09:08:00+08:00",
        "review_decision_time": "2026-11-11T09:11:00+08:00",
        "entry_price": 50.0, "manual_entry_reference_price": 51.2,
        "manual_reference_quantity": 3000,
        "manual_reference_stop_price": 49.3,
        "relative_strength_5m": 0.012,
        "conservative_net_pnl_twd": 2200,
        "manual_review_expires_at": "2026-11-11T09:12:00+08:00",
        "policy_hash": "abc",
    }


class ManualReviewNotificationTests(unittest.TestCase):
    def test_message_requires_manual_order_and_has_expiry(self):
        text = manual_review_message(_payload())
        self.assertIn("手動下單", text)
        self.assertIn("逾時不要追價", text)
        self.assertIn("系統沒有送單", text)
        self.assertIn("actual_orders=0", text)

    def test_delivery_uses_existing_notifier_without_order_path(self):
        with patch(
            "dynamic_market_scanner_shadow_v01.manual_review_notification.load_into_environment",
            return_value="KEYCHAIN_CONFIGURED",
        ), patch(
            "dynamic_market_scanner_shadow_v01.manual_review_notification.notify",
            return_value={"TELEGRAM": "SUCCESS"},
        ) as notify:
            result = deliver_manual_review(_payload())
        self.assertEqual(result["providers"]["TELEGRAM"], "SUCCESS")
        self.assertEqual(result["actual_orders"], 0)
        self.assertEqual(notify.call_args.args[3], "MANUAL_ORDER_REVIEW")

    def test_only_confirmed_continuation_invokes_callback(self):
        captured = []
        with tempfile.TemporaryDirectory() as folder:
            observer = RealtimeExpandedShadow(
                queue_size=10, manual_review_callback=lambda row: captured.append(row) or {"TELEGRAM": "SUCCESS"},
            )
            observer.start(run_dir=Path(folder), run_id="manual", expanded_items=[Item()])
            decision = datetime(2026, 11, 11, 9, 8, tzinfo=TAIPEI)
            observer._selected = {
                "stock_id": "1234", "stock_name": "測試股票", "side": "LONG",
                "decision_time": decision - timedelta(minutes=3),
                "entry_price": 50.0, "quantity": 3000,
                "relative_strength_5m": 0.012, "rs_challenger_passed": True,
                "continuation_due_at": decision,
            }
            observer._data["1234"]["ticks"].append({
                "time": decision, "bid": 51.0, "ask": 51.1,
            })
            observer._data["1234"]["books"].append({
                "time": decision, "buy_prices": [51.0], "buy_volumes": [10],
                "sell_prices": [51.1], "sell_volumes": [10],
            })
            observer._check_continuation(decision)
            observer.close()
            summary = json.loads((Path(folder) / SUMMARY_NAME).read_text())
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0]["manual_review_ready"])
        self.assertEqual(captured[0]["actual_orders"], 0)
        self.assertEqual(summary["manual_review_notification"]["status"], "ATTEMPTED")

    def test_reference_stop_reaches_frozen_net_loss(self):
        stop = _reference_stop_price(51.2, 3000)
        self.assertLess(stop, 51.2)


if __name__ == "__main__":
    unittest.main()
