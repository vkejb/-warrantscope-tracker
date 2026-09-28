from datetime import datetime, timedelta
import unittest

from current_indicator_stop_study_v01.analysis import (
    CurrentIndicatorVariant,
    simulate_current_indicator_stop,
)
from mfe_profit_protection_study_v01.analysis import ResearchTrade, derive_initial_stop_price
from yuanta_intraday_shadow_v01.direction_follow_backtest import TAIPEI
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint
from yuanta_intraday_shadow_v01.flow_exit_sweep import FlowSnapshot


NOW = datetime(2026, 9, 24, 10, 0, tzinfo=TAIPEI)


def snapshot(at, flow=-0.4, large=-0.3, book=-0.2):
    return FlowSnapshot(
        at=at, buy_qty_30=10, sell_qty_30=20, buy_qty_60=20, sell_qty_60=40,
        buy_sell_ratio_30=0.5, sell_buy_ratio_30=2.0, net_aggressive_30=-10,
        normalized_delta_30=flow, net_aggressive_60=-20, normalized_delta_60=flow,
        buy_qty_30_previous=10, sell_qty_30_previous=20, buy_decay_from_peak=0.5,
        sell_increase_vs_previous=0.5, large_threshold=5, large_buy_qty_60=5,
        large_sell_qty_60=10, large_buy_sell_ratio_60=0.5,
        large_trade_delta_60=large, max_consecutive_sell_ticks_30=3,
        average_buy_trade_size_30=2, average_sell_trade_size_30=4,
        book_imbalance=book,
    )


def trade(pnls):
    stop = derive_initial_stop_price("LONG", 100.0, 1000, 3500.0)
    points = tuple(
        PathPoint(
            at=NOW + timedelta(seconds=30 * (index + 1)),
            exit_price=100 + pnl / 1000,
            projected_net_pnl=float(pnl),
            current_return=float(pnl) / 100000,
            reversal=False,
        )
        for index, pnl in enumerate(pnls)
    )
    return ResearchTrade(
        trade_id="test", session_date="20260924", symbol="2330",
        stock_name="台積電", side="LONG", entry_time=NOW, entry_price=100,
        quantity=1000, initial_stop_price=stop, points=points,
        force_last_point_exit=True,
    )


class CurrentIndicatorStopTests(unittest.TestCase):
    def test_quarter_r_requires_two_consecutive_snapshots(self):
        variant = CurrentIndicatorVariant("TEST", 0.25)
        rows = (snapshot(NOW + timedelta(seconds=30)), snapshot(NOW + timedelta(seconds=60)))
        result = simulate_current_indicator_stop(trade([-900, -1000, 1000]), {}, variant, rows)
        self.assertEqual(result["exit_reason"], "INDICATOR_STOP")
        self.assertEqual(result["net_pnl"], -1000)

    def test_quarter_r_does_not_trigger_above_activation_loss(self):
        variant = CurrentIndicatorVariant("TEST", 0.25)
        rows = (snapshot(NOW + timedelta(seconds=30)), snapshot(NOW + timedelta(seconds=60)))
        result = simulate_current_indicator_stop(trade([-800, -850]), {}, variant, rows)
        self.assertEqual(result["exit_reason"], "HARD_EXIT")

    def test_half_r_activation_waits_for_deeper_loss(self):
        variant = CurrentIndicatorVariant("TEST", 0.5)
        rows = (snapshot(NOW + timedelta(seconds=30)), snapshot(NOW + timedelta(seconds=60)))
        result = simulate_current_indicator_stop(trade([-1000, -1200]), {}, variant, rows)
        self.assertEqual(result["exit_reason"], "HARD_EXIT")

    def test_disaster_stop_keeps_priority(self):
        variant = CurrentIndicatorVariant("TEST", 0.25)
        rows = (snapshot(NOW + timedelta(seconds=30)), snapshot(NOW + timedelta(seconds=60)))
        result = simulate_current_indicator_stop(trade([-900, -3600]), {}, variant, rows)
        self.assertEqual(result["exit_reason"], "DISASTER_STOP_NEG_1R")


if __name__ == "__main__":
    unittest.main()
