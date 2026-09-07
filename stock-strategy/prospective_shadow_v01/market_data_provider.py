from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
import csv
import hashlib
import json
from pathlib import Path
from typing import Protocol

from multi_setup_study_v01.config import CFG as MULTI_CFG
from surge_event_study_v01.data import (
    load_ohlcv,
    prepare_stocks,
    sha256_file,
)
from surge_event_study_v01.models import PreparedBenchmark, PreparedStock


@dataclass(frozen=True, slots=True)
class InputFile:
    order: int
    kind: str
    name: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class MarketDataSnapshot:
    """Local daily data, physically truncated at ``data_through_date``."""

    provider_name: str
    data_through_date: str
    prepared_stocks: list[PreparedStock]
    benchmark: PreparedBenchmark
    input_manifest: tuple[InputFile, ...]
    input_manifest_hash: str
    audit: dict


class MarketDataProvider(Protocol):
    """Future providers, including Yuanta, must implement this boundary."""

    def load_through(self, data_through_date: str) -> MarketDataSnapshot: ...


def normalize_date(value: str) -> str:
    result = str(value).strip().replace("-", "")
    if len(result) != 8 or not result.isdigit():
        raise ValueError(f"invalid YYYYMMDD date: {value!r}")
    try:
        datetime.strptime(result, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"invalid calendar date: {value!r}") from exc
    return result


class ExistingDailyDataProvider:
    """Read only the repository's existing local ZIP/CSV daily-data format.

    This provider contains no downloader, network client, broker SDK, account
    access, or order method.
    """

    name = "EXISTING_LOCAL_DAILY_OHLCV"

    def __init__(
        self,
        archive_paths: list[Path],
        supplement_paths: list[Path] | None = None,
        trading_calendar_path: Path | None = None,
    ) -> None:
        if not archive_paths:
            raise ValueError("at least one local OHLCV archive is required")
        self.archive_paths = tuple(Path(path) for path in archive_paths)
        self.supplement_paths = tuple(
            Path(path) for path in (supplement_paths or [])
        )
        if trading_calendar_path is None:
            raise ValueError("an independent point-in-time trading calendar is required")
        self.trading_calendar_path = Path(trading_calendar_path)

    def _manifest(self) -> tuple[InputFile, ...]:
        rows: list[InputFile] = []
        order = 0
        for kind, paths in (
            ("archive", self.archive_paths),
            ("supplement", self.supplement_paths),
            ("trading_calendar", (self.trading_calendar_path,)),
        ):
            for path in paths:
                if not path.is_file():
                    raise FileNotFoundError(path)
                rows.append(
                    InputFile(
                        order=order,
                        kind=kind,
                        name=path.name,
                        bytes=path.stat().st_size,
                        sha256=sha256_file(path),
                    )
                )
                order += 1
        return tuple(rows)

    def _trading_sessions(self, through: str) -> list[str]:
        with self.trading_calendar_path.open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "date" not in reader.fieldnames:
                raise ValueError("trading calendar must contain a date column")
            sessions = [normalize_date(row["date"]) for row in reader]
        if sessions != sorted(set(sessions)):
            raise ValueError("trading calendar dates must be unique and ascending")
        return [day for day in sessions if day <= through]

    def load_through(self, data_through_date: str) -> MarketDataSnapshot:
        through = normalize_date(data_through_date)
        manifest_before = self._manifest()
        expected_sessions = self._trading_sessions(through)
        if not expected_sessions or expected_sessions[-1] != through:
            raise RuntimeError("requested date is not in the independent trading calendar")
        # load_ohlcv rejects every later row before PreparedStock is built.
        # No downstream signal code can observe T+1 or later data.
        loader_cfg = replace(
            MULTI_CFG,
            maximum_input_date=through,
            feature_oos_end=through,
        )
        stocks, benchmark_rows, load_audit = load_ohlcv(
            list(self.archive_paths),
            supplement_paths=list(self.supplement_paths),
            cfg=loader_cfg,
        )
        prepared, benchmark, prepare_audit = prepare_stocks(
            stocks, benchmark_rows, loader_cfg
        )
        manifest_after = self._manifest()
        if manifest_before != manifest_after:
            raise RuntimeError(
                "market-data inputs changed while they were being loaded"
            )
        if not benchmark.calendar:
            raise RuntimeError("prepared market calendar is empty")
        if benchmark.calendar[-1] > through:
            raise RuntimeError("provider leaked data beyond data_through_date")
        encoded = json.dumps(
            [asdict(item) for item in manifest_before],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest_hash = hashlib.sha256(encoded).hexdigest()
        audit = {
            **load_audit,
            **prepare_audit,
            "provider": self.name,
            "data_through_date": through,
            "input_manifest_hash": manifest_hash,
        }
        if audit.get("skipped_archives"):
            raise RuntimeError("one or more OHLCV archives could not be read")
        if int(audit.get("invalid_rows", 0)):
            raise RuntimeError(
                "invalid OHLCV rows detected; prospective load fails closed"
            )
        if int(audit.get("duplicate_rows_overridden", 0)):
            raise RuntimeError(
                "duplicate code-date rows detected; prospective load fails closed"
            )
        if audit.get("broad_source_gap_dates"):
            raise RuntimeError(
                "broad market-data gaps detected; prospective scan fails closed"
            )
        actual_sessions = set(benchmark.calendar)
        if expected_sessions[0] > benchmark.calendar[0]:
            raise RuntimeError(
                "independent trading calendar does not cover the loaded history"
            )
        required_sessions = {
            day
            for day in expected_sessions
            if day >= max(loader_cfg.warmup_start, benchmark.calendar[0])
        }
        missing_sessions = sorted(required_sessions - actual_sessions)
        if missing_sessions:
            raise RuntimeError(
                f"market data omit {len(missing_sessions)} calendar sessions; "
                f"first={missing_sessions[0]}"
            )
        if not benchmark.calendar or benchmark.calendar[-1] != through:
            raise RuntimeError("target session data are absent or incomplete")
        recent_sessions = set(benchmark.calendar[-21:])
        counts = {day: 0 for day in recent_sessions}
        for stock in prepared:
            for day in {bar.date for bar in stock.bars if bar.date in recent_sessions}:
                counts[day] += 1
        prior_counts = [counts[day] for day in benchmark.calendar[-21:-1]]
        if prior_counts:
            ordered = sorted(prior_counts)
            middle = len(ordered) // 2
            median = (
                ordered[middle]
                if len(ordered) % 2
                else (ordered[middle - 1] + ordered[middle]) / 2
            )
            if median and counts[through] < median * 0.70:
                raise RuntimeError(
                    "target-session stock coverage is below 70% of prior median"
                )
        audit["independent_calendar_path"] = self.trading_calendar_path.name
        audit["independent_calendar_last_session"] = expected_sessions[-1]
        audit["target_session_prepared_stock_rows"] = counts[through]
        return MarketDataSnapshot(
            provider_name=self.name,
            data_through_date=through,
            prepared_stocks=prepared,
            benchmark=benchmark,
            input_manifest=manifest_before,
            input_manifest_hash=manifest_hash,
            audit=audit,
        )
