"""Synthetic receipt-time tests only: no archive, broker, or performance run."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from cash_short_intraday_research_v01.signals import SignalScanner, feed_engine
from cash_short_intraday_research_v01.execution import shift_price
from yuanta_live_runtime_v01.strategy import LiveDirectionEngine, TAIPEI


OPEN = datetime(2026, 10, 19, 9, tzinfo=TAIPEI)
CONFIG = {
    "capital_twd": 190000, "warmup_seconds": 300,
    "opening_first_tick_latest": "09:00:05", "gap_reset_seconds": 60,
    "minimum_ticks_60s": 10, "tick_stale_seconds": 5,
    "book_stale_seconds": 10, "maximum_spread_bps": 40,
    "decision_interval_seconds": 30, "entry_start": "09:05", "last_entry": "13:10",
    "confirmation_min_seconds": 30, "confirmation_max_seconds": 120,
    "setup_ttl_seconds": 300, "retest_tolerance_ticks": 2,
    "retest_tolerance_fraction": 0.0015, "maximum_entry_distance_fraction": 0.005,
    "maximum_structure_stop_fraction": 0.01,
    "flow_threshold": -0.35, "large_flow_threshold": -0.25,
    "unit_verified": False,
}


def tick(second, price=40.0, *, symbol="2330", flag="0", volume=100, **extra):
    at = OPEN + timedelta(seconds=second)
    event = {
        "kind": "tick", "symbol": symbol, "received_at": at, "exchange_time": at,
        "sequence": int(second * 10 + (1 if symbol == "2330" else 4)),
        "sequence_known": True, "serial": second + 1, "price": price,
        "volume": volume, "bid": shift_price(price, 1, -1), "ask": shift_price(price, 1, 1),
        "flag": flag, "quote_valid": True,
    }
    event.update(extra)
    return event


def book(second, price=40.0, *, symbol="2330", negative=False, **extra):
    at = OPEN + timedelta(seconds=second)
    buy, sell = (10, 90) if negative else (90, 10)
    event = {
        "kind": "book", "symbol": symbol, "received_at": at, "exchange_time": at,
        "sequence": int(second * 10 + (2 if symbol == "2330" else 5)),
        "sequence_known": True, "quote_valid": True,
        "bids": [(shift_price(price, level + 1, -1), buy) for level in range(5)],
        "asks": [(shift_price(price, level + 1, 1), sell) for level in range(5)],
    }
    event.update(extra)
    return event


def scanner(benchmark=True, **config):
    metadata = {"2330": {"name": "合成個股"}}
    if benchmark:
        metadata["0050"] = "合成基準"
    return SignalScanner(metadata, {"2330"}, dict(CONFIG, **config))


def push_pair(target, second, price=40.0, *, negative=False, flag="0", benchmark=True, symbol="2330", **extra):
    target.ingest(tick(second, price, flag=flag, symbol=symbol, **extra))
    target.ingest(book(second, price, negative=negative, symbol=symbol))
    if benchmark and "0050" in target.observed:
        benchmark_price = 50.0 if second < 295 else 49.95
        target.ingest(tick(second, benchmark_price, symbol="0050"))
        target.ingest(book(second, benchmark_price, symbol="0050"))


def warm(target, *, negative=False, long=False, first=0):
    for second in range(first, 301, 5):
        price = (40.2 if second == 300 else 40.1 if second >= 245 else 40.0) if long else (39.9 if second == 300 else 40.0)
        push_pair(target, second, price, negative=negative, flag="1" if long else "0")


def retest_path(target, *, break_price=39.85, negative=False, end=360):
    for second in range(305, end + 1, 5):
        push_pair(target, second, 39.95 if second < 335 else break_price, negative=negative)


class FeedEngineTests(unittest.TestCase):
    def test_memory_only_helper_preserves_auction_vwap_not_directional_volume(self):
        engine = LiveDirectionEngine({"2330": "合成"})
        with patch("builtins.open", side_effect=AssertionError("external file access forbidden")):
            first = feed_engine(engine, tick(0, volume=999999))
            second = feed_engine(engine, tick(5, price=39.95, volume=10))
            result = feed_engine(engine, book(5, price=39.95, exchange_time=None))
        self.assertTrue(first.accepted and second.accepted and result.accepted)
        self.assertEqual(engine._states["2330"].cumulative_volume, 1000009)
        self.assertEqual(engine._states["2330"].ticks[0]["volume"], 0)
        self.assertEqual(engine._states["2330"].ticks[0]["opening_auction_volume"], 999999)
        self.assertAlmostEqual(engine._states["2330"].cumulative_pv, 999999 * 40 + 10 * 39.95)
        self.assertEqual(engine._states["2330"].session_open_price, 40)
        self.assertEqual(engine._states["2330"].latest_book["available_at"], OPEN + timedelta(seconds=5))

    def test_invalid_opening_volume_not_hidden_by_flow_exclusion(self):
        for volume in (-1, "not-a-volume", float("nan")):
            engine = LiveDirectionEngine({"2330": "合成"})
            self.assertFalse(feed_engine(engine, tick(0, volume=volume)).accepted)
            self.assertEqual(engine._states["2330"].cumulative_volume, 0)

    def test_duplicate_auction_cannot_double_count_cumulative_volume(self):
        engine = LiveDirectionEngine({"2330": "合成"})
        self.assertTrue(feed_engine(engine, tick(0, volume=1000)).accepted)
        self.assertFalse(feed_engine(engine, tick(0, volume=1000)).accepted)
        self.assertEqual(engine._states["2330"].cumulative_volume, 1000)

    def test_non_directional_reference_and_invalid_quote_never_enter_flow(self):
        engine = LiveDirectionEngine({"2330": "合成"})
        feed_engine(engine, tick(0))
        for flag in ("2", "3", "10", "15", ""):
            self.assertIsNone(feed_engine(engine, tick(5, flag=flag)))
        self.assertIsNone(feed_engine(engine, tick(5, serial=-1)))
        self.assertIsNone(feed_engine(engine, tick(5, quote_valid=False)))
        self.assertEqual(len(engine._states["2330"].ticks), 1)
        self.assertEqual(engine._states["2330"].cumulative_volume, 100)


class CausalFeatureTests(unittest.TestCase):
    def test_full_warmup_features_and_weak_benchmark(self):
        target = scanner()
        warm(target)
        feature = target.features("2330", OPEN + timedelta(seconds=300))
        self.assertTrue(feature["eligible"])
        self.assertIsNone(feature["warmup_reason"])
        self.assertEqual(feature["tick_count_60s"], 13)
        self.assertEqual(feature["volume_delta"], -1)
        self.assertEqual(feature["large_trade_delta"], -1)
        self.assertEqual(feature["large_trade_threshold_p90"], 100)
        self.assertAlmostEqual(feature["vwap"], (60 * 40 + 39.9) / 61)
        self.assertEqual(feature["opening_low"], 40)
        self.assertTrue(feature["opening_range_complete"])
        self.assertTrue(feature["market_filter_pass"])
        self.assertLess(feature["benchmark_return_5m"], 0)
        self.assertIsNotNone(feature["relative_strength_5m"])
        self.assertIn("VOLUME_UNIT_UNVERIFIED", feature["conditional_reasons"])
        self.assertEqual(feature["entry_timing_regime"], "EXCHANGE_AND_RECEIPT")
        json.dumps(feature, allow_nan=False)

    def test_large_opening_auction_changes_vwap_not_flow_or_p90(self):
        target = scanner()
        target.ingest(tick(0, price=40, volume=10000, flag="1"))
        target.ingest(book(0))
        for second in range(5, 301, 5):
            push_pair(target, second, 39.9)
        feature = target.features("2330", OPEN + timedelta(seconds=300))
        self.assertAlmostEqual(feature["vwap"], (10000 * 40 + 6000 * 39.9) / 16000)
        self.assertEqual(feature["volume_delta"], -1)
        self.assertEqual(feature["large_trade_delta"], -1)
        self.assertEqual(feature["large_trade_threshold_p90"], 100)

    def test_opening_range_does_not_require_an_extra_0455_trade(self):
        target = scanner()
        for second in range(0, 301, 5):
            if second not in {290, 295}:
                push_pair(target, second, 39.9 if second == 300 else 40)
        feature = target.features("2330", OPEN + timedelta(seconds=300))
        self.assertTrue(feature["eligible"])
        self.assertTrue(feature["opening_range_complete"])

    def test_future_receipt_perturbation_cannot_change_prior_decision(self):
        control, future = scanner(), scanner()
        for target in (control, future):
            warm(target, negative=True)
        future.ingest(tick(600, price=1, volume=999999))
        future.ingest(book(600, price=1, negative=False))
        at = OPEN + timedelta(seconds=300)
        self.assertEqual(control.decide(at), future.decide(at))
        self.assertEqual(control.features("2330", at), future.features("2330", at))
        self.assertEqual(future.engine._states["2330"].ticks[-1]["time"], at)

    def test_receipt_time_not_exchange_time_controls_availability(self):
        target = scanner()
        warm(target)
        event = tick(305, price=39.95, received_at=OPEN + timedelta(seconds=307))
        target.ingest(event)
        self.assertEqual(target.features("2330", OPEN + timedelta(seconds=305))["price"], 39.9)
        self.assertEqual(target.features("2330", OPEN + timedelta(seconds=307))["price"], 39.95)

    def test_unknown_book_exchange_clock_remains_conditional(self):
        target = scanner(unit_verified=True)
        warm(target)
        target.ingest(book(300, price=39.9, exchange_time=None, sequence=3003))
        feature = target.features("2330", OPEN + timedelta(seconds=300))
        self.assertTrue(feature["eligible"])
        self.assertEqual(feature["entry_timing_regime"], "CONDITIONAL_RECEIPT_ONLY")
        self.assertIn("EXCHANGE_TIME_UNKNOWN_RECEIPT_ONLY", feature["conditional_reasons"])

    def test_late_opening_never_becomes_certified_by_elapsed_time(self):
        target = scanner()
        warm(target, first=10)
        for second in range(305, 331, 5):
            push_pair(target, second, 39.9)
        feature = target.features("2330", OPEN + timedelta(seconds=330))
        self.assertFalse(feature["eligible"])
        self.assertEqual(feature["warmup_reason"], "OPENING_FIRST_TICK_MISSING_OR_LATE")
        self.assertEqual(target.decide(OPEN + timedelta(seconds=330)), [])

    def test_gap_over_sixty_seconds_resets_warmup_and_setup(self):
        target = scanner()
        warm(target)
        target.features("2330", OPEN + timedelta(seconds=300))
        push_pair(target, 365, 39.95)
        feature = target.features("2330", OPEN + timedelta(seconds=365))
        self.assertEqual(feature["gap_resets"], 1)
        self.assertEqual(feature["warmup_reason"], "CONTINUOUS_300S_WARMUP_INCOMPLETE")
        self.assertEqual(target.observed["2330"].setups["S1"].phase, "IDLE")

    def test_stale_wide_spread_and_sparse_direction_windows_reject(self):
        target = scanner()
        warm(target)
        self.assertEqual(target.features("2330", OPEN + timedelta(seconds=306))["warmup_reason"], "TICK_STALE")
        target = scanner()
        warm(target)
        wide = book(300, price=39.9, sequence=3003)
        wide["asks"][0] = (40.5, 10)
        target.ingest(wide)
        self.assertEqual(target.features("2330", OPEN + timedelta(seconds=300))["warmup_reason"], "SPREAD_TOO_WIDE_OR_MISSING")
        target = scanner()
        for second in range(0, 301, 30):
            push_pair(target, second, 39.9 if second == 300 else 40)
        self.assertEqual(target.features("2330", OPEN + timedelta(seconds=300))["warmup_reason"], "INSUFFICIENT_60S_DIRECTION_TICKS")

    def test_paused_status_requires_new_continuous_warmup(self):
        target = scanner()
        warm(target)
        target.ingest({"kind": "status", "symbol": "2330", "received_at": OPEN + timedelta(seconds=301), "status": "HALT"})
        self.assertEqual(target.features("2330", OPEN + timedelta(seconds=301))["warmup_reason"], "TRADING_PAUSED")
        target.ingest({"kind": "status", "symbol": "2330", "received_at": OPEN + timedelta(seconds=302), "status": "RESUMED"})
        push_pair(target, 305, 39.95)
        self.assertEqual(target.features("2330", OPEN + timedelta(seconds=305))["warmup_reason"], "CONTINUOUS_300S_WARMUP_INCOMPLETE")

    def test_aware_timestamp_and_monotonic_cursor_enforced(self):
        target = scanner()
        with self.assertRaises(ValueError):
            target.ingest(tick(0, received_at=OPEN.replace(tzinfo=None)))
        warm(target)
        target.features("2330", OPEN + timedelta(seconds=300))
        self.assertFalse(target.ingest(tick(10)))
        with self.assertRaises(ValueError):
            target.features("2330", OPEN + timedelta(seconds=299))


class SignalModelTests(unittest.TestCase):
    def test_continuous_decline_in_band_never_forms_a_retest(self):
        target = scanner()
        warm(target)
        for second, price in ((305, 39.9), (310, 39.85), (315, 39.8)):
            push_pair(target, second, price)
        target.features("2330", OPEN + timedelta(seconds=315))
        self.assertEqual({setup.phase for setup in target.observed["2330"].setups.values()}, {"ARMED"})
        self.assertFalse(any(row["to"] == "RETEST" for row in target.transitions))
        self.assertEqual(target.observed["2330"].setups["S1"].observed_low, 39.8)

    def test_rebound_requires_one_legal_tick_and_uptick_from_previous_receipt(self):
        target = scanner()
        warm(target)
        for second, price in ((305, 39.85), (310, 39.85)):
            push_pair(target, second, price)
        target.features("2330", OPEN + timedelta(seconds=310))
        self.assertTrue(all(setup.phase == "ARMED" for setup in target.observed["2330"].setups.values()))
        push_pair(target, 315, 39.9)
        target.features("2330", OPEN + timedelta(seconds=315))
        self.assertTrue(all(setup.phase == "RETEST" for setup in target.observed["2330"].setups.values()))
        self.assertEqual(target.observed["2330"].setups["S1"].observed_low, 39.85)
        self.assertEqual(target.observed["2330"].setups["S1"].retest_at, OPEN + timedelta(seconds=315))

    def test_s2_reclaim_at_tolerance_ceiling_invalidates_old_setup(self):
        target = scanner()
        warm(target)
        push_pair(target, 305, 39.95)
        target.features("2330", OPEN + timedelta(seconds=305))
        self.assertEqual(target.observed["2330"].setups["S2"].phase, "RETEST")
        push_pair(target, 310, 40.1)
        target.features("2330", OPEN + timedelta(seconds=310))
        self.assertEqual(target.observed["2330"].setups["S2"].phase, "IDLE")
        for second in range(315, 361, 5):
            push_pair(target, second, 39.95 if second == 315 else 39.85)
        self.assertFalse(any(row["entry_model"] == "S2" for row in target.decide(OPEN + timedelta(seconds=360))))
        self.assertTrue(any(row["reason"] == "OPENING_LOW_RECLAIM_INVALIDATED" for row in target.transitions))

    def test_legal_ticks_across_fifty_rebound_and_s2_reclaim(self):
        target = scanner()
        for second in range(0, 301, 5):
            push_pair(target, second, 49.95 if second == 300 else 50)
        push_pair(target, 305, 50)
        target.features("2330", OPEN + timedelta(seconds=305))
        self.assertEqual(target.observed["2330"].setups["S2"].phase, "RETEST")
        self.assertEqual(target.observed["2330"].setups["S2"].observed_low, 49.95)
        self.assertTrue(target._within_retest_band(50, 49.998))
        push_pair(target, 310, 50.2)
        target.features("2330", OPEN + timedelta(seconds=310))
        self.assertEqual(target.observed["2330"].setups["S2"].phase, "IDLE")

    def test_s1_s2_wait_thirty_seconds_then_emit_with_positive_book(self):
        target = scanner()
        warm(target)
        retest_path(target)
        early = target.decide(OPEN + timedelta(seconds=330))
        self.assertFalse(any(row["entry_model"] in {"S1", "S2"} for row in early))
        signals = target.decide(OPEN + timedelta(seconds=360))
        self.assertEqual({row["entry_model"] for row in signals}, {"S1", "S2"})
        for signal in signals:
            self.assertEqual(signal["side"], "SHORT")
            self.assertEqual(signal["structure_high"], 39.95)
            self.assertAlmostEqual(signal["stop_distance"], 0.1)
            self.assertGreater(signal["features"]["book_imbalance"], 0)
            self.assertTrue(signal["features"]["book_imbalance_is_auxiliary"])
            self.assertEqual(signal["features"]["retest_range_definition"], "FIRST_30_SECONDS_THEN_FROZEN")
            self.assertNotIn("quantity", signal)
            json.dumps(signal, allow_nan=False)
        self.assertEqual(target.decide(OPEN + timedelta(seconds=360)), [])

    def test_missing_benchmark_does_not_drop_none_short_groups(self):
        target = scanner(benchmark=False)
        warm(target)
        retest_path(target)
        signals = target.decide(OPEN + timedelta(seconds=360))
        self.assertEqual({row["entry_model"] for row in signals}, {"S1", "S2"})
        self.assertTrue(all(row["features"]["market_filter_pass"] is None for row in signals))

    def test_vwap_reclaim_invalidates_s1(self):
        target = scanner()
        warm(target)
        push_pair(target, 305, 39.95)
        push_pair(target, 310, 40.1)
        for second in range(315, 361, 5):
            push_pair(target, second, 39.85)
        signals = target.decide(OPEN + timedelta(seconds=360))
        self.assertFalse(any(row["entry_model"] == "S1" for row in signals))
        self.assertTrue(any(row["reason"] == "VWAP_RECLAIM_INVALIDATED" for row in target.transitions))

    def test_too_far_from_reference_does_not_chase_short(self):
        target = scanner()
        warm(target)
        retest_path(target, break_price=39.7)
        signals = target.decide(OPEN + timedelta(seconds=360))
        self.assertFalse(any(row["entry_model"] in {"S1", "S2"} for row in signals))

    def test_expired_retest_does_not_emit(self):
        target = scanner()
        warm(target)
        for second in range(305, 456, 5):
            push_pair(target, second, 39.95 if second < 435 else 39.85)
        signals = target.decide(OPEN + timedelta(seconds=450))
        self.assertFalse(any(row["entry_model"] in {"S1", "S2"} for row in signals))
        self.assertTrue(any(row["reason"] == "RETEST_CONFIRMATION_EXPIRED" for row in target.transitions))

    def test_m0_reuses_original_mirror_and_has_no_sizing_output(self):
        target = scanner()
        warm(target, negative=True)
        signals = target.decide(OPEN + timedelta(seconds=300))
        self.assertEqual([row["entry_model"] for row in signals], ["M0"])
        original = target.engine._signal_for("2330", OPEN + timedelta(seconds=300))
        self.assertEqual(original["side"], "SHORT")
        self.assertEqual(signals[0]["score"], original["score"])
        self.assertIsNone(signals[0]["structure_high"])
        self.assertNotIn("quantity", signals[0])

    def test_actual_long_engine_confirmations_gate_and_warmup_provenance(self):
        target = scanner()
        warm(target, long=True)
        first = target.decide(OPEN + timedelta(seconds=300))
        self.assertFalse(any(row["entry_model"] == "LONG" for row in first))
        for second in range(305, 331, 5):
            push_pair(target, second, 40.3 if second == 330 else 40.25, flag="1")
        signals = target.decide(OPEN + timedelta(seconds=330))
        longs = [row for row in signals if row["entry_model"] == "LONG"]
        self.assertEqual(len(longs), 1)
        self.assertEqual(longs[0]["side"], "LONG")
        self.assertEqual(longs[0]["features"]["source_engine"], "yuanta_live_runtime_v01.strategy.LiveDirectionEngine")
        self.assertIn("300S", longs[0]["features"]["warmup_difference_from_production"])
        self.assertNotIn("quantity", longs[0])

    def test_long_requires_benchmark_even_if_original_engine_would_not(self):
        target = scanner(benchmark=False)
        warm(target, long=True)
        with patch.object(target.engine, "choose_entry", side_effect=AssertionError("missing benchmark must fail closed")):
            self.assertFalse(any(row["entry_model"] == "LONG" for row in target.decide(OPEN + timedelta(seconds=300))))

    def test_non_selected_approved_long_is_separate_diagnostic_only(self):
        target = SignalScanner({"2330": "合成一", "2603": "合成二", "0050": "基準"}, {"2330", "2603"}, CONFIG)
        for second in range(0, 331, 5):
            price = 40 if second < 245 else 40.1 if second < 300 else 40.2 if second == 300 else 40.3 if second == 330 else 40.25
            push_pair(target, second, price, flag="1")
            push_pair(target, second, price, flag="1", symbol="2603", benchmark=False)
            if second == 300:
                target.decide(OPEN + timedelta(seconds=300))
        signals = target.decide(OPEN + timedelta(seconds=330))
        self.assertEqual(len([row for row in signals if row["entry_model"] == "LONG"]), 1)
        self.assertEqual({row["symbol"] for row in target.long_diagnostics}, {"2330", "2603"})
        self.assertEqual({row["selection_status"] for row in target.long_diagnostics}, {"SELECTED_TOP_RANK", "DIAGNOSTIC_NOT_TOP_RANK"})
        selected = next(row for row in target.long_diagnostics if row["selection_status"] == "SELECTED_TOP_RANK")
        self.assertEqual(selected["signal_id"], signals[0]["signal_id"])
        self.assertTrue(all("quantity" not in row for row in target.long_diagnostics))

    def test_decisions_only_on_grid_and_deterministic_ids(self):
        targets = [scanner(), scanner()]
        for target in targets:
            warm(target, negative=True)
        self.assertEqual(targets[0].decide(OPEN + timedelta(seconds=299)), [])
        left = targets[0].decide(OPEN + timedelta(seconds=300))
        right = targets[1].decide(OPEN + timedelta(seconds=300))
        self.assertEqual(left, right)
        self.assertEqual(len(left[0]["signal_id"]), 24)
        self.assertEqual(targets[0].decide(OPEN + timedelta(seconds=301)), [])


if __name__ == "__main__":
    unittest.main()
