from __future__ import annotations

from datetime import datetime, timedelta
import unittest

from mfe_profit_protection_study_v01.analysis import ResearchTrade
from stitched_session_backtest_v01.early_stop_grace_validation import (
    GraceVariant,
    simulate_grace,
    variants,
)
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint
from yuanta_live_runtime_v01.strategy import TAIPEI


class EarlyStopGraceValidationTests(unittest.TestCase):
    @staticmethod
    def _trade(pnls: list[tuple[int, float]], *, reversal_at: int | None = None):
        entry = datetime(2026, 10, 1, 9, 10, tzinfo=TAIPEI)
        return ResearchTrade(
            trade_id="20261001-TEST-091000",
            session_date="20261001",
            symbol="TEST",
            stock_name="測試股",
            side="LONG",
            entry_time=entry,
            entry_price=100.0,
            quantity=1000,
            initial_stop_price=96.5,
            points=tuple(
                PathPoint(
                    entry + timedelta(seconds=seconds),
                    100.0 + pnl / 1000.0,
                    pnl,
                    0.0,
                    seconds == reversal_at,
                )
                for seconds, pnl in pnls
            ),
            force_last_point_exit=True,
        )

    def test_grid_is_fixed(self):
        self.assertEqual(len(variants()), 9)

    def test_pending_stop_expires_at_observed_price(self):
        trade = self._trade([(30, -200), (120, -900), (140, -950), (150, -1100)])
        row = simulate_grace(trade, GraceVariant(30, 0.20))
        self.assertEqual(row["exit_reason"], "RECOVERY_GRACE_EXPIRED")
        self.assertEqual(row["holding_seconds"], 150)
        self.assertEqual(row["net_pnl_twd"], -1100)
        self.assertTrue(row["pending_started"])
        self.assertFalse(row["pending_cancelled"])

    def test_recovery_cancels_pending_once_and_preserves_later_winner(self):
        trade = self._trade([
            (30, -200), (120, -900), (130, -100), (180, 500), (240, 2500),
        ])
        row = simulate_grace(trade, GraceVariant(30, 0.20))
        self.assertTrue(row["pending_cancelled"])
        self.assertEqual(row["checkpoint_action"], "PENDING_CANCELLED_BY_RECOVERY")
        self.assertEqual(row["exit_reason"], "HARD_EXIT")
        self.assertEqual(row["net_pnl_twd"], 2500)

    def test_hard_stop_stays_immediate_during_grace(self):
        trade = self._trade([(120, -900), (125, -3600), (160, 1000)])
        row = simulate_grace(trade, GraceVariant(60, 0.10))
        self.assertEqual(row["exit_reason"], "DISASTER_STOP_NEG_1R")
        self.assertEqual(row["holding_seconds"], 125)

    def test_existing_mfe_floor_stays_immediate(self):
        trade = self._trade([(30, 2800), (60, 900), (120, -900), (150, 3000)])
        row = simulate_grace(trade, GraceVariant(60, 0.10))
        self.assertEqual(row["exit_reason"], "BUFFERED_NET_MFE_PROFIT_PROTECTION")
        self.assertEqual(row["holding_seconds"], 60)

    def test_no_grace_without_original_trigger(self):
        trade = self._trade([(60, 500), (120, -900), (180, 2000)])
        row = simulate_grace(trade, GraceVariant(30, 0.10))
        self.assertFalse(row["pending_started"])
        self.assertEqual(row["checkpoint_action"], "HOLD")


if __name__ == "__main__":
    unittest.main()
