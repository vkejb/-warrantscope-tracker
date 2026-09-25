from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from yuanta_intraday_shadow_v01.exit_parameter_sweep import (
    PathPoint,
    TradePath,
)
from yuanta_intraday_shadow_v01.trailing_exit_sweep import (
    ORIGINAL_VARIANT_ID,
    TRAILING_VARIANTS,
    TrailingVariant,
    simulate_trailing_variant,
)


TAIPEI = ZoneInfo("Asia/Taipei")


class TrailingExitSweepTests(unittest.TestCase):
    def _path(self):
        decision = datetime(
            2026, 9, 24, 9, 9,
            tzinfo=TAIPEI,
        )

        return TradePath(
            session_date="20260924",
            stock_id="3605",
            stock_name="測試股",
            decision_time=decision,
            entry_price=100.0,
            quantity=1000,
            notional_used=100000.0,
            points=(
                PathPoint(
                    at=decision + timedelta(minutes=1),
                    exit_price=101.9,
                    projected_net_pnl=1573.0,
                    current_return=0.01573,
                    reversal=False,
                ),
                PathPoint(
                    at=decision + timedelta(minutes=2),
                    exit_price=101.2,
                    projected_net_pnl=875.0,
                    current_return=0.00875,
                    reversal=False,
                ),
                PathPoint(
                    at=decision + timedelta(minutes=3),
                    exit_price=100.8,
                    projected_net_pnl=475.0,
                    current_return=0.00475,
                    reversal=False,
                ),
                PathPoint(
                    at=decision.replace(hour=13, minute=20),
                    exit_price=100.7,
                    projected_net_pnl=375.0,
                    current_return=0.00375,
                    reversal=False,
                ),
            ),
        )

    def test_expected_trailing_grid(self):
        values = [
            (v.activation, v.drawdown)
            for v in TRAILING_VARIANTS
        ]

        self.assertEqual(
            values,
            [
                (0.015, 0.006),
                (0.015, 0.008),
                (0.015, 0.010),
                (0.020, 0.020),
            ],
        )
        self.assertEqual(
            ORIGINAL_VARIANT_ID,
            "TRAIL_2_0_2_0",
        )

    def test_tight_trailing_exits_after_six_tenths_pp_giveback(self):
        result = simulate_trailing_variant(
            self._path(),
            TrailingVariant("TEST", 0.015, 0.006),
        )

        self.assertTrue(result["scorable"])
        self.assertTrue(result["trailing_armed"])
        self.assertTrue(result["trailing_triggered"])
        self.assertEqual(
            result["exit_reason"],
            "TRAILING_PROFIT",
        )
        self.assertEqual(
            result["exit_time"],
            "2026-09-24T09:11:00+08:00",
        )
        self.assertEqual(
            result["giveback_from_peak_to_exit_twd"],
            698.0,
        )

    def test_one_point_zero_pp_variant_waits_for_larger_drawdown(self):
        result = simulate_trailing_variant(
            self._path(),
            TrailingVariant("TEST", 0.015, 0.010),
        )

        self.assertTrue(result["scorable"])
        self.assertEqual(
            result["exit_reason"],
            "TRAILING_PROFIT",
        )
        self.assertEqual(
            result["exit_time"],
            "2026-09-24T09:12:00+08:00",
        )

    def test_original_two_percent_activation_never_arms(self):
        result = simulate_trailing_variant(
            self._path(),
            TrailingVariant("TEST", 0.020, 0.020),
        )

        self.assertTrue(result["scorable"])
        self.assertFalse(result["trailing_armed"])
        self.assertFalse(result["trailing_triggered"])
        self.assertEqual(
            result["exit_reason"],
            "HARD_EXIT",
        )
        self.assertTrue(result["post_exit_new_high"] is False)


if __name__ == "__main__":
    unittest.main()
