from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from mfe_profit_protection_study_v01.analysis import ResearchTrade
from stitched_session_backtest_v01.path_failure_stop_validation import CausalState
from stitched_session_backtest_v01.recovery_aware_exit_validation import (
    RecoveryVariant,
    simulate_recovery_aware,
    variants,
)
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint
from yuanta_live_runtime_v01.strategy import TAIPEI


class RecoveryAwareExitValidationTests(unittest.TestCase):
    def test_grid_is_fixed(self):
        rows = variants()
        self.assertEqual(len(rows), 24)
        self.assertEqual({row.loss_threshold_r for row in rows}, {0.0, 0.2, 0.3, 0.4})
        self.assertEqual(
            {row.maximum_recovery_from_mae_r for row in rows},
            {0.1, 0.2, 0.3},
        )
        self.assertEqual({row.require_flow_2of3 for row in rows}, {False, True})

    @staticmethod
    def _trade(values):
        entry = datetime(2026, 10, 1, 9, 30, tzinfo=TAIPEI)
        return ResearchTrade(
            trade_id="test", session_date="20261001", symbol="3094",
            stock_name="聯傑", side="LONG", entry_time=entry,
            entry_price=100.0, quantity=1000, initial_stop_price=96.5,
            points=tuple(
                PathPoint(
                    entry + timedelta(seconds=seconds), price, pnl,
                    pnl / 100_000, False,
                )
                for seconds, price, pnl in values
            ),
            force_last_point_exit=True,
        )

    @staticmethod
    def _states(count):
        state = CausalState(True, True, -0.01, True, 2, True)
        return (state,) * count

    def test_recovery_at_checkpoint_holds_baseline_exit(self):
        trade = self._trade(((60, 98.0, -2000.0), (120, 99.5, -500.0),
                             (180, 99.9, -100.0)))
        variant = RecoveryVariant("TEST", 0.0, 0.2, False)
        row = simulate_recovery_aware(trade, {}, 99.0, variant, self._states(3))
        self.assertEqual(row["checkpoint_action"], "HOLD_BASELINE")
        self.assertEqual(row["net_pnl_twd"], -100.0)

    def test_earlier_hard_stop_cannot_be_overridden(self):
        trade = self._trade(((60, 96.0, -4000.0), (120, 98.0, -2000.0)))
        variant = RecoveryVariant("TEST", 0.0, 0.2, False)
        row = simulate_recovery_aware(trade, {}, 99.0, variant, self._states(2))
        self.assertEqual(row["checkpoint_action"], "EARLIER_BASELINE_EXIT_WON")
        self.assertEqual(row["exit_reason"], "DISASTER_STOP_NEG_1R")
        self.assertEqual(row["net_pnl_twd"], -4000.0)


if __name__ == "__main__":
    unittest.main()
