from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from yuanta_intraday_shadow_v01.exit_parameter_sweep import (
    PathPoint,
    TradePath,
)
from yuanta_intraday_shadow_v01.flow_exit_sweep import FlowSnapshot
from yuanta_intraday_shadow_v01.hybrid_exit_sweep import (
    VARIANTS,
    HybridVariant,
    simulate_hybrid_variant,
)


TAIPEI = ZoneInfo("Asia/Taipei")


def flow(at, negative=True):
    sign = -0.5 if negative else 0.5

    return FlowSnapshot(
        at=at,
        buy_qty_30=10.0,
        sell_qty_30=20.0 if negative else 5.0,
        buy_qty_60=20.0,
        sell_qty_60=40.0 if negative else 10.0,
        buy_sell_ratio_30=0.5 if negative else 2.0,
        sell_buy_ratio_30=2.0 if negative else 0.5,
        net_aggressive_30=-10.0 if negative else 5.0,
        normalized_delta_30=sign,
        net_aggressive_60=-20.0 if negative else 10.0,
        normalized_delta_60=sign,
        buy_qty_30_previous=20.0,
        sell_qty_30_previous=10.0,
        buy_decay_from_peak=0.5,
        sell_increase_vs_previous=1.0,
        large_threshold=10.0,
        large_buy_qty_60=5.0,
        large_sell_qty_60=20.0 if negative else 2.0,
        large_buy_sell_ratio_60=0.25 if negative else 2.5,
        large_trade_delta_60=sign,
        max_consecutive_sell_ticks_30=3,
        average_buy_trade_size_30=5.0,
        average_sell_trade_size_30=10.0,
        book_imbalance=sign,
    )


class HybridExitSweepTests(unittest.TestCase):
    def _path(self, values):
        decision = datetime(
            2026, 9, 24, 9, 9,
            tzinfo=TAIPEI,
        )

        points = []

        for minute, pnl, ret in values:
            points.append(
                PathPoint(
                    at=decision + timedelta(minutes=minute),
                    exit_price=100.0 + ret * 100,
                    projected_net_pnl=pnl,
                    current_return=ret,
                    reversal=False,
                )
            )

        points.append(
            PathPoint(
                at=decision.replace(hour=13, minute=20),
                exit_price=100.5,
                projected_net_pnl=200.0,
                current_return=0.002,
                reversal=False,
            )
        )

        return TradePath(
            session_date="20260924",
            stock_id="3605",
            stock_name="測試股",
            decision_time=decision,
            entry_price=100.0,
            quantity=1000,
            notional_used=100000.0,
            points=tuple(points),
        )

    def test_expected_variant_grid(self):
        self.assertEqual(len(VARIANTS), 9)

        ids = [variant.variant_id for variant in VARIANTS]

        self.assertIn(
            "TIME_3M_NO_POS_MFE_FLOW_2OF3",
            ids,
        )
        self.assertIn(
            "HYBRID_FIXED_1500_3000",
            ids,
        )
        self.assertIn(
            "HYBRID_PERCENT_1_2_2_0",
            ids,
        )

    def test_fixed_soft_stop_requires_flow_confirmation(self):
        path = self._path([
            (1, -1600.0, -0.016),
            (2, -1700.0, -0.017),
        ])

        at = path.decision_time + timedelta(minutes=1)

        result = simulate_hybrid_variant(
            path,
            (flow(at, negative=True),),
            HybridVariant(
                "TEST",
                "HYBRID_FIXED",
                soft_value=1500.0,
                hard_value=3000.0,
            ),
        )

        self.assertEqual(
            result["exit_reason"],
            "HYBRID_SOFT_STOP_FIXED_FLOW",
        )
        self.assertTrue(result["flow_weak_at_exit"])

    def test_fixed_soft_stop_does_not_fire_without_flow(self):
        path = self._path([
            (1, -1600.0, -0.016),
            (2, -1700.0, -0.017),
        ])

        at = path.decision_time + timedelta(minutes=1)

        result = simulate_hybrid_variant(
            path,
            (flow(at, negative=False),),
            HybridVariant(
                "TEST",
                "HYBRID_FIXED",
                soft_value=1500.0,
                hard_value=3000.0,
            ),
        )

        self.assertNotEqual(
            result["exit_reason"],
            "HYBRID_SOFT_STOP_FIXED_FLOW",
        )

    def test_hard_stop_needs_no_flow_confirmation(self):
        path = self._path([
            (1, -3100.0, -0.031),
        ])

        result = simulate_hybrid_variant(
            path,
            (),
            HybridVariant(
                "TEST",
                "HYBRID_FIXED",
                soft_value=1500.0,
                hard_value=3000.0,
            ),
        )

        self.assertEqual(
            result["exit_reason"],
            "HYBRID_HARD_STOP_FIXED",
        )

    def test_three_minute_time_stop_needs_no_positive_mfe_and_weak_flow(self):
        path = self._path([
            (1, -500.0, -0.005),
            (2, -700.0, -0.007),
            (3, -900.0, -0.009),
        ])

        at = path.decision_time + timedelta(minutes=3)

        result = simulate_hybrid_variant(
            path,
            (flow(at, negative=True),),
            HybridVariant(
                "TEST",
                "TIME_NO_POS_MFE_FLOW",
                minutes=3,
            ),
        )

        self.assertEqual(
            result["exit_reason"],
            "TIME_3M_FLOW_STOP",
        )

    def test_positive_mfe_prevents_three_minute_time_stop(self):
        path = self._path([
            (1, 100.0, 0.001),
            (2, -500.0, -0.005),
            (3, -900.0, -0.009),
        ])

        at = path.decision_time + timedelta(minutes=3)

        result = simulate_hybrid_variant(
            path,
            (flow(at, negative=True),),
            HybridVariant(
                "TEST",
                "TIME_NO_POS_MFE_FLOW",
                minutes=3,
            ),
        )

        self.assertNotEqual(
            result["exit_reason"],
            "TIME_3M_FLOW_STOP",
        )

    def test_time_stop_uses_latest_causal_flow_before_millisecond_quote(self):
        decision = datetime(
            2026, 9, 24, 9, 9,
            tzinfo=TAIPEI,
        )

        snapshot_at = decision + timedelta(minutes=3)
        executable_at = snapshot_at + timedelta(milliseconds=250)

        path = TradePath(
            session_date="20260924",
            stock_id="3605",
            stock_name="測試股",
            decision_time=decision,
            entry_price=100.0,
            quantity=1000,
            notional_used=100000.0,
            points=(
                PathPoint(
                    at=executable_at,
                    exit_price=99.0,
                    projected_net_pnl=-1300.0,
                    current_return=-0.013,
                    reversal=False,
                ),
                PathPoint(
                    at=decision.replace(hour=13, minute=20),
                    exit_price=100.0,
                    projected_net_pnl=-323.0,
                    current_return=-0.00323,
                    reversal=False,
                ),
            ),
        )

        result = simulate_hybrid_variant(
            path,
            (flow(snapshot_at, negative=True),),
            HybridVariant(
                "TEST",
                "TIME_NO_POS_MFE_FLOW",
                minutes=3,
            ),
        )

        self.assertEqual(
            result["exit_reason"],
            "TIME_3M_FLOW_STOP",
        )
        self.assertEqual(
            result["exit_time"],
            executable_at.isoformat(),
        )
        self.assertTrue(result["flow_weak_at_exit"])

    def test_ten_minute_cost_zone_stop(self):
        path = self._path([
            (5, -300.0, -0.003),
            (10, -100.0, -0.001),
        ])

        result = simulate_hybrid_variant(
            path,
            (),
            HybridVariant(
                "TEST",
                "TIME_COST_ZONE",
                minutes=10,
            ),
        )

        self.assertEqual(
            result["exit_reason"],
            "TIME_10M_COST_ZONE",
        )


if __name__ == "__main__":
    unittest.main()
