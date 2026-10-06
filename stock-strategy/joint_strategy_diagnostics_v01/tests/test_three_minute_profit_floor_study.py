from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

from joint_strategy_diagnostics_v01.three_minute_profit_floor_study import (
    _confirmation_trade,
    _simulate_cost_aware_breakeven,
    _summary,
)
from mfe_profit_protection_study_v01.analysis import ResearchTrade, derive_initial_stop_price
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint


TAIPEI = ZoneInfo("Asia/Taipei")


def _trade() -> ResearchTrade:
    entry = datetime(2026, 9, 22, 9, 0, tzinfo=TAIPEI)
    price = 100.0
    quantity = 1000
    return ResearchTrade(
        trade_id="t1",
        session_date="20260922",
        symbol="1234",
        stock_name="測試",
        side="LONG",
        entry_time=entry,
        entry_price=price,
        quantity=quantity,
        initial_stop_price=derive_initial_stop_price("LONG", price, quantity, 3500),
        points=(
            PathPoint(entry + timedelta(minutes=5), 102.0, 1500.0, 0.015, False),
        ),
        force_last_point_exit=True,
    )


class ThreeMinuteProfitFloorStudyTests(unittest.TestCase):
    def test_rejects_if_original_exit_precedes_checkpoint(self):
        trade = _trade()
        feature = {
            "signal_time": trade.entry_time.isoformat(),
            "exit_time": (trade.entry_time + timedelta(seconds=120)).isoformat(),
            "breakout_boundary_price": 99.0,
        }
        delayed, diagnostic = _confirmation_trade(feature, {"ticks": []}, trade, 190000)
        self.assertIsNone(delayed)
        self.assertEqual(diagnostic["confirmation_reason"], "ORIGINAL_EXIT_BEFORE_CHECKPOINT")

    def test_summary_counts_rejected_winner_as_zero_opportunity_pnl(self):
        baseline = {
            "trade_id": "t1", "original_entry_time": "2026-09-22T09:00:00+08:00",
            "action": "ENTERED", "new_pnl_twd": 1000.0,
        }
        rejected = {
            "trade_id": "t1", "original_entry_time": baseline["original_entry_time"],
            "action": "REJECTED_CONFIRMATION", "new_pnl_twd": 0.0,
        }
        result = _summary("CONFIRM_3M", [rejected], {"t1": baseline})
        self.assertEqual(result["net_difference_vs_baseline_twd"], -1000.0)
        self.assertEqual(result["baseline_winners_rejected"], 1)
        self.assertEqual(result["expectancy_per_opportunity_twd"], 0.0)

    def test_summary_excludes_missing_quote_from_matched_comparison(self):
        baseline = {
            "trade_id": "t1", "original_entry_time": "2026-09-22T09:00:00+08:00",
            "action": "ENTERED", "new_pnl_twd": 1000.0,
        }
        unscorable = {
            "trade_id": "t1", "original_entry_time": baseline["original_entry_time"],
            "action": "UNSCORABLE_DATA", "new_pnl_twd": None,
        }
        result = _summary("CONFIRM_3M", [unscorable], {"t1": baseline})
        self.assertEqual(result["evaluable_opportunities"], 0)
        self.assertEqual(result["unscorable_data"], 1)
        self.assertEqual(result["net_difference_vs_baseline_twd"], 0.0)

    def test_cost_aware_floor_uses_net_zero_without_changing_mfe_profile(self):
        trade = _trade()
        entry = trade.entry_time
        profitable = trade.pnl_at_price(104.0)
        # The price is still above entry, so the existing price-breakeven floor
        # has not fired, while fees/tax already make net PnL negative.
        below_net_zero = trade.pnl_at_price(100.2)
        armed_then_flat = ResearchTrade(
            trade_id=trade.trade_id,
            session_date=trade.session_date,
            symbol=trade.symbol,
            stock_name=trade.stock_name,
            side=trade.side,
            entry_time=entry,
            entry_price=trade.entry_price,
            quantity=trade.quantity,
            initial_stop_price=trade.initial_stop_price,
            points=(
                PathPoint(entry + timedelta(minutes=1), 104.0, profitable, profitable / 100000, False),
                PathPoint(entry + timedelta(minutes=2), 100.2, below_net_zero, below_net_zero / 100000, False),
            ),
            force_last_point_exit=True,
        )
        result = _simulate_cost_aware_breakeven(armed_then_flat)
        self.assertEqual(result["exit_reason"], "MFE_COST_AWARE_BREAKEVEN")
        self.assertLessEqual(result["net_pnl"], 0)


if __name__ == "__main__":
    unittest.main()
