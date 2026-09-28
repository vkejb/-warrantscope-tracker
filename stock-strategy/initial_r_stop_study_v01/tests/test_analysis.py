from datetime import datetime, timedelta
import unittest

from initial_r_stop_study_v01.analysis import StopVariant, _simulate
from mfe_profit_protection_study_v01.analysis import ResearchTrade, derive_initial_stop_price
from yuanta_intraday_shadow_v01.direction_follow_backtest import TAIPEI
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint


class InitialRStopTests(unittest.TestCase):
    def trade(self, pnls):
        start = datetime(2026, 9, 24, 10, 0, tzinfo=TAIPEI)
        entry = 100.0
        stop = derive_initial_stop_price("LONG", entry, 1000, 3500.0)
        points = tuple(
            PathPoint(
                at=start + timedelta(seconds=index + 1),
                exit_price=100.0 + pnl / 1000,
                projected_net_pnl=float(pnl),
                current_return=float(pnl) / 100000,
                reversal=False,
            )
            for index, pnl in enumerate(pnls)
        )
        return ResearchTrade(
            trade_id="test", session_date="20260924", symbol="2330",
            stock_name="台積電", side="LONG", entry_time=start,
            entry_price=entry, quantity=1000, initial_stop_price=stop,
            points=points, force_last_point_exit=True,
        )

    def test_half_r_stops_at_net_minus_1750(self):
        result = _simulate(self.trade([-1000, -1750, 2000]), StopVariant("STOP_NEG_0_5R", 0.5))
        self.assertEqual(result["net_pnl"], -1750)
        self.assertEqual(result["exit_reason"], "STOP_NEG_0_5R")

    def test_half_r_does_not_stop_early(self):
        result = _simulate(self.trade([-1749, 500]), StopVariant("STOP_NEG_0_5R", 0.5))
        self.assertEqual(result["net_pnl"], 500)
        self.assertEqual(result["exit_reason"], "HARD_EXIT")

    def test_zero_r_exits_on_first_non_positive_quote(self):
        result = _simulate(self.trade([-300, 2000]), StopVariant("STOP_0R", 0.0))
        self.assertEqual(result["net_pnl"], -300)
        self.assertEqual(result["exit_reason"], "STOP_0R")

    def test_stop_has_priority_over_mfe(self):
        result = _simulate(self.trade([4000, -1800]), StopVariant("STOP_NEG_0_5R", 0.5))
        self.assertEqual(result["exit_reason"], "STOP_NEG_0_5R")


if __name__ == "__main__":
    unittest.main()
