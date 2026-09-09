from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
import statistics
from typing import Iterable
from urllib.parse import urlparse

from .calendar import (
    AD_HOC_CLOSURE_TITLE,
    read_sessions,
    validate_ad_hoc_closure_evidence,
)
from .config import CFG, RunnerConfig
from .io_utils import atomic_write_json, sha256_bytes, sha256_file, utc_timestamp
from .normalize import UNIVERSE_FILTER_DESCRIPTION, parse_release_archive
from .sources import SourceSnapshot


HEX64 = re.compile(r"^[0-9a-f]{64}$")
OFFICIAL_HOST_SUFFIXES = {
    "TWSE": ("twse.com.tw",),
    "TPEX": ("tpex.org.tw",),
}


@dataclass(frozen=True, slots=True)
class HistoricalHealthResult:
    ready: bool
    audit_path: Path
    audit: dict


def _iso_day(value: object) -> str:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    parsed = date.fromisoformat(text)
    if text != parsed.isoformat():
        raise ValueError(f"non-canonical date: {value!r}")
    return text


def _source_from_path(path: Path) -> SourceSnapshot:
    payload = path.read_bytes()
    return SourceSnapshot(
        source=path.stem,
        request_url="LOCAL_IMMUTABLE_ARCHIVE",
        retrieved_at_utc="LOCAL_IMMUTABLE",
        sha256=sha256_bytes(payload),
        path=path,
        payload=payload,
    )


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")
    return value


def _official_host(market: str, request_url: str) -> bool:
    host = (urlparse(request_url).hostname or "").lower()
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in OFFICIAL_HOST_SUFFIXES[market]
    )


def _archive_audit(archives: Iterable[Path]) -> dict:
    rows_by_key: dict[tuple[str, str], dict[str, str]] = {}
    duplicates: list[tuple[str, str]] = []
    invalid: list[dict] = []
    archive_hashes: list[dict] = []
    hash_issues: list[dict] = []
    seen_paths: set[Path] = set()

    for archive in archives:
        path = Path(archive).resolve()
        if path in seen_paths:
            hash_issues.append({"path": str(path), "reason": "duplicate archive path"})
            continue
        seen_paths.add(path)
        if not path.is_file():
            hash_issues.append({"path": str(path), "reason": "archive does not exist"})
            continue
        actual = sha256_file(path)
        metadata_path = path.with_suffix(path.suffix + ".metadata.json")
        expected = None
        source_manifest_hash = None
        try:
            metadata = _load_json(metadata_path)
            expected = metadata.get("sha256")
            source_manifest_hash = metadata.get("source_manifest_hash")
            if metadata.get("filename") != path.name:
                raise RuntimeError("metadata filename does not match archive")
            if not isinstance(expected, str) or not HEX64.fullmatch(expected):
                raise RuntimeError("metadata sha256 is not a 64-character hex digest")
            if not isinstance(source_manifest_hash, str) or not HEX64.fullmatch(
                source_manifest_hash
            ):
                raise RuntimeError("metadata source_manifest_hash is invalid")
            if expected != actual:
                raise RuntimeError("archive hash does not match immutable metadata")
        except Exception as exc:
            hash_issues.append(
                {
                    "path": str(path),
                    "metadata_path": str(metadata_path),
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
        archive_hashes.append(
            {
                "path": str(path),
                "sha256": actual,
                "metadata_path": str(metadata_path),
                "metadata_file_sha256": (
                    sha256_file(metadata_path) if metadata_path.is_file() else None
                ),
                "metadata_sha256": expected,
                "source_manifest_hash": source_manifest_hash,
            }
        )

        try:
            parsed = parse_release_archive(_source_from_path(path))
        except Exception as exc:
            invalid.append(
                {
                    "source": str(path),
                    "category": "parse_or_schema_error",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "resolved": False,
                }
            )
            continue
        if sha256_file(path) != actual:
            hash_issues.append(
                {"path": str(path), "reason": "archive changed while being audited"}
            )
        invalid.extend(parsed.errors)
        invalid.extend(
            event for event in parsed.excluded if not event.get("resolved", False)
        )
        for row in parsed.rows:
            key = (row["date"], row["code"])
            if key in rows_by_key:
                duplicates.append(key)
            else:
                rows_by_key[key] = row

    return {
        "rows_by_key": rows_by_key,
        "daily_counts": Counter(day for day, _ in rows_by_key),
        "duplicate_code_dates": sorted(set(duplicates)),
        "invalid_tradable_rows": invalid,
        "archive_hashes": archive_hashes,
        "archive_hash_issues": hash_issues,
    }


def _calendar_metadata_audit(calendar_path: Path, cfg: RunnerConfig) -> dict:
    actual = sha256_file(calendar_path)
    issues: list[str] = []
    metadata: dict = {}
    try:
        metadata = _load_json(cfg.calendar_metadata_path)
        if metadata.get("calendar_sha256") != actual:
            issues.append("calendar_sha256 does not match trading_calendar.csv")
        source = metadata.get("source")
        if not isinstance(source, dict):
            issues.append("calendar source metadata is missing")
        else:
            digest = source.get("sha256")
            if not isinstance(digest, str) or not HEX64.fullmatch(digest):
                issues.append("calendar source sha256 is invalid")
            raw_path = Path(str(source.get("path", "")))
            if not raw_path.is_file():
                issues.append("calendar raw source path is missing")
            elif isinstance(digest, str) and HEX64.fullmatch(digest):
                if sha256_file(raw_path) != digest:
                    issues.append("calendar raw source hash does not match sha256")
            url = str(source.get("request_url", ""))
            if not _official_host("TWSE", url):
                issues.append("calendar source is not an official TWSE URL")
            if not source.get("retrieved_at_utc"):
                issues.append("calendar retrieval timestamp is missing")

        closure_rows = metadata.get("official_ad_hoc_closure_rows", [])
        if not isinstance(closure_rows, list):
            issues.append("calendar ad-hoc closure evidence is not a list")
            closure_rows = []
        closure_dates: list[str] = []
        calendar_year = metadata.get("year")
        for index, item in enumerate(closure_rows):
            prefix = f"ad-hoc closure[{index}]"
            if not isinstance(item, dict):
                issues.append(f"{prefix} is not an object")
                continue
            try:
                closure_day = _iso_day(item.get("date"))
                closure_dates.append(closure_day)
            except Exception:
                issues.append(f"{prefix} date is invalid")
                closure_day = ""
            title = str(item.get("title", "")).strip()
            match = AD_HOC_CLOSURE_TITLE.fullmatch(title)
            if match is None:
                issues.append(f"{prefix} title is not an exact TWSE closure title")
            elif closure_day:
                title_day = (
                    f"{int(match.group('roc_year')) + 1911:04d}-"
                    f"{int(match.group('month')):02d}-"
                    f"{int(match.group('day')):02d}"
                )
                if title_day != closure_day:
                    issues.append(f"{prefix} title/date mismatch")

            source_digest = item.get("source_sha256")
            if not isinstance(source_digest, str) or not HEX64.fullmatch(source_digest):
                issues.append(f"{prefix} source sha256 is invalid")
            source_path = Path(str(item.get("source_path", "")))
            if not source_path.is_file():
                issues.append(f"{prefix} raw source path is missing")
            elif isinstance(source_digest, str) and HEX64.fullmatch(source_digest):
                if sha256_file(source_path) != source_digest:
                    issues.append(f"{prefix} raw source hash does not match sha256")
            if not _official_host("TWSE", str(item.get("source_request_url", ""))):
                issues.append(f"{prefix} source is not an official TWSE URL")
            if not item.get("source_retrieved_at_utc"):
                issues.append(f"{prefix} retrieval timestamp is missing")
            record_url = str(item.get("url", ""))
            if not _official_host("TWSE", record_url):
                issues.append(f"{prefix} news record is not an official TWSE URL")
            canonical_record = (
                f"{item.get('announcement_date_roc', '')}\n{title}\n{record_url}\n"
            ).encode("utf-8")
            record_digest = item.get("record_sha256")
            if (
                not isinstance(record_digest, str)
                or not HEX64.fullmatch(record_digest)
                or sha256_bytes(canonical_record) != record_digest
            ):
                issues.append(f"{prefix} record sha256 does not match evidence")
            try:
                if isinstance(calendar_year, bool) or not isinstance(calendar_year, int):
                    raise RuntimeError("calendar year is missing")
                validate_ad_hoc_closure_evidence(item, calendar_year)
            except Exception as exc:
                issues.append(f"{prefix} raw record linkage failed: {exc}")
        if len(closure_dates) != len(set(closure_dates)):
            issues.append("calendar ad-hoc closure evidence contains duplicate dates")
    except Exception as exc:
        issues.append(f"{type(exc).__name__}: {exc}")
    return {
        "path": str(calendar_path),
        "sha256": actual,
        "metadata_path": str(cfg.calendar_metadata_path),
        "metadata": metadata,
        "issues": issues,
    }


def _coverage_audit(
    path: Path,
    expected_sessions: list[str],
    calendar_sha256: str,
    minimum_ratio: float,
) -> dict:
    errors: list[str] = []
    payload: dict = {}
    records: dict[str, dict] = {}
    duplicate_dates: list[str] = []
    source_issues: list[dict] = []
    one_market_dates: list[dict] = []
    manifest_sha256 = None
    input_dates: list[str] = []

    try:
        manifest_sha256 = sha256_file(path)
        payload = _load_json(path)
        if payload.get("schema_version") != "1":
            errors.append("schema_version must be '1'")
        if payload.get("universe_filter") != UNIVERSE_FILTER_DESCRIPTION:
            errors.append("historical universe filter contract does not match")
        if not payload.get("generated_at_utc"):
            errors.append("generated_at_utc is missing")
        calendar = payload.get("trading_calendar")
        if not isinstance(calendar, dict) or calendar.get("sha256") != calendar_sha256:
            errors.append("trading calendar hash does not match coverage manifest")
        items = payload.get("sessions")
        if not isinstance(items, list):
            raise RuntimeError("sessions must be a list")
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"sessions[{index}] is not an object")
                continue
            try:
                day = _iso_day(item.get("date"))
            except Exception as exc:
                errors.append(f"sessions[{index}] invalid date: {exc}")
                continue
            input_dates.append(day)
            if day in records:
                duplicate_dates.append(day)
                continue
            records[day] = item
            markets_ok = True
            counts: dict[str, int] = {}
            for market in ("TWSE", "TPEX"):
                source = item.get(market)
                issues: list[str] = []
                if not isinstance(source, dict):
                    issues.append("source object is missing")
                    source = {}
                if source.get("status") != "READY":
                    issues.append("status is not READY")
                count = source.get("row_count")
                if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                    issues.append("row_count must be a positive integer")
                    count = 0
                counts[market] = count
                digest = source.get("sha256")
                if not isinstance(digest, str) or not HEX64.fullmatch(digest):
                    issues.append("sha256 is invalid")
                raw_path = Path(str(source.get("path", "")))
                if not raw_path.is_file():
                    issues.append("immutable raw source path is missing")
                elif isinstance(digest, str) and HEX64.fullmatch(digest):
                    if sha256_file(raw_path) != digest:
                        issues.append("raw source hash does not match sha256")
                request_url = str(source.get("request_url", ""))
                if not _official_host(market, request_url):
                    issues.append("request_url is not an official market URL")
                if not source.get("retrieved_at_utc"):
                    issues.append("retrieved_at_utc is missing")
                response_date = source.get("response_date")
                if response_date is not None:
                    try:
                        if _iso_day(response_date) != day:
                            issues.append("response_date does not match session date")
                    except Exception:
                        issues.append("response_date is invalid")
                if issues:
                    markets_ok = False
                    source_issues.append(
                        {"date": day, "market": market, "issues": issues}
                    )
            combined = item.get("combined_count")
            if (
                isinstance(combined, bool)
                or not isinstance(combined, int)
                or combined != counts["TWSE"] + counts["TPEX"]
            ):
                markets_ok = False
                source_issues.append(
                    {
                        "date": day,
                        "market": "COMBINED",
                        "issues": ["combined_count does not equal TWSE + TPEX"],
                    }
                )
            if item.get("status") != "READY":
                markets_ok = False
            if not markets_ok:
                one_market_dates.append(
                    {
                        "date": day,
                        "TWSE_rows": counts["TWSE"],
                        "TPEX_rows": counts["TPEX"],
                        "status": item.get("status"),
                    }
                )
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")

    if input_dates != sorted(input_dates):
        errors.append("sessions must be sorted by ascending date")
    scope = payload.get("scope")
    if not isinstance(scope, dict):
        errors.append("scope is missing")
    elif records:
        if scope.get("start_date") != min(records):
            errors.append("scope.start_date does not match the first session")
        if scope.get("through_date") != max(records):
            errors.append("scope.through_date does not match the last session")

    expected = set(expected_sessions)
    observed = set(records)
    missing = sorted(expected - observed)
    expected_end = max(expected) if expected else None
    future_out_of_scope = sorted(
        day for day in observed if expected_end is not None and day > expected_end
    )
    unexpected = sorted((observed - expected) - set(future_out_of_scope))

    healthy_records = [
        item
        for day, item in records.items()
        if day in expected
        and item.get("status") == "READY"
        and all(isinstance(item.get(market), dict) for market in ("TWSE", "TPEX"))
    ]
    reference_medians: dict[str, float] = {}
    low_coverage: list[dict] = []
    for scope in ("TWSE", "TPEX", "TOTAL"):
        values = []
        for item in healthy_records:
            value = (
                item.get("combined_count")
                if scope == "TOTAL"
                else item.get(scope, {}).get("row_count")
            )
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                values.append(value)
        median = float(statistics.median(values)) if values else 0.0
        reference_medians[scope] = median
        if not median:
            continue
        for day, item in sorted(records.items()):
            if day not in expected:
                continue
            value = (
                item.get("combined_count")
                if scope == "TOTAL"
                else (item.get(scope) or {}).get("row_count")
            )
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            ratio = value / median
            if ratio < minimum_ratio:
                low_coverage.append(
                    {
                        "date": day,
                        "market": scope,
                        "row_count": value,
                        "reference_median": median,
                        "ratio": ratio,
                        "minimum_ratio": minimum_ratio,
                    }
                )

    return {
        "path": str(path),
        "sha256": manifest_sha256,
        "schema_errors": errors,
        "records": records,
        "duplicate_dates": sorted(set(duplicate_dates)),
        "missing_calendar_sessions": missing,
        "unexpected_session_dates": unexpected,
        "future_out_of_scope_session_dates": future_out_of_scope,
        "source_metadata_issues": source_issues,
        "source_hash_issues": [
            issue
            for issue in source_issues
            if any(
                "sha256" in message or "hash" in message or "source path" in message
                for message in issue["issues"]
            )
        ],
        "one_market_dates": one_market_dates,
        "coverage_reference_medians": reference_medians,
        "low_coverage_dates": low_coverage,
    }


def extend_source_coverage_from_direct_audit(
    *,
    path: Path,
    calendar_path: Path,
    direct_audit: dict,
) -> dict:
    """Add official direct-source days while preserving older coverage records."""

    if direct_audit.get("failures") or direct_audit.get("unresolved_invalid_rows"):
        raise RuntimeError("cannot extend coverage from an unresolved direct-source audit")
    if path.is_file():
        payload = _load_json(path)
        if payload.get("schema_version") != "1" or not isinstance(
            payload.get("sessions"), list
        ):
            raise RuntimeError("existing historical source coverage manifest is malformed")
    else:
        payload = {"schema_version": "1", "sessions": []}

    records: dict[str, dict] = {}
    original_days: list[str] = []
    for item in payload["sessions"]:
        day = _iso_day(item.get("date"))
        original_days.append(day)
        if day in records:
            raise RuntimeError(f"duplicate date in source coverage manifest: {day}")
        records[day] = item
    if original_days != sorted(original_days):
        raise RuntimeError("source coverage sessions are not sorted")

    sources_by_day: dict[str, dict[str, dict]] = {}
    for archive in direct_audit.get("archives", []):
        for source in archive.get("sources", []):
            source_name = str(source.get("source", "")).lower()
            market = "TWSE" if source_name.startswith("twse_") else (
                "TPEX" if source_name.startswith("tpex_") else ""
            )
            if not market:
                raise RuntimeError(f"unknown direct official source: {source_name!r}")
            day = _iso_day(source.get("response_date"))
            if market in sources_by_day.setdefault(day, {}):
                raise RuntimeError(f"duplicate {market} source metadata on {day}")
            sources_by_day[day][market] = source

    for compact, counts in sorted(direct_audit.get("market_daily_counts", {}).items()):
        day = _iso_day(compact)
        markets = sources_by_day.get(day, {})
        record = {"date": day, "status": "READY"}
        for market in ("TWSE", "TPEX"):
            source = markets.get(market)
            count = counts.get(market)
            if not isinstance(source, dict) or not isinstance(count, int) or count <= 0:
                raise RuntimeError(f"incomplete direct {market} coverage on {day}")
            record[market] = {
                "status": "READY",
                "row_count": count,
                "request_url": source.get("request_url"),
                "retrieved_at_utc": source.get("retrieved_at_utc"),
                "sha256": source.get("sha256"),
                "path": source.get("path"),
                "response_date": day,
            }
        combined = counts.get("TOTAL")
        if combined != record["TWSE"]["row_count"] + record["TPEX"]["row_count"]:
            raise RuntimeError(f"direct combined coverage mismatch on {day}")
        record["combined_count"] = combined
        records[day] = record

    ordered = [records[day] for day in sorted(records)]
    payload.update(
        {
            "schema_version": "1",
            "universe_filter": UNIVERSE_FILTER_DESCRIPTION,
            "generated_at_utc": utc_timestamp(),
            "scope": {
                "start_date": ordered[0]["date"] if ordered else None,
                "through_date": ordered[-1]["date"] if ordered else None,
            },
            "trading_calendar": {
                "path": str(calendar_path),
                "sha256": sha256_file(calendar_path),
            },
            "sessions": ordered,
        }
    )
    atomic_write_json(path, payload)
    return payload


def run_historical_preflight(
    *,
    archives: Iterable[Path],
    calendar_path: Path,
    source_coverage_path: Path,
    through_date: str,
    cfg: RunnerConfig = CFG,
    audit_path: Path | None = None,
) -> HistoricalHealthResult:
    """Audit the complete historical input layer without touching signal ledgers."""

    output_path = audit_path or cfg.historical_health_audit_path
    started = utc_timestamp()
    try:
        through = _iso_day(through_date)
        sessions = read_sessions(calendar_path.read_bytes())
        expected = [
            item
            for item in sessions
            if cfg.data_start <= item.replace("-", "") <= through.replace("-", "")
        ]
        calendar_audit = _calendar_metadata_audit(calendar_path, cfg)
        archive_audit = _archive_audit(archives)
        coverage_audit = _coverage_audit(
            source_coverage_path,
            expected,
            calendar_audit["sha256"],
            cfg.minimum_market_coverage_ratio,
        )

        rows_by_key = archive_audit.pop("rows_by_key")
        all_daily_counts = archive_audit.pop("daily_counts")
        through_compact = through.replace("-", "")
        daily_counts = Counter(
            {
                day: count
                for day, count in all_daily_counts.items()
                if day <= through_compact
            }
        )
        observed_dates = {_iso_day(day) for day in daily_counts}
        future_archive_dates = sorted(
            _iso_day(day) for day in all_daily_counts if day > through_compact
        )
        missing_data = sorted(set(expected) - observed_dates)
        unexpected_data = sorted(observed_dates - set(expected))
        count_mismatches: list[dict] = []
        for day in expected:
            compact = day.replace("-", "")
            coverage = coverage_audit["records"].get(day)
            if coverage is None or not isinstance(coverage.get("combined_count"), int):
                continue
            archive_count = daily_counts.get(compact, 0)
            if archive_count != coverage["combined_count"]:
                count_mismatches.append(
                    {
                        "date": day,
                        "archive_rows": archive_count,
                        "source_coverage_rows": coverage["combined_count"],
                    }
                )

        checks = {
            "calendar_metadata_valid": not calendar_audit["issues"],
            "coverage_manifest_schema_valid": not coverage_audit["schema_errors"],
            "coverage_manifest_calendar_hash_match": not any(
                "calendar hash" in error for error in coverage_audit["schema_errors"]
            ),
            "coverage_sessions_complete": not coverage_audit["missing_calendar_sessions"],
            "coverage_duplicate_dates_zero": not coverage_audit["duplicate_dates"],
            "coverage_unexpected_dates_zero": not coverage_audit["unexpected_session_dates"],
            "both_markets_present_every_session": not coverage_audit["one_market_dates"],
            "coverage_source_metadata_valid": not coverage_audit["source_metadata_issues"],
            "official_source_hashes_fixed": not coverage_audit["source_hash_issues"],
            "low_coverage_dates_zero": not coverage_audit["low_coverage_dates"],
            "calendar_sessions_complete": not missing_data,
            "unexpected_archive_dates_zero": not unexpected_data,
            "archive_counts_match_source_coverage": not count_mismatches,
            "duplicate_code_date_zero": not archive_audit["duplicate_code_dates"],
            "invalid_tradable_ohlcv_zero": not archive_audit["invalid_tradable_rows"],
            "archive_source_hashes_fixed": not archive_audit["archive_hash_issues"],
        }
        passed = all(checks.values())
        unresolved_calendar_gaps = sorted(
            set(missing_data) | set(coverage_audit["missing_calendar_sessions"])
        )
        audit = {
            "schema_version": "1",
            "started_at_utc": started,
            "finished_at_utc": utc_timestamp(),
            "status": "PASS" if passed else "FAIL_CLOSED",
            "pass": passed,
            "scope": {
                "start_date": expected[0] if expected else None,
                "through_date": through,
                "calendar_sessions": len(expected),
            },
            "checks": checks,
            "calendar": calendar_audit,
            "source_coverage": {
                key: value for key, value in coverage_audit.items() if key != "records"
            },
            "archives": archive_audit["archive_hashes"],
            "archive_hash_issues": archive_audit["archive_hash_issues"],
            "missing_calendar_sessions": missing_data,
            "unresolved_calendar_gaps": unresolved_calendar_gaps,
            "unresolved_calendar_gap_count": len(unresolved_calendar_gaps),
            "unexpected_archive_dates": unexpected_data,
            "future_out_of_scope_archive_dates": future_archive_dates,
            "one_market_dates": coverage_audit["one_market_dates"],
            "low_coverage_dates": coverage_audit["low_coverage_dates"],
            "archive_count_mismatches": count_mismatches,
            "duplicate_code_date_count": len(archive_audit["duplicate_code_dates"]),
            "duplicate_code_date_samples": archive_audit["duplicate_code_dates"][:20],
            "invalid_tradable_rows": len(archive_audit["invalid_tradable_rows"]),
            "unresolved_invalid_tradable_rows": len(
                archive_audit["invalid_tradable_rows"]
            ),
            "invalid_tradable_samples": archive_audit["invalid_tradable_rows"][:20],
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
            "signal_ledgers_touched": False,
        }
    except Exception as exc:
        audit = {
            "schema_version": "1",
            "started_at_utc": started,
            "finished_at_utc": utc_timestamp(),
            "status": "FAIL_CLOSED",
            "pass": False,
            "failure_type": type(exc).__name__,
            "failure_reason": str(exc),
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
            "signal_ledgers_touched": False,
        }
    atomic_write_json(output_path, audit)
    return HistoricalHealthResult(bool(audit.get("pass")), output_path, audit)
