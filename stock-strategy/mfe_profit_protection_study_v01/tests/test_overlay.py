from __future__ import annotations

from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC, _projected_net_pnl
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint

from mfe_profit_protection_study_v01.analysis import (
    BASELINE,
    ResearchTrade,
    compare_trade,
    derive_initial_stop_price,
    simulate_trade,
)
from mfe_profit_protection_study_v01.overlay import (
    ENABLE_MFE_PROFIT_PROTECTION,
    EntryFill,
    MFEProtectionState,
    OhlcBar,
    PositionBasis,
    VARIANTS,
    evaluate_ohlc_bar,
)


TAIPEI = ZoneInfo("Asia/Taipei")


class OverlayStateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 24, 9, 30, tzinfo=TAIPEI)

    def basis(self, side="LONG"):
        stop = 98 if side == "LONG" else 102
        return PositionBasis.from_fills(
            side,
            [EntryFill(self.now, 100, 1000)],
            initial_stop_price=stop,
        )

    def state(self, side="LONG", variant="MFE_V1"):
        return MFEProtectionState(self.basis(side), VARIANTS[variant])

    @staticmethod
    def pnl(price):
        return (price - 100) * 1000

    def test_feature_flag_defaults_false(self):
        self.assertFalse(ENABLE_MFE_PROFIT_PROTECTION)

    def test_correct_long_mfe_calculation(self):
        state = self.state("LONG")
        state.observe(price=106, at=self.now, projected_net_pnl=6000, pnl_at_price=self.pnl)
        self.assertEqual(state.mfe_price, 106)
        self.assertEqual(state.mfe_r, 3)
        self.assertEqual(state.locked_profit_r, 2.25)

    def test_correct_short_mfe_calculation(self):
        state = self.state("SHORT")
        state.observe(price=94, at=self.now, projected_net_pnl=6000, pnl_at_price=lambda p: (100 - p) * 1000)
        self.assertEqual(state.mfe_price, 94)
        self.assertEqual(state.mfe_r, 3)
        self.assertEqual(state.basis.price_for_r(state.locked_profit_r), 95.5)
        self.assertFalse(state.triggered(95.4))
        self.assertTrue(state.triggered(95.5))

    def test_exact_boundaries(self):
        variant = VARIANTS["MFE_V1"]
        self.assertEqual(variant.candidate_locked_r(1.0), 0.0)
        self.assertAlmostEqual(variant.candidate_locked_r(1.5), 0.9)
        self.assertAlmostEqual(variant.candidate_locked_r(2.0), 1.4)
        self.assertAlmostEqual(variant.candidate_locked_r(3.0), 2.25)

    def test_fixed_variant_schedules(self):
        expected = {
            "MFE_LOOSE": (0.75, 1.2, 2.1),
            "MFE_V1": (0.9, 1.4, 2.25),
            "MFE_AGGRESSIVE": (1.05, 1.5, 2.4),
        }
        for name, locked in expected.items():
            variant = VARIANTS[name]
            observed = tuple(
                variant.candidate_locked_r(mfe_r) for mfe_r in (1.5, 2.0, 3.0)
            )
            for actual, target in zip(observed, locked):
                self.assertAlmostEqual(actual, target)

    def test_no_activation_below_one_r(self):
        state = self.state()
        state.observe(price=101.99, at=self.now, projected_net_pnl=1990, pnl_at_price=self.pnl)
        self.assertFalse(state.armed)

    def test_one_r_activation(self):
        state = self.state()
        state.observe(price=102, at=self.now, projected_net_pnl=2000, pnl_at_price=self.pnl)
        self.assertTrue(state.armed)
        self.assertEqual(state.locked_profit_r, 0)

    def test_one_and_half_r_activation(self):
        state = self.state()
        state.observe(price=103, at=self.now, projected_net_pnl=3000, pnl_at_price=self.pnl)
        self.assertAlmostEqual(state.locked_profit_r, 0.9)

    def test_two_r_activation(self):
        state = self.state()
        state.observe(price=104, at=self.now, projected_net_pnl=4000, pnl_at_price=self.pnl)
        self.assertAlmostEqual(state.locked_profit_r, 1.4)

    def test_three_r_activation(self):
        state = self.state()
        state.observe(price=106, at=self.now, projected_net_pnl=6000, pnl_at_price=self.pnl)
        self.assertAlmostEqual(state.locked_profit_r, 2.25)

    def test_locked_profit_never_loosens(self):
        state = self.state()
        state.observe(price=106, at=self.now, projected_net_pnl=6000, pnl_at_price=self.pnl)
        locked = state.locked_profit_r
        state.observe(price=103, at=self.now + timedelta(seconds=1), projected_net_pnl=3000, pnl_at_price=self.pnl)
        self.assertEqual(state.locked_profit_r, locked)

    def test_partial_and_multiple_fills_use_weighted_entry(self):
        basis = PositionBasis.from_fills(
            "LONG",
            [EntryFill(self.now, 100, 400), EntryFill(self.now + timedelta(seconds=1), 102, 600)],
            initial_stop_price=99.2,
        )
        self.assertEqual(basis.quantity, 1000)
        self.assertAlmostEqual(basis.entry_price, 101.2)
        self.assertAlmostEqual(basis.initial_risk_per_share, 2.0)

    def test_zero_initial_r_rejected(self):
        with self.assertRaises(ValueError):
            PositionBasis.from_fills(
                "LONG", [EntryFill(self.now, 100, 1000)], initial_stop_price=100
            )

    def test_intrabar_ambiguity_is_marked(self):
        state = self.state()
        bar = OhlcBar(self.now, open=105, high=106, low=104, close=105)
        result = evaluate_ohlc_bar(state, bar, pnl_at_price=self.pnl)
        self.assertIsNotNone(result)
        self.assertEqual(result.reason, "MFE_PROFIT_PROTECTION")
        self.assertTrue(result.intrabar_ambiguous)

    def test_original_stop_wins_same_bar(self):
        state = self.state()
        bar = OhlcBar(self.now, open=100, high=106, low=97, close=105)
        result = evaluate_ohlc_bar(state, bar, pnl_at_price=self.pnl)
        self.assertEqual(result.reason, "STOP_LOSS")
        self.assertEqual(result.price, 98)

    def test_force_flat_wins_same_bar(self):
        state = self.state()
        bar = OhlcBar(self.now, open=105, high=106, low=104, close=105)
        result = evaluate_ohlc_bar(state, bar, pnl_at_price=self.pnl, forced_exit=True)
        self.assertEqual(result.reason, "HARD_EXIT")
        self.assertFalse(result.intrabar_ambiguous)


class TickReplayTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 24, 9, 30, tzinfo=TAIPEI)

    def point(self, price, seconds, entry=100, quantity=1000, reversal=False):
        pnl = _projected_net_pnl("LONG", entry, price, quantity)[3]
        return PathPoint(
            at=self.start + timedelta(seconds=seconds),
            exit_price=price,
            projected_net_pnl=pnl,
            current_return=pnl / (entry * quantity),
            reversal=reversal,
        )

    def trade(self, prices, *, stop=98, times=None):
        seconds = times or list(range(1, len(prices) + 1))
        return ResearchTrade(
            trade_id="T1",
            session_date="20260924",
            symbol="TEST",
            stock_name="Test",
            side="LONG",
            entry_time=self.start,
            entry_price=100,
            quantity=1000,
            initial_stop_price=stop,
            points=tuple(self.point(price, at) for price, at in zip(prices, seconds)),
        )

    def test_gap_through_protected_floor_uses_observed_exit_price(self):
        trade = self.trade([106, 104.4])
        result = simulate_trade(trade, "MFE_V1", enable_mfe_profit_protection=True)
        self.assertEqual(result.reason, "MFE_PROFIT_PROTECTION")
        self.assertEqual(result.price, 104.4)
        self.assertLess(result.price, trade.basis.price_for_r(result.state.locked_profit_r))

    def test_original_stop_remains_active(self):
        stop = derive_initial_stop_price("LONG", 100, 1000, SPEC["stop_loss_net_twd"])
        trade = self.trade([stop - 0.2], stop=stop)
        result = simulate_trade(trade, "MFE_AGGRESSIVE", enable_mfe_profit_protection=True)
        self.assertEqual(result.reason, "STOP_LOSS")

    def test_existing_exit_wins_same_tick(self):
        trade = self.trade([106, 104.4], times=[1, (13 * 60 + 20) * 60 - (9 * 60 + 30) * 60])
        result = simulate_trade(trade, "MFE_V1", enable_mfe_profit_protection=True)
        self.assertEqual(result.reason, "HARD_EXIT")

    def test_closed_before_activation(self):
        hard_seconds = (13 * 60 + 20) * 60 - (9 * 60 + 30) * 60
        trade = self.trade([100.5], times=[hard_seconds])
        result = simulate_trade(trade, "MFE_V1", enable_mfe_profit_protection=True)
        self.assertEqual(result.reason, "HARD_EXIT")
        self.assertFalse(result.state.armed)

    def test_missing_market_path_refuses_to_invent_exit(self):
        trade = self.trade([])
        with self.assertRaises(RuntimeError):
            simulate_trade(trade, BASELINE)

    def test_positive_mfe_with_negative_final_pnl_is_reported(self):
        hard_seconds = (13 * 60 + 20) * 60 - (9 * 60 + 30) * 60
        trade = self.trade([101, 99], times=[1, hard_seconds])
        original = simulate_trade(trade, BASELINE)
        row, _ = compare_trade(trade, "MFE_V1", original)
        self.assertGreater(row["MFE_pnl"], 0)
        self.assertLess(row["original_realized_pnl"], 0)
        self.assertLess(row["profit_retention_original"], 0)


if __name__ == "__main__":
    unittest.main()
