from __future__ import annotations

from datetime import datetime, timedelta
import unittest

from mfe_profit_protection_study_v01.analysis import ResearchTrade
from stitched_session_backtest_v01.exit_first_profitability_study import (
    ExitVariant,
    locked_floor_r,
    simulate_exit,
)
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint
from yuanta_live_runtime_v01.strategy import TAIPEI


class ExitFirstProfitabilityTests(unittest.TestCase):
    def test_gentle_low_mfe_boundaries(self):
        self.assertIsNone(locked_floor_r("GENTLE", 0.2999))
        self.assertEqual(locked_floor_r("GENTLE", 0.30), -0.20)
        self.assertEqual(locked_floor_r("GENTLE", 0.50), 0.0)
        self.assertEqual(locked_floor_r("GENTLE", 0.75), 0.25)
        self.assertEqual(locked_floor_r("GENTLE", 1.49), 0.25)
        self.assertIsNone(locked_floor_r("GENTLE", 1.50))

    def test_tight_low_mfe_boundaries(self):
        self.assertIsNone(locked_floor_r("TIGHT", 0.2499))
        self.assertEqual(locked_floor_r("TIGHT", 0.25), -0.10)
        self.assertEqual(locked_floor_r("TIGHT", 0.50), 0.10)
        self.assertEqual(locked_floor_r("TIGHT", 0.75), 0.35)
        self.assertIsNone(locked_floor_r(None, 5.0))

    def test_sixty_second_early_failure_requires_loss_and_no_progress(self):
        entry = datetime(2026, 10, 1, 9, 30, tzinfo=TAIPEI)
        trade = ResearchTrade(
            trade_id="test",
            session_date="20261001",
            symbol="3094",
            stock_name="聯傑",
            side="LONG",
            entry_time=entry,
            entry_price=100.0,
            quantity=1000,
            initial_stop_price=98.0,
            points=(
                PathPoint(entry + timedelta(seconds=30), 99.0, -1000.0, -0.01, False),
                PathPoint(entry + timedelta(seconds=60), 98.5, -1500.0, -0.015, False),
                PathPoint(entry + timedelta(seconds=90), 98.0, -2000.0, -0.02, False),
            ),
            force_last_point_exit=True,
        )
        result = simulate_exit(
            trade,
            {},
            ExitVariant("EARLY_60", early_failure_seconds=60),
            breakout_boundary_price=99.0,
        )
        self.assertEqual(result["exit_reason"], "EARLY_FAILURE_NO_PROGRESS")
        self.assertEqual(result["exit_time"], (entry + timedelta(seconds=60)).isoformat())
        self.assertEqual(result["net_pnl_twd"], -1500.0)


if __name__ == "__main__":
    unittest.main()
