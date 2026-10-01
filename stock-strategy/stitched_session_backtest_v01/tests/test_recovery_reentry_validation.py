from __future__ import annotations

from datetime import datetime, timedelta
import unittest

from mfe_profit_protection_study_v01.analysis import ResearchTrade
from stitched_session_backtest_v01.recovery_reentry_validation import (
    ReentryVariant,
    find_reentry,
    simulate_reentry,
    variants,
)
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint
from yuanta_live_runtime_v01.strategy import TAIPEI


class RecoveryReentryTests(unittest.TestCase):
    @staticmethod
    def _fixture():
        entry = datetime(2026, 10, 1, 9, 10, tzinfo=TAIPEI)
        ticks = []
        prices = [
            100.0, 98.0, 97.0, 99.0, 100.5, 100.7, 101.0, 102.0,
            103.0, 104.0, 105.0,
        ]
        for index, price in enumerate(prices):
            at = entry + timedelta(seconds=index * 30)
            ticks.append({
                "time": at, "price": price, "volume": 100.0,
                "bid": price - 0.1, "ask": price + 0.1,
            })
        trade = ResearchTrade(
            trade_id="20261001-TEST-091000", session_date="20261001",
            symbol="TEST", stock_name="測試股", side="LONG",
            entry_time=entry, entry_price=100.2, quantity=1000,
            initial_stop_price=96.7,
            points=tuple(
                PathPoint(
                    row["time"], row["bid"] - 0.1,
                    -4000.0 if index == 1 else (index - 3) * 1000.0,
                    0.0, False,
                )
                for index, row in enumerate(ticks[1:], 1)
            ),
            force_last_point_exit=True,
        )
        return trade, {"ticks": ticks}

    def test_grid_is_fixed(self):
        self.assertEqual(len(variants()), 9)

    def test_reentry_requires_structure_vwap_and_confirmation(self):
        trade, data = self._fixture()
        candidate, diagnostic = find_reentry(
            trade, data,
            first_exit_time=trade.entry_time + timedelta(seconds=30),
            breakout_boundary_price=100.0,
            variant=ReentryVariant(0.0, 30),
            capital_twd=190_000,
        )
        self.assertIsNotNone(candidate)
        self.assertGreaterEqual(candidate.decision_time, trade.entry_time + timedelta(seconds=150))
        self.assertGreater(candidate.entry_price, 100.0)
        self.assertEqual(diagnostic["reentry_reason"], "CONFIRMED")

    def test_reentry_uses_fresh_fill_and_only_one_second_leg(self):
        trade, data = self._fixture()
        result = simulate_reentry(
            trade, data, 100.0, ReentryVariant(0.0, 0),
        )
        self.assertTrue(result["reentry_taken"])
        self.assertEqual(result["reentry_count"], 1)
        self.assertGreater(result["reentry_price"], 100.0)
        self.assertIn("second_leg_net_pnl_twd", result)

    def test_no_reentry_when_breakout_is_not_recovered(self):
        trade, data = self._fixture()
        candidate, diagnostic = find_reentry(
            trade, data,
            first_exit_time=trade.entry_time + timedelta(seconds=30),
            breakout_boundary_price=110.0,
            variant=ReentryVariant(0.0, 0),
            capital_twd=190_000,
        )
        self.assertIsNone(candidate)
        self.assertEqual(
            diagnostic["reentry_reason"],
            "NO_CAUSAL_RECOVERY_BEFORE_LAST_ENTRY",
        )


if __name__ == "__main__":
    unittest.main()
