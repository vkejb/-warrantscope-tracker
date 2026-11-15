import unittest

from dynamic_market_scanner_shadow_v01.v2_gate_exit_diagnostic import replay
from yuanta_intraday_shadow_v01.direction_follow_backtest import _projected_net_pnl


class V2GateExitDiagnosticTests(unittest.TestCase):
    def test_rs_gate_precedes_reservation_and_keeps_later_candidate(self):
        rows = [
            {"event": "CANDIDATE_SEEN", "decision_time": "2026-11-12T09:12:30+08:00",
             "stock_id": "6770", "score": 0.6, "gate_passed": True,
             "relative_strength_5m": 0.0015},
            {"event": "CANDIDATE_SEEN", "decision_time": "2026-11-12T09:13:30+08:00",
             "stock_id": "1528", "score": 0.73, "gate_passed": True,
             "relative_strength_5m": 0.0223},
        ]
        self.assertEqual(replay.choose_immediate(rows)["stock_id"], "1528")

    def test_tie_break_score_then_symbol(self):
        rows = [
            {"event": "CANDIDATE_SEEN", "decision_time": "t", "stock_id": "B",
             "score": 0.8, "gate_passed": True, "relative_strength_5m": 0.02},
            {"event": "CANDIDATE_SEEN", "decision_time": "t", "stock_id": "A",
             "score": 0.8, "gate_passed": True, "relative_strength_5m": 0.02},
        ]
        self.assertEqual(replay.choose_immediate(rows)["stock_id"], "A")

    def test_breakeven_tick_uses_after_cost_model(self):
        floor = replay.breakeven_tick(46.75, 4000)
        self.assertGreaterEqual(_projected_net_pnl("LONG", 46.75, floor, 4000)[3], 0)
        self.assertLess(_projected_net_pnl("LONG", 46.75, floor - 0.05, 4000)[3], 0)

    def test_research_policy_never_orders(self):
        self.assertEqual(replay.POLICY["actual_orders"], 0)
        self.assertEqual(replay.POLICY["actual_fills"], 0)
        self.assertEqual(replay.POLICY["broker_order_calls"], 0)


if __name__ == "__main__":
    unittest.main()
