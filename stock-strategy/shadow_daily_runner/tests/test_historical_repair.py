from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from shadow_daily_runner.calendar import (
    build_trading_calendar,
    collect_ad_hoc_closures,
    discover_ad_hoc_closures,
    read_sessions,
)
from shadow_daily_runner.config import CFG
from shadow_daily_runner.historical_repair import repair_history
from shadow_daily_runner.io_utils import sha256_file
from shadow_daily_runner.normalize import deterministic_zip, parse_release_archive
from shadow_daily_runner.pipeline import prepare_inputs
from shadow_daily_runner.preflight import HistoricalHealthResult, run_historical_preflight
from shadow_daily_runner.sources import SourceSnapshot


def source_snapshot(path: Path, payload: bytes, source: str, request_url: str) -> SourceSnapshot:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return SourceSnapshot(
        source=source,
        request_url=request_url,
        retrieved_at_utc="2026-09-08T10:12:06Z",
        sha256=hashlib.sha256(payload).hexdigest(),
        path=path,
        payload=payload,
    )


def calendar_payload() -> bytes:
    return json.dumps(
        [{"Name": "中華民國開國紀念日", "Date": "1150101", "Description": "放假"}],
        ensure_ascii=False,
    ).encode("utf-8")


def news_payload() -> bytes:
    return json.dumps(
        [
            {
                "Date": "1150710",
                "Title": "臺灣證券交易所集中交易市場115年7月10日休市一天",
                "Url": (
                    "https://www.twse.com.tw/zh/about/news/news/content.html?"
                    "8a8216d69ef76943019f46cb86bf0111"
                ),
            }
        ],
        ensure_ascii=False,
    ).encode("utf-8")


def twse_payload(day: str) -> bytes:
    fields = [
        "證券代號",
        "證券名稱",
        "成交股數",
        "成交筆數",
        "成交金額",
        "開盤價",
        "最高價",
        "最低價",
        "收盤價",
    ]
    return json.dumps(
        {
            "stat": "OK",
            "date": day,
            "tables": [
                {
                    "title": "每日收盤行情",
                    "fields": fields,
                    "data": [
                        ["0050", "元大台灣50", "1,000", "10", "100", "66", "67", "65", "66.5"]
                    ],
                }
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")


def tpex_payload(day: str) -> bytes:
    roc = int(day[:4]) - 1911
    text = "\n".join(
        [
            "上櫃股票每日收盤行情(不含定價)",
            "產業類別:所有證券",
            f"資料日期:{roc:03d}/{day[4:6]}/{day[6:]}",
            "代號,名稱,收盤 ,漲跌,開盤 ,最高 ,最低,成交股數  ,成交金額(元),成交筆數",
            '"6488","環球晶","500","+1","495","505","490","1,000","500,000","100"',
        ]
    )
    return text.encode("big5")


def clean_row(day: str, code: str) -> dict[str, str]:
    if code == "0050":
        return {
            "date": day,
            "code": code,
            "name": "元大台灣50",
            "volume": "1000",
            "open": "66",
            "high": "67",
            "low": "65",
            "close": "66.5",
        }
    return {
        "date": day,
        "code": code,
        "name": "環球晶",
        "volume": "1000",
        "open": "495",
        "high": "505",
        "low": "490",
        "close": "500",
    }


def write_clean_archive(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = deterministic_zip(f"{path.stem}.csv", rows)
    path.write_bytes(payload)
    path.with_suffix(path.suffix + ".metadata.json").write_text(
        json.dumps(
            {
                "filename": path.name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "source_manifest_hash": "d" * 64,
            }
        ),
        encoding="utf-8",
    )


class FixtureOfficialClient:
    def __init__(self, root: Path) -> None:
        self.release_calls = 0
        self.twse_calls = 0
        self.tpex_calls = 0
        self._calendar = source_snapshot(
            root / "calendar.json",
            calendar_payload(),
            "twse_calendar",
            "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule",
        )
        self._news = source_snapshot(
            root / "news.json",
            news_payload(),
            "twse_news",
            "https://openapi.twse.com.tw/v1/news/newsList",
        )
        self._twse = {
            day: source_snapshot(
                root / f"twse_{day}.json",
                twse_payload(day),
                f"twse_eod_{day}",
                f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={day}",
            )
            for day in ("20260907", "20260908")
        }
        self._tpex = {
            day: source_snapshot(
                root / f"tpex_{day}.csv",
                tpex_payload(day),
                f"tpex_eod_{day}",
                f"https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={day}",
            )
            for day in ("20260907", "20260908")
        }

    def calendar(self) -> SourceSnapshot:
        return self._calendar

    def twse_news(self, *, refresh: bool = True) -> SourceSnapshot:
        del refresh
        return self._news

    def release_week(self, year: int, week: int) -> SourceSnapshot:
        del year, week
        self.release_calls += 1
        raise AssertionError("historical release must not be downloaded in this fixture")

    def twse_eod(self, day: str, *, refresh: bool = True) -> SourceSnapshot:
        del refresh
        self.twse_calls += 1
        return self._twse[day]

    def tpex_eod(self, day: str, *, refresh: bool = True) -> SourceSnapshot:
        del refresh
        self.tpex_calls += 1
        return self._tpex[day]


class HistoricalRepairTests(unittest.TestCase):
    def temporary_config(self, root: Path):
        return replace(
            CFG,
            runtime_dir=root / "runtime",
            shadow_store_dir=root / "prospective",
            data_start="20260907",
            historical_release_last_week=37,
            historical_release_missing_weeks=(),
        )

    def seed_ledgers(self, cfg) -> dict[str, bytes]:
        cfg.shadow_store_dir.mkdir(parents=True, exist_ok=True)
        result = {}
        for name in (
            "prospective_signals.csv",
            "prospective_outcomes.csv",
            "prospective_scan_log.csv",
            "shadow_status.json",
        ):
            payload = f"immutable:{name}\n".encode("utf-8")
            (cfg.shadow_store_dir / name).write_bytes(payload)
            result[name] = payload
        return result

    def test_official_ad_hoc_closure_is_exact_and_carries_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calendar_source = source_snapshot(
                root / "calendar.json",
                calendar_payload(),
                "twse_calendar",
                "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule",
            )
            news_source = source_snapshot(
                root / "news.json",
                news_payload(),
                "twse_news",
                "https://openapi.twse.com.tw/v1/news/newsList",
            )
            closures = discover_ad_hoc_closures(news_source, 2026)
            self.assertEqual(["2026-07-10"], [item["date"] for item in closures])
            self.assertEqual(news_source.sha256, closures[0]["source_sha256"])
            self.assertEqual(str(news_source.path), closures[0]["source_path"])
            self.assertEqual(64, len(closures[0]["record_sha256"]))

            calendar, metadata = build_trading_calendar(
                calendar_source, 2026, ad_hoc_closures=closures
            )
            sessions = read_sessions(calendar)
            self.assertNotIn("2026-07-10", sessions)
            self.assertIn("2026-07-09", sessions)
            self.assertIn("2026-07-13", sessions)
            self.assertEqual(closures, metadata["official_ad_hoc_closure_rows"])

            # The latest news endpoint may later roll this item out.  Previously
            # verified immutable evidence must remain effective rather than
            # silently turning a closed day back into a trading session.
            previous = root / "trading_calendar.metadata.json"
            previous.write_text(
                json.dumps({"official_ad_hoc_closure_rows": closures}),
                encoding="utf-8",
            )
            empty_news = source_snapshot(
                root / "news-later.json",
                b"[]",
                "twse_news",
                "https://openapi.twse.com.tw/v1/news/newsList",
            )
            retained = collect_ad_hoc_closures(
                empty_news,
                2026,
                previous_calendar_metadata_path=previous,
            )
            self.assertEqual(closures, retained)

    def test_repair_is_additive_and_does_not_mutate_prospective_ledgers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = self.temporary_config(root)
            base = cfg.clean_dir / "weekly_2026_W37_clean_fixture.zip"
            write_clean_archive(
                base,
                [
                    clean_row("20260907", "0050"),
                    clean_row("20260907", "6488"),
                    clean_row("20260908", "0050"),
                ],
            )
            base_before = base.read_bytes()
            base_hash = sha256_file(base)
            ledgers = self.seed_ledgers(cfg)
            client = FixtureOfficialClient(root / "official")

            result = repair_history("2026-09-08", cfg=cfg, client=client)

            self.assertEqual("PASS", result.audit["status"])
            self.assertEqual(base_before, base.read_bytes())
            self.assertEqual(base_hash, sha256_file(base))
            self.assertEqual(1, len(result.patch_archives))
            patch_archive = result.patch_archives[0]
            parsed = parse_release_archive(
                source_snapshot(
                    root / "patch-copy.zip",
                    patch_archive.read_bytes(),
                    "patch",
                    "LOCAL_IMMUTABLE_ARCHIVE",
                )
            )
            self.assertEqual(
                [("20260908", "6488")],
                [(row["date"], row["code"]) for row in parsed.rows],
            )
            patch_metadata = json.loads(
                patch_archive.with_suffix(patch_archive.suffix + ".metadata.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(base_hash, patch_metadata["original_archive"]["sha256"])
            self.assertFalse(patch_metadata["overwrites_existing_code_date"])
            for name, payload in ledgers.items():
                self.assertEqual(payload, (cfg.shadow_store_dir / name).read_bytes())
            self.assertTrue(result.audit["checks"]["prospective_ledgers_unchanged"])
            self.assertEqual(0, result.audit["actual_orders"])
            self.assertEqual(0, result.audit["actual_fills"])
            self.assertEqual(0, result.audit["broker_connections"])

            # The canonical preflight must fail closed if the immutable news
            # snapshot evidencing the emergency closure later drifts.
            client._news.path.write_bytes(b"drift")
            health = run_historical_preflight(
                archives=[*result.base_archives, *result.patch_archives],
                calendar_path=result.calendar_path,
                source_coverage_path=result.coverage_path,
                through_date="2026-09-08",
                cfg=cfg,
            )
            self.assertFalse(health.ready)
            self.assertFalse(health.audit["checks"]["calendar_metadata_valid"])
            self.assertTrue(
                any("ad-hoc closure" in issue for issue in health.audit["calendar"]["issues"])
            )

    def test_early_historical_preflight_fails_before_any_eod_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = self.temporary_config(root)
            client = FixtureOfficialClient(root / "official")
            failed = HistoricalHealthResult(
                ready=False,
                audit_path=root / "early-preflight.json",
                audit={
                    "status": "FAIL_CLOSED",
                    "checks": {"calendar_sessions_complete": False},
                },
            )
            with (
                patch(
                    "shadow_daily_runner.pipeline.load_previous_archives_for_preflight",
                    return_value=[root / "unused.zip"],
                ),
                patch(
                    "shadow_daily_runner.pipeline.run_historical_preflight",
                    return_value=failed,
                ),
                patch("shadow_daily_runner.pipeline._prepare_history") as prepare_history,
                patch("shadow_daily_runner.pipeline._prepare_direct_official") as prepare_direct,
            ):
                result = prepare_inputs(
                    now=datetime(
                        2026, 9, 9, 14, 30, tzinfo=ZoneInfo("Asia/Taipei")
                    ),
                    cfg=cfg,
                    client=client,
                )
            self.assertFalse(result.ready)
            self.assertEqual("READINESS_FAILED", result.audit["status"])
            self.assertIn("before target download failed closed", result.audit["failure_reason"])
            self.assertEqual(0, client.release_calls)
            self.assertEqual(0, client.twse_calls)
            self.assertEqual(0, client.tpex_calls)
            prepare_history.assert_not_called()
            prepare_direct.assert_not_called()


if __name__ == "__main__":
    unittest.main()
