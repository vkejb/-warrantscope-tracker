import unittest
from datetime import datetime, timedelta

from dynamic_market_scanner_shadow_v01.v2_gate_exit_diagnostic import (
    cohort_replay, coverage_audit, summarize_cohort,
)


class CohortReplayTests(unittest.TestCase):
    def test_sources_include_frozen_18_plus_complete_12_13(self):
        selected = cohort_replay.sources()
        self.assertEqual(len(selected), 20)
        self.assertEqual(selected["20261112"].name,
                         "20261112T005011.064631Z_fcdaa5e4")
        self.assertEqual(selected["20261113"].name,
                         "20261113T005010.446883Z_c876f743")

    def test_preopen_sealed_complete_archive_audit(self):
        audit, manifest = cohort_replay.audit_source(
            "20261111", cohort_replay.sources()["20261111"])
        self.assertEqual(audit["status"], "REPLAYABLE_ARCHIVE_COMPLETE")
        self.assertIsNotNone(manifest)
        self.assertGreater(audit["expanded_pool_count"], 0)
        self.assertGreater(audit["market_0050_ticks"], 0)

    def test_quote_path_keeps_first_positive_and_max_time(self):
        at = datetime.fromisoformat("2026-11-12T09:13:30+08:00")
        data = {
            "ticks": [
                {"time": at + timedelta(seconds=1), "bid": 28.8},
                {"time": at + timedelta(seconds=30), "bid": 28.9},
                {"time": at + timedelta(minutes=2), "bid": 29.4},
            ],
            "books": [],
        }
        result = cohort_replay.quote_path_15m(data, at, 28.8, 6000)
        self.assertEqual(result["first_net_positive_status"], "OBSERVED")
        self.assertEqual(result["first_net_positive_at"],
                         (at + timedelta(seconds=30)).isoformat())
        self.assertEqual(result["maximum_bid_price"], 29.4)
        self.assertEqual(result["maximum_bid_at"],
                         (at + timedelta(minutes=2)).isoformat())

    def test_no_bid_is_not_zero_return(self):
        at = datetime.fromisoformat("2026-11-12T09:13:30+08:00")
        result = cohort_replay.quote_path_15m({"ticks": [], "books": []},
                                               at, 28.8, 6000)
        self.assertEqual(result["maximum_bid_net_status"], "NA_NO_BID_15M")
        self.assertNotIn("maximum_bid_net_twd", result)

    def test_research_has_no_order_capability(self):
        self.assertEqual(cohort_replay.POLICY["actual_orders"], 0)
        self.assertEqual(cohort_replay.POLICY["broker_order_calls"], 0)

    def test_0050_full_session_freshness_sample(self):
        result = coverage_audit.inspect(
            "20261111", cohort_replay.sources()["20261111"])
        self.assertEqual(result["minute_grid_09_00_to_13_30"], 271)
        self.assertGreater(result["grid_fresh_10s"], 250)
        self.assertGreater(result["tick_rows_0050"], 0)
        self.assertGreater(result["book_rows_0050"], 0)

    def test_summary_keeps_empty_cohort_as_na(self):
        result = summarize_cohort.distribution([])
        self.assertEqual(result["n"], 0)
        self.assertIsNone(result["mean_twd"])
        self.assertIsNone(result["sum_twd"])


if __name__ == "__main__":
    unittest.main()
