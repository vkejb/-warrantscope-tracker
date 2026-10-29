"""Synthetic-only tests for frozen six-filter receipt-causal annotations."""
from copy import deepcopy
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from cash_short_intraday_research_v01.execution import shift_price
from cash_short_intraday_research_v01.signals import SignalScanner
from cash_short_intraday_research_v01.round2.features import (
    FeatureMismatchError, VARIANTS, build_feature_context, build_signal_features,
    evaluate_variant,
)
from yuanta_live_runtime_v01.strategy import TAIPEI


OPEN = datetime(2026, 10, 19, 9, tzinfo=TAIPEI)
CONFIG = {"capital_twd": 190000, "warmup_seconds": 300,
          "opening_first_tick_latest": "09:00:05", "gap_reset_seconds": 60,
          "minimum_ticks_60s": 10, "tick_stale_seconds": 5,
          "book_stale_seconds": 10, "maximum_spread_bps": 40,
          "unit_verified": False}


def tick(second, price, symbol="2466", **extra):
    at = OPEN + timedelta(seconds=second)
    out = {"kind": "tick", "symbol": symbol, "received_at": at,
           "exchange_time": at, "sequence": second * 10,
           "sequence_known": True, "serial": second + 1,
           "price": price, "volume": 100,
           "bid": shift_price(price, 1, -1), "ask": shift_price(price, 1, 1),
           "flag": "0", "quote_valid": True}
    out.update(extra)
    return out


def book(second, price, symbol="2466"):
    at = OPEN + timedelta(seconds=second)
    return {"kind": "book", "symbol": symbol, "received_at": at,
            "exchange_time": at, "sequence": second * 10 + 1,
            "sequence_known": True, "quote_valid": True,
            "bids": [(shift_price(price, level, -1), 10) for level in range(1, 6)],
            "asks": [(shift_price(price, level, 1), 20) for level in range(1, 6)]}


def dataset(*, benchmark=True, start=0, until=390):
    metadata = {"2466": {"name": "synthetic stock"}}
    if benchmark:
        metadata["0050"] = {"name": "synthetic benchmark"}
    events = []
    for second in range(start, until + 1, 5):
        price = 40.0 if second <= 180 else 39.9 if second < 300 else 39.8
        events.extend([tick(second, price), book(second, price)])
        if benchmark:
            benchmark_price = 50.0 if second < 300 else 49.95
            events.extend([tick(second, benchmark_price, "0050"), book(second, benchmark_price, "0050")])
    events.sort(key=lambda item: (item["received_at"], item["sequence"], item["symbol"]))
    return {"day": "20261019", "metadata": metadata,
            "candidates": {"2466"}, "events": events}


def raw_signal(data, at=360, reference=39.9, identity="synthetic-s1"):
    scanner = SignalScanner(data["metadata"], data["candidates"], CONFIG)
    now = OPEN + timedelta(seconds=at)
    for event in data["events"]:
        if event["received_at"] <= now:
            scanner.ingest(event)
    feature = scanner.features("2466", now)
    feature.update(retest_reference=reference,
                   entry_distance_fraction=(reference - feature["price"]) / reference,
                   retest_high=40.0, retest_low=39.9)
    return {"signal_id": identity, "symbol": "2466", "entry_model": "S1",
            "side": "SHORT", "time": now, "features": feature}


def predicate_signal(**overrides):
    f = {"market_available": True, "market_weak": True,
         "market_unavailable_reasons": [], "rs_available": True,
         "rs_300": -0.002, "rs_unavailable_reasons": [],
         "vwap_slope_available": True, "vwap_down_180s": True,
         "vwap_slope_unavailable_reasons": [], "entry_distance_fraction": 0.003}
    f.update(overrides)
    return {"signal_id": "predicate", "entry_model": "S1", "round2_features": f}


class PredicateTests(unittest.TestCase):
    def test_exact_six_versions_and_fixed_thresholds(self):
        signal = predicate_signal()
        self.assertEqual(VARIANTS, ("R0", "R1", "R2", "R3", "R4", "R5"))
        for version in VARIANTS:
            result = evaluate_variant(signal, version)
            self.assertTrue(result["evaluable"])
            self.assertTrue(result["pass"])
        with self.assertRaises(ValueError):
            evaluate_variant(signal, "R6")

    def test_relative_weakness_is_percentage_point_difference(self):
        self.assertFalse(evaluate_variant(predicate_signal(rs_300=-0.00199), "R2")["pass"])
        self.assertTrue(evaluate_variant(predicate_signal(rs_300=-0.00201), "R2")["pass"])

    def test_strict_vwap_down_no_flat_equality(self):
        result = evaluate_variant(predicate_signal(vwap_down_180s=False), "R3")
        self.assertTrue(result["evaluable"])
        self.assertFalse(result["pass"])

    def test_tighter_chase_filter_only_changes_distance(self):
        signal = predicate_signal(entry_distance_fraction=0.00301,
                                  market_available=False, rs_available=False,
                                  vwap_slope_available=False)
        self.assertFalse(evaluate_variant(signal, "R4")["pass"])
        self.assertTrue(evaluate_variant(signal, "R0")["pass"])

    def test_missing_benchmark_is_unavailable_not_false_or_zero(self):
        signal = predicate_signal(market_available=False, market_weak=None,
                                  market_unavailable_reasons=["MISSING_0050"],
                                  rs_available=False, rs_300=None)
        for version in ("R1", "R2", "R5"):
            out = evaluate_variant(signal, version)
            self.assertFalse(out["evaluable"])
            self.assertIsNone(out["pass"])
            self.assertEqual(out["status"], "UNAVAILABLE")
        self.assertTrue(evaluate_variant(signal, "R0")["pass"])

    def test_r5_unknown_condition_overrides_known_failure(self):
        out = evaluate_variant(predicate_signal(market_weak=False, rs_available=False, rs_300=None), "R5")
        self.assertIsNone(out["pass"])
        self.assertIn("MARKET_NOT_WEAK", out["known_condition_failures"])

    def test_non_s1_and_nonfinite_values_fail_closed(self):
        with self.assertRaises(ValueError):
            evaluate_variant({"entry_model": "S2"}, "R0")
        out = evaluate_variant(predicate_signal(rs_300=float("nan")), "R2")
        self.assertIsNone(out["pass"])


class CausalReplayTests(unittest.TestCase):
    def test_shared_values_match_and_original_input_is_preserved(self):
        data = dataset()
        signal = raw_signal(data)
        original = deepcopy(signal)
        output = build_feature_context(data, [signal], CONFIG, decision_times=[])
        enriched = output["signals"][0]
        self.assertEqual(signal, original)
        self.assertEqual(enriched["signal_id"], original["signal_id"])
        self.assertEqual(enriched["time"], original["time"])
        self.assertEqual(enriched["features"], original["features"])
        f = enriched["round2_features"]
        self.assertEqual(f["shared_feature_verification"], "MATCH_REPLAYED_ORIGINAL_SCANNER")
        self.assertAlmostEqual(f["vwap_180s_ago"], 40.0)
        self.assertLess(f["vwap_now"], f["vwap_180s_ago"])
        self.assertAlmostEqual(f["rs_300"], f["stock_return_300s"] - f["benchmark_return_300s"])
        self.assertEqual(f["vwap_180s_ago_snapshot_at"], (OPEN + timedelta(seconds=180)).isoformat())
        self.assertTrue(output["common_mask"][0]["common_evaluable"])

    def test_vwap_history_is_saved_before_later_receipt(self):
        data = dataset()
        # A normal event after the historical cursor changes current VWAP but
        # must not leak into the already-saved 09:03 VWAP snapshot.
        for event in data["events"]:
            if event["kind"] == "tick" and event["symbol"] == "2466" and event["received_at"] == OPEN + timedelta(seconds=185):
                event.update(price=80.0, volume=1000000, bid=79.9, ask=80.1)
        signal = raw_signal(data)
        enriched = build_signal_features(data, [signal], CONFIG)[0]
        self.assertEqual(enriched["round2_features"]["vwap_180s_ago"], 40.0)
        self.assertGreater(enriched["round2_features"]["vwap_now"], 70.0)

    def test_future_events_do_not_affect_any_decision_feature(self):
        data = dataset()
        signal = raw_signal(data)
        first = build_signal_features(data, [signal], CONFIG)[0]
        data["events"].append(tick(400, 10.0, volume=9999999))
        second = build_signal_features(data, [signal], CONFIG)[0]
        self.assertEqual(first, second)

    def test_missing_0050_preserves_r0_and_marks_market_versions_unavailable(self):
        data = dataset(benchmark=False)
        enriched = build_signal_features(data, [raw_signal(data)], CONFIG)[0]
        self.assertTrue(evaluate_variant(enriched, "R0")["pass"])
        for version in ("R1", "R2", "R5"):
            self.assertIsNone(evaluate_variant(enriched, version)["pass"])
        context = build_feature_context(data, [raw_signal(data)], CONFIG, decision_times=[])
        self.assertFalse(context["common_mask"][0]["common_evaluable"])

    def test_missing_historical_fresh_tick_is_unknown_not_vwap_flat(self):
        data = dataset()
        # Current 09:06 signal is fully warmed again; historical snapshot at
        # 09:03 itself is stale, and cannot borrow the 09:03:05 observation.
        data["events"] = [event for event in data["events"]
                          if event["symbol"] != "2466" or not 170 <= (event["received_at"] - OPEN).total_seconds() <= 180]
        enriched = build_signal_features(data, [raw_signal(data)], CONFIG)[0]
        out = evaluate_variant(enriched, "R3")
        self.assertIsNone(out["pass"])
        self.assertIn("HISTORICAL_TICK_STALE", out["reasons"])

    def test_historical_vwap_need_not_have_300s_warmup(self):
        data = dataset()
        enriched = build_signal_features(data, [raw_signal(data, at=300)], CONFIG)[0]
        f = enriched["round2_features"]
        self.assertTrue(f["vwap_slope_available"])
        self.assertEqual(f["vwap_180s_ago_warmup_reason"], "CONTINUOUS_300S_WARMUP_INCOMPLETE")

    def test_shared_feature_mismatch_prevents_reuse(self):
        data = dataset()
        signal = raw_signal(data)
        signal["features"]["relative_strength_5m"] = 999
        with self.assertRaisesRegex(FeatureMismatchError, "relative_strength_5m"):
            build_signal_features(data, [signal], CONFIG)

    def test_missing_original_feature_is_not_silently_rebuilt(self):
        data = dataset()
        signal = raw_signal(data)
        del signal["features"]["benchmark_return_5m"]
        with self.assertRaisesRegex(FeatureMismatchError, "MISSING"):
            build_signal_features(data, [signal], CONFIG)

    def test_retest_distance_is_verified_without_rebuilding_setup(self):
        data = dataset()
        signal = raw_signal(data)
        signal["features"]["entry_distance_fraction"] = 0
        with self.assertRaisesRegex(FeatureMismatchError, "ENTRY_DISTANCE"):
            build_signal_features(data, [signal], CONFIG)

    def test_benchmark_missing_book_does_not_change_original_weak_filter(self):
        data = dataset()
        data["events"] = [event for event in data["events"]
                          if not (event["symbol"] == "0050" and event["kind"] == "book")]
        signal = raw_signal(data)
        enriched = build_signal_features(data, [signal], CONFIG)[0]
        self.assertTrue(signal["features"]["market_filter_pass"])
        self.assertTrue(evaluate_variant(enriched, "R1")["pass"])

    def test_long_market_mask_uses_readiness_not_no_long_signal(self):
        data = dataset(benchmark=False)
        out = build_feature_context(data, [], CONFIG,
                                    decision_times=[OPEN + timedelta(seconds=300)])
        self.assertFalse(out["market_decision_mask"][0]["long_market_evaluable"])
        self.assertIn("BENCHMARK_0050_NOT_IN_ARCHIVE", out["market_decision_mask"][0]["unavailable_reasons"])

    def test_common_mask_measures_availability_not_every_filter_passing(self):
        data = dataset()
        signal = raw_signal(data, reference=40.0)
        out = build_feature_context(data, [signal], CONFIG, decision_times=[])
        self.assertFalse(evaluate_variant(out["signals"][0], "R4")["pass"])
        self.assertTrue(out["common_mask"][0]["common_evaluable"])

    def test_no_file_network_broker_or_order_implementation_required(self):
        data = dataset()
        signal = raw_signal(data)
        with patch("builtins.open", side_effect=AssertionError("no external I/O")):
            out = build_feature_context(data, [signal], CONFIG, decision_times=[])
        self.assertEqual(out["actual_orders"], 0)
        self.assertEqual(out["broker_connections"], 0)
        self.assertEqual(out["market_subscriptions"], 0)

    def test_unsorted_archive_and_duplicate_ids_are_rejected(self):
        data = dataset()
        signal = raw_signal(data)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_signal_features(data, [signal, signal], CONFIG)
        data["events"] = list(reversed(data["events"]))
        with self.assertRaisesRegex(ValueError, "sorted"):
            build_signal_features(data, [signal], CONFIG)

    def test_empty_inputs_do_not_invent_a_signal(self):
        data = dataset()
        out = build_feature_context(data, [], CONFIG, decision_times=[])
        self.assertEqual(out["signals"], [])
        self.assertEqual(out["common_mask"], [])
        self.assertEqual(out["market_decision_mask"], [])


if __name__ == "__main__":
    unittest.main()
