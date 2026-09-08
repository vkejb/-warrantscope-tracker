from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
import zipfile

from shadow_daily_runner.calendar import build_trading_calendar, read_sessions
from shadow_daily_runner.config import CFG, MODULE_DIR
from shadow_daily_runner.normalize import (
    CATEGORY_BLANK_POSITIVE,
    CATEGORY_BLANK_ZERO,
    CATEGORY_PARSE,
    CATEGORY_PARTIAL,
    deterministic_zip,
    parse_release_archive,
    parse_tpex,
    parse_twse,
)
from shadow_daily_runner.pipeline import PreparedInputs
from shadow_daily_runner.runner import attempt
from shadow_daily_runner.sources import SourceSnapshot


def snapshot(path: Path, payload: bytes, source: str = "fixture") -> SourceSnapshot:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return SourceSnapshot(
        source=source,
        request_url=f"https://example.invalid/{source}",
        retrieved_at_utc="2026-09-08T00:00:00Z",
        sha256=hashlib.sha256(payload).hexdigest(),
        path=path,
        payload=payload,
    )


def release_payload(rows: list[list[str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["date", "code", "name", "volume", "open", "high", "low", "close"])
    writer.writerows(rows)
    csv_payload = output.getvalue().encode("utf-8")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("weekly_fixture.csv", csv_payload)
    return archive.getvalue()


class NormalizationTests(unittest.TestCase):
    def test_four_way_classification_and_exact_positive_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = release_payload(
                [
                    ["20260907", "1101", "valid", "100", "10", "11", "9", "10.5"],
                    ["20260907", "1102", "blank-zero", "0", "", "", "", ""],
                    ["20260907", "1103", "blank-positive", "17", "--", "--", "--", "--"],
                    ["20260907", "1104", "partial", "10", "10", "--", "9", "9.5"],
                    ["20260907", "1105", "bad", "10", "10", "9", "8", "9"],
                ]
            )
            source = snapshot(Path(temporary) / "source.zip", payload)
            parsed = parse_release_archive(source)
            self.assertEqual(1, len(parsed.rows))
            self.assertEqual(1, parsed.category_counts[CATEGORY_BLANK_ZERO])
            self.assertEqual(1, parsed.category_counts[CATEGORY_BLANK_POSITIVE])
            self.assertEqual(1, parsed.category_counts[CATEGORY_PARTIAL])
            self.assertEqual(1, parsed.category_counts[CATEGORY_PARSE])
            resolutions = {
                ("20260907", "1103"): {
                    "date": "20260907",
                    "code": "1103",
                    "volume": 17,
                    "all_ohlc_missing": True,
                    "market": "TWSE",
                    "source_sha256": "a" * 64,
                }
            }
            reconciled = parse_release_archive(source, resolutions)
            positive = [
                row for row in reconciled.excluded
                if row["category"] == CATEGORY_BLANK_POSITIVE
            ]
            self.assertEqual(1, len(positive))
            self.assertTrue(positive[0]["resolved"])
            self.assertIn("OFFICIAL_NO_REGULAR_SESSION_PRICE", positive[0]["resolution"])
            unresolved_categories = {row["category"] for row in reconciled.errors}
            self.assertEqual({CATEGORY_PARTIAL, CATEGORY_PARSE}, unresolved_categories)

    def test_positive_reconciliation_volume_must_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = release_payload(
                [["20260907", "1103", "x", "17", "", "", "", ""]]
            )
            source = snapshot(Path(temporary) / "source.zip", payload)
            parsed = parse_release_archive(
                source,
                {
                    ("20260907", "1103"): {
                        "date": "20260907",
                        "code": "1103",
                        "volume": 18,
                        "all_ohlc_missing": True,
                        "source_sha256": "b" * 64,
                    }
                },
            )
            self.assertEqual(1, len(parsed.errors))
            self.assertFalse(parsed.errors[0]["resolved"])

    def test_deterministic_zip(self):
        rows = [
            {
                "date": "20260907", "code": "1101", "name": "台泥",
                "volume": "100", "open": "10", "high": "11", "low": "9", "close": "10.5",
            }
        ]
        self.assertEqual(
            deterministic_zip("fixed.csv", rows),
            deterministic_zip("fixed.csv", rows),
        )

    def test_twse_official_status_date_schema_and_blank_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            fields = [
                "證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額",
                "開盤價", "最高價", "最低價", "收盤價",
            ]
            payload = json.dumps(
                {
                    "stat": "OK", "date": "20260907",
                    "tables": [{"title": "每日收盤行情", "fields": fields, "data": [
                        ["0050", "元大台灣50", "1,000", "10", "100", "66", "67", "65", "66.5"],
                        ["1103", "positive-no-price", "17", "2", "100", "--", "--", "--", "--"],
                    ]}],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            parsed = parse_twse(snapshot(Path(temporary) / "twse.json", payload), "20260907")
            self.assertEqual(1, len(parsed.rows))
            self.assertEqual(1, len(parsed.excluded))
            self.assertTrue(parsed.excluded[0]["resolved"])
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                parse_twse(snapshot(Path(temporary) / "twse2.json", payload), "20260908")

    def test_tpex_big5_schema_and_response_date(self):
        with tempfile.TemporaryDirectory() as temporary:
            text = "\n".join(
                [
                    "上櫃股票每日收盤行情(不含定價)",
                    "產業類別:所有證券",
                    "資料日期:115/09/07",
                    "代號,名稱,收盤 ,漲跌,開盤 ,最高 ,最低,成交股數  ,成交金額(元),成交筆數",
                    '"6488","環球晶","500","+1","495","505","490","1,000","500,000","100"',
                    '"2724","無價","----","---","----","----","----","0","0","0"',
                ]
            )
            payload = text.encode("big5")
            parsed = parse_tpex(snapshot(Path(temporary) / "tpex.csv", payload), "20260907")
            self.assertEqual(1, len(parsed.rows))
            self.assertEqual(1, len(parsed.excluded))
            self.assertEqual(CATEGORY_BLANK_ZERO, parsed.excluded[0]["category"])


class CalendarTests(unittest.TestCase):
    def test_calendar_is_officially_derived_sorted_unique_and_contains_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = json.dumps(
                [
                    {"Name": "中華民國開國紀念日", "Date": "1150101", "Description": "放假"},
                    {"Name": "國曆新年開始交易日", "Date": "1150102", "Description": "開始交易"},
                    {"Name": "休市", "Date": "1150909", "Description": "市場無交易"},
                ],
                ensure_ascii=False,
            ).encode("utf-8")
            calendar_bytes, metadata = build_trading_calendar(
                snapshot(Path(temporary) / "calendar.json", payload), 2026
            )
            sessions = read_sessions(calendar_bytes)
            self.assertIn("2026-09-08", sessions)
            self.assertNotIn("2026-09-09", sessions)
            self.assertEqual(sessions, sorted(set(sessions)))
            self.assertEqual(hashlib.sha256(calendar_bytes).hexdigest(), metadata["calendar_sha256"])


class RunnerSafetyTests(unittest.TestCase):
    def temporary_config(self, root: Path):
        return replace(
            CFG,
            runtime_dir=root / "runtime",
            shadow_store_dir=root / "shadow",
        )

    def seed_ledgers(self, cfg) -> dict[str, bytes]:
        cfg.shadow_store_dir.mkdir(parents=True)
        payloads = {}
        for name in (
            "prospective_signals.csv",
            "prospective_outcomes.csv",
            "prospective_scan_log.csv",
            "shadow_status.json",
        ):
            payload = f"immutable-{name}\n".encode()
            (cfg.shadow_store_dir / name).write_bytes(payload)
            payloads[name] = payload
        return payloads

    def test_readiness_failure_never_changes_prospective_ledgers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = self.temporary_config(root)
            original = self.seed_ledgers(cfg)
            audit_path = cfg.audit_dir / "readiness.json"

            def fail_prepare(**_kwargs):
                return PreparedInputs(
                    target_date="20260908", trading_day=True, ready=False,
                    archives=(), calendar_path=cfg.calendar_path,
                    audit_path=audit_path,
                    audit={"status": "READINESS_FAILED", "failure_reason": "fixture incomplete"},
                )

            result = attempt(
                now=datetime(2026, 9, 8, 14, 30, tzinfo=ZoneInfo("Asia/Taipei")),
                cfg=cfg,
                prepare=fail_prepare,
            )
            self.assertEqual("READINESS_FAILED_NO_LEDGER_WRITE", result["status"])
            for name, payload in original.items():
                self.assertEqual(payload, (cfg.shadow_store_dir / name).read_bytes())
            self.assertFalse(cfg.runner_state_path.exists())

    def test_outside_window_refuses_before_prepare(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = self.temporary_config(Path(temporary))
            called = False

            def should_not_run(**_kwargs):
                nonlocal called
                called = True
                raise AssertionError

            result = attempt(
                now=datetime(2026, 9, 8, 18, 0, tzinfo=ZoneInfo("Asia/Taipei")),
                cfg=cfg,
                prepare=should_not_run,
            )
            self.assertEqual("REFUSED_OUTSIDE_ATTEMPT_WINDOW", result["status"])
            self.assertFalse(called)

    def test_success_requires_builtin_outcomes_and_zero_safety_counters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = self.temporary_config(root)
            self.seed_ledgers(cfg)
            archive = root / "ready.zip"
            archive.write_bytes(b"fixture")
            calendar_path = root / "calendar.csv"
            calendar_path.write_text("date\n2026-09-08\n", encoding="utf-8")
            audit_path = root / "ready.json"

            def ready_prepare(**_kwargs):
                return PreparedInputs(
                    target_date="20260908", trading_day=True, ready=True,
                    archives=(archive,), calendar_path=calendar_path,
                    audit_path=audit_path, audit={"status": "READY"},
                )

            daily = {
                "signal_date": "20260908", "actual_orders": 0, "actual_fills": 0,
                "outcomes": {"status": "NO_ACTIVE_SIGNALS", "appended_outcomes": 0},
                "input_manifest_hash": "c" * 64, "run_manifest": "runs/x/run_manifest.json",
                "scan": {"status": "APPENDED", "appended_signals": 0}, "signal_count": 0,
            }
            status = {
                "status": {
                    "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
                    "last_successful_signal_date": "20260908",
                }
            }
            with patch("shadow_daily_runner.runner._run_json", side_effect=[daily, status]) as run:
                result = attempt(
                    now=datetime(2026, 9, 8, 14, 30, tzinfo=ZoneInfo("Asia/Taipei")),
                    cfg=cfg,
                    prepare=ready_prepare,
                )
            self.assertEqual("SUCCESS", result["status"])
            self.assertEqual("BUILT_IN_TO_RUN_DAILY", result["outcome_update_mode"])
            self.assertEqual(2, run.call_count)
            self.assertNotIn("update-outcomes", run.call_args_list[0].args[0])
            self.assertEqual("20260908", json.loads(cfg.runner_state_path.read_text())["last_successful_target"])

    def test_launchd_has_exact_four_weekday_slots_and_no_keepalive(self):
        path = MODULE_DIR / "launchd" / "com.linyunyan.warrantscope.shadow-daily.plist"
        payload = plistlib.loads(path.read_bytes())
        slots = payload["StartCalendarInterval"]
        self.assertEqual(20, len(slots))
        self.assertNotIn("KeepAlive", payload)
        self.assertNotIn("RunAtLoad", payload)
        self.assertNotIn("--date", payload["ProgramArguments"])
        for weekday in range(1, 6):
            actual = sorted(
                (item["Hour"], item["Minute"])
                for item in slots if item["Weekday"] == weekday
            )
            self.assertEqual([(14, 30), (15, 0), (15, 30), (16, 0)], actual)


class FrozenContractTests(unittest.TestCase):
    def test_prospective_sources_match_pre_runner_hashes(self):
        expected = {
            "config.py": "86249fdc5259a11aadb5a47890bc4e236cf2a71044b2598b3baaee12aba53536",
            "detector.py": "5c8559a07ce3281e723a0aa2c50d70b282d16c4c8b78f99d26c02273ca7acb76",
            "market_data_provider.py": "36a9e3df29a1f4d4225df8093f616f3de7d1061c44301d0d8b0a3b632dea90ee",
            "service.py": "bf164081cea4434242c86fca9f3b67a36452211d3270f72f76cc7339c2d2ba9a",
            "storage.py": "638d507756b85a2ea8cb300e6f56e569a4b14a34bc94c554bd101c9d1ad4d2c5",
        }
        prospective = MODULE_DIR.parent / "prospective_shadow_v01"
        for filename, digest in expected.items():
            self.assertEqual(digest, hashlib.sha256((prospective / filename).read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
