from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.realtime_expanded_shadow import POLICY_HASH
from yuanta_live_runtime_v01.main import _expanded_v2_live_signal


TAIPEI = ZoneInfo("Asia/Taipei")


def _payload() -> dict:
    return {
        "mode": "EXPANDED_V2_MATURE_CANDIDATE",
        "policy_hash": POLICY_HASH,
        "manual_review_ready": True,
        "passed": True,
        "rs_challenger_passed": True,
        "review_decision_time": "2026-11-12T09:11:00+08:00",
        "manual_review_expires_at": "2026-11-12T09:12:00+08:00",
        "manual_entry_reference_price": 51.2,
        "manual_reference_quantity": 3000,
        "stock_id": "1234",
        "stock_name": "測試股票",
        "score": 2.5,
        "volume_delta": 0.55,
        "large_trade_delta": 0.45,
        "vwap_gap": 0.008,
        "book_imbalance": 0.30,
        "spread_bps": 7.0,
        "relative_strength_5m": 0.012,
        "market_regime": "BULLISH",
    }


class ExpandedV2LiveCanaryTests(unittest.TestCase):
    def test_mature_candidate_converts_to_whole_lot_long_signal(self):
        signal = _expanded_v2_live_signal(
            _payload(),
            now=datetime(2026, 11, 12, 9, 11, 30, tzinfo=TAIPEI),
        )
        self.assertEqual(signal.stock_id, "1234")
        self.assertEqual(signal.side, "LONG")
        self.assertEqual(signal.entry_price, 51.2)
        self.assertEqual(signal.quantity, 3000)

    def test_expired_candidate_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "CANDIDATE_EXPIRED"):
            _expanded_v2_live_signal(
                _payload(),
                now=datetime(2026, 11, 12, 9, 12, 1, tzinfo=TAIPEI),
            )

    def test_price_cap_and_whole_lot_are_fail_closed(self):
        payload = _payload()
        payload["manual_entry_reference_price"] = 190.01
        payload["manual_reference_quantity"] = 999
        with self.assertRaisesRegex(RuntimeError, "ORDER_SHAPE_INVALID"):
            _expanded_v2_live_signal(
                payload,
                now=datetime(2026, 11, 12, 9, 11, 30, tzinfo=TAIPEI),
            )


if __name__ == "__main__":
    unittest.main()
