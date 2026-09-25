from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from yuanta_intraday_shadow_v01.exit_parameter_sweep import (
    ExitVariant,
    PathPoint,
    TradePath,
    simulate_variant,
    variants,
)


TAIPEI = ZoneInfo("Asia/Taipei")


class ExitParameterSweepTests(unittest.TestCase):
    def _path(self):
        decision = datetime(2026, 9, 24, 9, 9, tzinfo=TAIPEI)

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
                    exit_price=98.5,
                    projected_net_pnl=-1700.0,
                    current_return=-0.017,
                    reversal=False,
                ),
                PathPoint(
                    at=decision + timedelta(minutes=2),
                    exit_price=100.3,
                    projected_net_pnl=100.0,
                    current_return=0.001,
                    reversal=False,
                ),
                PathPoint(
                    at=decision.replace(hour=13, minute=20),
                    exit_price=100.4,
                    projected_net_pnl=200.0,
                    current_return=0.002,
                    reversal=False,
                ),
            ),
        )

    def test_expected_parameter_grid(self):
        grid = variants()

        self.assertEqual(len(grid), 12)
        self.assertEqual(
            [v.stop_value for v in grid if v.stop_type == "FIXED_TWD"],
            [1500.0, 2000.0, 2500.0, 3000.0, 4000.0, 5000.0],
        )
        self.assertEqual(
            [v.stop_value for v in grid if v.stop_type == "PERCENT_NET_RETURN"],
            [0.008, 0.010, 0.012, 0.015, 0.020, 0.025],
        )

    def test_fixed_stop_records_counterfactual_recovery(self):
        result = simulate_variant(
            self._path(),
            ExitVariant("FIXED_1500", "FIXED_TWD", 1500.0),
        )

        self.assertTrue(result["scorable"])
        self.assertEqual(result["exit_reason"], "STOP_LOSS_FIXED")
        self.assertTrue(result["recovered_positive_net_after_stop"])
        self.assertTrue(result["recovered_entry_price_after_stop"])
        self.assertLess(result["held_mae_net_return"], 0)
        self.assertEqual(result["held_mfe_net_return"], 0.0)
        self.assertGreater(result["full_path_mfe_net_return"], 0.0)
        self.assertGreater(result["post_exit_best_net_pnl"], 0.0)

    def test_percent_stop_uses_net_return_not_raw_price_change(self):
        result = simulate_variant(
            self._path(),
            ExitVariant("PERCENT_0.8", "PERCENT_NET_RETURN", 0.008),
        )

        self.assertTrue(result["scorable"])
        self.assertEqual(result["exit_reason"], "STOP_LOSS_PERCENT")
        self.assertEqual(result["exit_time"], "2026-09-24T09:10:00+08:00")

    def test_wider_stop_survives_initial_drawdown(self):
        result = simulate_variant(
            self._path(),
            ExitVariant("PERCENT_2.0", "PERCENT_NET_RETURN", 0.020),
        )

        self.assertTrue(result["scorable"])
        self.assertEqual(result["exit_reason"], "HARD_EXIT")
        # The wider stop survives the drawdown and exits positive after
        # commission and day-trade sell tax are included.
        self.assertGreater(result["net_pnl"], 0.0)


if __name__ == "__main__":
    unittest.main()
