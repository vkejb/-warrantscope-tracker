from __future__ import annotations

from datetime import datetime, timedelta
import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from yuanta_live_runtime_v01.late_start import (
    warm_start_from_collector,
    warmup_required,
)
from yuanta_live_runtime_v01.strategy import LiveDirectionEngine


TAIPEI = ZoneInfo("Asia/Taipei")


class LateStartWarmupTests(unittest.TestCase):
    def engine(self):
        return LiveDirectionEngine(
            {"2330": "台積電", "0050": "元大台灣50"},
            candidate_symbols={"2330"},
            benchmark_symbol="0050",
        )

    def write_run(
        self,
        root: Path,
        *,
        latest: datetime,
        callback_error: bool = False,
        truncate_gzip_footer: bool = False,
    ) -> Path:
        run = root / "runs" / "20261005T005000.000000Z_test"
        run.mkdir(parents=True)
        watch = {
            "schema_version": 2,
            "run_id": run.name,
            "created_at": "2026-10-05T00:50:00.000Z",
            "signal_date": "20261002",
            "stage_a_seal_hash": "seal",
            "mode": "SHADOW_ONLY_READ_ONLY_QUOTES",
            "stocks": [{"stock_id": "2330"}],
            "market_context": [{"stock_id": "0050"}],
        }
        (run / "watchlist.json").write_text(json.dumps(watch), encoding="utf-8")
        (run / "callback_errors.jsonl").write_text(
            '{"error":"TEST"}\n' if callback_error else "",
            encoding="utf-8",
        )

        def rows(symbol: str):
            start = datetime(2026, 10, 5, 9, 0, tzinfo=TAIPEI)
            stamp = start
            serial = 1
            result = []
            while stamp <= latest:
                price = 100.0 + serial / 1000
                result.append({
                    "event_type": "STOCK_TICK",
                    "stock_id": symbol,
                    "quote_time": stamp.strftime("%H:%M:%S.%f")[:-3],
                    "received_at": (stamp + timedelta(milliseconds=50)).isoformat(),
                    "deal_price": str(price),
                    "deal_volume": "1",
                    "buy_price": str(price - 0.05),
                    "sell_price": str(price + 0.05),
                    "serial_no": serial,
                    "in_out_flag": "1",
                })
                serial += 1
                stamp += timedelta(seconds=10)
            return result

        for filename, symbol in (
            ("ticks.jsonl.gz", "2330"),
            ("market_context_ticks.jsonl.gz", "0050"),
        ):
            path = run / filename
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                for row in rows(symbol):
                    handle.write(json.dumps(row) + "\n")
            if truncate_gzip_footer:
                payload = path.read_bytes()
                path.write_bytes(payload[:-8])
        return run

    def test_warmup_is_only_required_after_opening_deadline(self):
        self.assertFalse(warmup_required(datetime(2026, 10, 5, 9, 5, tzinfo=TAIPEI)))
        self.assertTrue(warmup_required(datetime(2026, 10, 5, 9, 5, 1, tzinfo=TAIPEI)))
        self.assertFalse(warmup_required(datetime(2026, 10, 5, 13, 11, tzinfo=TAIPEI)))

    def test_verified_collector_history_warms_all_symbols(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 10, 5, 9, 10, tzinfo=TAIPEI)
            self.write_run(root, latest=now - timedelta(seconds=5))
            engine = self.engine()
            result = warm_start_from_collector(
                engine,
                runtime_dir=root,
                now=now,
                signal_date="20261002",
                seal_hash="seal",
                required_symbols={"2330", "0050"},
            )
            self.assertEqual(result.status, "READY")
            self.assertEqual(result.symbols_warmed, 2)
            summary = engine.observation_state_summary(now)
            self.assertEqual(summary["2330"]["session_open_time"], "2026-10-05T09:00:00+08:00")
            self.assertTrue(summary["2330"]["session_open_source"].startswith("COLLECTOR:"))
            self.assertGreater(summary["2330"]["causal_cumulative_volume"], 0)

    def test_active_gzip_without_footer_keeps_complete_records(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 10, 5, 9, 10, tzinfo=TAIPEI)
            self.write_run(
                root,
                latest=now - timedelta(seconds=5),
                truncate_gzip_footer=True,
            )
            result = warm_start_from_collector(
                self.engine(),
                runtime_dir=root,
                now=now,
                signal_date="20261002",
                seal_hash="seal",
                required_symbols={"2330", "0050"},
            )
            self.assertEqual(result.status, "READY")
            self.assertTrue(result.incomplete_gzip_tail_ignored)

    def test_stale_collector_fails_closed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 10, 5, 9, 10, tzinfo=TAIPEI)
            self.write_run(root, latest=now - timedelta(minutes=2))
            result = warm_start_from_collector(
                self.engine(), runtime_dir=root, now=now,
                signal_date="20261002", seal_hash="seal",
                required_symbols={"2330", "0050"},
            )
            self.assertEqual(result.status, "SOURCE_STALE")
            self.assertEqual(result.symbols_warmed, 0)

    def test_callback_error_source_is_not_eligible(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 10, 5, 9, 10, tzinfo=TAIPEI)
            self.write_run(root, latest=now, callback_error=True)
            result = warm_start_from_collector(
                self.engine(), runtime_dir=root, now=now,
                signal_date="20261002", seal_hash="seal",
                required_symbols={"2330", "0050"},
            )
            self.assertEqual(result.status, "SOURCE_NOT_FOUND")

    def test_complete_malformed_json_record_fails_closed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 10, 5, 9, 10, tzinfo=TAIPEI)
            run = self.write_run(root, latest=now)
            with gzip.open(run / "ticks.jsonl.gz", "at", encoding="utf-8") as handle:
                handle.write("{not-json}\n")
            with self.assertRaisesRegex(RuntimeError, "invalid complete JSON"):
                warm_start_from_collector(
                    self.engine(), runtime_dir=root, now=now,
                    signal_date="20261002", seal_hash="seal",
                    required_symbols={"2330", "0050"},
                )

    def test_engine_rejects_late_opening_reference(self):
        engine = self.engine()
        opened = datetime(2026, 10, 5, 9, 6, tzinfo=TAIPEI)
        outcome = engine.seed_session_history(
            "2330",
            session_date=opened.date(),
            opening_price=100,
            opening_time=opened,
            cumulative_volume=1,
            cumulative_pv=100,
            retained_ticks=[{
                "time": opened,
                "received_at": opened,
                "price": 100,
                "volume": 1,
                "bid": 99.9,
                "ask": 100.0,
                "flag": "1",
                "serial": 1,
            }],
            last_serial=1,
        )
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "OPENING_REFERENCE_TOO_LATE")


if __name__ == "__main__":
    unittest.main()
