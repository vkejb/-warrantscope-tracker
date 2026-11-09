from __future__ import annotations

import unittest

from dynamic_market_scanner_shadow_v01.scanner import (
    DynamicCandidateScanner,
    affordable_quantity,
)
from dynamic_market_scanner_shadow_v01.aggregate import _metrics


def candidate(symbol: str, *, price: float = 100.0, gate: str = "CONFIRMATIONS_INCOMPLETE", score: float = 0.5) -> dict:
    return {
        "stock_id": symbol,
        "stock_name": symbol,
        "entry_price": price,
        "quantity": 1000,
        "gate_reason": gate,
        "score": score,
    }


class AffordabilityTest(unittest.TestCase):
    def test_price_above_cap_is_rejected(self):
        self.assertEqual(affordable_quantity(190.01), (0, 0, 0.0))

    def test_exact_cap_is_unaffordable_after_fee(self):
        self.assertEqual(affordable_quantity(190.0), (0, 0, 0.0))

    def test_below_cap_keeps_whole_lot(self):
        quantity, fee, cash = affordable_quantity(189.0)
        self.assertEqual(quantity, 1000)
        self.assertGreater(fee, 0)
        self.assertLessEqual(cash, 190_000)


class LifecycleTest(unittest.TestCase):
    def test_observing_candidate_expires_when_signal_disappears(self):
        scanner = DynamicCandidateScanner()
        scanner.process({"decision_time": "t1", "candidates": [candidate("1111")]})
        scanner.process({"decision_time": "t2", "candidates": []})
        self.assertEqual([row["state"] for row in scanner.events], ["OBSERVING", "EXPIRED"])

    def test_selected_candidate_is_paper_only(self):
        scanner = DynamicCandidateScanner()
        scanner.process({
            "decision_time": "t1",
            "selected_stock_id": "2222",
            "candidates": [candidate("2222", gate="MARKET_GATE_PASSED")],
        })
        row = scanner.events[-1]
        self.assertEqual(row["state"], "PAPER_SELECTED")
        self.assertEqual(row["actual_orders"], 0)
        self.assertEqual(row["broker_order_calls"], 0)

    def test_gate_failure_is_not_promoted(self):
        scanner = DynamicCandidateScanner()
        scanner.process({
            "decision_time": "t1",
            "candidates": [candidate("3333", gate="ANTI_CHASE_VWAP_EXTENSION")],
        })
        self.assertEqual(scanner.events[-1]["state"], "REJECTED")


class AggregateTest(unittest.TestCase):
    def test_drawdown_and_profit_factor_include_losses(self):
        result = _metrics([100.0, -300.0, 50.0])
        self.assertEqual(result["net_pnl_twd"], -150.0)
        self.assertEqual(result["maximum_drawdown_twd"], 300.0)
        self.assertAlmostEqual(result["profit_factor"], 0.5)


if __name__ == "__main__":
    unittest.main()
