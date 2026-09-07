from __future__ import annotations

import ast
import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


# ``stock-strategy`` is deliberately not a package because its directory name
# contains a dash.  Keep this test runnable both directly and via discovery.
STOCK_STRATEGY_ROOT = Path(__file__).resolve().parents[2]
if str(STOCK_STRATEGY_ROOT) not in sys.path:
    sys.path.insert(0, str(STOCK_STRATEGY_ROOT))

from multi_setup_study_v01.config import CFG as MULTI_CFG  # noqa: E402
from multi_setup_study_v01.setup_detectors import (  # noqa: E402
    is_compact_retest as shared_is_compact_retest,
)
from prospective_shadow_v01.config import CFG  # noqa: E402
from prospective_shadow_v01.detector import (  # noqa: E402
    ScanResult,
    assert_frozen_contract,
    scan_snapshot,
)
from prospective_shadow_v01.market_data_provider import (  # noqa: E402
    ExistingDailyDataProvider,
)
from prospective_shadow_v01.outcomes import (  # noqa: E402
    build_outcome_candidates,
)
from prospective_shadow_v01.service import (  # noqa: E402
    update_outcomes_from_snapshot,
)
from prospective_shadow_v01.storage import (  # noqa: E402
    ImmutableSignalConflict,
    OutcomeRevisionConflict,
    ShadowStore,
)
from surge_event_study_v01.models import Bar  # noqa: E402


def trading_dates(count: int, start: date = date(2026, 6, 15)) -> list[str]:
    """Index 60 is 2026-09-07, the first prospective session."""

    result: list[str] = []
    cursor = start
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return result


def make_bar(
    day: str,
    code: str,
    close: float,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: int = 3_000_000,
) -> Bar:
    open_value = close if open_ is None else open_
    return Bar(
        day,
        code,
        "測試股" if code != "0050" else "元大台灣50",
        volume,
        open_value,
        max(open_value, close) + 0.5 if high is None else high,
        min(open_value, close) - 0.5 if low is None else low,
        close,
    )


def compact_rows(*, future_close: float = 100.0) -> list[Bar]:
    dates = trading_dates(75)
    closes = [100.0] * len(dates)
    # Two seven-session-apart local lows, a higher second low, a >=6% bounce,
    # and a T confirmation.  This is an N_RETEST before compact annotation.
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
    for index in range(61, len(closes)):
        closes[index] = future_close

    rows: list[Bar] = []
    for index, (day, close) in enumerate(zip(dates, closes)):
        open_value = closes[index - 1] if index else close
        values = {}
        if index == 60:
            values = {"open_": 89.0, "high": 93.0, "low": 88.0}
        elif index >= 61:
            values = {
                "open_": future_close,
                "high": future_close + 1.0,
                "low": future_close - 1.0,
            }
        else:
            values = {"open_": open_value}
        rows.append(make_bar(day, "1234", close, **values))
    return rows


def write_archive(path: Path, stock_rows: list[Bar]) -> None:
    benchmark = [
        make_bar(row.date, "0050", 100.0, open_=100.0)
        for row in stock_rows
    ]
    fields = ("date", "code", "name", "volume", "open", "high", "low", "close")
    lines = [",".join(fields)]
    for row in [*stock_rows, *benchmark]:
        lines.append(
            ",".join(
                str(value)
                for value in (
                    row.date,
                    row.code,
                    row.name,
                    row.volume,
                    row.open,
                    row.high,
                    row.low,
                    row.close,
                )
            )
        )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("synthetic.csv", "\n".join(lines) + "\n")


def write_calendar(path: Path, dates: list[str] | None = None) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date"])
        writer.writeheader()
        writer.writerows({"date": day} for day in (dates or trading_dates(75)))


class FrozenDetectorTests(unittest.TestCase):
    def test_provider_physically_hides_mutated_t_plus_1_and_later(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ordinary = root / "ordinary.zip"
            mutated = root / "mutated.zip"
            calendar = root / "trading_calendar.csv"
            write_archive(ordinary, compact_rows(future_close=100.0))
            write_archive(mutated, compact_rows(future_close=240.0))
            write_calendar(calendar)

            first_snapshot = ExistingDailyDataProvider(
                [ordinary], trading_calendar_path=calendar
            ).load_through("20260907")
            second_snapshot = ExistingDailyDataProvider(
                [mutated], trading_calendar_path=calendar
            ).load_through("20260907")
            first = scan_snapshot(first_snapshot, "20260907")
            second = scan_snapshot(second_snapshot, "20260907")

            self.assertEqual(first.compact_count, 1)
            self.assertEqual(second.compact_count, 1)
            self.assertEqual(first_snapshot.benchmark.calendar[-1], "20260907")
            self.assertEqual(second_snapshot.benchmark.calendar[-1], "20260907")
            self.assertEqual(
                first_snapshot.prepared_stocks[0].bars[-1].date, "20260907"
            )
            self.assertEqual(
                second_snapshot.prepared_stocks[0].bars[-1].date, "20260907"
            )

            # The archive manifest legitimately differs, but no T feature or
            # frozen setup geometry may depend on the unseen future payload.
            provenance = {
                "input_manifest_hash",
                "provider_name",
            }
            causal_first = {
                key: value
                for key, value in first.signals[0].items()
                if key not in provenance
            }
            causal_second = {
                key: value
                for key, value in second.signals[0].items()
                if key not in provenance
            }
            self.assertEqual(causal_first, causal_second)

    def test_compact_boundary_is_the_shared_multi_setup_predicate(self):
        cases = (
            ({"pivot_separation_sessions": 7, "bottom_difference": 1e-12}, True),
            ({"pivot_separation_sessions": 8, "bottom_difference": 1e-12}, False),
            ({"pivot_separation_sessions": 7, "bottom_difference": 0.0}, False),
            ({"pivot_separation_sessions": 6, "bottom_difference": -1e-12}, False),
        )
        for geometry, expected in cases:
            with self.subTest(geometry=geometry):
                self.assertIs(
                    shared_is_compact_retest(geometry, MULTI_CFG), expected
                )
        self.assertEqual(
            CFG.compact_rule,
            "pivot_separation_sessions <= 7 and bottom_difference > 0",
        )
        assert_frozen_contract()

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "compact.zip"
            calendar = Path(directory) / "trading_calendar.csv"
            write_archive(archive, compact_rows())
            write_calendar(calendar)
            snapshot = ExistingDailyDataProvider(
                [archive], trading_calendar_path=calendar
            ).load_through("20260907")
            with patch(
                "prospective_shadow_v01.detector.is_compact_retest",
                wraps=shared_is_compact_retest,
            ) as classifier:
                result = scan_snapshot(snapshot, "20260907")
            self.assertEqual(result.compact_count, 1)
            classifier.assert_called()
            geometry = classifier.call_args.args[0]
            self.assertEqual(geometry["pivot_separation_sessions"], 7)
            self.assertGreater(geometry["bottom_difference"], 0.0)


class AppendOnlyStorageTests(unittest.TestCase):
    @staticmethod
    def _fixture(root: Path):
        archive = root / "synthetic.zip"
        calendar = root / "trading_calendar.csv"
        write_archive(archive, compact_rows())
        write_calendar(calendar)
        provider = ExistingDailyDataProvider(
            [archive], trading_calendar_path=calendar
        )
        snapshot = provider.load_through("20260907")
        result = scan_snapshot(snapshot, "20260907")
        store = ShadowStore(root / "shadow")
        store.initialize()
        return archive, provider, snapshot, result, store

    def test_exact_rerun_is_noop_with_identical_ledger_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, snapshot, result, store = self._fixture(Path(directory))
            first = store.append_scan(
                result,
                provider_name=snapshot.provider_name,
                input_manifest_hash=snapshot.input_manifest_hash,
                now=datetime(2026, 9, 7, 6, 0, tzinfo=timezone.utc),
            )
            signal_bytes = store.signals_path.read_bytes()
            scan_bytes = store.scans_path.read_bytes()

            second = store.append_scan(
                result,
                provider_name=snapshot.provider_name,
                input_manifest_hash=snapshot.input_manifest_hash,
                # A later wall-clock time may update the reconstructible status
                # cache, but it must never append or rewrite either ledger.
                now=datetime(2026, 9, 7, 7, 0, tzinfo=timezone.utc),
            )

            self.assertEqual(first["status"], "APPENDED")
            self.assertEqual(first["appended_signals"], 1)
            self.assertEqual(second["status"], "EXACT_RERUN_NO_OP")
            self.assertEqual(second["appended_signals"], 0)
            self.assertEqual(len(store.read_signals()), 1)
            self.assertEqual(len(store.read_scans()), 1)
            self.assertEqual(store.signals_path.read_bytes(), signal_bytes)
            self.assertEqual(store.scans_path.read_bytes(), scan_bytes)

    def test_changed_rerun_conflict_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, snapshot, result, store = self._fixture(Path(directory))
            store.append_scan(
                result,
                provider_name=snapshot.provider_name,
                input_manifest_hash=snapshot.input_manifest_hash,
            )
            before_signals = store.signals_path.read_bytes()
            before_scans = store.scans_path.read_bytes()
            changed = dict(result.signals[0])
            changed["signal_close"] = float(changed["signal_close"]) + 0.01
            conflicting = ScanResult(
                signal_date=result.signal_date,
                signals=(changed,),
                raw_n_retest_count=result.raw_n_retest_count,
                accepted_n_retest_count=result.accepted_n_retest_count,
                compact_count=result.compact_count,
                stocks_with_target_bar=result.stocks_with_target_bar,
            )

            with self.assertRaises(ImmutableSignalConflict):
                store.append_scan(
                    conflicting,
                    provider_name=snapshot.provider_name,
                    input_manifest_hash=snapshot.input_manifest_hash,
                )

            self.assertEqual(store.signals_path.read_bytes(), before_signals)
            self.assertEqual(store.scans_path.read_bytes(), before_scans)
            self.assertEqual(len(store.read_signals()), 1)
            self.assertEqual(len(store.read_scans()), 1)

    def test_outcome_progress_is_append_only_and_never_modifies_signal_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            _, provider, snapshot, result, store = self._fixture(Path(directory))
            store.append_scan(
                result,
                provider_name=snapshot.provider_name,
                input_manifest_hash=snapshot.input_manifest_hash,
            )
            signals_before = store.signals_path.read_bytes()
            stored_signals = store.read_signals()
            dates = trading_dates(75)

            d1_snapshot = provider.load_through(dates[61])
            d1 = build_outcome_candidates(
                d1_snapshot, stored_signals, dates[61]
            )
            appended_d1 = store.append_outcomes(d1)
            self.assertEqual(appended_d1["appended_outcomes"], 1)
            self.assertEqual(d1[0]["available_forward_sessions"], 1)
            self.assertIsNotNone(d1[0]["entry_open_proxy"])
            self.assertIsNotNone(d1[0]["day1_close_return"])
            self.assertIsNone(d1[0]["day3_close_return"])
            self.assertIsNone(d1[0]["mfe_close_5"])
            self.assertEqual(store.signals_path.read_bytes(), signals_before)

            d5_snapshot = provider.load_through(dates[65])
            d5 = build_outcome_candidates(
                d5_snapshot, stored_signals, dates[65]
            )
            appended_d5 = store.append_outcomes(d5)
            self.assertEqual(appended_d5["appended_outcomes"], 1)
            self.assertEqual(d5[0]["available_forward_sessions"], 5)
            self.assertIsNotNone(d5[0]["day3_close_return"])
            self.assertIsNotNone(d5[0]["day5_close_return"])
            self.assertIsNotNone(d5[0]["mfe_close_5"])
            self.assertIsNone(d5[0]["day10_close_return"])
            self.assertEqual(store.signals_path.read_bytes(), signals_before)

            # Repeating an identical state appends no duplicate revision.
            outcome_bytes = store.outcomes_path.read_bytes()
            no_op = store.append_outcomes(d5)
            self.assertEqual(no_op["status"], "NO_OP")
            self.assertEqual(no_op["appended_outcomes"], 0)
            self.assertEqual(store.outcomes_path.read_bytes(), outcome_bytes)
            self.assertEqual(store.signals_path.read_bytes(), signals_before)

            d10_snapshot = provider.load_through(dates[70])
            d10 = build_outcome_candidates(
                d10_snapshot, stored_signals, dates[70]
            )
            store.append_outcomes(d10)
            revisions = store.read_outcomes()
            self.assertEqual([row["revision"] for row in revisions], ["1", "2", "3"])
            self.assertEqual(revisions[-1]["outcome_status"], "COMPLETE")
            self.assertEqual(revisions[-1]["forward_window_complete"], "true")
            self.assertEqual(revisions[-1]["is_final"], "true")
            self.assertEqual(store.signals_path.read_bytes(), signals_before)

    def test_outcome_cannot_rewrite_an_already_observed_value(self):
        with tempfile.TemporaryDirectory() as directory:
            _, provider, snapshot, result, store = self._fixture(Path(directory))
            store.append_scan(
                result,
                provider_name=snapshot.provider_name,
                input_manifest_hash=snapshot.input_manifest_hash,
            )
            signals = store.read_signals()
            dates = trading_dates(75)
            d1 = build_outcome_candidates(
                provider.load_through(dates[61]), signals, dates[61]
            )
            store.append_outcomes(d1)
            before = store.outcomes_path.read_bytes()
            corrupt = dict(d1[0])
            corrupt["entry_gap"] = float(corrupt["entry_gap"]) + 0.01

            with self.assertRaises(OutcomeRevisionConflict):
                store.append_outcomes([corrupt])

            self.assertEqual(store.outcomes_path.read_bytes(), before)
            self.assertEqual(len(store.read_outcomes()), 1)

    def test_service_outcome_updater_keeps_signal_ledger_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            _, provider, snapshot, result, store = self._fixture(Path(directory))
            store.append_scan(
                result,
                provider_name=snapshot.provider_name,
                input_manifest_hash=snapshot.input_manifest_hash,
            )
            before = store.signals_path.read_bytes()
            dates = trading_dates(75)

            updated = update_outcomes_from_snapshot(
                store,
                provider.load_through(dates[65]),
                dates[65],
                now=datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc),
            )

            self.assertEqual(updated["appended_outcomes"], 1)
            self.assertEqual(store.signals_path.read_bytes(), before)
            self.assertEqual(len(store.read_signals()), 1)
            self.assertEqual(len(store.read_outcomes()), 1)


class SafetySurfaceTests(unittest.TestCase):
    def test_production_package_has_no_broker_network_or_credentials_imports(self):
        package = STOCK_STRATEGY_ROOT / "prospective_shadow_v01"
        forbidden_roots = {
            "requests",
            "httpx",
            "urllib",
            "socket",
            "websocket",
            "aiohttp",
            "grpc",
            "yuanta",
            "fugle",
            "shioaji",
        }
        forbidden_identifiers = {
            "password",
            "passwd",
            "credential",
            "certificate",
            "private_key",
            "place_order",
            "submit_order",
            "send_order",
        }
        for source_path in sorted(package.glob("*.py")):
            source = source_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(source_path))
            imported_roots: set[str] = set()
            identifiers: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_roots.update(
                        alias.name.split(".", 1)[0].lower() for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_roots.add(node.module.split(".", 1)[0].lower())
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    identifiers.add(node.name.lower())
                elif isinstance(node, ast.Name):
                    identifiers.add(node.id.lower())
            self.assertFalse(
                imported_roots & forbidden_roots,
                f"forbidden import in {source_path.name}",
            )
            self.assertFalse(
                identifiers & forbidden_identifiers,
                f"forbidden credential/order surface in {source_path.name}",
            )


if __name__ == "__main__":
    unittest.main()
