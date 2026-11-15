import unittest

from dynamic_market_scanner_shadow_v01.raw_signal_comparison import (
    _all_groups, _bool, _group, _no_wait_ordering,
)


class RawSignalComparisonTest(unittest.TestCase):
    def test_unknown_chip_is_not_nonpositive(self):
        self.assertIsNone(_bool(""))
        self.assertIsNone(_bool("None"))
        self.assertFalse(_bool("False"))
        self.assertTrue(_bool("True"))

    def test_missing_horizon_is_not_zero_or_in_denominator(self):
        rows = [
            {"chip_group": "POSITIVE", "session_date": "20261116",
             "mark_5m_status": "COMPLETE", "net_5m_twd": -100.0},
            {"chip_group": "POSITIVE", "session_date": "20261116",
             "mark_5m_status": "PARTIAL_OR_STALE", "net_5m_twd": None},
        ]
        group = _group(rows, 5, "POSITIVE")
        self.assertEqual(group["raw_signal_count"], 2)
        self.assertEqual(group["complete_count"], 1)
        self.assertEqual(group["partial_or_stale_count"], 1)
        self.assertEqual(group["entry_unaffordable_count"], 0)
        self.assertEqual(group["mean_signal_net_twd"], -100.0)

    def test_rule_reason_preserves_rejected_profitable_signal(self):
        rows = [{"session_date": "20261116", "chip_group": "POSITIVE",
                 "chart_pass": "False", "original_rule_pass": "False",
                 "original_rule_reason": "RS_BELOW_1PCT",
                 **{f"mark_{n}m_status": "COMPLETE" for n in (5, 15, 30)},
                 **{f"net_{n}m_twd": 200.0 for n in (5, 15, 30)}}]
        groups = _all_groups(rows)
        reason = next(row for row in groups if row["dimension"] == "ORIGINAL_RULE_REASON"
                      and row["horizon_minutes"] == 30)
        self.assertEqual(reason["positive_count"], 1)
        self.assertEqual(reason["original_rule_pass_count"], 0)

    def test_raw_chip_order_cannot_use_later_signal(self):
        a = {"session_date": "20261116", "stock_id": "1111",
             "signal_time": "2026-11-16T09:10:00+08:00",
             "relative_strength_5m": "0.04", "chip_group": "NONPOSITIVE"}
        b = {"session_date": "20261116", "stock_id": "2222",
             "signal_time": "2026-11-16T09:10:01+08:00",
             "relative_strength_5m": "0.03", "chip_group": "POSITIVE"}
        result = _no_wait_ordering([b, a])[0]
        self.assertFalse(result["selection_changed"])
        b["signal_time"] = a["signal_time"]
        result = _no_wait_ordering([b, a])[0]
        self.assertTrue(result["selection_changed"])


if __name__ == "__main__":
    unittest.main()
