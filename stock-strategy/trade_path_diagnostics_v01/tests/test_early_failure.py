from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from mfe_profit_protection_study_v01.analysis import ResearchTrade
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint

from trade_path_diagnostics_v01.analysis import Outcome
from trade_path_diagnostics_v01.early_failure import (
    TradeCase,
    _grid_rows,
    _impact_rows,
    _robustness,
    build_checkpoint_rows,
)


UTC8 = timezone(timedelta(hours=8))


def _trade(prices: list[float], seconds: list[int], trade_id: str = "t") -> ResearchTrade:
    entry = datetime(2026, 9, 24, 9, 0, tzinfo=UTC8)
    points = tuple(
        PathPoint(
            at=entry + timedelta(seconds=offset),
            exit_price=price,
            projected_net_pnl=(price - 100) * 1000,
            current_return=0,
            reversal=False,
        )
        for price, offset in zip(prices, seconds)
    )
    return ResearchTrade(
        trade_id=trade_id, session_date="20260924", symbol=trade_id,
        stock_name=trade_id, side="LONG", entry_time=entry, entry_price=100,
        quantity=1000, initial_stop_price=96.5, points=points,
        force_last_point_exit=True,
    )


class EarlyFailureTests(unittest.TestCase):
    def test_checkpoint_uses_past_observation_and_future_fill(self):
        trade = _trade([99.0, 98.9], [299, 301])
        outcome = Outcome("SCORED", trade.entry_time + timedelta(minutes=20), 101, "X", 1000, 1200)
        row = build_checkpoint_rows(trade, outcome)[0]
        self.assertTrue(row["evaluable"])
        self.assertEqual(row["current_pnl"], -1000)
        self.assertAlmostEqual(row["execution_pnl"], -1100)
        self.assertEqual(row["observation_staleness_seconds"], 1)
        self.assertEqual(row["execution_delay_seconds"], 1)

    def test_original_exit_before_checkpoint_has_priority(self):
        trade = _trade([99.0, 98.9], [299, 301])
        outcome = Outcome("SCORED", trade.entry_time + timedelta(minutes=4), 99, "STOP", -1000, 240)
        self.assertFalse(build_checkpoint_rows(trade, outcome)[0]["evaluable"])

    def test_rule_requires_loss_and_low_mfe(self):
        loss = _trade([100.2, 98.9, 98.8], [60, 299, 301], "loss")
        winner = _trade([102.0, 98.9, 98.8], [60, 299, 301], "winner")
        end = loss.entry_time + timedelta(minutes=20)
        loss_out = Outcome("SCORED", end, 98, "X", -2000, 1200)
        win_out = Outcome("SCORED", end, 104, "X", 4000, 1200)
        cases = [
            TradeCase(loss, loss_out, tuple(build_checkpoint_rows(loss, loss_out))),
            TradeCase(winner, win_out, tuple(build_checkpoint_rows(winner, win_out))),
        ]
        impacts = _impact_rows(cases)
        candidate = [
            row for row in impacts
            if row["candidate_id"] == "T5_NEG0.20R_MFE0.10R"
        ]
        self.assertTrue(candidate[0]["triggered"])
        self.assertFalse(candidate[1]["triggered"])

    def test_leave_one_out_flags_single_trade_dependency(self):
        rows = [
            {"candidate_id": "c", "trade_id": "gain", "entry_time": "1", "original_pnl": -1000.0, "new_pnl": -500.0, "pnl_delta": 500.0, "triggered": True, "evaluable": True, "original_loser_exited": True, "incorrect_winner_exit": False, "checkpoint_minutes": 5, "negative_threshold_r": -0.2, "mfe_progress_threshold_r": 0.1},
            {"candidate_id": "c", "trade_id": "flat", "entry_time": "2", "original_pnl": 100.0, "new_pnl": 100.0, "pnl_delta": 0.0, "triggered": False, "evaluable": True, "original_loser_exited": False, "incorrect_winner_exit": False, "checkpoint_minutes": 5, "negative_threshold_r": -0.2, "mfe_progress_threshold_r": 0.1},
        ]
        grid = _grid_rows(rows)
        _, summary = _robustness(rows, grid)
        self.assertTrue(summary["c"]["fragile"])
        self.assertEqual(summary["c"]["loo_min_net_pnl_difference"], 0)

    def test_single_trade_grid_preserves_undefined_group_metrics(self):
        rows = [{
            "candidate_id": "c", "trade_id": "winner", "entry_time": "1",
            "original_pnl": 1000.0, "new_pnl": 1000.0, "pnl_delta": 0.0,
            "triggered": False, "evaluable": True,
            "original_loser_exited": False, "incorrect_winner_exit": False,
            "checkpoint_minutes": 5, "negative_threshold_r": -0.2,
            "mfe_progress_threshold_r": 0.1,
        }]
        grid = _grid_rows(rows)[0]
        self.assertIsNone(grid["average_loser"])
        self.assertIsNone(grid["average_loser_difference"])
        self.assertIsNone(grid["profit_factor"])
        self.assertIsNone(grid["profit_factor_difference"])


if __name__ == "__main__":
    unittest.main()
