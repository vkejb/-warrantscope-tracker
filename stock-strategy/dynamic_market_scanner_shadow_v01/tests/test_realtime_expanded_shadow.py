from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.realtime_expanded_shadow import (
    EVENTS_NAME, POLICY_HASH, SUMMARY_NAME, RealtimeExpandedShadow,
)


@dataclass(frozen=True)
class Item:
    stock_id: str = "1234"
    stock_name: str = "測試股票"


class RealtimeExpandedShadowTests(unittest.TestCase):
    def test_empty_observer_closes_with_zero_order_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            observer = RealtimeExpandedShadow(queue_size=10)
            observer.start(run_dir=root, run_id="run-1", expanded_items=[Item()])
            paths = observer.close()
            self.assertEqual({path.name for path in paths}, {EVENTS_NAME, SUMMARY_NAME})
            summary = json.loads((root / SUMMARY_NAME).read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "VALID_SHADOW_EVIDENCE")
            self.assertEqual(summary["policy_hash"], POLICY_HASH)
            self.assertEqual(summary["actual_orders"], 0)
            self.assertEqual(summary["actual_fills"], 0)
            self.assertEqual(summary["broker_order_calls"], 0)
            self.assertFalse(summary["live_enabled"])

    def test_offer_after_close_is_rejected_without_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            observer = RealtimeExpandedShadow(queue_size=10)
            observer.start(
                run_dir=Path(folder), run_id="run-2", expanded_items=[Item()],
            )
            observer.close()
            self.assertFalse(observer.offer("expanded_ticks", {}))

    def test_upstream_invalidation_makes_live_consumer_health_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            observer = RealtimeExpandedShadow(queue_size=10)
            observer.start(
                run_dir=Path(folder), run_id="run-invalid", expanded_items=[Item()],
            )
            observer.invalidate("ARCHIVE_QUOTE_WRITE_FAILED")
            health = observer.health_snapshot()
            observer.close()
        self.assertFalse(health["healthy"])
        self.assertIn("ARCHIVE_QUOTE_WRITE_FAILED", health["errors"])

    def test_policy_has_no_live_switch(self):
        with tempfile.TemporaryDirectory() as folder:
            observer = RealtimeExpandedShadow(queue_size=10)
            observer.start(
                run_dir=Path(folder), run_id="run-3", expanded_items=[Item()],
            )
            observer.close()
            summary = json.loads(
                (Path(folder) / SUMMARY_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(summary["policy"]["mode"], "SHADOW_ONLY_NO_BROKER_NO_ORDER")
            self.assertFalse(summary["policy"]["live_enabled"])

    def test_early_states_are_telemetry_only_and_monotonic(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            observer = RealtimeExpandedShadow(queue_size=10)
            observer.start(run_dir=root, run_id="run-early", expanded_items=[Item()])
            at = datetime(2026, 11, 12, 9, 10,
                          tzinfo=ZoneInfo("Asia/Taipei"))
            checks = {name: False for name in (
                "volume_delta", "large_trade_delta", "above_vwap",
                "book_imbalance", "breakout",
            )}
            snapshots = iter((3, 4, 5))

            def features(_symbol, stamp):
                count = next(snapshots)
                current = dict(checks)
                for name in list(current)[:count]:
                    current[name] = True
                return {
                    "stock_id": "1234", "stock_name": "測試股票",
                    "observed_at": stamp, "source_check_count": count,
                    "source_checks": current, "market_passed": True,
                    "anti_chase_passed": True,
                    "volume_gate_passed": count == 5,
                }

            observer._early_features = features
            observer._observe_early("1234", at, source="TEST")
            observer._observe_early("1234", at + timedelta(seconds=5), source="TEST")
            observer._observe_early("1234", at + timedelta(seconds=10), source="TEST")
            observer.close()
            summary = json.loads((root / SUMMARY_NAME).read_text(encoding="utf-8"))
            early = summary["early_observation"]
            self.assertEqual(
                early["timelines"]["1234"]["state"],
                "EARLY_ENTRY_READY_OBSERVED",
            )
            self.assertFalse(early["notification_enabled"])
            self.assertFalse(early["order_enabled"])
            events = [json.loads(line) for line in
                      (root / EVENTS_NAME).read_text(encoding="utf-8").splitlines()]
            transitions = [row for row in events
                           if row["event"] == "EARLY_STATE_TRANSITION"]
            self.assertEqual(
                [row["state"] for row in transitions],
                ["PREWATCH", "ARMED", "EARLY_ENTRY_READY_OBSERVED"],
            )


if __name__ == "__main__":
    unittest.main()
