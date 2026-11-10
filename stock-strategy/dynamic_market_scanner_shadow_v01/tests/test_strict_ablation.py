from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.strict_ablation import (
    accepted_by_variant, select_daily,
)


class StrictAblationTest(unittest.TestCase):
    def row(self, reason, symbol="A", minute=10):
        at = datetime(2026, 11, 10, 9, minute, tzinfo=ZoneInfo("Asia/Taipei"))
        return {"strict_passed": "False", "strict_reason": reason,
                "session_date": "20261110", "stock_id": symbol,
                "confirmation_time": at, "hypothetical_net_progress_twd": -100,
                "relative_strength_5m": 0.01, "production_score": 0.5}

    def test_one_removed_gate_does_not_hide_other_failure(self):
        row = self.row("FLOW_TURNED_NEGATIVE+NET_PROGRESS_NOT_POSITIVE")
        self.assertFalse(accepted_by_variant(row, "A1_REMOVE_NET_PROGRESS_ONLY"))
        self.assertFalse(accepted_by_variant(row, "A2_REMOVE_FLOW_ONLY"))

    def test_net_progress_only_is_accepted_only_by_a1(self):
        row = self.row("NET_PROGRESS_NOT_POSITIVE")
        self.assertTrue(accepted_by_variant(row, "A1_REMOVE_NET_PROGRESS_ONLY"))
        self.assertFalse(accepted_by_variant(row, "A0_ALL_STRICT_GATES"))

    def test_selection_is_earliest_not_end_of_day_best(self):
        early = self.row("NET_PROGRESS_NOT_POSITIVE", "EARLY", 10)
        late = self.row("NET_PROGRESS_NOT_POSITIVE", "LATE", 11)
        late["hypothetical_net_progress_twd"] = 999
        selected = select_daily([late, early], "A1_REMOVE_NET_PROGRESS_ONLY")
        self.assertEqual(selected[0]["stock_id"], "EARLY")


if __name__ == "__main__":
    unittest.main()
