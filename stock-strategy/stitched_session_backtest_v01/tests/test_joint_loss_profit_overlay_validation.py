from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from mfe_profit_protection_study_v01.analysis import ResearchTrade
from stitched_session_backtest_v01.joint_loss_profit_overlay_validation import (
    JointVariant,
    simulate_joint,
    variants,
)
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint
from yuanta_live_runtime_v01.strategy import TAIPEI


class JointLossProfitOverlayValidationTests(unittest.TestCase):
    @staticmethod
    def _trade(values):
        entry = datetime(2026, 10, 1, 9, 30, tzinfo=TAIPEI)
        return ResearchTrade(
            trade_id="test", session_date="20261001", symbol="3094",
            stock_name="聯傑", side="LONG", entry_time=entry,
            entry_price=100.0, quantity=1000, initial_stop_price=96.5,
            points=tuple(
                PathPoint(entry + timedelta(seconds=seconds), price, pnl,
                          pnl / 100_000, False)
                for seconds, price, pnl in values
            ),
            force_last_point_exit=True,
        )

    def test_grid_is_fixed_and_contains_production_reference(self):
        rows = variants()
        self.assertEqual(len(rows), 21)
        self.assertIn(
            "CURRENT_LOSS__MFE_V1", {variant.name for variant in rows},
        )

    def test_earlier_hard_stop_wins_over_checkpoint(self):
        trade = self._trade(((60, 96.0, -4000.0), (120, 98.0, -2000.0)))
        row = simulate_joint(trade, JointVariant("RECOVERY_AWARE_120S", "MFE_V1"))
        self.assertEqual(row["exit_reason"], "DISASTER_STOP_NEG_1R")
        self.assertEqual(row["checkpoint_action"], "NOT_APPLICABLE")

    def test_recovery_aware_checkpoint_is_one_time(self):
        trade = self._trade(((60, 98.0, -2000.0), (120, 99.5, -500.0),
                             (180, 98.0, -2000.0), (240, 99.9, -100.0)))
        row = simulate_joint(trade, JointVariant("RECOVERY_AWARE_120S", "NO_MFE"))
        self.assertEqual(row["checkpoint_action"], "HOLD")
        self.assertEqual(row["exit_time"], trade.points[-1].at.isoformat())

    def test_mfe_variant_is_independent_of_loss_overlay(self):
        trade = self._trade(((60, 107.0, 6500.0), (90, 105.0, 4500.0),
                             (150, 104.0, 3500.0)))
        loose = simulate_joint(trade, JointVariant("CURRENT_LOSS", "MFE_LOOSE"))
        aggressive = simulate_joint(
            trade, JointVariant("CURRENT_LOSS", "MFE_AGGRESSIVE")
        )
        self.assertEqual(aggressive["exit_reason"], "MFE_PROFIT_PROTECTION")
        self.assertGreater(aggressive["max_locked_profit_r"], loose["max_locked_profit_r"])

    def test_net_mfe_waits_for_net_one_r_and_uses_net_floor(self):
        trade = self._trade(((60, 104.0, 3000.0), (90, 105.0, 4000.0),
                             (150, 99.5, -500.0), (180, 101.0, 500.0)))
        row = simulate_joint(trade, JointVariant("CURRENT_LOSS", "NET_MFE_V1"))
        self.assertEqual(row["exit_reason"], "NET_MFE_PROFIT_PROTECTION")
        self.assertEqual(row["mfe_activation_time"], trade.points[1].at.isoformat())
        self.assertEqual(row["max_locked_profit_r"], 0.0)


if __name__ == "__main__":
    unittest.main()
