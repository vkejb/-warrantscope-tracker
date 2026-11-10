from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
