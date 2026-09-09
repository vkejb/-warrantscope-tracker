from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import statistics
from typing import Callable, Iterable

from .calendar import build_trading_calendar, collect_ad_hoc_closures, read_sessions
from .config import CFG, RunnerConfig
from .io_utils import (
    atomic_write_bytes,
    atomic_write_json,
    sha256_bytes,
    sha256_file,
    utc_timestamp,
    write_immutable,
)
from .normalize import (
    UNIVERSE_FILTER_DESCRIPTION,
    ParsedSource,
    deterministic_zip,
    parse_release_archive,
    parse_tpex,
    parse_twse,
)
from .preflight import run_historical_preflight
from .sources import OfficialSourceClient, SourceSnapshot


HEX = frozenset("0123456789abcdef")
LEDGER_FILENAMES = (
    "prospective_signals.csv",
    "prospective_outcomes.csv",
    "prospective_scan_log.csv",
    "shadow_status.json",
)


@dataclass(frozen=True, slots=True)
class HistoricalRepairResult:
    through_date: str
    calendar_path: Path
    base_archives: tuple[Path, ...]
    patch_archives: tuple[Path, ...]
    coverage_path: Path
    repair_audit_path: Path
    health_audit_path: Path
    active_inputs_path: Path
    audit: dict


def normalize_compact_date(value: str) -> str:
    compact = str(value).strip().replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError(f"invalid YYYY-MM-DD/YYYMMDD date: {value!r}")
    datetime.strptime(compact, "%Y%m%d")
    return compact


def _iso(compact: str) -> str:
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def _ledger_hashes(cfg: RunnerConfig) -> dict[str, str | None]:
    return {
        name: sha256_file(cfg.shadow_store_dir / name)
        if (cfg.shadow_store_dir / name).is_file()
        else None
        for name in LEDGER_FILENAMES
    }


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


def _parse_clean(path: Path) -> ParsedSource:
    before = sha256_file(path)
    parsed = parse_release_archive(_source_from_path(path))
    if before != sha256_file(path):
        raise RuntimeError(f"archive changed while it was read: {path}")
    unresolved = [*parsed.errors, *(row for row in parsed.excluded if not row.get("resolved"))]
    if unresolved:
        raise RuntimeError(f"clean archive contains unresolved rows: {path}")
    return parsed


def _candidate_score(path: Path, expected: set[str], through: str) -> tuple[int, int, int, str]:
    parsed = _parse_clean(path)
    dates = {row["date"] for row in parsed.rows if row["date"] <= through}
    later = any(row["date"] > through for row in parsed.rows)
    if later:
        return (-1, -1, -1, path.name)
    return (len(dates & expected), len(parsed.rows), path.stat().st_mtime_ns, path.name)


def select_base_archives(
    sessions: list[str],
    through: str,
    cfg: RunnerConfig = CFG,
) -> tuple[list[Path], dict[int, Path]]:
    """Select one immutable base archive per ISO week; patches are excluded."""

    compact_sessions = [item.replace("-", "") for item in sessions]
    expected_by_week: dict[int, set[str]] = defaultdict(set)
    for compact in compact_sessions:
        if cfg.data_start <= compact <= through:
            expected_by_week[date.fromisoformat(_iso(compact)).isocalendar().week].add(compact)

    selected: list[Path] = []
    by_week: dict[int, Path] = {}
    for week, expected in sorted(expected_by_week.items()):
        if week <= cfg.historical_release_last_week:
            if week in cfg.historical_release_missing_weeks:
                if expected:
                    raise RuntimeError(f"configured missing release W{week:02d} has sessions")
                continue
            candidates = sorted(cfg.clean_dir.glob(f"weekly_2026_W{week:02d}_clean_*.zip"))
        else:
            candidates = sorted(
                path
                for path in cfg.clean_dir.glob(
                    f"weekly_2026_W{week:02d}_official_through_*.zip"
                )
                if "patch" not in path.name
            )
        if not candidates:
            raise RuntimeError(f"no immutable base archive for W{week:02d}")
        scores = [(_candidate_score(path, expected, through), path) for path in candidates]
        score, chosen = max(scores, key=lambda item: item[0])
        if score[0] <= 0:
            raise RuntimeError(f"no usable base archive for W{week:02d}")
        selected.append(chosen)
        by_week[week] = chosen
    return selected, by_week


def _rows_by_key(paths: Iterable[Path]) -> tuple[dict[tuple[str, str], dict[str, str]], list[tuple[str, str]], list[dict]]:
    rows: dict[tuple[str, str], dict[str, str]] = {}
    duplicates: list[tuple[str, str]] = []
    invalid: list[dict] = []
    for path in paths:
        parsed = _parse_clean(path)
        invalid.extend(parsed.errors)
        invalid.extend(row for row in parsed.excluded if not row.get("resolved"))
        for row in parsed.rows:
            key = (row["date"], row["code"])
            if key in rows:
                duplicates.append(key)
            else:
                rows[key] = row
    return rows, duplicates, invalid


def _numeric_row_equal(left: dict[str, str], right: dict[str, str]) -> bool:
    if left["code"] != right["code"] or left["date"] != right["date"]:
        return False
    if int(left["volume"]) != int(right["volume"]):
        return False
    return all(float(left[field]) == float(right[field]) for field in ("open", "high", "low", "close"))


def _market_payload(parsed: ParsedSource) -> dict:
    source = parsed.source_metadata
    return {
        "status": "READY",
        "row_count": len(parsed.rows),
        "request_url": str(source["request_url"]),
        "retrieved_at_utc": str(source["retrieved_at_utc"]),
        "sha256": str(source["sha256"]),
        "path": str(source["path"]),
        "response_date": _iso(str(parsed.requested_date)),
    }


def _rolling_low_dates(counts: dict[str, int], sessions: list[str], ratio: float) -> list[dict]:
    findings: list[dict] = []
    for index, day in enumerate(sessions):
        prior = [counts[item] for item in sessions[max(0, index - 20) : index] if counts.get(item, 0) > 0]
        if len(prior) < 5 or counts.get(day, 0) <= 0:
            continue
        median = statistics.median(prior)
        observed_ratio = counts[day] / median if median else 0.0
        if observed_ratio < ratio:
            findings.append(
                {
                    "date": _iso(day),
                    "row_count": counts[day],
                    "prior20_median": median,
                    "ratio": observed_ratio,
                }
            )
    return findings


def _write_patch(
    *,
    week: int,
    through: str,
    rows: list[dict[str, str]],
    base_archive: Path,
    sources_by_date: dict[str, dict],
    cfg: RunnerConfig,
) -> tuple[Path, dict]:
    rows = sorted(rows, key=lambda item: (item["date"], item["code"]))
    affected_dates = sorted({row["date"] for row in rows})
    source_items = [
        {
            "date": _iso(day),
            "TWSE": sources_by_date[day]["TWSE"],
            "TPEX": sources_by_date[day]["TPEX"],
        }
        for day in affected_dates
    ]
    provenance_core = {
        "schema_version": "1",
        "repair_type": "MISSING_OFFICIAL_CODE_DATE_PATCH",
        "year": 2026,
        "iso_week": week,
        "through_date": _iso(through),
        "original_archive": {
            "path": str(base_archive),
            "sha256": sha256_file(base_archive),
        },
        "missing_dates": [_iso(day) for day in affected_dates],
        "official_sources": source_items,
        "patch_rows": len(rows),
        "patch_key_sha256": sha256_bytes(
            "".join(f"{row['date']}|{row['code']}\n" for row in rows).encode("utf-8")
        ),
        "forward_fill": False,
        "overwrites_existing_code_date": False,
    }
    source_manifest_hash = sha256_bytes(
        json.dumps(provenance_core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    stem = f"weekly_2026_W{week:02d}_official_gap_patch_v01_through_{through}"
    member = f"{stem}.csv"
    payload = deterministic_zip(member, rows)
    patch_hash = sha256_bytes(payload)
    path = cfg.clean_dir / f"{stem}_{source_manifest_hash[:12]}_{patch_hash[:12]}.zip"
    write_immutable(path, payload)
    metadata = {
        **provenance_core,
        "path": str(path),
        "filename": path.name,
        "member": member,
        "source_manifest_hash": source_manifest_hash,
        "sha256": patch_hash,
        "bytes": len(payload),
        "immutable": True,
    }
    write_immutable(
        path.with_suffix(path.suffix + ".metadata.json"),
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    write_immutable(
        path.with_suffix(path.suffix + ".provenance.json"),
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    return path, metadata


def _archive_declares_established_universe(path: Path) -> bool:
    metadata_path = path.with_suffix(path.suffix + ".metadata.json")
    if not metadata_path.is_file():
        return False
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        metadata.get("universe_filter") == UNIVERSE_FILTER_DESCRIPTION
        and metadata.get("normalization_type")
        == "DIRECT_OFFICIAL_ESTABLISHED_RELEASE_UNIVERSE"
    )


def _write_direct_official_base(
    *,
    week: int,
    rows: list[dict[str, str]],
    days: list[str],
    previous_archive: Path,
    sources_by_date: dict[str, dict],
    cfg: RunnerConfig,
) -> tuple[Path, dict]:
    """Write a physical, filtered W36+ base without replacing its predecessor."""

    ordered_rows = sorted(rows, key=lambda item: (item["date"], item["code"]))
    source_items = [
        {
            "date": _iso(day),
            "TWSE": sources_by_date[day]["TWSE"],
            "TPEX": sources_by_date[day]["TPEX"],
        }
        for day in days
    ]
    provenance_core = {
        "schema_version": "1",
        "normalization_type": "DIRECT_OFFICIAL_ESTABLISHED_RELEASE_UNIVERSE",
        "year": 2026,
        "iso_week": week,
        "sessions": [_iso(day) for day in days],
        "universe_filter": UNIVERSE_FILTER_DESCRIPTION,
        "previous_archive": {
            "path": str(previous_archive),
            "sha256": sha256_file(previous_archive),
        },
        "official_sources": source_items,
        "tradable_rows": len(ordered_rows),
        "forward_fill": False,
    }
    source_manifest_hash = sha256_bytes(
        json.dumps(
            provenance_core,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    through = days[-1]
    stem = f"weekly_2026_W{week:02d}_official_through_{through}"
    member = f"{stem}.csv"
    payload = deterministic_zip(member, ordered_rows)
    archive_hash = sha256_bytes(payload)
    path = cfg.clean_dir / (
        f"{stem}_{source_manifest_hash[:12]}_{archive_hash[:12]}.zip"
    )
    write_immutable(path, payload)
    metadata = {
        **provenance_core,
        "path": str(path),
        "filename": path.name,
        "member": member,
        "source_manifest_hash": source_manifest_hash,
        "sha256": archive_hash,
        "bytes": len(payload),
        "immutable": True,
    }
    encoded = (
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    write_immutable(path.with_suffix(path.suffix + ".metadata.json"), encoded)
    write_immutable(path.with_suffix(path.suffix + ".provenance.json"), encoded)
    return path, metadata


def repair_history(
    through_date: str,
    *,
    cfg: RunnerConfig = CFG,
    client: OfficialSourceClient | None = None,
    progress: Callable[[str], None] | None = None,
) -> HistoricalRepairResult:
    """Audit all sessions through ``through_date`` and build immutable gap patches.

    This function has no access to the prospective storage API. It snapshots the
    four ledger files before and after and fails if even one byte changes.
    """

    through = normalize_compact_date(through_date)
    if through[:4] != "2026":
        raise RuntimeError("historical repair v01 is scoped to 2026")
    notify = progress or (lambda _message: None)
    source_client = client or OfficialSourceClient(cfg)
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    cfg.audit_dir.mkdir(parents=True, exist_ok=True)
    cfg.clean_dir.mkdir(parents=True, exist_ok=True)
    ledgers_before = _ledger_hashes(cfg)

    calendar_snapshot = source_client.calendar()
    news_snapshot = source_client.twse_news(refresh=True)
    closures = collect_ad_hoc_closures(
        news_snapshot,
        int(through[:4]),
        previous_calendar_metadata_path=cfg.calendar_metadata_path,
    )
    calendar_bytes, calendar_metadata = build_trading_calendar(
        calendar_snapshot,
        int(through[:4]),
        ad_hoc_closures=closures,
    )
    sessions_iso = read_sessions(calendar_bytes)
    sessions = [
        item.replace("-", "")
        for item in sessions_iso
        if cfg.data_start <= item.replace("-", "") <= through
    ]
    if through not in sessions:
        raise RuntimeError("repair through-date is not an official trading session")
    atomic_write_bytes(cfg.calendar_path, calendar_bytes)
    calendar_metadata.update(
        {
            "generated_at_utc": utc_timestamp(),
            "target_date": _iso(through),
            "target_is_trading_day": True,
        }
    )
    atomic_write_json(cfg.calendar_metadata_path, calendar_metadata)

    base_archives, archive_by_week = select_base_archives(sessions_iso, through, cfg)
    base_rows, base_duplicates, base_invalid = _rows_by_key(base_archives)
    if base_duplicates or base_invalid:
        raise RuntimeError("base archives contain duplicates or invalid tradable rows")

    missing_dates_before = sorted(set(sessions) - {day for day, _ in base_rows})
    base_daily_counts = Counter(day for day, _ in base_rows)
    official_daily_counts: dict[str, dict[str, int]] = {}
    sources_by_date: dict[str, dict] = {}
    official_rows_by_week: dict[int, list[dict[str, str]]] = defaultdict(list)
    official_days_by_week: dict[int, list[str]] = defaultdict(list)
    coverage_sessions: list[dict] = []
    patch_rows_by_week: dict[int, list[dict[str, str]]] = defaultdict(list)
    one_market_dates: list[dict] = []
    low_market_dates: list[dict] = []
    value_mismatches: list[dict] = []
    extra_keys: set[tuple[str, str]] = set(base_rows)

    for index, day in enumerate(sessions, start=1):
        twse = parse_twse(source_client.twse_eod(day, refresh=False), day)
        tpex = parse_tpex(source_client.tpex_eod(day, refresh=False), day)
        if twse.errors or tpex.errors:
            raise RuntimeError(f"official source has unresolved invalid rows on {day}")
        market_rows = {"TWSE": twse.rows, "TPEX": tpex.rows}
        sources_by_date[day] = {
            "TWSE": _market_payload(twse),
            "TPEX": _market_payload(tpex),
        }
        combined: dict[tuple[str, str], dict[str, str]] = {}
        present: dict[str, int] = {}
        missing: dict[str, int] = {}
        for market, rows in market_rows.items():
            present[market] = 0
            missing[market] = 0
            for row in rows:
                key = (day, row["code"])
                if key in combined:
                    raise RuntimeError(f"official TWSE/TPEx duplicate code-date: {key}")
                combined[key] = row
                extra_keys.discard(key)
                current = base_rows.get(key)
                if current is None:
                    patch_rows_by_week[date.fromisoformat(_iso(day)).isocalendar().week].append(row)
                    missing[market] += 1
                else:
                    present[market] += 1
                    if not _numeric_row_equal(current, row) and len(value_mismatches) < 100:
                        value_mismatches.append(
                            {"date": _iso(day), "code": row["code"], "market": market}
                        )
        official_daily_counts[day] = {
            "TWSE": len(twse.rows),
            "TPEX": len(tpex.rows),
            "TOTAL": len(combined),
        }
        iso_week = date.fromisoformat(_iso(day)).isocalendar().week
        official_days_by_week[iso_week].append(day)
        official_rows_by_week[iso_week].extend(
            combined[key] for key in sorted(combined)
        )
        if any(present[market] == 0 and len(market_rows[market]) > 0 for market in market_rows):
            one_market_dates.append(
                {
                    "date": _iso(day),
                    "base_present": present,
                    "official_expected": {market: len(rows) for market, rows in market_rows.items()},
                }
            )
        ratios = {
            market: present[market] / len(rows) if rows else 0.0
            for market, rows in market_rows.items()
        }
        if any(ratios[market] < cfg.minimum_market_coverage_ratio for market in ratios):
            low_market_dates.append(
                {
                    "date": _iso(day),
                    "base_present": present,
                    "official_expected": {market: len(rows) for market, rows in market_rows.items()},
                    "ratios": ratios,
                    "missing": missing,
                }
            )
        coverage_sessions.append(
            {
                "date": _iso(day),
                "status": "READY",
                "TWSE": sources_by_date[day]["TWSE"],
                "TPEX": sources_by_date[day]["TPEX"],
                "combined_count": len(combined),
            }
        )
        if index == 1 or index % 10 == 0 or index == len(sessions):
            notify(f"official coverage {index}/{len(sessions)} through {_iso(day)}")

    normalized_direct_bases: list[dict] = []
    for week in sorted(official_rows_by_week):
        if week <= cfg.historical_release_last_week:
            continue
        previous = archive_by_week.get(week)
        if previous is None:
            raise RuntimeError(f"missing direct-official base archive for W{week:02d}")
        if _archive_declares_established_universe(previous):
            continue
        normalized, metadata = _write_direct_official_base(
            week=week,
            rows=official_rows_by_week[week],
            days=official_days_by_week[week],
            previous_archive=previous,
            sources_by_date=sources_by_date,
            cfg=cfg,
        )
        archive_by_week[week] = normalized
        normalized_direct_bases.append(metadata)
    if normalized_direct_bases:
        base_archives = [archive_by_week[week] for week in sorted(archive_by_week)]
        normalized_rows, normalized_duplicates, normalized_invalid = _rows_by_key(
            base_archives
        )
        if normalized_duplicates or normalized_invalid or normalized_rows != base_rows:
            raise RuntimeError(
                "direct-official universe normalization changed eligible base rows"
            )

    if value_mismatches:
        raise RuntimeError(
            f"official/base OHLCV mismatches remain; first={value_mismatches[0]}"
        )
    if extra_keys:
        first = sorted(extra_keys)[0]
        raise RuntimeError(f"base archive key is absent from official sources: {first}")

    patches: list[Path] = []
    patch_metadata: list[dict] = []
    for week, rows in sorted(patch_rows_by_week.items()):
        base_archive = archive_by_week.get(week)
        if base_archive is None:
            raise RuntimeError(f"missing base archive provenance for W{week:02d}")
        patch, metadata = _write_patch(
            week=week,
            through=through,
            rows=rows,
            base_archive=base_archive,
            sources_by_date=sources_by_date,
            cfg=cfg,
        )
        patches.append(patch)
        patch_metadata.append(metadata)

    combined_rows, post_duplicates, post_invalid = _rows_by_key([*base_archives, *patches])
    post_dates = {day for day, _ in combined_rows}
    missing_after = sorted(set(sessions) - post_dates)
    combined_daily_counts = Counter(day for day, _ in combined_rows)
    coverage_deficits: list[dict] = []
    for day in sessions:
        if combined_daily_counts[day] < official_daily_counts[day]["TOTAL"]:
            coverage_deficits.append(
                {
                    "date": _iso(day),
                    "combined_rows": combined_daily_counts[day],
                    "official_rows": official_daily_counts[day]["TOTAL"],
                }
            )

    coverage = {
        "schema_version": "1",
        "universe_filter": UNIVERSE_FILTER_DESCRIPTION,
        "generated_at_utc": utc_timestamp(),
        "scope": {"start_date": _iso(sessions[0]), "through_date": _iso(through)},
        "trading_calendar": {
            "path": str(cfg.calendar_path),
            "sha256": sha256_file(cfg.calendar_path),
        },
        "sessions": coverage_sessions,
    }
    atomic_write_json(cfg.historical_source_coverage_path, coverage)
    coverage_hash = sha256_file(cfg.historical_source_coverage_path)

    all_archives = [*base_archives, *patches]
    source_hashes_fixed = all(
        path.is_file() and len(sha256_file(path)) == 64 and set(sha256_file(path)) <= HEX
        for path in all_archives
    )
    checks = {
        "calendar_sessions_complete": not missing_after,
        "twse_tpex_source_coverage_complete": len(coverage_sessions) == len(sessions),
        "duplicate_code_date_zero": not post_duplicates,
        "invalid_tradable_ohlcv_zero": not post_invalid,
        "daily_coverage_at_least_official": not coverage_deficits,
        "source_hashes_fixed": source_hashes_fixed,
        "prospective_ledgers_unchanged": ledgers_before == _ledger_hashes(cfg),
    }
    repair_audit_path = cfg.audit_dir / f"historical_repair_audit_{through}.json"
    audit = {
        "schema_version": "1",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "generated_at_utc": utc_timestamp(),
        "scope": {"start_date": _iso(sessions[0]), "through_date": _iso(through)},
        "checks": checks,
        "calendar": {
            "path": str(cfg.calendar_path),
            "sha256": sha256_file(cfg.calendar_path),
            "official_schedule_source": calendar_snapshot.metadata(),
            "ad_hoc_closures": closures,
        },
        "before_repair": {
            "apparent_missing_calendar_sessions": [_iso(day) for day in missing_dates_before],
            "one_market_dates": one_market_dates,
            "low_market_coverage_dates": low_market_dates,
            "low_total_count_dates": _rolling_low_dates(
                dict(base_daily_counts), sessions, cfg.minimum_market_coverage_ratio
            ),
            "duplicate_code_dates": len(base_duplicates),
            "invalid_tradable_rows": len(base_invalid),
        },
        "calendar_reclassifications": [
            {
                "date": item["date"],
                "classification": "OFFICIAL_AD_HOC_MARKET_CLOSURE_NOT_A_TRADING_SESSION",
                "title": item["title"],
                "url": item["url"],
                "source_sha256": item["source_sha256"],
                "record_sha256": item["record_sha256"],
            }
            for item in closures
            if cfg.data_start <= item["date"].replace("-", "") <= through
        ],
        "patches": patch_metadata,
        "normalized_direct_bases": normalized_direct_bases,
        "universe_filter": UNIVERSE_FILTER_DESCRIPTION,
        "after_repair": {
            "unresolved_calendar_gaps": [_iso(day) for day in missing_after],
            "unresolved_coverage_deficits": coverage_deficits,
            "duplicate_code_dates": len(post_duplicates),
            "invalid_tradable_rows": len(post_invalid),
            "low_total_count_dates": _rolling_low_dates(
                dict(combined_daily_counts), sessions, cfg.minimum_market_coverage_ratio
            ),
            "session_count": len(sessions),
            "first_session": _iso(sessions[0]),
            "last_session": _iso(sessions[-1]),
        },
        "official_source_coverage": {
            "path": str(cfg.historical_source_coverage_path),
            "sha256": coverage_hash,
            "sessions": len(coverage_sessions),
        },
        "base_archives": [
            {"path": str(path), "sha256": sha256_file(path)} for path in base_archives
        ],
        "prospective_ledgers": {
            "before": ledgers_before,
            "after": _ledger_hashes(cfg),
        },
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    if all(checks.values()):
        canonical_health = run_historical_preflight(
            archives=all_archives,
            calendar_path=cfg.calendar_path,
            source_coverage_path=cfg.historical_source_coverage_path,
            through_date=_iso(through),
            cfg=cfg,
        )
        audit["canonical_preflight"] = {
            "path": str(canonical_health.audit_path),
            "sha256": sha256_file(canonical_health.audit_path),
            "status": canonical_health.audit.get("status"),
            "checks": canonical_health.audit.get("checks", {}),
        }
        checks["canonical_preflight_pass"] = canonical_health.ready
    audit["status"] = "PASS" if all(checks.values()) else "FAIL"
    atomic_write_json(repair_audit_path, audit)
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise RuntimeError(f"historical health audit failed: {', '.join(failed)}")

    active_inputs = {
        "schema_version": "1",
        "through_date": _iso(through),
        "calendar": {
            "path": str(cfg.calendar_path),
            "sha256": sha256_file(cfg.calendar_path),
        },
        "base_archives": [
            {"path": str(path), "sha256": sha256_file(path)} for path in base_archives
        ],
        "patch_archives": [
            {"path": str(path), "sha256": sha256_file(path)} for path in patches
        ],
        "coverage_manifest": {
            "path": str(cfg.historical_source_coverage_path),
            "sha256": coverage_hash,
        },
        "health_audit": {
            "path": str(cfg.historical_health_audit_path),
            "sha256": sha256_file(cfg.historical_health_audit_path),
        },
        "repair_audit": {
            "path": str(repair_audit_path),
            "sha256": sha256_file(repair_audit_path),
        },
        "execution_mode": "DATA_REPAIR_ONLY_NO_PROSPECTIVE_LEDGER_WRITE",
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    atomic_write_json(cfg.historical_repair_active_inputs_path, active_inputs)
    if ledgers_before != _ledger_hashes(cfg):
        raise RuntimeError("prospective ledger changed during historical repair")
    return HistoricalRepairResult(
        through_date=through,
        calendar_path=cfg.calendar_path,
        base_archives=tuple(base_archives),
        patch_archives=tuple(patches),
        coverage_path=cfg.historical_source_coverage_path,
        repair_audit_path=repair_audit_path,
        health_audit_path=cfg.historical_health_audit_path,
        active_inputs_path=cfg.historical_repair_active_inputs_path,
        audit=audit,
    )


def public_repair_result(result: HistoricalRepairResult) -> dict:
    return {
        "status": result.audit["status"],
        "through_date": _iso(result.through_date),
        "calendar_path": str(result.calendar_path),
        "base_archives": len(result.base_archives),
        "patch_archives": [str(path) for path in result.patch_archives],
        "coverage_path": str(result.coverage_path),
        "repair_audit_path": str(result.repair_audit_path),
        "health_audit_path": str(result.health_audit_path),
        "active_inputs_path": str(result.active_inputs_path),
        "checks": result.audit["checks"],
        "before_repair": result.audit["before_repair"],
        "calendar_reclassifications": result.audit["calendar_reclassifications"],
        "after_repair": result.audit["after_repair"],
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }


def load_repair_patch_archives(cfg: RunnerConfig = CFG) -> list[Path]:
    """Return the explicitly activated immutable patch layer, or fail closed."""

    path = cfg.historical_repair_active_inputs_path
    if not path.is_file():
        raise RuntimeError("historical repair active-input manifest is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("historical repair active-input manifest is invalid") from exc
    if payload.get("schema_version") != "1":
        raise RuntimeError("historical repair active-input schema drifted")
    entries = payload.get("patch_archives")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("historical repair patch layer is empty")
    result: list[Path] = []
    seen: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("historical repair patch entry is malformed")
        archive = Path(str(entry.get("path", ""))).resolve()
        expected = str(entry.get("sha256", ""))
        if archive in seen:
            raise RuntimeError("historical repair patch archive is duplicated")
        if not archive.is_file() or len(expected) != 64 or sha256_file(archive) != expected:
            raise RuntimeError(f"historical repair patch hash mismatch: {archive}")
        metadata = archive.with_suffix(archive.suffix + ".metadata.json")
        if not metadata.is_file():
            raise RuntimeError(f"historical repair patch metadata missing: {archive}")
        result.append(archive)
        seen.add(archive)
    return result


def load_repair_archives_for_preflight(cfg: RunnerConfig = CFG) -> list[Path]:
    """Load the last fully repaired archive set without any filename globbing."""

    path = cfg.historical_repair_active_inputs_path
    if not path.is_file():
        raise RuntimeError("historical repair active-input manifest is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("historical repair active-input manifest is invalid") from exc
    result: list[Path] = []
    seen: set[Path] = set()
    for group in ("base_archives", "patch_archives"):
        entries = payload.get(group)
        if not isinstance(entries, list):
            raise RuntimeError(f"historical repair {group} are missing")
        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError(f"historical repair {group} entry is malformed")
            archive = Path(str(entry.get("path", ""))).resolve()
            expected = str(entry.get("sha256", ""))
            if archive in seen:
                raise RuntimeError("historical repair archive is duplicated")
            if not archive.is_file() or len(expected) != 64 or sha256_file(archive) != expected:
                raise RuntimeError(f"historical repair archive hash mismatch: {archive}")
            result.append(archive)
            seen.add(archive)
    if not result:
        raise RuntimeError("historical repair archive set is empty")
    return result


def load_previous_archives_for_preflight(
    through_date: str,
    cfg: RunnerConfig = CFG,
) -> list[Path]:
    """Use the latest explicitly prepared input set, falling back to repair output."""

    through = normalize_compact_date(through_date)
    if cfg.active_inputs_path.is_file():
        try:
            active = json.loads(cfg.active_inputs_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("latest prepared-input manifest is invalid") from exc
        active_target = normalize_compact_date(str(active.get("target_date", "")))
        if active_target >= through:
            archives = active.get("archives")
            declared = active.get("archive_hashes")
            if not isinstance(archives, list) or not isinstance(declared, list):
                raise RuntimeError("latest prepared-input archive manifest is malformed")
            expected = {str(item.get("path")): str(item.get("sha256")) for item in declared}
            result = [Path(str(value)).resolve() for value in archives]
            if len(result) != len(set(result)) or not result:
                raise RuntimeError("latest prepared-input archives are duplicated or empty")
            for archive in result:
                if not archive.is_file() or sha256_file(archive) != expected.get(str(archive)):
                    raise RuntimeError(f"latest prepared-input archive hash mismatch: {archive}")
            return result

    repair_manifest = cfg.historical_repair_active_inputs_path
    if not repair_manifest.is_file():
        raise RuntimeError("no repaired historical input set is available")
    payload = json.loads(repair_manifest.read_text(encoding="utf-8"))
    repair_through = normalize_compact_date(str(payload.get("through_date", "")))
    if repair_through < through:
        raise RuntimeError(
            f"historical repair input ends at {_iso(repair_through)}, before {_iso(through)}"
        )
    return load_repair_archives_for_preflight(cfg)
