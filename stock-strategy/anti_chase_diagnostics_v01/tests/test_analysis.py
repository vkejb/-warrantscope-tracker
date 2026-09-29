from __future__ import annotations

import unittest

from anti_chase_diagnostics_v01.analysis import Gate, evaluate_gate


class AntiChaseDiagnosticsTests(unittest.TestCase):
    def rows(self):
        base = {
            "side": "LONG", "score": 0.6, "directional_return_60s": 0.01,
            "directional_return_300s": 0.02, "directional_opening_extension": 0.03,
            "breakout_overshoot": 0.004, "volume_strength": 0.6,
        }
        return [
            {**base, "trade_id": "w", "session_date": "1", "symbol": "1", "stock_name": "W", "outcome": "WINNER", "directional_vwap_extension": 0.005, "realized_net_pnl": 1000.0},
            {**base, "trade_id": "l1", "session_date": "1", "symbol": "2", "stock_name": "L1", "outcome": "LOSER", "directional_vwap_extension": 0.03, "realized_net_pnl": -700.0},
            {**base, "trade_id": "l2", "session_date": "2", "symbol": "3", "stock_name": "L2", "outcome": "LOSER", "directional_vwap_extension": 0.04, "realized_net_pnl": -800.0},
        ]

    def test_gate_reports_saved_losses_without_rewriting_trades(self):
        gate = Gate("TEST", "VWAP", "test", lambda row: row["directional_vwap_extension"] <= 0.01)
        metrics, impacts = evaluate_gate(self.rows(), gate)
        self.assertEqual(metrics["avoided_losers"], 2)
        self.assertEqual(metrics["removed_winners"], 0)
        self.assertEqual(metrics["net_pnl_difference"], 1500.0)
        self.assertTrue(metrics["leave_one_out_positive"])
        self.assertEqual(sum(row["decision"] == "KEEP" for row in impacts), 1)

    def test_posthoc_gate_cannot_be_shadow_candidate(self):
        gate = Gate("POSTHOC", "SCORE", "test", lambda row: row["outcome"] == "WINNER", True)
        metrics, _impacts = evaluate_gate(self.rows(), gate)
        self.assertFalse(metrics["shadow_candidate"])


if __name__ == "__main__":
    unittest.main()
