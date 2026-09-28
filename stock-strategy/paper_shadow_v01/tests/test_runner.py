from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from paper_shadow_v01.runner import (
    PAPER_CONTRACT,
    publish_paper_day,
    replay_paper_session,
)
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from paper_shadow_v01.status import latest_status


TAIPEI = ZoneInfo("Asia/Taipei")


def _empty_stock(name: str) -> dict:
    return {
        "ticks": [], "books": [], "tick_times": [], "book_times": [],
        "meta": {"stock_name": name},
    }


def _market_stock(name: str, *, benchmark: bool = False) -> dict:
    start = datetime(2026, 9, 29, 9, 0, tzinfo=TAIPEI)
    ticks, books = [], []
    for index in range(61):
        at = start + timedelta(seconds=index * 5)
        if benchmark:
            price = 100.0 + index * 0.01
        else:
            price = 100.0 if index < 36 else 100.0 + (index - 35) * 0.12
        ticks.append({
            "time": at, "price": price, "volume": 10.0,
            "bid": price - 0.1, "ask": price, "flag": "1", "serial": index + 1,
        })
        books.append({
            "time": at, "buy_volume": 500.0, "sell_volume": 50.0,
            "best_bid": price - 0.1, "best_ask": price,
        })
    if not benchmark:
        for offset in (600, 900, 1200, 15600):
            at = start + timedelta(seconds=offset)
            ticks.append({
                "time": at, "price": 103.1, "volume": 10.0,
                "bid": 103.0, "ask": 103.1, "flag": "1",
                "serial": len(ticks) + 1,
            })
    ticks.sort(key=lambda row: row["time"])
    books.sort(key=lambda row: row["time"])
    return {
        "ticks": ticks, "books": books,
        "tick_times": [row["time"] for row in ticks],
        "book_times": [row["time"] for row in books],
        "meta": {"stock_name": name},
    }


def _source_manifest() -> dict:
    source = {"run_id": "r1"}
    source["manifest_hash"] = __import__("hashlib").sha256(canonical_bytes(source)).hexdigest()
    return source


def _session_manifest(source: dict, *, coverage: str = "FULL_SESSION") -> dict:
    session = {
        "coverage_status": coverage,
        "stream_coverage_pass": coverage == "FULL_SESSION",
        "source_run_id": source["run_id"],
        "source_manifest_hash": source["manifest_hash"],
        "session_date": "20260929",
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
    }
    session["analysis_hash"] = __import__("hashlib").sha256(
        canonical_bytes(session)
    ).hexdigest()
    return session


class PaperShadowTests(unittest.TestCase):
    def test_status_is_safe_before_first_paper_day(self):
        with tempfile.TemporaryDirectory() as temp:
            result = latest_status(Path(temp))
        self.assertEqual(result["status"], "NO_PAPER_DAY_YET")
        self.assertEqual(result["actual_orders"], 0)

    def test_causal_long_paper_trade_collects_checkpoints_and_grid(self):
        coverage = {
            "session_date": "20260929",
            "source_statuses": ["COMPLETE"], "callback_errors": 0,
            "started_at_taipei": "2026-09-29T08:50:00+08:00",
            "ended_at_taipei": "2026-09-29T13:35:00+08:00",
        }
        result = replay_paper_session(
            {"1001": _market_stock("測試股")},
            {"0050": _market_stock("元大台灣50", benchmark=True)},
            coverage,
        )
        self.assertEqual(result["reason"], "PAPER_TRADE_SCORED")
        self.assertEqual(result["trade"]["side"], "LONG")
        self.assertEqual(result["trade"]["exit_reason"], "HARD_EXIT")
        self.assertEqual(len(result["checkpoint_rows"]), 3)
        self.assertEqual(len(result["candidate_impacts"]), 75)
        self.assertEqual(len(result["candidate_grid"]), 75)

    def test_no_signal_is_a_valid_zero_trade_day(self):
        coverage = {
            "session_date": "20260929",
            "source_statuses": ["COMPLETE"],
            "callback_errors": 0,
            "started_at_taipei": "2026-09-29T08:50:00+08:00",
            "ended_at_taipei": "2026-09-29T13:35:00+08:00",
        }
        result = replay_paper_session(
            {"1001": _empty_stock("測試股")},
            {"0050": _empty_stock("元大台灣50")},
            coverage,
        )
        self.assertEqual(result["reason"], "NO_APPROVED_LONG_ENTRY")
        self.assertIsNone(result["trade"])
        self.assertGreater(len(result["decision_diagnostics"]), 0)

    def test_partial_session_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"; run.mkdir()
            source = _source_manifest()
            (run / "run_manifest.json").write_text(json.dumps(source))
            session = _session_manifest(source, coverage="PARTIAL_SESSION")
            with self.assertRaisesRegex(RuntimeError, "FULL_SESSION"):
                publish_paper_day(run, session, runtime_dir=Path(temp) / "runtime")

    def test_publish_is_immutable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / "run"; run.mkdir()
            source = _source_manifest()
            (run / "run_manifest.json").write_text(json.dumps(source))
            session = _session_manifest(source)
            coverage = {
                "session_date": "20260929", "source_statuses": ["COMPLETE"],
                "callback_errors": 0,
                "started_at_taipei": "2026-09-29T08:50:00+08:00",
                "ended_at_taipei": "2026-09-29T13:35:00+08:00",
            }
            replay = {
                "trade": None, "checkpoint_rows": [], "candidate_impacts": [],
                "candidate_grid": [], "decision_diagnostics": [],
                "reason": "NO_APPROVED_LONG_ENTRY",
            }
            runtime = root / "paper"
            with patch("paper_shadow_v01.runner.load_session", return_value=({}, coverage)), patch(
                "paper_shadow_v01.runner._validate_full_session_coverage"
            ), patch(
                "paper_shadow_v01.runner._load_market_context", return_value={"0050": _empty_stock("0050")}
            ), patch(
                "paper_shadow_v01.runner.replay_paper_session", return_value=replay
            ):
                first = publish_paper_day(run, session, runtime_dir=runtime)
                second = publish_paper_day(run, session, runtime_dir=runtime)
            self.assertEqual(first["publish_status"], "PUBLISHED")
            self.assertEqual(second["publish_status"], "ALREADY_PUBLISHED")
            self.assertEqual(first["paper_run_id"], second["paper_run_id"])
            self.assertEqual(first["manifest_hash"], second["manifest_hash"])
            self.assertEqual(first["actual_orders"], 0)
            self.assertEqual(first["actual_fills"], 0)
            self.assertEqual(first["broker_connections"], 0)

    def test_contract_is_paper_only(self):
        self.assertEqual(PAPER_CONTRACT["mode"], "POST_SESSION_CAUSAL_PAPER_REPLAY")
        self.assertEqual(PAPER_CONTRACT["direction"], "LONG_ONLY")
        self.assertEqual(PAPER_CONTRACT["early_failure_mode"], "OBSERVE_ONLY_DO_NOT_EXIT")
        self.assertEqual(PAPER_CONTRACT["actual_orders"], 0)


if __name__ == "__main__":
    unittest.main()
