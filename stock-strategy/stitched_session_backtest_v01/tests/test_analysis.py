from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from stitched_session_backtest_v01.analysis import _decision_summary, stitch_streams
from stitched_session_backtest_v01.near_miss_counterfactual import (
    _candidate_as_signal,
)
from stitched_session_backtest_v01.relative_strength_gate_study import (
    relative_strength_gate,
)
from stitched_session_backtest_v01.anti_chase_sensitivity_study import (
    VARIANTS,
    _anti_chase_limits,
    effective_limits,
)
from yuanta_live_runtime_v01.strategy import (
    ANTI_CHASE_ENTRY_POLICY,
    LONG_MARKET_REGIME_POLICY,
)


TAIPEI = ZoneInfo("Asia/Taipei")


def row(at: datetime, serial: int) -> dict:
    return {"time": at, "serial": serial}


class StitchTests(unittest.TestCase):
    def test_cutover_uses_early_before_and_late_at_or_after(self):
        cutover = datetime(2026, 9, 30, 9, 27, tzinfo=TAIPEI)
        early = {
            "0050": {
                "ticks": [row(cutover - timedelta(seconds=1), 1), row(cutover, 2)],
                "books": [row(cutover - timedelta(seconds=1), 0), row(cutover, 0)],
                "meta": {"stock_name": "early"},
            }
        }
        late = {
            "0050": {
                "ticks": [row(cutover, 3), row(cutover + timedelta(seconds=1), 4)],
                "books": [row(cutover, 0), row(cutover + timedelta(seconds=1), 0)],
                "meta": {"stock_name": "late"},
            }
        }

        result = stitch_streams(early, late, cutover)["0050"]

        self.assertEqual([item["serial"] for item in result["ticks"]], [1, 3, 4])
        self.assertEqual(len(result["books"]), 3)
        self.assertEqual(result["meta"]["stock_name"], "early")

    def test_decision_summary_preserves_near_miss_gate(self):
        summary = _decision_summary(
            [
                {"decision": "REJECTED", "candidates": []},
                {
                    "decision": "NO_APPROVED_CANDIDATE",
                    "decision_time": "2026-09-30T09:07:00+08:00",
                    "candidates": [
                        {
                            "stock_id": "3016",
                            "stock_name": "嘉晶",
                            "score": 0.7,
                            "gate_reason": "CONFIRMATIONS_INCOMPLETE",
                            "required_confirmations": 2,
                            "streak": 1,
                        }
                    ],
                },
            ]
        )

        self.assertEqual(summary["decision_windows"], 2)
        self.assertEqual(summary["decisions"]["REJECTED"], 1)
        self.assertEqual(summary["near_miss_gate_counts"]["CONFIRMATIONS_INCOMPLETE"], 1)
        self.assertEqual(summary["near_miss_symbol_counts"]["3016"], 1)

    def test_near_miss_candidate_reconstructs_signal_without_gate_metadata(self):
        decision = datetime(2026, 9, 30, 9, 7, tzinfo=TAIPEI)
        candidate = {
            "stock_id": "3016",
            "stock_name": "嘉晶",
            "side": "LONG",
            "decision_time": decision,
            "score": 0.7,
            "volume_delta": 0.4,
            "large_trade_delta": 0.3,
            "vwap_gap": 0.01,
            "book_imbalance": 0.2,
            "spread_bps": 10.0,
            "entry_price": 171.0,
            "quantity": 1000,
            "gate_reason": "CONFIRMATIONS_INCOMPLETE",
            "streak": 1,
        }

        signal = _candidate_as_signal(candidate)

        self.assertEqual(signal.stock_id, "3016")
        self.assertEqual(signal.entry_price, 171.0)
        self.assertEqual(signal.quantity, 1000)

    def test_relative_strength_override_is_backtest_only_and_restored(self):
        keys = (
            "bullish_min_relative_strength",
            "neutral_min_relative_strength",
            "bearish_min_relative_strength",
        )
        original = {key: LONG_MARKET_REGIME_POLICY[key] for key in keys}

        with relative_strength_gate(False):
            self.assertTrue(all(LONG_MARKET_REGIME_POLICY[key] < -100 for key in keys))

        self.assertEqual(
            {key: LONG_MARKET_REGIME_POLICY[key] for key in keys}, original
        )

    def test_early_anti_chase_exemption_has_exact_time_boundary(self):
        variant = next(
            row for row in VARIANTS
            if row["variant_id"] == "EARLY_EXEMPT_UNTIL_0915"
        )
        before = datetime(2026, 10, 1, 9, 14, 59, tzinfo=TAIPEI)
        boundary = datetime(2026, 10, 1, 9, 15, 0, tzinfo=TAIPEI)

        self.assertEqual(effective_limits(variant, before), (float("inf"), float("inf")))
        self.assertEqual(effective_limits(variant, boundary), (0.020, 0.0125))

    def test_backtest_anti_chase_overlay_restores_production_policy(self):
        opening_key = "maximum_directional_opening_extension"
        vwap_key = "maximum_directional_vwap_extension"
        original = (
            ANTI_CHASE_ENTRY_POLICY[opening_key],
            ANTI_CHASE_ENTRY_POLICY[vwap_key],
        )

        with _anti_chase_limits(0.99, 0.88):
            self.assertEqual(ANTI_CHASE_ENTRY_POLICY[opening_key], 0.99)
            self.assertEqual(ANTI_CHASE_ENTRY_POLICY[vwap_key], 0.88)

        self.assertEqual(
            (
                ANTI_CHASE_ENTRY_POLICY[opening_key],
                ANTI_CHASE_ENTRY_POLICY[vwap_key],
            ),
            original,
        )


if __name__ == "__main__":
    unittest.main()
