from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo


STOCK_STRATEGY = Path(__file__).resolve().parents[2]
if str(STOCK_STRATEGY) not in sys.path:
    sys.path.insert(0, str(STOCK_STRATEGY))

from intraday_execution_research_v01.aggregator import aggregate_ticks, validate_no_future_bar  # noqa: E402
from intraday_execution_research_v01.config import CFG  # noqa: E402
from intraday_execution_research_v01.features import FEATURE_NAMES, build_daily_snapshots, snapshot_features  # noqa: E402
from intraday_execution_research_v01.main import healthcheck  # noqa: E402
from intraday_execution_research_v01.mock_feed import MockQuoteAdapter  # noqa: E402
from intraday_execution_research_v01.outcomes import blank_outcome  # noqa: E402
from intraday_execution_research_v01.recorder import TickRecorder  # noqa: E402
from intraday_execution_research_v01.replay import replay  # noqa: E402
from intraday_execution_research_v01.schema import TICK_FIELDS, Tick  # noqa: E402
from intraday_execution_research_v01.storage import immutable_json, sha256_file  # noqa: E402
from intraday_execution_research_v01.validation import seal_day, validate_seal  # noqa: E402
from intraday_execution_research_v01.watchlist import (  # noqa: E402
    build_watchlist, canonical_hash, export_watchlist, load_frozen_stage_a, verify_watchlist,
)


TZ = ZoneInfo("Asia/Taipei")


def tick(clock: str, sequence: str = "1", code: str = "2330", price: float = 100.0,
         size: int = 10, cumulative: int = 10, book: bool = True) -> Tick:
    exchange = datetime.fromisoformat(f"2025-12-31T{clock}").replace(tzinfo=TZ)
    received = exchange.astimezone(timezone.utc) + timedelta(milliseconds=20)
    return Tick(
        1, "2025-12-31", exchange.isoformat(), received.isoformat(), received.isoformat(),
        code, "TWSE", price, size, cumulative,
        price - .05 if book else None, 120 if book else None,
        price + .05 if book else None, 80 if book else None,
        sequence_id=sequence, source="MOCK", is_mock=True,
    )


class TestIntradayExecutionResearch(unittest.TestCase):
    def test_frozen_stage_a_load_has_no_refit(self):
        arrays, audit = load_frozen_stage_a(STOCK_STRATEGY)
        self.assertEqual(audit["stage_a_refit_count"], 0)
        self.assertEqual(audit["model_fingerprint"], CFG.expected_stage_a_model_fingerprint)
        self.assertEqual(len(arrays["meta"]), 704327)

    def test_watchlist_is_exact_deterministic_top30(self):
        first = build_watchlist(STOCK_STRATEGY, "2025-12-30")
        second = build_watchlist(STOCK_STRATEGY, "2025-12-30")
        self.assertEqual(first, second)
        self.assertEqual([row["stage_a_rank"] for row in first["symbols"]], list(range(1, 31)))
        self.assertEqual(first["subscription_trading_date"], "20251231")
        verify_watchlist(first)

    def test_watchlist_export_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, one = export_watchlist(STOCK_STRATEGY, root, "2025-12-30")
            b, two = export_watchlist(STOCK_STRATEGY, root, "2025-12-30")
            self.assertEqual(a, b)
            self.assertEqual(one, two)

    def test_watchlist_hash_mismatch_fails(self):
        payload = build_watchlist(STOCK_STRATEGY, "2025-12-30")
        payload["symbols"][0]["stage_a_rank"] = 99
        with self.assertRaises(RuntimeError): verify_watchlist(payload)

    def test_tick_schema_validates_three_timestamps(self):
        sample = tick("09:00:01")
        sample.validate()
        self.assertIn("processing_timestamp", TICK_FIELDS)
        with self.assertRaises(ValueError): replace(sample, processing_timestamp="2025-12-31T09:00:01").validate()

    def test_leading_zero_stock_code_is_preserved(self):
        sample = tick("09:00:01", code="006203")
        restored = Tick.from_payload(sample.payload())
        self.assertEqual(restored.stock_code, "006203")

    def test_duplicate_identity_uses_sequence_not_price(self):
        one = tick("09:00:01", sequence="1", price=100)
        two = tick("09:00:01", sequence="2", price=100)
        self.assertNotEqual(one.duplicate_key(), two.duplicate_key())

    def test_recorder_suppresses_exact_duplicate(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = TickRecorder(Path(temporary), "2025-12-31", ["2330"], "hash")
            recorder.prepare(); recorder.start()
            self.assertTrue(recorder.record(tick("09:00:01")))
            self.assertFalse(recorder.record(tick("09:00:01")))
            self.assertEqual(recorder.raw_manifest()["duplicate_count"], 1)

    def test_recorder_flags_exchange_out_of_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = TickRecorder(Path(temporary), "2025-12-31", ["2330"], "hash")
            recorder.prepare(); recorder.start()
            recorder.record(tick("09:05:00", "2", cumulative=20))
            recorder.record(tick("09:04:00", "1", cumulative=10))
            self.assertEqual(recorder.raw_manifest()["out_of_order_count"], 1)

    def test_recorder_flags_cumulative_volume_regression(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = TickRecorder(Path(temporary), "2025-12-31", ["2330"], "hash")
            recorder.prepare(); recorder.start()
            recorder.record(tick("09:00:00", "1", cumulative=20))
            recorder.record(tick("09:01:00", "2", cumulative=10))
            self.assertEqual(recorder.raw_manifest()["cumulative_volume_regressions"], 1)

    def test_raw_manifest_has_gap_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = TickRecorder(Path(temporary), "2025-12-31", ["2330"], "hash")
            recorder.prepare(); recorder.start()
            recorder.record(tick("09:00:00", "1"))
            recorder.record(tick("09:11:00", "2", cumulative=20))
            diagnostic = recorder.raw_manifest()["gap_diagnostics"]
            self.assertEqual(diagnostic["max_event_gap_seconds"], 660)
            self.assertEqual(diagnostic["gaps_over_10_minutes"], 1)

    def test_interrupt_and_resume_lifecycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = TickRecorder(Path(temporary), "2025-12-31", ["2330"], "hash")
            recorder.prepare(); recorder.start(); recorder.interrupt(); recorder.start()
            self.assertEqual(recorder.state, "RESUMED")
            recorder.record(tick("09:00:01"))
            self.assertEqual(recorder.raw_manifest()["interrupted_symbols"], ["2330"])

    def test_crash_recovery_restores_duplicate_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = TickRecorder(root, "2025-12-31", ["2330"], "hash")
            first.prepare(); first.start(); first.record(tick("09:00:01"))
            recovered = TickRecorder(root, "2025-12-31", ["2330"], "hash")
            self.assertFalse(recovered.record(tick("09:00:01")))
            self.assertEqual(recovered.raw_manifest()["tick_count"], 1)

    def test_crash_recovery_preserves_duplicate_audit_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = TickRecorder(root, "2025-12-31", ["2330"], "hash")
            first.prepare(); first.start(); first.record(tick("09:00:01"))
            self.assertFalse(first.record(tick("09:00:01")))
            recovered = TickRecorder(root, "2025-12-31", ["2330"], "hash")
            self.assertEqual(recovered.raw_manifest()["duplicate_count"], 1)

    def test_raw_is_append_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = TickRecorder(Path(temporary), "2025-12-31", ["2330"], "hash")
            recorder.prepare(); recorder.start()
            recorder.record(tick("09:00:01", "1")); recorder.record(tick("09:01:01", "2"))
            lines = (recorder.raw_dir / "2330.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["sequence_id"], "1")

    def test_one_minute_aggregation_and_vwap(self):
        ticks = [tick("09:00:01", "1", price=100, size=10), tick("09:00:30", "2", price=102, size=30)]
        bars = aggregate_ticks(ticks, 1)
        self.assertEqual(len(bars), 1)
        self.assertEqual((bars[0]["open"], bars[0]["high"], bars[0]["close"]), (100, 102, 102))
        self.assertAlmostEqual(bars[0]["vwap"], 101.5)
        validate_no_future_bar(ticks, bars, 1)

    def test_five_minute_aggregation(self):
        ticks = [tick("09:00:01", "1"), tick("09:04:59", "2"), tick("09:05:00", "3")]
        bars = aggregate_ticks(ticks, 5)
        self.assertEqual(len(bars), 2)

    def test_snapshot_never_uses_future_for_features(self):
        past = [tick("09:00:01", "1",  price=100), tick("09:14:59", "2", price=101)]
        first = snapshot_features(past, "2025-12-31", "09:15", 99)["features"]
        second = snapshot_features(past + [tick("09:16:00", "3", price=999)], "2025-12-31", "09:15", 99)["features"]
        self.assertEqual(first, second)

    def test_entry_proxy_is_strictly_after_snapshot(self):
        ticks = [tick("09:15:00", "1", price=100), tick("09:15:01", "2", price=101)]
        result = snapshot_features(ticks, "2025-12-31", "09:15", 99)
        self.assertEqual(result["entry_proxy"]["price"], 101)

    def test_opening_range(self):
        ticks = [tick("09:00:01", "1", price=100), tick("09:04:59", "2", price=102), tick("09:05:01", "3", price=101)]
        result = snapshot_features(ticks, "2025-12-31", "09:15", 99)
        self.assertAlmostEqual(result["features"]["opening_range_5m"], .02)

    def test_intraday_drawdown_uses_path_low_not_last_return(self):
        ticks = [
            tick("09:00:01", "1", price=100),
            tick("09:10:00", "2", price=90),
            tick("09:14:59", "3", price=105),
        ]
        result = snapshot_features(ticks, "2025-12-31", "09:15", 99)
        self.assertAlmostEqual(result["features"]["intraday_drawdown_from_open"], -0.10)

    def test_vwap_slope_compares_adjacent_five_minute_windows(self):
        ticks = [
            tick("09:06:00", "1", price=100),
            tick("09:11:00", "2", price=110),
        ]
        result = snapshot_features(ticks, "2025-12-31", "09:15", 99)
        self.assertAlmostEqual(result["features"]["vwap_slope_5m"], 0.10)

    def test_missing_book_and_market_are_flagged(self):
        result = snapshot_features([tick("09:00:01", book=False)], "2025-12-31", "09:05", 99)
        self.assertIn("TOP1_BOOK_UNAVAILABLE", result["quality_flags"])
        self.assertIsNone(result["features"]["stock_return_minus_market_return"])

    def test_mock_signal_close_proxy_is_explicitly_flagged(self):
        watchlist = {
            "subscription_trading_date_formatted": "2025-12-31",
            "symbols": [{"stock_code": "2330", "signal_close": None}],
        }
        rows = build_daily_snapshots({"2330": [tick("09:00:01")]}, watchlist)
        self.assertTrue(all("SIGNAL_CLOSE_PROXY_FROM_FIRST_MOCK_TICK" in row["quality_flags"] for row in rows))

    def test_mock_feed_is_deterministic_and_has_duplicate(self):
        def events():
            adapter = MockQuoteAdapter("2025-12-31"); adapter.connect(); adapter.subscribe(["2330", "2317"])
            return [item.canonical_json() for item in adapter.events()]
        first, second = events(), events()
        self.assertEqual(first, second)
        self.assertGreater(len(first), len(set(first)))

    def test_mock_reconnect_health(self):
        adapter = MockQuoteAdapter("2025-12-31"); adapter.connect(); adapter.subscribe(["2330"]); adapter.simulate_reconnect()
        self.assertEqual(adapter.healthcheck()["reconnect_count"], 1)
        self.assertFalse(adapter.healthcheck()["broker_connection"])

    def test_replay_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary)
            recorder = TickRecorder(raw.parent, "2025-12-31", ["2330"], "hash")
            recorder.raw_dir = raw; recorder.prepare(); recorder.start()
            recorder.record(tick("09:01:00", "2")); recorder.record(tick("09:00:00", "1"))
            one, two = [], []
            replay(raw, lambda event: one.append(event.sequence_id), "instant")
            replay(raw, lambda event: two.append(event.sequence_id), "instant")
            self.assertEqual(one, two)
            self.assertEqual(one, ["1", "2"])

    def test_seal_hashes_and_sealed_day_immutable(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            recorder = TickRecorder(runtime, "2025-12-31", ["2330", "2317"], "watch")
            recorder.prepare(); recorder.start(); recorder.record(tick("09:00:00"))
            raw = recorder.raw_manifest()
            artifacts = []
            for name in ("one.json", "five.json", "features.json"):
                path = runtime / name; immutable_json(path, {"name": name}); artifacts.append(path)
            seal_path, _ = seal_day(runtime, "2025-12-31", "watch", raw, *artifacts)
            recorder.mark_sealed()
            self.assertTrue(validate_seal(runtime, seal_path)["pass"])
            self.assertIn("gap_diagnostics", json.loads(seal_path.read_text(encoding="utf-8")))
            with self.assertRaises(RuntimeError): recorder.record(tick("09:01:00", "2"))

    def test_missing_symbol_is_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = TickRecorder(Path(temporary), "2025-12-31", ["2330", "2317"], "watch")
            recorder.prepare(); recorder.start(); recorder.record(tick("09:00:00"))
            self.assertEqual(recorder.raw_manifest()["missing_symbols"], ["2317"])

    def test_outcome_schema_does_not_claim_real_evidence(self):
        payload = blank_outcome("2330", "09:15", None)
        self.assertEqual(payload["status"], "NOT_EVALUATED_NO_REAL_INTRADAY_HISTORY")
        self.assertIsNone(payload["plus8_before_minus5"])

    def test_healthcheck_is_mock_only(self):
        result = healthcheck()
        self.assertEqual(result["adapter"], "MOCK_ONLY")
        self.assertFalse(result["broker_connection"])
        self.assertEqual(result["real_quote_permission"], "NOT_TESTED")

    def test_no_broker_order_or_credentials_path(self):
        root = STOCK_STRATEGY / "intraday_execution_research_v01"
        source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
        self.assertNotIn("Yuanta", source)
        self.assertNotIn("place_order", source)
        self.assertNotIn("submit_order", source)
        self.assertNotIn("password", source.lower())

    def test_feature_set_and_snapshot_times_are_frozen(self):
        self.assertEqual(CFG.snapshot_times, ("09:05", "09:15", "09:30", "10:00", "11:00", "13:00"))
        self.assertEqual(len(FEATURE_NAMES), 25)


if __name__ == "__main__":
    unittest.main()
