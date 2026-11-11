import json
from pathlib import Path
import tempfile
import unittest

from dynamic_market_scanner_shadow_v01.early_signal_status import summarize
from dynamic_market_scanner_shadow_v01.realtime_expanded_shadow import EVENTS_NAME


class EarlySignalStatusTest(unittest.TestCase):
    def test_summary_separates_telemetry_candidate_and_orders(self):
        rows = [
            {"at": "2026-11-12T01:10:00Z", "event": "EARLY_STATE_TRANSITION",
             "stock_id": "1234", "stock_name": "測試", "state": "ARMED",
             "observed_at": "2026-11-12T09:10:00+08:00",
             "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0},
            {"at": "2026-11-12T01:11:00Z", "event": "CANDIDATE_SEEN",
             "stock_id": "1234", "gate_passed": False,
             "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0},
        ]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / EVENTS_NAME).write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
            )
            result = summarize(root)
        self.assertEqual(result["state_counts"], {"ARMED": 1})
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["selected_count"], 0)
        self.assertEqual(result["actual_orders"], 0)
        self.assertEqual(
            result["interpretation"],
            "EARLY_STATES_ARE_TELEMETRY_NOT_ENTRY_APPROVAL",
        )


if __name__ == "__main__":
    unittest.main()
