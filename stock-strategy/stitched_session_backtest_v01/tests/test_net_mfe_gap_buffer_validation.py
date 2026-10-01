from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from mfe_profit_protection_study_v01.analysis import ResearchTrade
from stitched_session_backtest_v01.net_mfe_gap_buffer_validation import (
    BufferVariant,
    candidate_floor_r,
    simulate_buffered,
    variants,
)
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint
from yuanta_live_runtime_v01.strategy import TAIPEI


class NetMfeGapBufferValidationTests(unittest.TestCase):
    @staticmethod
    def _trade(values):
        entry = datetime(2026, 10, 1, 9, 30, tzinfo=TAIPEI)
        return ResearchTrade(
            trade_id="test", session_date="20261001", symbol="2033",
            stock_name="佳大", side="LONG", entry_time=entry, entry_price=100.0,
            quantity=1000, initial_stop_price=96.5,
            points=tuple(
                PathPoint(entry + timedelta(seconds=seconds), price, pnl, pnl / 100_000, False)
                for seconds, price, pnl in values
            ), force_last_point_exit=True,
        )

    def test_grid_is_declared(self):
        self.assertEqual(len(variants()), 52)
        self.assertTrue(
            all(row.initial_lock_buffer_r < row.activation_r for row in variants())
        )

    def test_candidate_floor_never_drops_below_buffer(self):
        self.assertIsNone(candidate_floor_r(0.74, activation_r=0.75, initial_buffer_r=0.2))
        self.assertEqual(candidate_floor_r(0.75, activation_r=0.75, initial_buffer_r=0.2), 0.2)
        self.assertEqual(candidate_floor_r(1.6, activation_r=0.75, initial_buffer_r=0.2), 0.8)

    def test_buffer_can_trigger_before_zero_net_gap(self):
        trade = self._trade(((60, 103.0, 2800.0), (90, 101.0, 500.0),
                             (150, 99.5, -500.0)))
        row = simulate_buffered(trade, BufferVariant("PLAIN_120S", 0.75, 0.20))
        self.assertEqual(row["exit_reason"], "BUFFERED_NET_MFE_PROFIT_PROTECTION")
        self.assertEqual(row["net_pnl_twd"], 500.0)

    def test_hard_stop_keeps_priority(self):
        trade = self._trade(((60, 96.0, -4000.0), (120, 98.0, -2000.0)))
        row = simulate_buffered(trade, BufferVariant("RECOVERY_AWARE_120S", 1.0, 0.2))
        self.assertEqual(row["exit_reason"], "DISASTER_STOP_NEG_1R")

    def test_wider_hard_stop_uses_observed_fill_and_can_survive_one_r(self):
        trade = self._trade(((60, 96.0, -4000.0), (120, 101.0, 500.0)))
        variant = BufferVariant("RECOVERY_AWARE_120S", 0.75, 0.30)
        normal = simulate_buffered(trade, variant)
        wider = simulate_buffered(trade, variant, hard_stop_r=1.25)
        self.assertEqual(normal["exit_reason"], "DISASTER_STOP_NEG_1R")
        self.assertEqual(wider["net_pnl_twd"], 500.0)


if __name__ == "__main__":
    unittest.main()
