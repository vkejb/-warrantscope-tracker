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
from shadow_daily_runner.io_utils import sha256_file
from shadow_daily_runner.normalize import (
    CATEGORY_BLANK_POSITIVE,
    CATEGORY_BLANK_ZERO,
    CATEGORY_PARSE,
    CATEGORY_PARTIAL,
    UNIVERSE_FILTER_DESCRIPTION,
    deterministic_zip,
    eligible_security,
    parse_release_archive,
    parse_tpex,
    parse_twse,
)
from shadow_daily_runner.pipeline import PreparedInputs
from shadow_daily_runner.preflight import (
    extend_source_coverage_from_direct_audit,
    run_historical_preflight,
)
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
    def test_direct_official_universe_keeps_established_name_suffix_filter(self):
        self.assertTrue(eligible_security("2330", "台積電"))
        self.assertTrue(eligible_security("0050", "元大台灣50"))
        # This deliberately mirrors the established release producer.  It is a
        # compatibility proxy, not a claim that every company ending in 「特」
        # is a special security.
        self.assertFalse(eligible_security("3289", "宜特"))
        self.assertFalse(eligible_security("9103", "美德醫療-DR"))

        with tempfile.TemporaryDirectory() as temporary:
            payload = release_payload(
                [
                    ["20260908", "2330", "台積電", "100", "10", "11", "9", "10"],
                    ["20260908", "3289", "宜特", "100", "10", "11", "9", "10"],
                    ["20260908", "9103", "美德醫療-DR", "100", "10", "11", "9", "10"],
                ]
            )
            parsed = parse_release_archive(
                snapshot(Path(temporary) / "source.zip", payload)
            )
            self.assertEqual(["2330"], [row["code"] for row in parsed.rows])

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


class HistoricalPreflightTests(unittest.TestCase):
    def temporary_config(self, root: Path):
        return replace(CFG, runtime_dir=root / "runtime")

    def write_calendar(self, cfg, days: list[str]) -> Path:
        cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        payload = ("date\n" + "".join(f"{day}\n" for day in days)).encode("utf-8")
        cfg.calendar_path.write_bytes(payload)
        raw_calendar = cfg.runtime_dir / "official_calendar.raw"
        raw_calendar.write_bytes(b"official calendar fixture")
        cfg.calendar_metadata_path.write_text(
            json.dumps(
                {
                    "calendar_sha256": hashlib.sha256(payload).hexdigest(),
                    "source": {
                        "request_url": "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule",
                        "retrieved_at_utc": "2026-09-08T00:00:00Z",
                        "sha256": sha256_file(raw_calendar),
                        "path": str(raw_calendar),
                    },
                }
            ),
            encoding="utf-8",
        )
        return cfg.calendar_path

    def write_archive(self, root: Path, name: str, rows: list[dict[str, str]]) -> Path:
        archive = root / name
        archive.parent.mkdir(parents=True, exist_ok=True)
        payload = deterministic_zip(f"{archive.stem}.csv", rows)
        archive.write_bytes(payload)
        archive.with_suffix(archive.suffix + ".metadata.json").write_text(
            json.dumps(
                {
                    "filename": archive.name,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "source_manifest_hash": "d" * 64,
                }
            ),
            encoding="utf-8",
        )
        return archive

    def rows_for_days(self, days: list[str], per_day: int = 3) -> list[dict[str, str]]:
        rows = []
        for day_index, day in enumerate(days):
            for offset in range(per_day):
                code = "0050" if offset == 0 else f"{1100 + offset:04d}"
                price = 10 + day_index + offset
                rows.append(
                    {
                        "date": day.replace("-", ""),
                        "code": code,
                        "name": code,
                        "volume": "100",
                        "open": str(price),
                        "high": str(price + 1),
                        "low": str(price - 1),
                        "close": str(price),
                    }
                )
        return rows

    def coverage(self, calendar: Path, days: list[str], per_day: int = 3) -> dict:
        sessions = []
        for index, day in enumerate(days):
            twse_raw = calendar.parent / f"twse_{day}.raw"
            tpex_raw = calendar.parent / f"tpex_{day}.raw"
            twse_raw.write_bytes(f"TWSE {day}".encode())
            tpex_raw.write_bytes(f"TPEX {day}".encode())
            sessions.append(
                {
                    "date": day,
                    "status": "READY",
                    "TWSE": {
                        "status": "READY",
                        "row_count": per_day - 1,
                        "request_url": f"https://www.twse.com.tw/eod?date={day}",
                        "retrieved_at_utc": "2026-09-08T00:00:00Z",
                        "sha256": sha256_file(twse_raw),
                        "path": str(twse_raw),
                        "response_date": day,
                    },
                    "TPEX": {
                        "status": "READY",
                        "row_count": 1,
                        "request_url": f"https://www.tpex.org.tw/eod?date={day}",
                        "retrieved_at_utc": "2026-09-08T00:00:00Z",
                        "sha256": sha256_file(tpex_raw),
                        "path": str(tpex_raw),
                        "response_date": day,
                    },
                    "combined_count": per_day,
                }
            )
        return {
            "schema_version": "1",
            "universe_filter": UNIVERSE_FILTER_DESCRIPTION,
            "generated_at_utc": "2026-09-08T00:00:00Z",
            "scope": {"start_date": days[0], "through_date": days[-1]},
            "trading_calendar": {"path": str(calendar), "sha256": sha256_file(calendar)},
            "sessions": sessions,
        }

    def test_complete_history_passes_all_fail_closed_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = self.temporary_config(root)
            days = ["2026-09-07", "2026-09-08"]
            calendar = self.write_calendar(cfg, days)
            archive = self.write_archive(
                root, "weekly_2026_W37_clean.zip", self.rows_for_days(days)
            )
            cfg.historical_source_coverage_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.historical_source_coverage_path.write_text(
                json.dumps(self.coverage(calendar, days)), encoding="utf-8"
            )
            result = run_historical_preflight(
                archives=[archive],
                calendar_path=calendar,
                source_coverage_path=cfg.historical_source_coverage_path,
                through_date="2026-09-08",
                cfg=cfg,
            )
            self.assertTrue(result.ready)
            self.assertTrue(all(result.audit["checks"].values()))
            self.assertEqual([], result.audit["missing_calendar_sessions"])
            self.assertEqual(0, result.audit["actual_orders"])
            self.assertFalse(result.audit["signal_ledgers_touched"])

    def test_missing_market_and_calendar_day_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = self.temporary_config(root)
            days = ["2026-09-07", "2026-09-08"]
            calendar = self.write_calendar(cfg, days)
            archive = self.write_archive(
                root, "weekly_2026_W37_clean.zip", self.rows_for_days(days[:1])
            )
            coverage = self.coverage(calendar, days)
            coverage["sessions"][1]["status"] = "UNRESOLVED"
            coverage["sessions"][1]["TPEX"]["status"] = "MISSING"
            coverage["sessions"][1]["TPEX"]["row_count"] = 0
            coverage["sessions"][1]["combined_count"] = 2
            cfg.historical_source_coverage_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.historical_source_coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
            result = run_historical_preflight(
                archives=[archive], calendar_path=calendar,
                source_coverage_path=cfg.historical_source_coverage_path,
                through_date="2026-09-08", cfg=cfg,
            )
            self.assertFalse(result.ready)
            self.assertEqual(["2026-09-08"], result.audit["missing_calendar_sessions"])
            self.assertEqual("2026-09-08", result.audit["one_market_dates"][0]["date"])

    def test_official_raw_source_hash_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = self.temporary_config(root)
            days = ["2026-09-08"]
            calendar = self.write_calendar(cfg, days)
            archive = self.write_archive(root, "one.zip", self.rows_for_days(days))
            coverage = self.coverage(calendar, days)
            Path(coverage["sessions"][0]["TWSE"]["path"]).write_bytes(b"drift")
            cfg.historical_source_coverage_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.historical_source_coverage_path.write_text(
                json.dumps(coverage), encoding="utf-8"
            )
            result = run_historical_preflight(
                archives=[archive], calendar_path=calendar,
                source_coverage_path=cfg.historical_source_coverage_path,
                through_date="2026-09-08", cfg=cfg,
            )
            self.assertFalse(result.ready)
            self.assertFalse(result.audit["checks"]["official_source_hashes_fixed"])

    def test_duplicate_invalid_and_archive_hash_drift_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = self.temporary_config(root)
            days = ["2026-09-08"]
            calendar = self.write_calendar(cfg, days)
            rows = self.rows_for_days(days)
            first = self.write_archive(root, "one.zip", rows)
            second_rows = self.rows_for_days(days)
            second_rows.append(
                {
                    "date": "20260908", "code": "2201", "name": "bad",
                    "volume": "1", "open": "10", "high": "8", "low": "9", "close": "10",
                }
            )
            second = self.write_archive(root, "two.zip", second_rows)
            second.with_suffix(second.suffix + ".metadata.json").write_text(
                json.dumps(
                    {"filename": second.name, "sha256": "0" * 64,
                     "source_manifest_hash": "d" * 64}
                ),
                encoding="utf-8",
            )
            cfg.historical_source_coverage_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.historical_source_coverage_path.write_text(
                json.dumps(self.coverage(calendar, days)), encoding="utf-8"
            )
            result = run_historical_preflight(
                archives=[first, second], calendar_path=calendar,
                source_coverage_path=cfg.historical_source_coverage_path,
                through_date="2026-09-08", cfg=cfg,
            )
            self.assertFalse(result.ready)
            self.assertGreater(result.audit["duplicate_code_date_count"], 0)
            self.assertGreater(result.audit["invalid_tradable_rows"], 0)
            self.assertTrue(result.audit["archive_hash_issues"])

    def test_abnormally_low_daily_market_coverage_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = self.temporary_config(root)
            days = [
                "2026-09-01", "2026-09-02", "2026-09-03",
                "2026-09-04", "2026-09-07",
            ]
            calendar = self.write_calendar(cfg, days)
            rows = []
            for day in days[:-1]:
                rows.extend(self.rows_for_days([day], per_day=6))
            rows.extend(self.rows_for_days([days[-1]], per_day=2))
            archive = self.write_archive(root, "coverage.zip", rows)
            coverage = self.coverage(calendar, days, per_day=6)
            coverage["sessions"][-1]["TWSE"]["row_count"] = 1
            coverage["sessions"][-1]["TPEX"]["row_count"] = 1
            coverage["sessions"][-1]["combined_count"] = 2
            cfg.historical_source_coverage_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.historical_source_coverage_path.write_text(
                json.dumps(coverage), encoding="utf-8"
            )
            result = run_historical_preflight(
                archives=[archive], calendar_path=calendar,
                source_coverage_path=cfg.historical_source_coverage_path,
                through_date="2026-09-07", cfg=cfg,
            )
            self.assertFalse(result.ready)
            affected = {
                (row["date"], row["market"])
                for row in result.audit["low_coverage_dates"]
            }
            self.assertIn(("2026-09-07", "TWSE"), affected)
            self.assertIn(("2026-09-07", "TOTAL"), affected)

    def test_direct_official_coverage_extension_preserves_older_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = self.temporary_config(root)
            calendar = self.write_calendar(cfg, ["2026-09-07", "2026-09-08"])
            existing = self.coverage(calendar, ["2026-09-07"])
            cfg.historical_source_coverage_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.historical_source_coverage_path.write_text(json.dumps(existing), encoding="utf-8")
            sources = []
            for market, host in (("twse", "www.twse.com.tw"), ("tpex", "www.tpex.org.tw")):
                raw = root / f"{market}_20260908.raw"
                raw.write_bytes(f"{market} 20260908".encode())
                sources.append(
                    {
                        "source": f"{market}_eod_20260908",
                        "response_date": "20260908",
                        "request_url": f"https://{host}/eod?date=20260908",
                        "retrieved_at_utc": "2026-09-08T06:30:00Z",
                        "sha256": sha256_file(raw),
                        "path": str(raw),
                    }
                )
            merged = extend_source_coverage_from_direct_audit(
                path=cfg.historical_source_coverage_path,
                calendar_path=calendar,
                direct_audit={
                    "failures": [], "unresolved_invalid_rows": 0,
                    "market_daily_counts": {
                        "20260908": {"TWSE": 2, "TPEX": 1, "TOTAL": 3}
                    },
                    "archives": [{"sources": sources}],
                },
            )
            self.assertEqual(
                ["2026-09-07", "2026-09-08"],
                [row["date"] for row in merged["sessions"]],
            )

    def test_preflight_through_date_ignores_later_valid_archive_and_coverage_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = self.temporary_config(root)
            days = ["2026-09-07", "2026-09-08"]
            calendar = self.write_calendar(cfg, days)
            archive = self.write_archive(
                root, "weekly_2026_W37_clean.zip", self.rows_for_days(days)
            )
            cfg.historical_source_coverage_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.historical_source_coverage_path.write_text(
                json.dumps(self.coverage(calendar, days)), encoding="utf-8"
            )
            result = run_historical_preflight(
                archives=[archive],
                calendar_path=calendar,
                source_coverage_path=cfg.historical_source_coverage_path,
                through_date="2026-09-07",
                cfg=cfg,
            )
            self.assertTrue(result.ready)
            self.assertEqual(
                ["2026-09-08"], result.audit["future_out_of_scope_archive_dates"]
            )
            self.assertEqual(
                ["2026-09-08"],
                result.audit["source_coverage"]["future_out_of_scope_session_dates"],
            )


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

    def test_historical_preflight_launchd_runs_once_before_daily_attempts(self):
        path = (
            MODULE_DIR
            / "launchd"
            / "com.linyunyan.warrantscope.shadow-preflight.plist"
        )
        payload = plistlib.loads(path.read_bytes())
        self.assertEqual(
            ["-B", "-m", "shadow_daily_runner.main", "preflight-latest"],
            payload["ProgramArguments"][1:],
        )
        self.assertNotIn("KeepAlive", payload)
        self.assertNotIn("RunAtLoad", payload)
        slots = payload["StartCalendarInterval"]
        self.assertEqual(5, len(slots))
        self.assertEqual(
            [(weekday, 14, 15) for weekday in range(1, 6)],
            sorted(
                (item["Weekday"], item["Hour"], item["Minute"])
                for item in slots
            ),
        )


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
