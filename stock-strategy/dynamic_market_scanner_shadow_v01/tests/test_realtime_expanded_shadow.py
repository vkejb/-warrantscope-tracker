from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.realtime_expanded_shadow import (
    EVENTS_NAME, POLICY_HASH, SUMMARY_NAME, RealtimeExpandedShadow,
)


TAIPEI = ZoneInfo("Asia/Taipei")


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

    def test_weak_candidate_does_not_reserve_slot_before_strong_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            observer = RealtimeExpandedShadow(queue_size=10)
            observer.start(
                run_dir=Path(folder), run_id="run-rearm",
                expanded_items=[
                    Item(stock_id="1234", stock_name="弱候選"),
                    Item(stock_id="5678", stock_name="強候選"),
                ],
            )
            observer._observe_all_early = lambda _decision: None
            observer._engine.choose_entry = lambda _decision, allow_short: None
            observer._engine.last_entry_diagnostics = {
                "candidates": [
                    {
                        "stock_id": "1234", "gate_reason": "MARKET_GATE_PASSED",
                        "score": 0.9, "relative_strength_5m": 0.005,
                        "entry_price": 50.0, "quantity": 3000,
                    },
                    {
                        "stock_id": "5678", "gate_reason": "MARKET_GATE_PASSED",
                        "score": 0.8, "relative_strength_5m": 0.02,
                        "entry_price": 40.0, "quantity": 4000,
                    },
                ],
            }
            decision = datetime(2026, 11, 12, 9, 12, 30, tzinfo=TAIPEI)
            gate = {
                "evaluable": True, "passed": True,
                "reason": "VOLUME_GATE_PASSED",
            }
            with patch(
                "dynamic_market_scanner_shadow_v01.realtime_expanded_shadow.evaluate_volume_gate",
                return_value=gate,
            ):
                observer._decide(decision)
            selected = dict(observer._selected)
            observer.close()
        self.assertEqual(selected["stock_id"], "5678")
        self.assertIn("1234", observer._seen)

    def test_failed_continuation_releases_selection_for_later_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            observer = RealtimeExpandedShadow(queue_size=10)
            observer.start(
                run_dir=Path(folder), run_id="run-release", expanded_items=[Item()],
            )
            decision = datetime(2026, 11, 12, 9, 15, 30, tzinfo=TAIPEI)
            observer._selected = {
                "stock_id": "1234", "stock_name": "測試股票", "side": "LONG",
                "decision_time": decision - timedelta(minutes=3),
                "entry_price": 50.0, "quantity": 3000,
                "relative_strength_5m": 0.02, "rs_challenger_passed": True,
                "continuation_due_at": decision,
            }
            observer._data["1234"]["ticks"].append({
                "time": decision, "bid": 49.0, "ask": 49.1,
            })
            observer._data["1234"]["books"].append({
                "time": decision, "buy_prices": [49.0], "buy_volumes": [10],
                "sell_prices": [49.1], "sell_volumes": [10],
            })
            observer._check_continuation(decision)
            self.assertIsNone(observer._selected)
            self.assertIsNone(observer._continuation)
            self.assertEqual(len(observer._continuation_attempts), 1)
            observer.close()

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
