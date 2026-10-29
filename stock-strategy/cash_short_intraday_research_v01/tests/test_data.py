"""Fixtures only: do not open any active collector/broker data."""
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cash_short_intraday_research_v01 import data


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data._canonical(value) + b"\n")


class DataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / "stock-strategy"
        self.seal = {"schema_version": "1", "signal_date": "20261028", "setup": "TOP30",
                     "mode": "SHADOW_ONLY", "model_hash": "m", "model_spec_hash": "s",
                     "config_hash": "c", "input_hash": "i", "eligible_stock_count": 30,
                     "created_at": "2026-10-28T17:00:00+08:00", "status": "SEALED",
                     "stocks": [{"stock_id": f"{2000+i}", "stock_name": f"Stock{i}", "rank": i+1, "score": 1-i/100}
                                for i in range(30)]}
        self.seal["seal_hash"] = hashlib.sha256(data._canonical({k: self.seal[k] for k in data._SEAL_FIELDS})).hexdigest()
        write_json(self.base / "stage_a_prospective_watchlist_v01/runtime/seals/20261028.json", self.seal)
        cal = self.base / "shadow_daily_runner/runtime/trading_calendar.csv"
        cal.parent.mkdir(parents=True)
        cal.write_text("date\n2026-10-28\n2026-10-29\n", encoding="utf-8")

    def tick(self, at="2026-10-29T09:01:00+08:00", serial=1, symbol="2000", **kw):
        return {"stock_id": symbol, "received_at": at, "quote_time": "09:00:59.900",
                "deal_price": "10", "deal_volume": "7", "buy_price": "9.99", "sell_price": "10.01",
                "serial_no": serial, "in_out_flag": "0", "password": "DO_NOT_RETAIN", **kw}

    def book(self, at="2026-10-29T09:01:01+08:00", symbol="2000", **kw):
        return {"stock_id": symbol, "received_at": at, "buy_prices": ["9.99", "9.98", "0"],
                "buy_volumes": ["12", "4", "0"], "sell_prices": ["10.01", "10.02", "0"],
                "sell_volumes": ["8", "9", "0"], **kw}

    def make_run(self, name="run1", ticks=None, books=None, status="COMPLETE", seal_hash=None, full=True, context_sequence=False):
        p = self.base / "yuanta_intraday_shadow_v01/runtime/runs" / name
        p.mkdir(parents=True)
        w = {"stocks": self.seal["stocks"], "signal_date": "20261028", "stage_a_seal_hash": seal_hash or self.seal["seal_hash"],
             "created_at": "2026-10-29T08:50:00+08:00", "market_context": [{"stock_id": "0050", "stock_name": "ETF"}]}
        write_json(p / "watchlist.json", w)
        streams = {"ticks.jsonl.gz": ticks if ticks is not None else [self.tick()],
                   "books.jsonl.gz": books if books is not None else [self.book()],
                   "market_context_ticks.jsonl.gz": [self.tick(symbol="0050", **({"ingest_sequence": 30} if context_sequence else {}))],
                   "market_context_books.jsonl.gz": [self.book(symbol="0050", **({"ingest_sequence": 40} if context_sequence else {}))]}
        for name, rows in streams.items():
            with gzip.open(p / name, "wt", encoding="utf-8") as h:
                for row in rows:
                    h.write(json.dumps(row) + "\n")
        m = {"run_id": p.name, "started_at": "2026-10-29T08:50:00+08:00",
             "ended_at": "2026-10-29T13:35:00+08:00", "signal_date": "20261028",
             "stage_a_seal_hash": w["stage_a_seal_hash"], "status": status, "mode": "SHADOW_ONLY_READ_ONLY_QUOTES",
             "event_counts": {"ticks": len(streams["ticks.jsonl.gz"]), "books": len(streams["books.jsonl.gz"]), "callback_errors": 0},
             "artifacts": {x.name: data._hash(x) for x in p.iterdir() if x.is_file()},
             "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0}
        m["manifest_hash"] = hashlib.sha256(data._canonical(m)).hexdigest()
        write_json(p / "run_manifest.json", m)
        session = {"coverage_status": "FULL_SESSION" if full else "PARTIAL_SESSION", "stream_coverage_pass": full,
                   "source_run_id": p.name, "source_manifest_hash": m["manifest_hash"], "session_date": "20261029"}
        session["analysis_hash"] = hashlib.sha256(data._canonical(session)).hexdigest()
        write_json(p.parents[1] / "session_analyses" / p.name / "session_manifest.json", session)
        return p

    def test_discover_has_all_runs_and_sizes_without_decompression(self):
        self.make_run(); self.make_run("stopped", status="STOPPED_CLEAN", full=False)
        from unittest.mock import patch
        with patch.object(data, "_iter_jsonl", side_effect=AssertionError("must not scan")):
            rows = data.discover(self.base.parent)
        self.assertEqual(len(rows), 2)
        self.assertEqual({x["day"] for x in rows}, {"20261029"})
        self.assertGreater(rows[0]["source_bytes"], 0)
        self.assertNotIn("expanded_ticks", rows[0]["paths"])

    def test_data_error_retains_source_evidence_without_type_error(self):
        evidence = [{"path": "safe-quote-file", "hash_ok": False}]
        exc = data.DataError("SOURCE_HASH_MISMATCH", sources=evidence)
        self.assertEqual(str(exc), "SOURCE_HASH_MISMATCH")
        self.assertIs(exc.sources, evidence)

    def test_load_preserves_five_levels_native_units_and_unknown_exchange(self):
        result = data.load_day([self.make_run()])
        tick = next(e for e in result["events"] if e["kind"] == "tick" and e["symbol"] == "2000")
        book = next(e for e in result["events"] if e["kind"] == "book" and e["symbol"] == "2000")
        self.assertEqual(tick["volume"], 7)
        self.assertEqual(tick["exchange_time"].isoformat(), "2026-10-29T09:00:59.900000+08:00")
        self.assertIsNone(book["exchange_time"])
        self.assertEqual(book["bids"], [(9.99, 12), (9.98, 4), (0, 0)])
        self.assertEqual(book["payload"]["buy_volumes"], ["12", "4", "0"])
        self.assertNotIn("password", tick["payload"])
        self.assertEqual(result["metadata"]["2000"]["name"], "Stock0")
        self.assertEqual(len(result["candidates"]), 30)
        self.assertEqual(result["volume_units"], data.UNITS)

    def test_earliest_receipt_dedup_only_same_symbol_and_complete_identity(self):
        duplicate = self.tick(at="2026-10-29T09:02:00+08:00")
        p = self.make_run(ticks=[duplicate, self.tick(), self.tick(symbol="2001"),
                           self.tick(serial=2), self.tick(quote_time="")])
        result = data.load_day([p])
        ticks = [e for e in result["events"] if e["kind"] == "tick" and e["symbol"] in result["candidates"]]
        self.assertEqual(result["duplicates_dropped"], 1)
        self.assertEqual(len(ticks), 4)
        self.assertEqual(ticks[0]["received_at"].minute, 1)
        self.assertTrue(any(e["out_of_order"] for e in ticks))

    def test_canonical_complete_run_does_not_merge_manual_duplicate(self):
        stopped = self.make_run("manual", status="STOPPED_CLEAN", full=False)
        complete = self.make_run("automatic")
        result = data.load_day([stopped, complete])
        self.assertEqual(result["selected_run_ids"], ["automatic"])
        self.assertEqual(len(result["events"]), 4)

    def test_distinct_candidate_seals_fail_before_merge(self):
        a = self.make_run("a"); b = self.make_run("b", seal_hash="different")
        with self.assertRaisesRegex(data.DataError, "DIFFERENT_CANDIDATE_SEALS"):
            data.load_day([a, b])

    def test_source_mutation_fails_closed(self):
        p = self.make_run()
        (p / "books.jsonl.gz").write_bytes(b"tampered")
        with self.assertRaisesRegex(data.DataError, "SOURCE_HASH_MISMATCH"):
            data.load_day([p])
        rows, sources = data.audit_all(self.base)
        self.assertEqual(rows[0]["audit_status"], "BLOCKED")
        self.assertTrue(any(x["role"] == "books" and x["hash_ok"] is False for x in sources))

    def test_ingest_sequence_receipt_ties_are_preserved_not_exchange_sorted(self):
        at = "2026-10-29T09:01:00+08:00"
        p = self.make_run(ticks=[self.tick(at=at, ingest_sequence=20)],
                     books=[self.book(at=at, ingest_sequence=10)], context_sequence=True)
        result = data.load_day([p])
        chosen = [e for e in result["events"] if e["symbol"] == "2000"]
        self.assertEqual([e["sequence"] for e in chosen], [10, 20])
        self.assertEqual(result["ambiguous_receipt_ties"], 0)

    def test_legacy_cross_stream_tie_is_explicitly_unknown(self):
        p = self.make_run(ticks=[self.tick()], books=[self.book(at="2026-10-29T09:01:00+08:00")])
        result = data.load_day([p])
        self.assertGreater(result["ambiguous_receipt_ties"], 0)
        self.assertTrue(any(e.get("receipt_order_ambiguous") for e in result["events"]))

    def test_audit_is_streaming_unknown_eligibility_not_zero_or_certified(self):
        self.make_run()
        rows, sources = data.audit_all(self.base)
        row = rows[0]
        self.assertTrue(row["source_hash_verified"])
        self.assertTrue(row["calendar_open"])
        self.assertTrue(row["point_in_time_pass"])
        self.assertEqual(row["eligibility_status"], "UNKNOWN_NOT_ARCHIVED")
        self.assertEqual(row["reference_price_status"], "UNKNOWN_NOT_ARCHIVED")
        self.assertTrue(row["context_0050_complete"])
        self.assertFalse(row["context_2330_complete"])
        self.assertEqual(row["unknown_exchange_time_events"], 2)
        self.assertTrue(all(x["hash_ok"] for x in sources if x["role"] != "calendar"))
        self.assertNotIn("events", row)

    def test_quote_outside_archived_candidates_fails_closed(self):
        p = self.make_run(ticks=[self.tick(symbol="9999")])
        with self.assertRaisesRegex(data.DataError, "QUOTE_OUTSIDE"):
            data.load_day([p])

    def test_late_seal_is_not_point_in_time_even_with_valid_content_hash(self):
        self.seal["created_at"] = "2026-10-29T09:30:00+08:00"
        write_json(self.base / "stage_a_prospective_watchlist_v01/runtime/seals/20261028.json", self.seal)
        self.make_run()
        rows, _ = data.audit_all(self.base)
        self.assertFalse(rows[0]["point_in_time_pass"])

    def test_recorded_coverage_is_hash_and_run_identity_verified(self):
        p = self.make_run()
        sp = p.parents[1] / "session_analyses" / p.name / "session_manifest.json"
        session = json.loads(sp.read_text())
        session["source_run_id"] = "not-the-original-run"
        session["analysis_hash"] = hashlib.sha256(data._canonical({k: v for k, v in session.items() if k != "analysis_hash"})).hexdigest()
        write_json(sp, session)
        with self.assertRaisesRegex(data.DataError, "SESSION_MANIFEST_HASH_OR_SOURCE_IDENTITY"):
            data.load_day([p])

    def test_rejected_ingest_is_status_not_a_new_executable_tick(self):
        p = self.make_run(ticks=[self.tick(ingest_accepted=False, ingest_sequence=2)])
        result = data.load_day([p])
        event = next(e for e in result["events"] if e["symbol"] == "2000" and e.get("serial") == 1)
        self.assertEqual(event["kind"], "status")
        self.assertEqual(event["status"], "ARCHIVED_INGEST_REJECTED")
        self.assertFalse(event["quote_valid"])

    def test_partial_source_quality_is_not_promoted_by_load(self):
        p = self.make_run(status="STOPPED_CLEAN", full=False)
        result = data.load_day([p])
        self.assertEqual(result["source_status"], "STOPPED_CLEAN")
        self.assertEqual(result["recorded_coverage_status"], "PARTIAL_SESSION")
        self.assertFalse(result["recorded_stream_coverage_pass"])

    def test_per_symbol_coverage_does_not_hide_sparse_stock_behind_busy_stock(self):
        ticks = [self.tick(at="2026-10-29T09:01:00+08:00", serial=1),
                 self.tick(at="2026-10-29T13:29:00+08:00", serial=2, quote_time="13:29:00"),
                 self.tick(at="2026-10-29T11:00:00+08:00", serial=1, symbol="2001"),
                 self.tick(at="2026-10-29T11:01:00+08:00", serial=2, symbol="2001", in_out_flag="?")]
        books = [self.book(at="2026-10-29T09:02:00+08:00"), self.book(at="2026-10-29T13:28:00+08:00")]
        self.make_run(ticks=ticks, books=books)
        progress = []
        rows, _ = data.audit_all(self.base, progress=progress.append)
        dense, sparse = (rows[0]["per_symbol_coverage"][s] for s in ("2000", "2001"))
        self.assertEqual(dense["first5m_tick_count"], 1)
        self.assertEqual(dense["last5m_tick_count"], 1)
        self.assertEqual(dense["max_tick_gap_seconds"], 268*60)
        self.assertEqual(sparse["max_tick_gap_seconds"], 150*60)
        self.assertFalse(sparse["has_regular_ticks_and_books"])
        self.assertEqual(sparse["unknown_in_out_flag_count"], 1)
        self.assertIsNone(sparse["first_regular_book_received_at"])
        self.assertIsNone(sparse["max_book_gap_seconds"])
        self.assertEqual(dense["unknown_book_exchange_time_count"], 2)
        self.assertEqual(dense["missing_book_ingest_sequence_count"], 2)
        self.assertEqual([e["event"] for e in progress], ["DAY_STARTED", "DAY_FINISHED"])

    def test_raw_14_15_times_retained_even_with_nontrade_zero_price(self):
        self.make_run(ticks=[self.tick(serial=0, in_out_flag="14", tick_type="0", deal_price="0", quote_time=""),
                             self.tick(at="2026-10-29T09:03:00+08:00", serial=0, in_out_flag="15", tick_type="0", quote_time="09:02:59")])
        rows, _ = data.audit_all(self.base)
        stats = rows[0]["per_symbol_coverage"]["2000"]
        self.assertEqual(stats["raw_tick_type_counts"], {"0": 2})
        self.assertEqual(len(stats["status_14_times"]), 1)
        self.assertIsNone(stats["status_14_times"][0]["exchange_time"])
        self.assertEqual(stats["status_15_times"][0]["exchange_time"], "2026-10-29T09:02:59+08:00")
        self.assertEqual(stats["trade_tick_events"], 0)
        self.assertEqual(stats["regular_tick_events"], 0)
        self.assertEqual(stats["native_trade_volume_total"], 0)
        result = data.load_day([self.base / "yuanta_intraday_shadow_v01/runtime/runs/run1"])
        self.assertEqual([e.get("status_code") for e in result["events"] if e["symbol"] == "2000" and e["kind"] == "status"], ["14", "15"])
        self.assertEqual([e.get("status") for e in result["events"] if e["symbol"] == "2000" and e["kind"] == "status"], ["HALT", "RESUMED"])

    def test_all_sdk_status_flags_excluded_from_trade_volume_and_trade_serial_baseline(self):
        controls = [self.tick(at=f"2026-10-29T09:01:{i:02d}+08:00", serial=0,
                              in_out_flag=str(flag), quote_time="00:00:00", deal_volume="999")
                    for i, flag in enumerate(range(10, 16))]
        before = self.tick(at="2026-10-29T09:00:59+08:00", serial=5, quote_time="09:00:58")
        after = self.tick(at="2026-10-29T09:01:10+08:00", serial=6, quote_time="09:01:09")
        p = self.make_run(ticks=[before, *controls, after])
        result = data.load_day([p])
        trades = [e for e in result["events"] if e["symbol"] == "2000" and e["kind"] == "tick"]
        self.assertEqual([e["serial"] for e in trades], [5, 6])
        self.assertTrue(all(not e["out_of_order"] for e in trades))
        rows, _ = data.audit_all(self.base)
        self.assertEqual(rows[0]["per_symbol_coverage"]["2000"]["native_trade_volume_total"], 14)

    def test_serial_minus_one_is_header_causally_carried_not_a_trade_or_future_backfill(self):
        header = self.tick(at="2026-10-29T09:02:00+08:00", serial=-1, buy_price="11", sell_price="9",
                           deal_price="10", deal_volume="0", quote_time="00:00:00")
        before = self.tick(serial=5)
        after = self.tick(at="2026-10-29T09:03:00+08:00", serial=6, quote_time="09:02:59")
        p = self.make_run(ticks=[before, header, after], books=[self.book(), self.book(at="2026-10-29T09:02:01+08:00")])
        result = data.load_day([p])
        books = [e for e in result["events"] if e["symbol"] == "2000" and e["kind"] == "book"]
        self.assertNotIn("upper_limit", books[0])
        self.assertEqual((books[1]["reference_price"], books[1]["upper_limit"], books[1]["lower_limit"]), (10, 11, 9))
        last = next(e for e in result["events"] if e["symbol"] == "2000" and e.get("serial") == 6)
        self.assertFalse(last["out_of_order"])
        rows, _ = data.audit_all(self.base)
        stats = rows[0]["per_symbol_coverage"]["2000"]
        self.assertEqual(stats["trade_tick_events"], 2)
        self.assertEqual(len(stats["header_records"]), 1)
        self.assertEqual(rows[0]["reference_price_known_candidate_count"], 1)

    def test_type_14_is_not_sdk_halt_code(self):
        p = self.make_run(ticks=[self.tick(tick_type="14", in_out_flag="0")])
        result = data.load_day([p])
        event = next(e for e in result["events"] if e["symbol"] == "2000" and e.get("serial") == 1)
        self.assertEqual(event["kind"], "status")
        self.assertEqual(event["status"], "UNKNOWN_TICK_TYPE")


if __name__ == "__main__":
    unittest.main()
