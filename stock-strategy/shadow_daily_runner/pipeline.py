from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
from typing import Iterable
from zoneinfo import ZoneInfo

from prospective_shadow_v01.market_data_provider import ExistingDailyDataProvider

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
from .historical_repair import (
    load_previous_archives_for_preflight,
    load_repair_patch_archives,
)
from .normalize import (
    CATEGORIES,
    CATEGORY_BLANK_POSITIVE,
    UNIVERSE_FILTER_DESCRIPTION,
    ParsedSource,
    deterministic_zip,
    official_exclusion_evidence,
    parse_release_archive,
    parse_tpex,
    parse_twse,
)
from .preflight import (
    extend_source_coverage_from_direct_audit,
    run_historical_preflight,
)
from .sources import OfficialSourceClient, SourceSnapshot


@dataclass(frozen=True, slots=True)
class PreparedInputs:
    target_date: str
    trading_day: bool
    ready: bool
    archives: tuple[Path, ...]
    calendar_path: Path
    audit_path: Path
    audit: dict


def taipei_now(now: datetime | None = None, cfg: RunnerConfig = CFG) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo(cfg.timezone))


def _source_from_path(path: Path, request_url: str = "LOCAL_CLEAN_ARCHIVE") -> SourceSnapshot:
    payload = path.read_bytes()
    return SourceSnapshot(
        source=path.stem,
        request_url=request_url,
        retrieved_at_utc="LOCAL_IMMUTABLE",
        sha256=sha256_bytes(payload),
        path=path,
        payload=payload,
    )


def _archive_path(
    cfg: RunnerConfig,
    stem: str,
    source_hash: str,
    rows: Iterable[dict[str, str]],
    *,
    metadata_extra: dict | None = None,
) -> tuple[Path, dict]:
    member_name = f"{stem}.csv"
    payload = deterministic_zip(member_name, rows)
    clean_hash = sha256_bytes(payload)
    path = cfg.clean_dir / f"{stem}_{source_hash[:12]}_{clean_hash[:12]}.zip"
    write_immutable(path, payload)
    metadata = {
        "path": str(path),
        "filename": path.name,
        "member": member_name,
        "source_manifest_hash": source_hash,
        "sha256": clean_hash,
        "bytes": len(payload),
        "immutable": True,
        **(metadata_extra or {}),
    }
    write_immutable(
        path.with_suffix(path.suffix + ".metadata.json"),
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        ),
    )
    return path, metadata


def _release_weeks(cfg: RunnerConfig) -> list[int]:
    return [
        week
        for week in range(1, cfg.historical_release_last_week + 1)
        if week not in cfg.historical_release_missing_weeks
    ]


def _reconcile_positive_rows(
    unresolved: list[dict],
    client: OfficialSourceClient,
) -> tuple[dict[tuple[str, str], dict], list[dict]]:
    by_date: dict[str, set[str]] = defaultdict(set)
    for event in unresolved:
        if event["category"] == CATEGORY_BLANK_POSITIVE:
            by_date[event["date"]].add(event["code"])
    resolutions: dict[tuple[str, str], dict] = {}
    source_audit: list[dict] = []
    for day, wanted in sorted(by_date.items()):
        twse_snapshot = client.twse_eod(day, refresh=False)
        twse = parse_twse(twse_snapshot, day)
        source_audit.append(
            {
                "date": day,
                "market": "TWSE",
                "source": twse.source_metadata,
                "unresolved_rows": len(twse.errors),
            }
        )
        if twse.errors:
            raise RuntimeError(f"official TWSE reconciliation has invalid rows on {day}")
        evidence = official_exclusion_evidence(twse)
        resolutions.update({key: value for key, value in evidence.items() if key[1] in wanted})
        remaining = wanted - {key[1] for key in resolutions if key[0] == day}
        if remaining:
            tpex_snapshot = client.tpex_eod(day, refresh=False)
            tpex = parse_tpex(tpex_snapshot, day)
            source_audit.append(
                {
                    "date": day,
                    "market": "TPEX",
                    "source": tpex.source_metadata,
                    "unresolved_rows": len(tpex.errors),
                }
            )
            if tpex.errors:
                raise RuntimeError(f"official TPEx reconciliation has invalid rows on {day}")
            evidence = official_exclusion_evidence(tpex)
            resolutions.update(
                {key: value for key, value in evidence.items() if key[1] in remaining}
            )
    return resolutions, source_audit


def _prepare_history(
    client: OfficialSourceClient,
    cfg: RunnerConfig,
) -> tuple[list[Path], dict]:
    snapshots: list[tuple[int, SourceSnapshot]] = []
    first_pass: list[tuple[int, ParsedSource]] = []
    for week in _release_weeks(cfg):
        snapshot = client.release_week(2026, week)
        snapshots.append((week, snapshot))
        first_pass.append((week, parse_release_archive(snapshot)))

    first_events = [
        event
        for _, parsed in first_pass
        for event in (*parsed.excluded, *parsed.errors)
    ]
    first_counts = Counter(event["category"] for event in first_events)
    positive = [
        event for event in first_events if event["category"] == CATEGORY_BLANK_POSITIVE
    ]
    resolutions, reconciliation_sources = _reconcile_positive_rows(positive, client)

    clean_paths: list[Path] = []
    clean_metadata: list[dict] = []
    normalized_events: list[dict] = []
    unresolved: list[dict] = []
    for (week, snapshot) in snapshots:
        parsed = parse_release_archive(snapshot, resolutions)
        normalized_events.extend(parsed.excluded)
        unresolved.extend(parsed.errors)
        source_manifest_hash = hashlib.sha256(
            json.dumps(
                {
                    "source_sha256": snapshot.sha256,
                    "resolution_sha256": hashlib.sha256(
                        json.dumps(
                            sorted(
                                (day, code, value["source_sha256"])
                                for (day, code), value in resolutions.items()
                                if any(
                                    item["date"] == day and item["code"] == code
                                    for item in (*parsed.excluded, *parsed.errors)
                                )
                            ),
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        path, metadata = _archive_path(
            cfg,
            f"weekly_2026_W{week:02d}_clean",
            source_manifest_hash,
            parsed.rows,
        )
        clean_paths.append(path)
        clean_metadata.append(
            {
                **metadata,
                "release_week": week,
                "release_source": snapshot.metadata(),
                "tradable_rows": len(parsed.rows),
                "excluded_rows": len(parsed.excluded),
                "unresolved_rows": len(parsed.errors),
            }
        )

    counts = {category: first_counts.get(category, 0) for category in CATEGORIES}
    audit = {
        "scope": "eligible 0050 plus four-digit ordinary-stock proxy rows in W01-W35",
        "source_project": cfg.release_project_url,
        "source_lineage": [cfg.twse_eod_url, cfg.tpex_eod_url],
        "schema": ["date", "code", "name", "volume", "open", "high", "low", "close"],
        "raw_invalid_rows": len(first_events),
        "classification_counts": counts,
        "official_positive_volume_reconciliations": len(resolutions),
        "unresolved_invalid_rows": len(unresolved),
        "normalization_policy": {
            "blank_zero_or_no_volume": "excluded with audit reason",
            "blank_positive_volume": (
                "excluded only after exact date/code/volume reconciliation to an official "
                "regular-session close source that also reports all OHLC missing"
            ),
            "partial_ohlc": "fail closed",
            "parse_or_schema": "fail closed",
            "forward_fill": False,
            "silent_dropna": False,
        },
        "events": normalized_events,
        "unresolved_events": unresolved,
        "reconciliation_sources": reconciliation_sources,
        "archives": clean_metadata,
    }
    atomic_write_json(cfg.audit_dir / "historical_invalid_ohlcv_audit.json", audit)
    return clean_paths, audit


def _week_sessions(sessions: list[str], start: str, through: str) -> dict[tuple[int, int], list[str]]:
    grouped: dict[tuple[int, int], list[str]] = defaultdict(list)
    for item in sessions:
        compact = item.replace("-", "")
        if compact < start or compact > through:
            continue
        day = date.fromisoformat(item)
        iso = day.isocalendar()
        grouped[(iso.year, iso.week)].append(compact)
    return dict(sorted(grouped.items()))


def _prepare_direct_official(
    target: str,
    sessions: list[str],
    client: OfficialSourceClient,
    cfg: RunnerConfig,
) -> tuple[list[Path], dict]:
    archives: list[Path] = []
    archive_metadata: list[dict] = []
    failures: list[dict] = []
    market_daily_counts: dict[str, dict[str, int]] = {}
    exclusions: list[dict] = []
    groups = _week_sessions(sessions, cfg.direct_official_start, target)
    for (year, week), days in groups.items():
        week_rows: list[dict[str, str]] = []
        source_items: list[dict] = []
        week_failed = False
        for day in days:
            refresh = day == target
            try:
                twse = parse_twse(client.twse_eod(day, refresh=refresh), day)
                tpex = parse_tpex(client.tpex_eod(day, refresh=refresh), day)
            except Exception as exc:
                failures.append(
                    {
                        "date": day,
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                week_failed = True
                break
            if twse.errors or tpex.errors:
                failures.append(
                    {
                        "date": day,
                        "reason": "official source contains unresolved invalid eligible rows",
                        "twse_errors": list(twse.errors),
                        "tpex_errors": list(tpex.errors),
                    }
                )
                week_failed = True
                break
            combined: dict[tuple[str, str], dict[str, str]] = {}
            for parsed in (twse, tpex):
                exclusions.extend(parsed.excluded)
                for row in parsed.rows:
                    key = (row["date"], row["code"])
                    if key in combined:
                        failures.append(
                            {
                                "date": day,
                                "reason": f"duplicate code-date across TWSE/TPEx: {key}",
                            }
                        )
                        week_failed = True
                        break
                    combined[key] = row
                if week_failed:
                    break
            if week_failed:
                break
            week_rows.extend(combined[key] for key in sorted(combined))
            market_daily_counts[day] = {
                "TWSE": len(twse.rows),
                "TPEX": len(tpex.rows),
                "TOTAL": len(combined),
            }
            source_items.extend([twse.source_metadata, tpex.source_metadata])
        if week_failed:
            continue
        manifest_payload = json.dumps(
            [
                {
                    "source": item["source"],
                    "request_url": item["request_url"],
                    "sha256": item["sha256"],
                }
                for item in source_items
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest_hash = sha256_bytes(manifest_payload)
        through = days[-1]
        path, metadata = _archive_path(
            cfg,
            f"weekly_{year}_W{week:02d}_official_through_{through}",
            manifest_hash,
            sorted(week_rows, key=lambda row: (row["date"], row["code"])),
            metadata_extra={
                "normalization_type": "DIRECT_OFFICIAL_ESTABLISHED_RELEASE_UNIVERSE",
                "universe_filter": UNIVERSE_FILTER_DESCRIPTION,
            },
        )
        archives.append(path)
        archive_metadata.append(
            {
                **metadata,
                "year": year,
                "week": week,
                "sessions": days,
                "sources": source_items,
                "tradable_rows": len(week_rows),
            }
        )

    audit = {
        "direct_official_start": cfg.direct_official_start,
        "target_date": target,
        "official_sources": {
            "TWSE": cfg.twse_eod_url,
            "TPEX": cfg.tpex_eod_url,
        },
        "archives": archive_metadata,
        "market_daily_counts": market_daily_counts,
        "excluded_nontradable_rows": exclusions,
        "unresolved_invalid_rows": sum(
            len(item.get("twse_errors", [])) + len(item.get("tpex_errors", []))
            for item in failures
        ),
        "failures": failures,
    }
    atomic_write_json(cfg.audit_dir / f"official_eod_through_{target}.json", audit)
    return archives, audit


def _audit_clean_archives(
    archives: list[Path],
    sessions: list[str],
    target: str,
    direct_audit: dict,
    cfg: RunnerConfig,
) -> dict:
    rows_by_key: dict[tuple[str, str], dict[str, str]] = {}
    duplicate_keys: list[tuple[str, str]] = []
    invalid_rows: list[dict] = []
    archive_hashes: list[dict] = []
    for path in archives:
        before = sha256_file(path)
        parsed = parse_release_archive(_source_from_path(path))
        after = sha256_file(path)
        if before != after:
            raise RuntimeError(f"clean archive changed during audit: {path}")
        invalid_rows.extend(parsed.errors)
        invalid_rows.extend(
            event for event in parsed.excluded if not event.get("resolved", False)
        )
        for row in parsed.rows:
            key = (row["date"], row["code"])
            if key in rows_by_key:
                duplicate_keys.append(key)
            else:
                rows_by_key[key] = row
        archive_hashes.append(
            {"path": str(path), "bytes": path.stat().st_size, "sha256": before}
        )

    dates = sorted({day for day, _ in rows_by_key})
    expected = [
        item.replace("-", "")
        for item in sessions
        if cfg.data_start <= item.replace("-", "") <= target
    ]
    missing_sessions = sorted(set(expected) - set(dates))
    target_rows = [row for (day, _), row in rows_by_key.items() if day == target]
    has_target_0050 = (target, "0050") in rows_by_key
    daily_counts = Counter(day for day, _ in rows_by_key)
    prior_days = expected[-21:-1]
    prior_total = [daily_counts[day] for day in prior_days if daily_counts[day] > 0]
    total_median = statistics.median(prior_total) if prior_total else 0
    total_ratio = daily_counts[target] / total_median if total_median else 0.0

    direct_counts = direct_audit.get("market_daily_counts", {})
    market_coverage: dict[str, dict] = {}
    for market in ("TWSE", "TPEX"):
        prior = [
            counts[market]
            for day, counts in sorted(direct_counts.items())
            if day < target and counts.get(market, 0) > 0
        ][-20:]
        target_count = direct_counts.get(target, {}).get(market, 0)
        median = statistics.median(prior) if prior else 0
        ratio = target_count / median if median else 0.0
        market_coverage[market] = {
            "target_rows": target_count,
            "prior_median": median,
            "ratio": ratio,
            "pass": bool(
                target_count > 0
                and median > 0
                and ratio >= cfg.minimum_market_coverage_ratio
            ),
        }

    checks = {
        "duplicate_code_date_zero": len(duplicate_keys) == 0,
        "invalid_tradable_ohlcv_zero": len(invalid_rows) == 0,
        "target_date_present": target in dates,
        "target_0050_present": has_target_0050,
        "calendar_sessions_complete": len(missing_sessions) == 0,
        "target_total_coverage_pass": bool(
            daily_counts[target] > 0
            and total_median > 0
            and total_ratio >= cfg.minimum_market_coverage_ratio
        ),
        "target_twse_coverage_pass": market_coverage["TWSE"]["pass"],
        "target_tpex_coverage_pass": market_coverage["TPEX"]["pass"],
        "source_hashes_fixed": all(
            len(item["sha256"]) == 64 for item in archive_hashes
        ),
    }
    return {
        "checks": checks,
        "pass": all(checks.values()),
        "archives": archive_hashes,
        "duplicate_code_date_count": len(duplicate_keys),
        "duplicate_code_date_samples": duplicate_keys[:20],
        "invalid_tradable_rows": len(invalid_rows),
        "invalid_tradable_samples": invalid_rows[:20],
        "target_date": target,
        "target_rows": len(target_rows),
        "target_total_coverage": {
            "target_rows": daily_counts[target],
            "prior20_median": total_median,
            "ratio": total_ratio,
        },
        "market_coverage": market_coverage,
        "missing_calendar_sessions": missing_sessions,
        "first_market_date": dates[0] if dates else None,
        "last_market_date": dates[-1] if dates else None,
    }


def prepare_inputs(
    *,
    now: datetime | None = None,
    cfg: RunnerConfig = CFG,
    client: OfficialSourceClient | None = None,
) -> PreparedInputs:
    local = taipei_now(now, cfg)
    target = local.strftime("%Y%m%d")
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    cfg.audit_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    source_client = client or OfficialSourceClient(cfg)
    audit_path = cfg.audit_dir / f"readiness_{target}.json"
    audit: dict = {
        "schema_version": "1",
        "target_date": target,
        "started_at_utc": utc_timestamp(),
        "strategy_rules_modified": False,
        "broker_connection_attempted": False,
        "actual_orders": 0,
        "actual_fills": 0,
    }
    try:
        calendar_snapshot = source_client.calendar()
        news_snapshot = source_client.twse_news(refresh=True)
        ad_hoc_closures = collect_ad_hoc_closures(
            news_snapshot,
            local.year,
            previous_calendar_metadata_path=cfg.calendar_metadata_path,
        )
        calendar_bytes, calendar_metadata = build_trading_calendar(
            calendar_snapshot,
            local.year,
            ad_hoc_closures=ad_hoc_closures,
        )
        sessions = read_sessions(calendar_bytes)
        atomic_write_bytes(cfg.calendar_path, calendar_bytes)
        calendar_metadata.update(
            {
                "generated_at_utc": utc_timestamp(),
                "target_date": f"{target[:4]}-{target[4:6]}-{target[6:]}",
                "target_is_trading_day": (
                    f"{target[:4]}-{target[4:6]}-{target[6:]}" in sessions
                ),
            }
        )
        atomic_write_json(cfg.calendar_metadata_path, calendar_metadata)
        audit["trading_calendar"] = calendar_metadata
        target_iso = f"{target[:4]}-{target[4:6]}-{target[6:]}"
        trading_day = target_iso in sessions
        if not trading_day:
            audit.update(
                {
                    "status": "NON_TRADING_DAY",
                    "ready": False,
                    "finished_at_utc": utc_timestamp(),
                }
            )
            atomic_write_json(audit_path, audit)
            return PreparedInputs(
                target_date=target,
                trading_day=False,
                ready=False,
                archives=(),
                calendar_path=cfg.calendar_path,
                audit_path=audit_path,
                audit=audit,
            )

        prior_sessions = [
            item
            for item in sessions
            if cfg.data_start <= item.replace("-", "") < target
        ]
        if prior_sessions:
            prior = prior_sessions[-1]
            prior_archives = load_previous_archives_for_preflight(prior, cfg)
            before_download_health = run_historical_preflight(
                archives=prior_archives,
                calendar_path=cfg.calendar_path,
                source_coverage_path=cfg.historical_source_coverage_path,
                through_date=prior,
                cfg=cfg,
                audit_path=cfg.audit_dir / f"historical_preflight_before_{target}.json",
            )
            audit["historical_preflight_before_target_download"] = {
                "path": str(before_download_health.audit_path),
                "through_date": prior,
                "status": before_download_health.audit.get("status"),
                "checks": before_download_health.audit.get("checks", {}),
            }
            if not before_download_health.ready:
                failed = [
                    name
                    for name, value in before_download_health.audit.get("checks", {}).items()
                    if not value
                ]
                detail = ", ".join(failed) or before_download_health.audit.get(
                    "failure_reason", "unknown historical preflight failure"
                )
                raise RuntimeError(
                    f"historical preflight before target download failed closed: {detail}"
                )

        history_paths, history_audit = _prepare_history(source_client, cfg)
        direct_paths, direct_audit = _prepare_direct_official(
            target, sessions, source_client, cfg
        )
        repair_patch_paths = load_repair_patch_archives(cfg)
        archives = [*history_paths, *direct_paths, *repair_patch_paths]
        audit["historical_normalization"] = {
            key: value for key, value in history_audit.items() if key != "events"
        }
        audit["direct_official"] = direct_audit
        audit["historical_repair_patches"] = [
            {"path": str(path), "sha256": sha256_file(path)}
            for path in repair_patch_paths
        ]
        if history_audit["unresolved_invalid_rows"]:
            raise RuntimeError(
                f"historical normalization retains {history_audit['unresolved_invalid_rows']} "
                "unresolved invalid rows"
            )
        if direct_audit["failures"]:
            first = direct_audit["failures"][0]
            raise RuntimeError(
                f"official EOD not ready: date={first.get('date')} reason={first.get('reason')}"
            )
        coverage_manifest = extend_source_coverage_from_direct_audit(
            path=cfg.historical_source_coverage_path,
            calendar_path=cfg.calendar_path,
            direct_audit=direct_audit,
        )
        audit["historical_source_coverage"] = {
            "path": str(cfg.historical_source_coverage_path),
            "sha256": sha256_file(cfg.historical_source_coverage_path),
            "sessions": len(coverage_manifest["sessions"]),
            "scope": coverage_manifest["scope"],
        }
        health = run_historical_preflight(
            archives=archives,
            calendar_path=cfg.calendar_path,
            source_coverage_path=cfg.historical_source_coverage_path,
            through_date=target,
            cfg=cfg,
        )
        audit["historical_preflight"] = {
            "path": str(health.audit_path),
            "status": health.audit.get("status"),
            "checks": health.audit.get("checks", {}),
        }
        if not health.ready:
            failed = [
                name
                for name, value in health.audit.get("checks", {}).items()
                if not value
            ]
            detail = ", ".join(failed) or health.audit.get(
                "failure_reason", "unknown health-check failure"
            )
            raise RuntimeError(f"historical preflight failed closed: {detail}")
        clean_audit = _audit_clean_archives(
            archives, sessions, target, direct_audit, cfg
        )
        audit["clean_loader_audit"] = clean_audit
        if not clean_audit["pass"]:
            failed = [name for name, value in clean_audit["checks"].items() if not value]
            raise RuntimeError(f"clean loader readiness failed: {', '.join(failed)}")

        provider = ExistingDailyDataProvider(
            archives, trading_calendar_path=cfg.calendar_path
        )
        snapshot = provider.load_through(target)
        audit["prospective_provider_audit"] = snapshot.audit
        audit["prospective_input_manifest_hash"] = snapshot.input_manifest_hash
        active = {
            "target_date": target,
            "created_at_utc": utc_timestamp(),
            "archives": [str(path) for path in archives],
            "archive_hashes": [
                {"path": str(path), "sha256": sha256_file(path)} for path in archives
            ],
            "trading_calendar": str(cfg.calendar_path),
            "trading_calendar_sha256": sha256_file(cfg.calendar_path),
            "prospective_input_manifest_hash": snapshot.input_manifest_hash,
            "readiness_audit": str(audit_path),
        }
        audit.update(
            {
                "status": "READY",
                "ready": True,
                "active_inputs": active,
                "finished_at_utc": utc_timestamp(),
            }
        )
        atomic_write_json(cfg.active_inputs_path, active)
        atomic_write_json(audit_path, audit)
        return PreparedInputs(
            target_date=target,
            trading_day=True,
            ready=True,
            archives=tuple(archives),
            calendar_path=cfg.calendar_path,
            audit_path=audit_path,
            audit=audit,
        )
    except Exception as exc:
        audit.update(
            {
                "status": "READINESS_FAILED",
                "ready": False,
                "failure_type": type(exc).__name__,
                "failure_reason": str(exc),
                "finished_at_utc": utc_timestamp(),
            }
        )
        atomic_write_json(audit_path, audit)
        return PreparedInputs(
            target_date=target,
            trading_day=True,
            ready=False,
            archives=(),
            calendar_path=cfg.calendar_path,
            audit_path=audit_path,
            audit=audit,
        )


def public_result(result: PreparedInputs) -> dict:
    return {
        "target_date": result.target_date,
        "trading_day": result.trading_day,
        "ready": result.ready,
        "archives": [str(path) for path in result.archives],
        "trading_calendar": str(result.calendar_path),
        "audit_path": str(result.audit_path),
        "status": result.audit.get("status"),
        "failure_reason": result.audit.get("failure_reason"),
    }
