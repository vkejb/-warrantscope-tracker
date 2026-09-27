from datetime import datetime, timezone
import tempfile
from pathlib import Path
import unittest

from intraday_loss_reduction_study_v01.analysis import build_report, write_report


def row(trade_id, side, hour, pnl):
    return {
        "trade_id": trade_id,
        "session_date": trade_id[:8],
        "symbol": "3605",
        "stock_name": "宏致",
        "side": side,
        "entry_time": datetime(2026, 9, 24, hour, tzinfo=timezone.utc),
        "pnl": float(pnl),
        "exit_reason": "TEST",
    }


class LossReductionTests(unittest.TestCase):
    def test_fixed_gates_are_causal_and_membership_is_auditable(self):
        report = build_report([
            row("20260922-a", "SHORT", 9, 5000),
            row("20260923-b", "SHORT", 9, -1000),
            row("20260924-c", "LONG", 11, -4000),
        ])
        summaries = {item["variant"]: item for item in report["summaries"]}
        self.assertEqual(summaries["SHORT_BEFORE_1030_DIAGNOSTIC"]["net_pnl"], 4000)
        self.assertEqual(summaries["LONG_BEFORE_1030"]["trades"], 0)
        self.assertFalse(any(item["eligible_for_live_promotion"] for item in report["summaries"]))
        self.assertEqual(report["broker_connections"], 0)

    def test_largest_winner_stress_is_not_optimistic(self):
        report = build_report([
            row("20260922-a", "SHORT", 9, 5000),
            row("20260923-b", "SHORT", 9, -2000),
        ])
        summary = next(item for item in report["summaries"] if item["variant"] == "SHORT_BEFORE_1030_DIAGNOSTIC")
        self.assertEqual(summary["net_pnl"], 3000)
        self.assertEqual(summary["net_without_largest_winner"], -2000)

    def test_outputs_have_zero_execution_manifest(self):
        report = build_report([row("20260922-a", "LONG", 9, 100)])
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            write_report(report, output)
            manifest = (output / "run_manifest.json").read_text(encoding="utf-8")
            self.assertIn('"actual_orders":0', manifest)
            self.assertIn('"broker_connections":0', manifest)


if __name__ == "__main__":
    unittest.main()
