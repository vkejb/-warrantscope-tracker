from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.expanded_volume_gate import (
    gate_result, select_candidate,
)


class ExpandedVolumeGateTest(unittest.TestCase):
    def test_v0_passes_without_data_dependent_gate(self):
        row = {"signal_time": datetime(2026, 11, 10, 9, 10,
                                       tzinfo=ZoneInfo("Asia/Taipei"))}
        result = gate_result(row, {"ticks": []}, "V0_NO_ADDITIONAL_GATE")
        self.assertTrue(result["passed"])

    def test_unknown_variant_fails(self):
        with self.assertRaises(ValueError):
            gate_result({"signal_time": datetime.now(ZoneInfo("Asia/Taipei"))},
                        {"ticks": []}, "UNKNOWN")

    def test_same_time_uses_production_score_not_input_order(self):
        at = datetime(2026, 11, 10, 9, 10, tzinfo=ZoneInfo("Asia/Taipei"))
        rows = [
            {"session_date": "20261110", "stock_id": "LOW", "stock_name": "L",
             "signal_time": at, "production_score": 0.1},
            {"session_date": "20261110", "stock_id": "HIGH", "stock_name": "H",
             "signal_time": at, "production_score": 0.9},
        ]
        selected, _ = select_candidate(rows, {"LOW": {}, "HIGH": {}},
                                       "V0_NO_ADDITIONAL_GATE")
        self.assertEqual(selected["stock_id"], "HIGH")


if __name__ == "__main__":
    unittest.main()
