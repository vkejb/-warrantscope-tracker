from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.flow_breakeven_exit_study import (
    _decision_times, flow_snapshot, simulate_flow_breakeven,
)


TAIPEI = ZoneInfo("Asia/Taipei")


def _data(adverse: bool = True):
    start = datetime(2026, 11, 11, 9, 5, tzinfo=TAIPEI)
    ticks = []
    for index in range(80):
        at = start + timedelta(seconds=index * 2)
        price = 101.0 if index < 35 else (100.5 if index < 60 else 100.0)
        ticks.append({"time": at, "price": price, "bid": price, "ask": price + .1,
                      "volume": 10.0, "flag": "0" if adverse and index >= 35 else "1"})
    books = [{"time": start + timedelta(seconds=index * 2), "buy_volume": 10.0,
              "sell_volume": 90.0 if adverse and index >= 35 else 10.0,
              "best_bid": 100.0, "best_ask": 100.1} for index in range(80)]
    return {"ticks": ticks, "books": books,
            "tick_times": [row["time"] for row in ticks],
            "book_times": [row["time"] for row in books]}


class FlowBreakevenExitStudyTests(unittest.TestCase):
    def test_decisions_are_strictly_after_entry(self):
        entry = datetime(2026, 11, 11, 9, 5, 30, tzinfo=TAIPEI)
        end = entry + timedelta(minutes=2)
        times = _decision_times(entry, end)
        self.assertEqual(times[0], datetime(2026, 11, 11, 9, 6, tzinfo=TAIPEI))

    def test_flow_snapshot_requires_two_adverse_components(self):
        data = _data(True)
        result = flow_snapshot(data, datetime(2026, 11, 11, 9, 7, tzinfo=TAIPEI))
        self.assertTrue(result["evaluable"])
        self.assertGreaterEqual(result["adverse_count"], 2)

    def test_overlay_exits_only_after_flow_activation_and_net_zero(self):
        data = _data(True)
        entry = data["ticks"][0]["time"]
        baseline = {"status": "SCORED", "entry_time": entry.isoformat(),
                    "entry_price": 100.0, "quantity": 1000,
                    "exit_time": data["ticks"][-1]["time"].isoformat(),
                    "exit_price": 99.9, "exit_reason": "STOP_LOSS", "net_pnl": -500.0}
        result, _events = simulate_flow_breakeven(baseline, data, 0.0)
        self.assertEqual(result["status"], "SCORED")
        self.assertEqual(result["action"], "FLOW_BREAKEVEN_EXIT")
        self.assertEqual(result["exit_reason"], "FLOW_REVERSAL_COST_AWARE_BREAKEVEN")
        self.assertGreater(result["net_mfe_twd"], 0)
        self.assertLessEqual(result["net_pnl"], 0)
        self.assertIsNotNone(result["flow_activation_time"])

    def test_missing_flow_never_activates(self):
        data = _data(False)
        entry = data["ticks"][0]["time"]
        baseline = {"status": "SCORED", "entry_time": entry.isoformat(),
                    "entry_price": 100.0, "quantity": 1000,
                    "exit_time": data["ticks"][-1]["time"].isoformat(),
                    "exit_price": 100.5, "exit_reason": "HARD_EXIT", "net_pnl": 100.0}
        result, _events = simulate_flow_breakeven(baseline, data, 0.0)
        self.assertEqual(result["action"], "BASELINE_EXIT_NO_FLOW_ACTIVATION")
        self.assertEqual(result["non_activation_reason"], "REVERSAL_NOT_CONFIRMED")

    def test_quarter_r_requires_frozen_cost_aware_mfe(self):
        data = _data(True)
        entry = data["ticks"][0]["time"]
        baseline = {"status": "SCORED", "entry_time": entry.isoformat(),
                    "entry_price": 100.0, "quantity": 1000,
                    "exit_time": data["ticks"][-1]["time"].isoformat(),
                    "exit_price": 99.9, "exit_reason": "STOP_LOSS", "net_pnl": -500.0}
        result, _events = simulate_flow_breakeven(baseline, data, 1250.0)
        self.assertEqual(result["action"], "BASELINE_EXIT_NO_FLOW_ACTIVATION")
        self.assertEqual(result["non_activation_reason"], "MFE_THRESHOLD_NOT_REACHED")
        self.assertFalse(result["mfe_threshold_reached"])


if __name__ == "__main__":
    unittest.main()
