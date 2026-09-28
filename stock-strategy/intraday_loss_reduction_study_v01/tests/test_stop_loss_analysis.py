from datetime import datetime, timedelta, timezone
import unittest

from mfe_profit_protection_study_v01.analysis import ResearchTrade
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint
from intraday_loss_reduction_study_v01.stop_loss_analysis import StopVariant, simulate_stop

NOW = datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc)

def trade(pnls):
    points = tuple(PathPoint(
        at=NOW + timedelta(minutes=index + 1), exit_price=100 + value / 1000,
        projected_net_pnl=float(value), current_return=float(value) / 100000,
        reversal=False,
    ) for index, value in enumerate(pnls))
    return ResearchTrade(
        trade_id="test", session_date="20260924", symbol="3605", stock_name="宏致",
        side="LONG", entry_time=NOW, entry_price=100, quantity=1000,
        initial_stop_price=95, points=points, force_last_point_exit=True,
    )

class StopLossTests(unittest.TestCase):
    def test_gap_uses_first_observed_executable_pnl(self):
        result = simulate_stop(trade([-1000, -4300, 2000]), StopVariant("HARD_4000", 4000))
        self.assertEqual(result["net_pnl"], -4300)
        self.assertEqual(result["exit_reason"], "STOP_LOSS_HARD")
        self.assertTrue(result["later_recovered_positive"])

    def test_no_positive_mfe_time_stop(self):
        result = simulate_stop(trade([-100, -200, -300, -400]), StopVariant("TIME", 5000, time_stop_minutes=3, require_no_positive_mfe=True))
        self.assertEqual(result["exit_reason"], "TIME_STOP_NO_POSITIVE_MFE")
        self.assertEqual(result["net_pnl"], -300)

    def test_positive_mfe_prevents_no_positive_mfe_stop(self):
        result = simulate_stop(trade([100, -200, -300]), StopVariant("TIME", 5000, time_stop_minutes=2, require_no_positive_mfe=True))
        self.assertEqual(result["exit_reason"], "HARD_EXIT")

    def test_soft_stop_only_before_positive_mfe(self):
        result = simulate_stop(trade([-1000, -2200, 500]), StopVariant("HYBRID", 4000, soft_stop_twd=2000, require_no_positive_mfe=True))
        self.assertEqual(result["exit_reason"], "STOP_LOSS_SOFT_NO_POSITIVE_MFE")

if __name__ == "__main__":
    unittest.main()
