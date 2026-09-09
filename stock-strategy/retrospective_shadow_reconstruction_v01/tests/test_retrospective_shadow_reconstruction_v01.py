from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile


STOCK_STRATEGY_ROOT = Path(__file__).resolve().parents[2]
if str(STOCK_STRATEGY_ROOT) not in sys.path:
    sys.path.insert(0, str(STOCK_STRATEGY_ROOT))

from multi_setup_study_v01.config import CFG as MULTI_CFG  # noqa: E402
from multi_setup_study_v01.setup_detectors import is_compact_retest  # noqa: E402
from prospective_shadow_v01.config import CFG as PROSPECTIVE_CFG  # noqa: E402
from retrospective_shadow_reconstruction_v01.reconstruct import (  # noqa: E402
    PROSPECTIVE_LEDGER_FILENAMES,
    reconstruct_date,
)


def trading_dates(count: int, start: date = date(2026, 6, 15)) -> list[str]:
    result: list[str] = []
    cursor = start
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return result


def write_calendar(path: Path, dates: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date"])
        writer.writeheader()
        writer.writerows({"date": item} for item in dates)


def write_compact_archive(path: Path, dates: list[str]) -> None:
    closes = [100.0] * len(dates)
    pattern = {
        47: 98.0,
        48: 95.0,
        49: 92.0,
        50: 90.0,
        51: 89.0,
        52: 88.0,
        53: 89.0,
        54: 91.0,
        55: 95.0,
        56: 94.0,
        57: 92.0,
        58: 90.0,
        59: 89.0,
        60: 92.0,
    }
    for index, value in pattern.items():
        closes[index] = value

    fields = ("date", "code", "name", "volume", "open", "high", "low", "close")
    lines = [",".join(fields)]
    for code, name, values in (
        ("1234", "測試股", closes),
        ("0050", "元大台灣50", [100.0] * len(dates)),
    ):
        for index, (day, close) in enumerate(zip(dates, values)):
            open_value = values[index - 1] if index else close
            high = max(open_value, close) + 0.5
            low = min(open_value, close) - 0.5
            if code == "1234" and index == 60:
                open_value, high, low = 89.0, 93.0, 88.0
            lines.append(
                ",".join(
                    str(value)
                    for value in (
                        day,
                        code,
                        name,
                        3_000_000,
                        open_value,
                        high,
                        low,
                        close,
                    )
                )
            )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("fixture.csv", "\n".join(lines) + "\n")


def seed_ledgers(store: Path) -> dict[str, str]:
    store.mkdir(parents=True)
    result = {}
    for filename in PROSPECTIVE_LEDGER_FILENAMES:
        payload = f"do-not-modify-{filename}\n".encode("utf-8")
        path = store / filename
        path.write_bytes(payload)
        result[filename] = hashlib.sha256(payload).hexdigest()
    return result


def ledger_hashes(store: Path) -> dict[str, str]:
    return {
        filename: hashlib.sha256((store / filename).read_bytes()).hexdigest()
        for filename in PROSPECTIVE_LEDGER_FILENAMES
    }


class RetrospectiveReconstructionTests(unittest.TestCase):
    def test_reconstruction_is_causal_labelled_immutable_and_ledger_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dates = trading_dates(75)
            archive = root / "weekly_fixture.zip"
            calendar = root / "trading_calendar.csv"
            output = root / "retrospective"
            store = root / "prospective_store"
            write_compact_archive(archive, dates)
            write_calendar(calendar, dates)
            original = seed_ledgers(store)

            first = reconstruct_date(
                "2026-09-07",
                [archive],
                calendar,
                output_dir=output,
                prospective_store_dir=store,
                now=datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc),
            )
            self.assertEqual("CREATED_IMMUTABLE_RETROSPECTIVE_OUTPUT", first["status"])
            self.assertEqual(original, ledger_hashes(store))
            self.assertTrue(first["prospective_ledgers_unchanged"])
            self.assertEqual(1, first["counts"]["accepted_n_retest"])
            self.assertEqual(1, first["counts"]["n_compact_retest"])

            run_dir = Path(first["run_dir"])
            with (run_dir / "signals.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(1, len(rows))
            self.assertEqual(
                "RETROSPECTIVE_RECONSTRUCTION_2026-09-07",
                rows[0]["reconstruction_label"],
            )
            self.assertEqual(
                "RETROSPECTIVE_RECONSTRUCTION_NOT_PROSPECTIVE",
                rows[0]["sample_classification"],
            )
            self.assertEqual("false", rows[0]["is_prospective_sample"])
            self.assertEqual(PROSPECTIVE_CFG.compact_rule, rows[0]["compact_rule"])

            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            without_hash = {
                key: value
                for key, value in manifest.items()
                if key != "manifest_payload_sha256"
            }
            payload = json.dumps(
                without_hash,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            self.assertEqual(
                hashlib.sha256(payload).hexdigest(),
                manifest["manifest_payload_sha256"],
            )
            self.assertEqual(original, manifest["prospective_ledger_hashes_before"])
            self.assertEqual(original, manifest["prospective_ledger_hashes_after"])
            self.assertEqual("20260907", manifest["provider_data_through_date"])

            second = reconstruct_date(
                "20260907",
                [archive],
                calendar,
                output_dir=output,
                prospective_store_dir=store,
                now=datetime(2026, 9, 8, 13, 0, tzinfo=timezone.utc),
            )
            self.assertEqual("VERIFIED_EXISTING_IMMUTABLE_OUTPUT", second["status"])
            self.assertEqual(first["run_dir"], second["run_dir"])
            self.assertEqual(original, ledger_hashes(store))

    def test_only_explicitly_sanctioned_dates_are_allowed(self):
        with self.assertRaisesRegex(RuntimeError, "only the explicitly sanctioned"):
            reconstruct_date(
                "2026-09-09",
                [],
                Path("unused-calendar.csv"),
            )

    def test_output_may_not_be_inside_prospective_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / "prospective"
            archive = root / "fixture.zip"
            archive.write_bytes(b"fixture")
            with self.assertRaisesRegex(RuntimeError, "outside the prospective store"):
                reconstruct_date(
                    "20260907",
                    [archive],
                    root / "calendar.csv",
                    output_dir=store / "retrospective",
                    prospective_store_dir=store,
                )

    def test_frozen_compact_boundary_remains_shared(self):
        cases = (
            ({"pivot_separation_sessions": 7, "bottom_difference": 1e-12}, True),
            ({"pivot_separation_sessions": 8, "bottom_difference": 1e-12}, False),
            ({"pivot_separation_sessions": 7, "bottom_difference": 0.0}, False),
        )
        for geometry, expected in cases:
            with self.subTest(geometry=geometry):
                self.assertIs(is_compact_retest(geometry, MULTI_CFG), expected)

    def test_implementation_has_no_prospective_store_or_service_mutator(self):
        source = (
            STOCK_STRATEGY_ROOT
            / "retrospective_shadow_reconstruction_v01"
            / "reconstruct.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "ShadowStore",
            "append_scan",
            "append_outcomes",
            "record_run",
            "run_daily(",
            "update_outcomes(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
