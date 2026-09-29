from __future__ import annotations

import unittest

from signal_quality_diagnostics_v01.analysis import (
    _correlations,
    _outcome_summary,
    _score_buckets,
)


class SignalQualityDiagnosticsTests(unittest.TestCase):
    def rows(self):
        return [
            {
                "outcome": "WINNER", "side": "LONG", "score": 0.50,
                "volume_strength": 0.40, "large_trade_strength": 0.60,
                "realized_net_pnl": 1000.0, "held_mfe_net_pnl": 1500.0,
            },
            {
                "outcome": "LOSER", "side": "LONG", "score": 0.80,
                "volume_strength": 0.90, "large_trade_strength": 1.00,
                "realized_net_pnl": -1000.0, "held_mfe_net_pnl": 100.0,
            },
            {
                "outcome": "LOSER", "side": "SHORT", "score": 0.70,
                "volume_strength": 0.80, "large_trade_strength": 0.90,
                "realized_net_pnl": -500.0, "held_mfe_net_pnl": 200.0,
            },
        ]

    def test_outcome_summary_uses_strength_values(self):
        rows = {row["outcome"]: row for row in _outcome_summary(self.rows())}
        self.assertEqual(rows["WINNER"]["average_volume_strength"], 0.4)
        self.assertAlmostEqual(rows["LOSER"]["average_large_trade_strength"], 0.95)

    def test_score_buckets_are_fixed_and_exhaustive(self):
        buckets = _score_buckets(self.rows())
        self.assertEqual(sum(row["trades"] for row in buckets), 3)
        self.assertEqual(buckets[0]["winners"], 1)
        self.assertEqual(buckets[-1]["losers"], 1)

    def test_correlations_do_not_claim_positive_relationship(self):
        rows = self.rows() + [{
            "outcome": "WINNER", "side": "SHORT", "score": 0.55,
            "volume_strength": 0.45, "large_trade_strength": 0.65,
            "realized_net_pnl": 500.0, "held_mfe_net_pnl": 800.0,
        }]
        correlations = {row["feature"]: row for row in _correlations(rows)}
        self.assertLess(correlations["score"]["spearman_vs_realized_pnl"], 0)
        self.assertFalse(correlations["score"]["positive_relationship_supported"])


if __name__ == "__main__":
    unittest.main()
