import unittest
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from unittest.mock import patch

from dynamic_market_scanner_shadow_v01.realtime_expanded_shadow import (
    RealtimeExpandedShadow,
)
from dynamic_market_scanner_shadow_v01.tests.test_realtime_expanded_shadow import Item

from dynamic_market_scanner_shadow_v01.realtime_expanded_shadow import (
    POLICY_HASH as CURRENT_OBSERVER_HASH,
)
from dynamic_market_scanner_shadow_v01.strict_replay import DEFAULT_RUNS_DIR
from dynamic_market_scanner_shadow_v01.v2_gate_exit_diagnostic import (
    followup_replay as followup,
)
from dynamic_market_scanner_shadow_v01.v2_gate_exit_diagnostic.replay import (
    choose_immediate, events,
)


class FollowupReplayTests(unittest.TestCase):
    def test_current_observer_replays_11_12_occupancy_order(self):
        run_dir = DEFAULT_RUNS_DIR / followup.RUNS["20261112"][0]
        archived = {str(row["stock_id"]): row for row in events(run_dir)
                    if row["event"] == "CANDIDATE_SEEN"}
        with tempfile.TemporaryDirectory() as folder:
            rejected = []
            observer = RealtimeExpandedShadow(
                queue_size=10,
                candidate_status_callback=lambda payload: rejected.append(payload),
            )
            observer.start(run_dir=Path(folder), run_id="archived-gate-regression",
                           expanded_items=[Item("6770", "力積電"), Item("1528", "恩德")])
            observer._observe_all_early = lambda _decision: None
            observer._engine.choose_entry = lambda _decision, allow_short: None
            gate = {"evaluable": True, "passed": True,
                    "reason": "VOLUME_GATE_PASSED"}
            with patch(
                "dynamic_market_scanner_shadow_v01.realtime_expanded_shadow.evaluate_volume_gate",
                return_value=gate,
            ):
                for symbol, decision in (("6770", "2026-11-12T09:12:30+08:00"),
                                         ("1528", "2026-11-12T09:13:30+08:00")):
                    source = archived[symbol]
                    observer._engine.last_entry_diagnostics = {"candidates": [{
                        "stock_id": symbol, "gate_reason": "MARKET_GATE_PASSED",
                        "score": source["score"],
                        "relative_strength_5m": source["relative_strength_5m"],
                        "entry_price": source["entry_price"],
                        "quantity": source["quantity"],
                    }]}
                    observer._decide(datetime.fromisoformat(decision))
                    if symbol == "6770":
                        self.assertIsNone(observer._selected)
                self.assertEqual(observer._selected["stock_id"], "1528")
            observer.close()
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["stock_id"], "6770")
        self.assertEqual(rejected[0]["reason"],
                         "RELATIVE_STRENGTH_BELOW_THRESHOLD")

    def test_11_12_archived_gate_rejects_6770_and_selects_1528(self):
        run_dir = DEFAULT_RUNS_DIR / followup.RUNS["20261112"][0]
        rows = events(run_dir)
        by_symbol = {str(row["stock_id"]): row for row in rows
                     if row["event"] == "CANDIDATE_SEEN"}
        self.assertLess(by_symbol["6770"]["relative_strength_5m"], 0.01)
        self.assertGreater(by_symbol["1528"]["relative_strength_5m"], 0.01)
        self.assertTrue(by_symbol["6770"]["gate_passed"])
        self.assertTrue(by_symbol["1528"]["gate_passed"])
        self.assertEqual(choose_immediate(rows)["stock_id"], "1528")

    def test_exact_one_percentage_point_passes(self):
        rows = [{"event": "CANDIDATE_SEEN", "decision_time": "t",
                 "stock_id": "1234", "score": 1.0, "gate_passed": True,
                 "relative_strength_5m": 0.01}]
        self.assertEqual(choose_immediate(rows)["stock_id"], "1234")
        rows[0]["relative_strength_5m"] = 0.01 - 1e-9
        self.assertIsNone(choose_immediate(rows))

    def test_current_observer_policy_matches_11_13_archive(self):
        self.assertEqual(CURRENT_OBSERVER_HASH,
                         "7f883490b65f2c74040fac43a50867401e8ee411d214c72726b5077cf31ca6f1")

    def test_fixed_holds_value_after_strategy_exit(self):
        at = datetime.fromisoformat("2026-11-12T09:13:31+08:00")
        t5 = at + timedelta(minutes=5)
        t15 = at + timedelta(minutes=15)
        data = {"ticks": [{"time": t5 - timedelta(seconds=1), "bid": 29.1}],
                "books": [{"time": t15 - timedelta(seconds=1), "best_bid": 29.3}]}
        data["tick_times"] = [row["time"] for row in data["ticks"]]
        data["book_times"] = [row["time"] for row in data["books"]]
        self.assertEqual(followup.fixed_hold(data, at, 28.8, 6000, 5)["bid"], 29.1)
        self.assertEqual(followup.fixed_hold(data, at, 28.8, 6000, 15)["bid"], 29.3)
        self.assertEqual(followup.fixed_hold(data, at, 28.8, 6000, 6)["status"],
                         "NA_STALE_BID")

    def test_one_r_uses_original_net_stop(self):
        at = datetime.fromisoformat("2026-11-12T09:13:31+08:00")
        row = followup.one_r_diagnostic("1528", "恩德", at, 28.8,
                                        6000, 3036, "MFE_PROFIT_PROTECTION")
        self.assertEqual(row["one_r_price"], 29.3)
        self.assertTrue(row["one_r_observed_or_armed"])

    def test_no_order_capability(self):
        self.assertEqual(followup.POLICY["actual_orders"], 0)
        self.assertEqual(followup.POLICY["actual_fills"], 0)
        self.assertEqual(followup.POLICY["broker_order_calls"], 0)


if __name__ == "__main__":
    unittest.main()
