from __future__ import annotations

from datetime import datetime, timedelta
import unittest

from mfe_profit_protection_study_v01.analysis import ResearchTrade
from stitched_session_backtest_v01.liquidity_qualified_early_stop_validation import (
    LiquidityVariant,
    causal_liquidity,
    liquidity_passes,
    simulate_liquidity_qualified,
    variants,
)
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint
from yuanta_live_runtime_v01.strategy import TAIPEI


class LiquidityQualifiedEarlyStopTests(unittest.TestCase):
    def setUp(self):
        self.entry = datetime(2026, 10, 1, 9, 10, tzinfo=TAIPEI)

    def _trade(self, pnls):
        return ResearchTrade(
            trade_id="20261001-TEST-091000", session_date="20261001",
            symbol="TEST", stock_name="測試股", side="LONG",
            entry_time=self.entry, entry_price=100.0, quantity=1000,
            initial_stop_price=96.5,
            points=tuple(
                PathPoint(
                    self.entry + timedelta(seconds=seconds),
                    100.0 + pnl / 1000.0, pnl, 0.0, False,
                )
                for seconds, pnl in pnls
            ),
            force_last_point_exit=True,
        )

    def _data(self, rows):
        ticks = [
            {
                "time": self.entry + timedelta(seconds=seconds),
                "price": 100.0, "volume": volume,
                "bid": 99.9, "ask": 100.1, "flag": "1",
            }
            for seconds, volume in rows
        ]
        return {"ticks": ticks, "tick_times": [row["time"] for row in ticks]}

    def test_grid_is_fixed(self):
        self.assertEqual(len(variants()), 9)

    def test_causal_liquidity_uses_disjoint_30_second_windows(self):
        data = self._data([(65, 10), (80, 10), (95, 2), (120, 2)])
        state = causal_liquidity(data, self.entry + timedelta(seconds=120))
        self.assertEqual(state["tick_count_previous_30s"], 2)
        self.assertEqual(state["tick_count_30s"], 2)
        self.assertEqual(state["volume_ratio_30s_vs_previous"], 0.2)

    def test_exact_thresholds_pass(self):
        state = {
            "tick_count_30s": 3,
            "volume_ratio_30s_vs_previous": 0.25,
        }
        self.assertTrue(liquidity_passes(state, LiquidityVariant(3, 0.25)))

    def test_thin_activity_holds_and_preserves_later_profit(self):
        trade = self._trade([(60, -200), (120, -900), (180, 1000), (240, 2500)])
        data = self._data([(65, 20), (80, 20), (120, 1)])
        row = simulate_liquidity_qualified(
            trade, data, LiquidityVariant(1, 0.10),
        )
        self.assertEqual(row["checkpoint_action"], "HOLD_INSUFFICIENT_ACTIVITY")
        self.assertEqual(row["exit_reason"], "HARD_EXIT")
        self.assertEqual(row["net_pnl_twd"], 2500)

    def test_sufficient_activity_exits_at_checkpoint(self):
        trade = self._trade([(60, -200), (120, -900), (180, 2000)])
        data = self._data([(65, 10), (80, 10), (95, 10), (110, 10), (120, 10)])
        row = simulate_liquidity_qualified(
            trade, data, LiquidityVariant(3, 0.50),
        )
        self.assertEqual(row["exit_reason"], "LIQUIDITY_QUALIFIED_EARLY_FAILURE")
        self.assertEqual(row["holding_seconds"], 120)

    def test_hard_stop_remains_first(self):
        trade = self._trade([(120, -3600), (180, 2000)])
        data = self._data([(65, 10), (80, 10), (120, 1)])
        row = simulate_liquidity_qualified(
            trade, data, LiquidityVariant(5, 0.50),
        )
        self.assertEqual(row["exit_reason"], "DISASTER_STOP_NEG_1R")

    def test_mfe_floor_remains_first(self):
        trade = self._trade([(30, 2800), (60, 900), (120, -900)])
        data = self._data([(30, 10), (60, 10), (120, 1)])
        row = simulate_liquidity_qualified(
            trade, data, LiquidityVariant(5, 0.50),
        )
        self.assertEqual(row["exit_reason"], "BUFFERED_NET_MFE_PROFIT_PROTECTION")


if __name__ == "__main__":
    unittest.main()
