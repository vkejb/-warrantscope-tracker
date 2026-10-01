from __future__ import annotations

from datetime import datetime, timedelta
import unittest

from paper_shadow_v01.runner import PAPER_CONFIRMATION_POLICY
from stitched_session_backtest_v01.entry_confirmation_sensitivity_study import (
    BASE_OPENING_LIMIT,
    BASE_VWAP_LIMIT,
    _confirmation_delay,
    _passes_chase,
    _select_rolling,
)
from yuanta_live_runtime_v01.strategy import LiveSignal, TAIPEI


def signal(at: datetime, *, opening: float = 0.01, vwap: float = 0.01) -> LiveSignal:
    return LiveSignal(
        stock_id="3094",
        stock_name="聯傑",
        side="LONG",
        decision_time=at,
        score=0.70,
        volume_delta=0.70,
        large_trade_delta=0.80,
        vwap_gap=vwap,
        book_imbalance=0.20,
        spread_bps=10.0,
        entry_price=64.0,
        quantity=2000,
        market_regime="BEARISH",
        benchmark_vwap_gap=-0.001,
        benchmark_return_5m=-0.001,
        relative_strength_5m=0.02,
        required_confirmations=2,
        directional_opening_extension=opening,
        directional_vwap_extension=vwap,
        breakout_boundary_price=63.5,
    )


class EntryConfirmationStudyTests(unittest.TestCase):
    def test_chase_limits_include_exact_boundary(self):
        at = datetime(2026, 10, 1, 9, 30, tzinfo=TAIPEI)
        self.assertTrue(
            _passes_chase(
                signal(at, opening=BASE_OPENING_LIMIT, vwap=BASE_VWAP_LIMIT),
                BASE_OPENING_LIMIT,
                BASE_VWAP_LIMIT,
            )
        )
        self.assertFalse(
            _passes_chase(
                signal(at, opening=BASE_OPENING_LIMIT + 0.0001),
                BASE_OPENING_LIMIT,
                BASE_VWAP_LIMIT,
            )
        )

    def test_confirmation_delay_overlay_restores_global_policy(self):
        original = int(PAPER_CONFIRMATION_POLICY["delay_seconds"])
        with _confirmation_delay(30):
            self.assertEqual(PAPER_CONFIRMATION_POLICY["delay_seconds"], 30)
        self.assertEqual(PAPER_CONFIRMATION_POLICY["delay_seconds"], original)

    def test_rolling_pair_allows_gap_but_rejects_opposite_signal(self):
        first = datetime(2026, 10, 1, 9, 30, tzinfo=TAIPEI)
        targets = [
            {"signal": signal(first), "gate_reason": "CONFIRMATIONS_INCOMPLETE", "streak": 1},
            {
                "signal": signal(first + timedelta(seconds=60)),
                "gate_reason": "CONFIRMATIONS_INCOMPLETE",
                "streak": 1,
            },
        ]
        variant = {
            "window_seconds": 90,
            "opening_limit": BASE_OPENING_LIMIT,
            "vwap_limit": BASE_VWAP_LIMIT,
        }
        selected, diagnostic = _select_rolling(targets, [], variant)
        self.assertIsNotNone(selected)
        self.assertEqual(diagnostic["elapsed_seconds"], 60.0)

        opposite = [{
            "stock_id": "3094",
            "side": "SHORT",
            "decision_time": first + timedelta(seconds=30),
        }]
        selected, diagnostic = _select_rolling(targets, opposite, variant)
        self.assertIsNone(selected)
        self.assertEqual(diagnostic["selection_reason"], "NO_QUALIFYING_PAIR")


if __name__ == "__main__":
    unittest.main()
