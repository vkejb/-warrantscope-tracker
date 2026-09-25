from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from yuanta_intraday_shadow_v01.exit_parameter_sweep import (
    PathPoint,
    TradePath,
)
from yuanta_intraday_shadow_v01.flow_exit_sweep import FlowSnapshot
from yuanta_intraday_shadow_v01.flow_trailing_sweep import (
    VARIANTS,
    FlowTrailingVariant,
    _dynamic_drawdown,
    simulate_flow_trailing_variant,
)


TAIPEI = ZoneInfo("Asia/Taipei")


def snapshot(at, state):
    if state == "STRONG":
        volume = 0.5
        large = 0.5
        book = 0.5
        buy = 20.0
        sell = 5.0
    elif state == "WEAK":
        volume = -0.5
        large = -0.5
        book = 0.2
        buy = 5.0
        sell = 20.0
    else:
        volume = 0.1
        large = -0.1
        book = 0.1
        buy = 10.0
        sell = 10.0

    return FlowSnapshot(
        at=at,
        buy_qty_30=buy,
        sell_qty_30=sell,
        buy_qty_60=buy,
        sell_qty_60=sell,
        buy_sell_ratio_30=(
            buy / sell if sell else None
        ),
        sell_buy_ratio_30=(
            sell / buy if buy else None
        ),
        net_aggressive_30=buy - sell,
        normalized_delta_30=volume,
        net_aggressive_60=buy - sell,
        normalized_delta_60=volume,
        buy_qty_30_previous=buy,
        sell_qty_30_previous=sell,
        buy_decay_from_peak=0.0,
        sell_increase_vs_previous=0.0,
        large_threshold=10.0,
        large_buy_qty_60=buy,
        large_sell_qty_60=sell,
        large_buy_sell_ratio_60=(
            buy / sell if sell else None
        ),
        large_trade_delta_60=large,
        max_consecutive_sell_ticks_30=1,
        average_buy_trade_size_30=5.0,
        average_sell_trade_size_30=5.0,
        book_imbalance=book,
    )


class FlowTrailingSweepTests(unittest.TestCase):
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
                    at=decision + timedelta(minutes=1, milliseconds=250),
                    exit_price=101.8,
                    projected_net_pnl=1473.0,
                    current_return=0.01473,
                    reversal=False,
                ),
                PathPoint(
                    at=decision + timedelta(minutes=2),
                    exit_price=101.0,
                    projected_net_pnl=675.0,
                    current_return=0.00675,
                    reversal=False,
                ),
                PathPoint(
                    at=decision.replace(hour=13, minute=20),
                    exit_price=100.5,
                    projected_net_pnl=175.0,
                    current_return=0.00175,
                    reversal=False,
                ),
            ),
        )

    def test_expected_variant_grid(self):
        self.assertEqual(len(VARIANTS), 5)

        ids = [variant.variant_id for variant in VARIANTS]

        self.assertIn("TRAIL15_FLOW_2OF3_NEG", ids)
        self.assertIn("TRAIL15_DYNAMIC_1_0_0_8_0_5", ids)

    def test_flow_exit_only_applies_after_trailing_is_armed(self):
        path = self._path()

        before_arm = path.decision_time + timedelta(seconds=30)
        after_arm = path.decision_time + timedelta(minutes=1)

        result = simulate_flow_trailing_variant(
            path,
            (
                snapshot(before_arm, "WEAK"),
                snapshot(after_arm, "WEAK"),
            ),
            FlowTrailingVariant(
                "TEST",
                "FLOW_2OF3_NEG",
            ),
        )

        self.assertTrue(result["trailing_armed"])
        self.assertEqual(
            result["exit_reason"],
            "FLOW_TRAILING:FLOW_2OF3_NEG",
        )
        self.assertGreaterEqual(
            datetime.fromisoformat(result["exit_time"]),
            after_arm,
        )

    def test_dynamic_drawdown_tightens_when_flow_is_weak(self):
        at = datetime(
            2026, 9, 24, 9, 10,
            tzinfo=TAIPEI,
        )

        self.assertEqual(
            _dynamic_drawdown(snapshot(at, "STRONG")),
            0.010,
        )
        self.assertEqual(
            _dynamic_drawdown(snapshot(at, "NEUTRAL")),
            0.008,
        )
        self.assertEqual(
            _dynamic_drawdown(snapshot(at, "WEAK")),
            0.005,
        )

    def test_dynamic_trailing_exits_on_weak_flow_tighter_drawdown(self):
        path = self._path()

        arm_time = path.decision_time + timedelta(minutes=1)
        weak_time = path.decision_time + timedelta(minutes=2)

        result = simulate_flow_trailing_variant(
            path,
            (
                snapshot(arm_time, "STRONG"),
                snapshot(weak_time, "WEAK"),
            ),
            FlowTrailingVariant(
                "TEST",
                "DYNAMIC_TRAILING",
            ),
        )

        self.assertTrue(result["trailing_armed"])
        self.assertEqual(
            result["exit_reason"],
            "DYNAMIC_TRAILING",
        )
        self.assertEqual(
            result["active_drawdown_at_exit"],
            0.005,
        )

    def test_loss_recovery_still_applies_before_trailing_activation(self):
        decision = datetime(
            2026, 9, 24, 9, 9,
            tzinfo=TAIPEI,
        )

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
                    at=decision + timedelta(minutes=1),
                    exit_price=98.0,
                    projected_net_pnl=-2300.0,
                    current_return=-0.023,
                    reversal=False,
                ),
                PathPoint(
                    at=decision + timedelta(minutes=2),
                    exit_price=100.5,
                    projected_net_pnl=175.0,
                    current_return=0.00175,
                    reversal=False,
                ),
            ),
        )

        result = simulate_flow_trailing_variant(
            path,
            (),
            FlowTrailingVariant(
                "TEST",
                "FLOW_2OF3_NEG",
            ),
        )

        self.assertFalse(result["trailing_armed"])
        self.assertEqual(
            result["exit_reason"],
            "LOSS_RECOVERY_TO_PROFIT",
        )


if __name__ == "__main__":
    unittest.main()
