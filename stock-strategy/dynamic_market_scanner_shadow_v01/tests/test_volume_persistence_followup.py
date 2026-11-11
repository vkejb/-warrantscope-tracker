from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.volume_persistence_followup import (
    _select, recovery_opportunity,
)


TZ = ZoneInfo("Asia/Taipei")


def tick(at, price, volume, flag="1"):
    return {"time": at, "received_at": at, "price": price, "volume": volume,
            "bid": price, "ask": price, "flag": flag, "serial": 1}


class VolumePersistenceFollowupTest(unittest.TestCase):
    def test_wait_uses_first_causal_recovery_checkpoint(self):
        at = datetime(2026, 11, 11, 9, 10, tzinfo=TZ)
        ticks = []
        # Original recent window is weaker than its previous window. The next
        # 30 seconds then accelerates with distributed buying.
        for i in range(12):
            ticks.append(tick(at - timedelta(seconds=55 - i * 4), 100, 10))
        for i in range(6):
            ticks.append(tick(at - timedelta(seconds=25 - i * 4), 100, 2))
        for i in range(10):
            ticks.append(tick(at + timedelta(seconds=2 + i * 3), 100.2, 10))
        ticks.sort(key=lambda row: row["time"])
        data = {"ticks": ticks, "tick_times": [row["time"] for row in ticks]}
        row = {"signal_time": at, "original_entry_price": 100.0}
        result = recovery_opportunity(row, data)
        self.assertTrue(result["passed"])
        self.assertEqual(result["effective_time"], at + timedelta(seconds=30))
        self.assertEqual(result["reason"], "V2_RECOVERED_WITHIN_90S")

    def test_selection_uses_effective_time_not_original_signal_time(self):
        at = datetime(2026, 11, 11, 9, 10, tzinfo=TZ)
        rows = [
            {"session_date": "20261111", "signal_time": at,
             "original_entry_price": 100.0, "stock_id": "A", "stock_name": "A",
             "production_score": 0.9},
            {"session_date": "20261111", "signal_time": at + timedelta(seconds=30),
             "original_entry_price": 100.0, "stock_id": "B", "stock_name": "B",
             "production_score": 0.5},
        ]
        def passing_data(start):
            ticks = [tick(start - timedelta(seconds=55 - i * 5), 100, 2)
                     for i in range(6)]
            ticks += [tick(start - timedelta(seconds=25 - i * 4), 100, 10)
                      for i in range(6)]
            ticks.sort(key=lambda row: row["time"])
            return {"ticks": ticks, "tick_times": [row["time"] for row in ticks]}
        selected, _result, _diagnostics = _select(
            rows, {"A": passing_data(at), "B": passing_data(at + timedelta(seconds=30))},
            "V2_RECENT_VOLUME_ACCELERATION",
        )
        self.assertEqual(selected["stock_id"], "A")


if __name__ == "__main__":
    unittest.main()
