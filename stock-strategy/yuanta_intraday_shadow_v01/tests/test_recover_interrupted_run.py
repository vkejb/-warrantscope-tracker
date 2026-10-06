import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.recover_interrupted_run import _subscription_symbols, recover


class InterruptedRunRecoveryTests(unittest.TestCase):
    def test_subscription_count_respects_disabled_stage_a_stream(self):
        snapshot = {
            "stage_a_quotes_enabled": False,
            "stocks": [{"stock_id": "TOP_ONLY"}, {"stock_id": "OVERLAP"}],
            "market_context": [{"stock_id": "0050"}],
            "expanded_shadow_universe": {
                "stocks": [{"stock_id": "OVERLAP"}, {"stock_id": "EXPANDED"}],
            },
        }
        self.assertEqual(_subscription_symbols(snapshot), {"0050", "OVERLAP", "EXPANDED"})
        snapshot["stage_a_quotes_enabled"] = True
        self.assertEqual(
            _subscription_symbols(snapshot),
            {"0050", "TOP_ONLY", "OVERLAP", "EXPANDED"},
        )

    def test_recovers_complete_rows_rebuilds_top30_and_marks_partial(self):
        with TemporaryDirectory(prefix="collector-recovery-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            snapshot = {
                "run_id": "MOCK_RUN", "created_at": "2026-10-06T01:00:00Z",
                "signal_date": "2026-10-05", "stage_a_seal_hash": "a" * 64,
                "mode": "SHADOW_ONLY_READ_ONLY_QUOTES",
                "stage_a_quotes_enabled": True,
                "stocks": [{"stock_id": "4919", "stock_name": "新唐", "rank": 1,
                            "score": 9.5, "market": "TWSE"}],
                "market_context": [{"stock_id": "0050", "stock_name": "元大台灣50",
                                    "market": "TWSE", "role": "MARKET_BENCHMARK"}],
                "expanded_shadow_universe": {"count": 2, "seal_hash": "b" * 64,
                    "stocks": [{"stock_id": "4919"}, {"stock_id": "9999"}]},
            }
            (source / "watchlist.json").write_bytes(canonical_bytes(snapshot) + b"\n")
            common = {
                "run_id": "MOCK_RUN", "subscription_generation": 1,
                "event_kind": "STOCK_TICK", "callback_received_at": "2026-10-06T01:00:01Z",
                "ingest_sequence": 1, "ingest_accepted": True, "ingest_reason": "ACCEPTED",
                "received_at": "2026-10-06T01:00:01Z", "stock_name": "Expanded",
                "market": "TWSE", "role": "EXPANDED_SHADOW_CANDIDATE",
                "expanded_rank": 2, "industry": "tech", "industry_code": "24",
            }
            expanded = [{**common, "stock_id": "4919"}, {**common, "stock_id": "9999", "ingest_sequence": 2}]
            market = [{**common, "stock_id": "0050", "role": "MARKET_BENCHMARK"}]
            for name, rows in {
                "expanded_ticks.jsonl.gz": expanded,
                "expanded_books.jsonl.gz": expanded,
                "market_context_ticks.jsonl.gz": market,
                "market_context_books.jsonl.gz": market,
            }.items():
                complete = gzip.compress(b"".join(canonical_bytes(row) + b"\n" for row in rows), mtime=0)
                (source / name).write_bytes(complete[:-8])
            for name in ("callback_errors.jsonl", "decision_evidence.jsonl", "subscription_evidence.jsonl"):
                (source / name).write_text("", encoding="utf-8")

            result = recover(source, root / "recovered")
            manifest = result["manifest"]
            destination = Path(result["run_dir"])
            self.assertEqual(manifest["status"], "PARTIAL_SESSION")
            self.assertEqual(manifest["event_counts"]["ticks"], 1)
            self.assertEqual(manifest["event_counts"]["books"], 1)
            self.assertFalse(manifest["recovery"]["normal_complete_session_claimed"])
            with gzip.open(destination / "ticks.jsonl.gz", "rt", encoding="utf-8") as handle:
                row = json.loads(handle.readline())
            self.assertEqual((row["stock_id"], row["role"], row["stage_a_rank"]),
                             ("4919", "STAGE_A_CANDIDATE", 1))
            self.assertNotIn("expanded_rank", row)
            for name in ("ticks.jsonl.gz", "books.jsonl.gz", "expanded_ticks.jsonl.gz",
                         "expanded_books.jsonl.gz", "market_context_ticks.jsonl.gz",
                         "market_context_books.jsonl.gz"):
                with gzip.open(destination / name, "rb") as handle:
                    handle.read()


if __name__ == "__main__":
    unittest.main()
