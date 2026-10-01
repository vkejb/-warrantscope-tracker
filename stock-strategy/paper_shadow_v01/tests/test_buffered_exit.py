from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from mfe_profit_protection_study_v01.analysis import ResearchTrade
from paper_shadow_v01.buffered_exit import (
    BufferedExitPolicy,
    ExistingExit,
    candidate_locked_r,
    simulate_buffered_exit,
)
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint
from yuanta_live_runtime_v01.strategy import TAIPEI


class BufferedExitTests(unittest.TestCase):
    @staticmethod
    def _trade(values):
        entry = datetime(2026, 10, 1, 9, 30, tzinfo=TAIPEI)
        return ResearchTrade(
            trade_id="paper", session_date="20261001", symbol="3094",
            stock_name="聯傑", side="LONG", entry_time=entry,
            entry_price=100.0, quantity=1000, initial_stop_price=96.5,
            points=tuple(
                PathPoint(entry + timedelta(seconds=seconds), price, pnl,
                          pnl / 100_000, False)
                for seconds, price, pnl in values
            ), force_last_point_exit=True,
        )

    def test_locked_floor_schedule(self):
        policy = BufferedExitPolicy("TEST", 0.75, 0.30)
        self.assertIsNone(candidate_locked_r(policy, 0.74))
        self.assertEqual(candidate_locked_r(policy, 0.75), 0.30)
        self.assertEqual(candidate_locked_r(policy, 1.6), 0.8)
        self.assertEqual(candidate_locked_r(policy, 2.5), 1.5)

    def test_buffered_exit_retains_positive_net(self):
        trade = self._trade(((60, 103.0, 2800.0), (90, 101.0, 500.0),
                             (150, 99.0, -1000.0)))
        result = simulate_buffered_exit(
            trade, BufferedExitPolicy("TEST", 0.75, 0.30)
        )
        self.assertEqual(result.exit_reason, "BUFFERED_NET_MFE_PROFIT_PROTECTION")
        self.assertEqual(result.net_pnl, 500.0)

    def test_stop_loss_keeps_priority(self):
        trade = self._trade(((60, 96.0, -4000.0), (120, 101.0, 500.0)))
        result = simulate_buffered_exit(
            trade, BufferedExitPolicy("TEST", 0.75, 0.30)
        )
        self.assertEqual(result.exit_reason, "STOP_LOSS")

    def test_existing_reversal_remains_authoritative(self):
        trade = self._trade(((60, 100.0, -100.0), (120, 101.0, 500.0)))
        existing = ExistingExit(
            trade.entry_time + timedelta(seconds=60), 99.9, -200.0,
            "SIGNAL_REVERSAL",
        )
        result = simulate_buffered_exit(
            trade, BufferedExitPolicy("TEST", 0.75, 0.30),
            existing_exit=existing,
        )
        self.assertEqual(result.exit_reason, "SIGNAL_REVERSAL")
        self.assertEqual(result.net_pnl, -200.0)

    def test_existing_mfe_exit_is_replaced_by_shadow_variant(self):
        trade = self._trade(((60, 104.0, 3800.0), (120, 106.0, 5800.0),
                             (180, 105.0, 4800.0)))
        existing = ExistingExit(
            trade.entry_time + timedelta(seconds=60), 104.0, 3800.0,
            "MFE_PROFIT_PROTECTION",
        )
        result = simulate_buffered_exit(
            trade, BufferedExitPolicy("TEST", 0.75, 0.30),
            existing_exit=existing,
        )
        self.assertGreater(result.exit_time, existing.at)


if __name__ == "__main__":
    unittest.main()
