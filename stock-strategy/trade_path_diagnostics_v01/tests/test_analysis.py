from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from mfe_profit_protection_study_v01.analysis import ResearchTrade
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint

from trade_path_diagnostics_v01.analysis import Outcome, analyze_trade


UTC8 = timezone(timedelta(hours=8))


def _trade(side: str, prices: list[float], seconds: list[int] | None = None) -> ResearchTrade:
    entry = datetime(2026, 9, 24, 9, 0, tzinfo=UTC8)
    seconds = seconds or [60 * (index + 1) for index in range(len(prices))]
    points = tuple(
        PathPoint(
            at=entry + timedelta(seconds=offset),
            exit_price=price,
            projected_net_pnl=(price - 100) * 1000 if side == "LONG" else (100 - price) * 1000,
            current_return=0,
            reversal=False,
        )
        for price, offset in zip(prices, seconds)
    )
    return ResearchTrade(
        trade_id=f"test-{side}", session_date="20260924", symbol="TEST",
        stock_name="Test", side=side, entry_time=entry, entry_price=100,
        quantity=1000, initial_stop_price=98 if side == "LONG" else 102,
        points=points, force_last_point_exit=True,
    )


class TradePathDiagnosticsTests(unittest.TestCase):
    def test_long_mfe_and_mae_are_side_aware(self):
        trade = _trade("LONG", [101, 98, 104, 100], [60, 120, 180, 300])
        _, rows = analyze_trade(trade, Outcome("SCORED", trade.points[-1].at, 100, "X", 1, 300), horizons=(5,), maximum_quote_staleness_seconds=0)
        self.assertEqual(rows[0]["mfe_price"], 104)
        self.assertEqual(rows[0]["mae_price"], 98)
        self.assertFalse(rows[0]["mfe_before_mae"])

    def test_short_mfe_and_mae_are_side_aware(self):
        trade = _trade("SHORT", [99, 102, 94, 100], [60, 120, 180, 300])
        _, rows = analyze_trade(trade, Outcome("SCORED", trade.points[-1].at, 100, "X", 1, 300), horizons=(5,), maximum_quote_staleness_seconds=0)
        self.assertEqual(rows[0]["mfe_price"], 94)
        self.assertEqual(rows[0]["mae_price"], 102)
        self.assertFalse(rows[0]["mfe_before_mae"])

    def test_stale_horizon_is_null_not_forward_filled(self):
        trade = _trade("LONG", [101, 102], [60, 240])
        _, rows = analyze_trade(trade, Outcome("SCORED", trade.points[-1].at, 102, "X", 1, 240), horizons=(5,), maximum_quote_staleness_seconds=5)
        self.assertFalse(rows[0]["horizon_complete"])
        self.assertIsNone(rows[0]["mark_price"])
        self.assertIsNone(rows[0]["mfe_price"])

    def test_path_after_exit_is_explicitly_counterfactual(self):
        trade = _trade("LONG", [101, 102, 103], [60, 300, 600])
        outcome = Outcome("SCORED", trade.entry_time + timedelta(seconds=60), 101, "X", 1, 60)
        _, rows = analyze_trade(trade, outcome, horizons=(5,), maximum_quote_staleness_seconds=0)
        self.assertTrue(rows[0]["counterfactual_after_exit"])
        self.assertFalse(rows[0]["position_still_open"])

    def test_holding_summary_stops_at_actual_exit(self):
        trade = _trade("LONG", [101, 99, 110], [60, 120, 300])
        outcome = Outcome("SCORED", trade.entry_time + timedelta(seconds=120), 99, "X", -1, 120)
        summary, _ = analyze_trade(trade, outcome, horizons=(5,), maximum_quote_staleness_seconds=0)
        self.assertEqual(summary["held_mfe_price"], 101)
        self.assertEqual(summary["held_mae_price"], 99)

    def test_unscorable_outcome_is_not_mislabeled(self):
        trade = _trade("LONG", [101], [300])
        summary, _ = analyze_trade(trade, Outcome("UNSCORABLE"), horizons=(5,), maximum_quote_staleness_seconds=0)
        self.assertEqual(summary["outcome"], "UNSCORABLE")

    def test_all_adverse_path_has_zero_price_mfe_at_entry(self):
        trade = _trade("LONG", [99, 98], [60, 300])
        summary, rows = analyze_trade(
            trade,
            Outcome("SCORED", trade.points[-1].at, 98, "X", -1, 300),
            horizons=(5,),
            maximum_quote_staleness_seconds=0,
        )
        self.assertEqual(summary["held_mfe_price"], 100)
        self.assertEqual(summary["held_mfe_move_pct"], 0)
        self.assertEqual(rows[0]["mfe_price"], 100)
        self.assertEqual(rows[0]["mfe_move_pct"], 0)


if __name__ == "__main__":
    unittest.main()
