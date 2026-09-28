from datetime import datetime, timedelta, timezone
import unittest

from intraday_loss_reduction_study_v01.indicator_stop_analysis import (
    IndicatorVariant,
    _adverse_components,
    _book_depleted,
    _indicator_triggered,
    _vwap_broken,
)
from mfe_profit_protection_study_v01.analysis import ResearchTrade
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint
from yuanta_intraday_shadow_v01.flow_exit_sweep import FlowSnapshot


NOW = datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc)


def snapshot(at, flow=-0.4, large=-0.3, book=-0.2):
    return FlowSnapshot(
        at=at,
        buy_qty_30=10,
        sell_qty_30=20,
        buy_qty_60=20,
        sell_qty_60=40,
        buy_sell_ratio_30=0.5,
        sell_buy_ratio_30=2.0,
        net_aggressive_30=-10,
        normalized_delta_30=flow,
        net_aggressive_60=-20,
        normalized_delta_60=flow,
        buy_qty_30_previous=10,
        sell_qty_30_previous=20,
        buy_decay_from_peak=0.5,
        sell_increase_vs_previous=0.5,
        large_threshold=5,
        large_buy_qty_60=5,
        large_sell_qty_60=10,
        large_buy_sell_ratio_60=0.5,
        large_trade_delta_60=large,
        max_consecutive_sell_ticks_30=3,
        average_buy_trade_size_30=2,
        average_sell_trade_size_30=4,
        book_imbalance=book,
    )


def trade(side="LONG"):
    point = PathPoint(
        at=NOW + timedelta(seconds=30),
        exit_price=99,
        projected_net_pnl=-1200,
        current_return=-0.01,
        reversal=False,
    )
    return ResearchTrade(
        trade_id="test",
        session_date="20260924",
        symbol="3605",
        stock_name="宏致",
        side=side,
        entry_time=NOW,
        entry_price=100,
        quantity=1000,
        initial_stop_price=95 if side == "LONG" else 105,
        points=(point,),
        force_last_point_exit=True,
    )


class IndicatorStopTests(unittest.TestCase):
    def test_adverse_components_are_side_aware(self):
        long_snapshot = snapshot(NOW)
        short_snapshot = snapshot(NOW, flow=0.4, large=0.3, book=0.2)
        self.assertEqual(_adverse_components("LONG", long_snapshot), 3)
        self.assertEqual(_adverse_components("SHORT", short_snapshot), 3)
        self.assertEqual(_adverse_components("SHORT", long_snapshot), 0)

    def test_two_consecutive_observations_are_required(self):
        variant = IndicatorVariant("TEST", consecutive=2)
        previous = snapshot(NOW)
        current = snapshot(NOW + timedelta(seconds=30))
        self.assertTrue(_indicator_triggered(
            variant, trade(), {}, current, previous, -100,
        ))
        self.assertFalse(_indicator_triggered(
            variant, trade(), {}, current, None, -100,
        ))
        stale_previous = snapshot(NOW - timedelta(seconds=30))
        self.assertFalse(_indicator_triggered(
            variant, trade(), {}, current, stale_previous, -100,
        ))

    def test_minimum_loss_gate_prevents_premature_stop(self):
        variant = IndicatorVariant("TEST", minimum_loss_twd=1000)
        previous = snapshot(NOW)
        current = snapshot(NOW + timedelta(seconds=30))
        self.assertFalse(_indicator_triggered(
            variant, trade(), {}, current, previous, -999,
        ))
        self.assertTrue(_indicator_triggered(
            variant, trade(), {}, current, previous, -1001,
        ))

    def test_positive_mfe_can_protect_recovering_trade(self):
        variant = IndicatorVariant(
            "TEST", require_no_positive_mfe=True,
        )
        previous = snapshot(NOW)
        current = snapshot(NOW + timedelta(seconds=30))
        self.assertFalse(_indicator_triggered(
            variant, trade(), {}, current, previous, -100, True,
        ))
        self.assertTrue(_indicator_triggered(
            variant, trade(), {}, current, previous, -100, False,
        ))

    def test_book_depletion_is_side_aware(self):
        long_previous = snapshot(NOW, book=0.15)
        long_current = snapshot(NOW + timedelta(seconds=30), book=-0.05)
        self.assertTrue(_book_depleted("LONG", long_previous, long_current))
        short_previous = snapshot(NOW, book=-0.15)
        short_current = snapshot(NOW + timedelta(seconds=30), book=0.05)
        self.assertTrue(_book_depleted("SHORT", short_previous, short_current))

    def test_vwap_break_is_side_aware(self):
        rows = [
            {"time": NOW, "price": 100.0, "volume": 10},
            {"time": NOW + timedelta(seconds=1), "price": 98.0, "volume": 10},
        ]
        data = {"ticks": rows, "tick_times": [row["time"] for row in rows]}
        self.assertTrue(_vwap_broken("LONG", data, NOW + timedelta(seconds=1)))
        self.assertFalse(_vwap_broken("SHORT", data, NOW + timedelta(seconds=1)))


if __name__ == "__main__":
    unittest.main()
