from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from exit_profit_research_v01.readonly_acceptance import inspect_run
from yuanta_intraday_shadow_v01.collector import AppendOnlyRun, WatchItem
from yuanta_live_runtime_v01 import main as runtime_main
from yuanta_live_runtime_v01.strategy import LiveDirectionEngine, TAIPEI


class ObservationEvidenceTests(unittest.TestCase):
    def test_engine_reports_actual_acceptance_branch(self):
        engine = LiveDirectionEngine({"0050": "ETF"}, candidate_symbols={"0050"})
        at = datetime(2026, 10, 2, 9, 5, tzinfo=TAIPEI)
        rejected = engine.ingest_tick(
            "0050", at=at, received_at=at + timedelta(seconds=10),
            price=100, volume=1, bid=99.9, ask=100.1, serial=1,
        )
        accepted = engine.ingest_tick(
            "0050", at=at, received_at=at,
            price=100, volume=1, bid=99.9, ask=100.1, serial=1,
        )
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "STALE_AT_INGEST")
        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.reason, "ACCEPTED")

    def test_callback_archives_actual_sequence_and_rejection(self):
        class Archive:
            run_id = "fixture-run"
            def __init__(self):
                self.rows = []
            def append(self, kind, payload):
                self.rows.append((kind, payload))
            def observation_failure(self, **_kwargs):
                raise AssertionError("unexpected observation failure")

        engine = LiveDirectionEngine({"0050": "ETF"}, candidate_symbols={"0050"})
        archive = Archive()
        session = runtime_main._Session(
            api_types={}, environment="UAT", credentials={"account": "fixture"},
            engine=engine, logger=lambda *_args, **_kwargs: None,
            archive=archive, non_archive_symbols={"0050"},
        )
        bad = SimpleNamespace(
            StkCode="0050", Time=SimpleNamespace(
                bytHour=99, bytMin=0, bytSec=0, ushtMSec=0,
            ), SerialNo=1, BuyPrice=99.9, SellPrice=100.1,
            DealPrice=100, DealVol=1, InOutFlag="1", Type="0",
        )
        session._on_response(2, 0, "SubscribeStockTick", None, bad)
        row = archive.rows[0][1]
        self.assertEqual(row["ingest_sequence"], 1)
        self.assertFalse(row["ingest_accepted"])
        self.assertEqual(row["ingest_reason"], "ADAPTER_INVALID_EXCHANGE_TIME")
        self.assertEqual(row["raw_serial_no"], 1)

    def test_observation_failure_never_escapes_decision_path(self):
        class BrokenArchive:
            run_id = "fixture"
            snapshot = {"signal_date": "20261001", "stage_a_seal_hash": "x"}
            def append_decision_evidence(self, _row):
                raise OSError("fixture failure")
            def observation_failure(self, **_kwargs):
                raise OSError("fixture failure")

        engine = LiveDirectionEngine({"0050": "ETF"}, candidate_symbols={"0050"})
        session = runtime_main._Session(
            api_types={}, environment="UAT", credentials={"account": "fixture"},
            engine=engine,
            logger=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("logger failure")
            ),
            archive=BrokenArchive(), non_archive_symbols={"0050"},
        )
        session.archive_decision_evidence(
            datetime(2026, 10, 2, 9, 5, tzinfo=TAIPEI),
            diagnostics={"decision": "REJECTED"}, candidate=None,
        )

    def test_new_archive_can_prove_three_dimensions(self):
        stocks = [
            WatchItem(str(1000 + index), f"股票{index}", index, 0.1, "TWSE")
            for index in range(1, 31)
        ]
        with tempfile.TemporaryDirectory() as temp:
            run = AppendOnlyRun(
                Path(temp),
                {"signal_date": "20261001", "seal_hash": "a" * 64},
                stocks, {}, mode="OBSERVE_ONLY_QUOTES",
            )
            base = {
                "run_id": run.run_id,
                "subscription_generation": 1,
                "stock_id": "0050",
                "callback_received_at": "2026-10-02T09:05:00+08:00",
                "received_at": "2026-10-02T09:05:00+08:00",
                "ingest_accepted": True,
                "ingest_reason": "ACCEPTED",
            }
            run.append("market_context_ticks", {
                **base, "event_kind": "STOCK_TICK", "ingest_sequence": 1,
                "serial_no": 1,
            })
            run.append("market_context_books", {
                **base, "event_kind": "FIVE_LEVEL", "ingest_sequence": 2,
                "serial_no": 0,
            })
            run.append_subscription_evidence({
                "run_id": run.run_id,
                "subscription_generation": 1,
                "event": "SUBSCRIBE_ACCEPTED",
            })
            run.append_decision_evidence({
                "run_id": run.run_id,
                "decision_time": "2026-10-02T09:05:30+08:00",
                "ingest_sequence_watermark": 2,
                "diagnostics": {"reason": "BENCHMARK_MISSING_OR_STALE"},
                "raw_quote_status": {"0050": {}},
                "engine_state_summary": {"0050": {}},
            })
            run.finalize(
                status="COMPLETE",
                started_at="2026-10-02T08:50:00+08:00",
                ended_at="2026-10-02T13:25:00+08:00",
            )
            result = inspect_run(run.run_dir)
            manifest = json.loads(
                (run.run_dir / "run_manifest.json").read_text(encoding="utf-8")
            )
        self.assertEqual(result["status"], "FORMAL_SOURCE_ELIGIBLE")
        self.assertEqual(
            result["qualification_dimensions"]["actual_live_input_decision_parity"],
            "PROVEN_BY_INGEST_AND_DECISION_LEDGER",
        )
        self.assertEqual(manifest["observation_evidence"]["queue_overflow_count"], 0)


if __name__ == "__main__":
    unittest.main()
