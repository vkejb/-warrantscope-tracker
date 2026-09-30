from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from stitched_session_backtest_v01.analysis import _decision_summary, stitch_streams


TAIPEI = ZoneInfo("Asia/Taipei")


def row(at: datetime, serial: int) -> dict:
    return {"time": at, "serial": serial}


class StitchTests(unittest.TestCase):
    def test_cutover_uses_early_before_and_late_at_or_after(self):
        cutover = datetime(2026, 9, 30, 9, 27, tzinfo=TAIPEI)
        early = {
            "0050": {
                "ticks": [row(cutover - timedelta(seconds=1), 1), row(cutover, 2)],
                "books": [row(cutover - timedelta(seconds=1), 0), row(cutover, 0)],
                "meta": {"stock_name": "early"},
            }
        }
        late = {
            "0050": {
                "ticks": [row(cutover, 3), row(cutover + timedelta(seconds=1), 4)],
                "books": [row(cutover, 0), row(cutover + timedelta(seconds=1), 0)],
                "meta": {"stock_name": "late"},
            }
        }

        result = stitch_streams(early, late, cutover)["0050"]

        self.assertEqual([item["serial"] for item in result["ticks"]], [1, 3, 4])
        self.assertEqual(len(result["books"]), 3)
        self.assertEqual(result["meta"]["stock_name"], "early")

    def test_decision_summary_preserves_near_miss_gate(self):
        summary = _decision_summary(
            [
                {"decision": "REJECTED", "candidates": []},
                {
                    "decision": "NO_APPROVED_CANDIDATE",
                    "decision_time": "2026-09-30T09:07:00+08:00",
                    "candidates": [
                        {
                            "stock_id": "3016",
                            "stock_name": "嘉晶",
                            "score": 0.7,
                            "gate_reason": "CONFIRMATIONS_INCOMPLETE",
                            "required_confirmations": 2,
                            "streak": 1,
                        }
                    ],
                },
            ]
        )

        self.assertEqual(summary["decision_windows"], 2)
        self.assertEqual(summary["decisions"]["REJECTED"], 1)
        self.assertEqual(summary["near_miss_gate_counts"]["CONFIRMATIONS_INCOMPLETE"], 1)
        self.assertEqual(summary["near_miss_symbol_counts"]["3016"], 1)


if __name__ == "__main__":
    unittest.main()
