from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import urlencode

import numpy as np


TWSE_INSTITUTIONAL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_INSTITUTIONAL = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
TWSE_MARGIN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
TPEX_MARGIN = "https://www.tpex.org.tw/www/zh-tw/margin/balance"
SOURCE_ORDER = (
    "TWSE_INSTITUTIONAL",
    "TPEX_INSTITUTIONAL",
    "TWSE_MARGIN",
    "TPEX_MARGIN",
)


class OfficialRateLimitError(RuntimeError):
    """The official endpoint explicitly rejected or throttled a request."""


class OfficialResponseError(RuntimeError):
    """The official response could not be accepted as the requested market day."""


def _int(value: object) -> int:
    text = str(value).replace(",", "").strip()
    return int(text) if text not in {"", "--"} else 0


def _roc(date: int) -> str:
    text = str(date)
    return f"{int(text[:4]) - 1911:03d}/{text[4:6]}/{text[6:]}"


def _get(url: str, params: dict[str, object]) -> tuple[dict, str, str, bytes]:
    query = urlencode(params)
    full_url = f"{url}?{query}"
    marker = b"\n__CHIP_HTTP_STATUS__="
    command = [
        "/usr/bin/curl", "-sS", "-L", "--max-time", "45", "--get", url,
        "--write-out", "\n__CHIP_HTTP_STATUS__=%{http_code}",
    ]
    for key, value in params.items():
        command.extend(["--data-urlencode", f"{key}={value}"])
    completed = subprocess.run(command, capture_output=True, timeout=50)
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise OfficialResponseError(f"curl exit {completed.returncode}: {message}")
    body, separator, status_text = completed.stdout.rpartition(marker)
    if not separator:
        raise OfficialResponseError("missing HTTP status marker")
    try:
        http_status = int(status_text.strip())
    except ValueError as exc:
        raise OfficialResponseError("invalid HTTP status marker") from exc
    sample = body[:4096].decode("utf-8", errors="ignore").lower()
    if http_status in {307, 308, 403, 429, 503} or "anti-ddos" in sample or "access denied" in sample:
        raise OfficialRateLimitError(f"official endpoint rejected request with HTTP {http_status}")
    if http_status != 200:
        raise OfficialResponseError(f"unexpected HTTP {http_status}")
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if "html" in sample or "captcha" in sample:
            raise OfficialRateLimitError("official endpoint returned an HTML challenge") from exc
        raise OfficialResponseError("official endpoint did not return JSON") from exc
    if not isinstance(payload, dict):
        raise OfficialResponseError("official JSON response is not an object")
    return payload, hashlib.sha256(body).hexdigest(), full_url, body


def _twse_institutional(date: int, wanted: set[int]) -> tuple[dict, str, str, bytes, int]:
    payload, digest, url, raw = _get(TWSE_INSTITUTIONAL, {
        "response": "json", "date": str(date), "selectType": "ALLBUT0999",
    })
    if payload.get("stat") != "OK" or int(payload.get("date", 0)) != date:
        raise OfficialResponseError(f"TWSE institutional invalid response for {date}: {payload.get('stat')}")
    fields = payload["fields"]
    positions = (
        fields.index("證券代號"),
        next(i for i, field in enumerate(fields) if "外陸資買賣超股數(不含外資自營商)" in field),
        fields.index("投信買賣超股數"),
        fields.index("自營商買賣超股數"),
    )
    rows = {}
    source_rows = payload.get("data", [])
    if not source_rows:
        raise OfficialResponseError(f"TWSE institutional returned no market rows for {date}")
    for row in source_rows:
        code = row[positions[0]].strip()
        if code.isdigit() and int(code) in wanted:
            rows[int(code)] = tuple(_int(row[index]) for index in positions[1:])
    return rows, digest, url, raw, len(source_rows)


def _tpex_institutional(date: int, wanted: set[int]) -> tuple[dict, str, str, bytes, int]:
    payload, digest, url, raw = _get(TPEX_INSTITUTIONAL, {
        "l": "zh-tw", "o": "json", "se": "EW", "t": "D",
        "d": _roc(date), "s": "0,asc",
    })
    if payload.get("stat") != "ok" or int(payload.get("date", 0)) != date:
        raise OfficialResponseError(f"TPEx institutional invalid response for {date}: {payload.get('stat')}")
    tables = payload.get("tables", [])
    data = tables[0].get("data", []) if tables else []
    if not data:
        raise OfficialResponseError(f"TPEx institutional returned no market rows for {date}")
    rows = {}
    for row in data:
        code = row[0].strip()
        if code.isdigit() and int(code) in wanted:
            rows[int(code)] = (_int(row[10]), _int(row[13]), _int(row[22]))
    return rows, digest, url, raw, len(data)


def _twse_margin(date: int, wanted: set[int]) -> tuple[dict, str, str, bytes, int]:
    payload, digest, url, raw = _get(TWSE_MARGIN, {
        "response": "json", "date": str(date), "selectType": "ALL",
    })
    if payload.get("stat") != "OK" or int(payload.get("date", 0)) != date:
        raise OfficialResponseError(f"TWSE margin invalid response for {date}: {payload.get('stat')}")
    table = next((item for item in payload.get("tables", []) if "融資融券彙總" in item.get("title", "")), None)
    if table is None:
        raise OfficialResponseError(f"TWSE margin detail missing for {date}")
    source_rows = table.get("data", [])
    if not source_rows:
        raise OfficialResponseError(f"TWSE margin returned no market rows for {date}")
    rows = {}
    for row in source_rows:
        code = row[0].strip()
        if code.isdigit() and int(code) in wanted:
            rows[int(code)] = (_int(row[6]), _int(row[12]))
    return rows, digest, url, raw, len(source_rows)


def _tpex_margin(date: int, wanted: set[int]) -> tuple[dict, str, str, bytes, int]:
    text = str(date)
    payload, digest, url, raw = _get(TPEX_MARGIN, {
        "date": f"{text[:4]}/{text[4:6]}/{text[6:]}", "id": "", "response": "json",
    })
    if payload.get("stat") != "ok" or int(payload.get("date", 0)) != date:
        raise OfficialResponseError(f"TPEx margin invalid response for {date}: {payload.get('stat')}")
    tables = payload.get("tables", [])
    data = tables[0].get("data", []) if tables else []
    if not data:
        raise OfficialResponseError(f"TPEx margin returned no market rows for {date}")
    rows = {}
    for row in data:
        code = row[0].strip()
        if code.isdigit() and int(code) in wanted:
            rows[int(code)] = (_int(row[6]), _int(row[14]))
    return rows, digest, url, raw, len(data)


def fetch_date(date: int, wanted: set[int]) -> dict:
    fetches = {
        "TWSE_INSTITUTIONAL": _twse_institutional,
        "TPEX_INSTITUTIONAL": _tpex_institutional,
        "TWSE_MARGIN": _twse_margin,
        "TPEX_MARGIN": _tpex_margin,
    }
    result = {"date": date, "retrieved_at_utc": datetime.now(timezone.utc).isoformat(), "sources": {}}
    institutional: dict[int, tuple[int, int, int, str]] = {}
    margin: dict[int, tuple[int, int, str]] = {}
    for name, function in fetches.items():
        rows, digest, url, _raw, source_rows = function(date, wanted)
        result["sources"][name] = {
            "sha256": digest,
            "url": url,
            "source_rows": source_rows,
            "matched_rows": len(rows),
        }
        target = institutional if "INSTITUTIONAL" in name else margin
        market = "TWSE" if name.startswith("TWSE") else "TPEX"
        for code, values in rows.items():
            if code in target:
                raise RuntimeError(f"duplicate market code {code} on {date} for {name}")
            target[code] = (*values, market)
    result["institutional"] = institutional
    result["margin"] = margin
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise RuntimeError(f"immutable cache collision: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise RuntimeError(f"immutable cache collision: {path}")
    finally:
        temporary.unlink(missing_ok=True)


def _append_manifest(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _canonical_json(event) + b"\n"
    with path.open("ab", buffering=0) as handle:
        handle.write(line)
        os.fsync(handle.fileno())


def _manifest_state(path: Path) -> tuple[set[tuple[str, int]], dict[tuple[str, int], int]]:
    complete: set[tuple[str, int]] = set()
    failures: dict[tuple[str, int], int] = {}
    if not path.exists():
        return complete, failures
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            event = json.loads(line)
            key = (str(event["source"]), int(event["date"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid download manifest line {number}") from exc
        if event.get("request_status") in {"COMPLETE", "CACHE_RECOVERED"}:
            complete.add(key)
        elif event.get("request_status") in {"RATE_LIMITED", "REQUEST_ERROR", "PARSE_ERROR"}:
            failures[key] = failures.get(key, 0) + 1
    return complete, failures


def _cache_paths(cache_dir: Path, source: str, date: int) -> tuple[Path, Path]:
    entry = cache_dir / "entries" / source / str(date)
    return (
        entry / "raw.json",
        entry / "parsed.json",
    )


def _write_cache_pair(
    cache_dir: Path,
    source: str,
    date: int,
    raw: bytes,
    parsed: bytes,
) -> None:
    raw_path, parsed_path = _cache_paths(cache_dir, source, date)
    entry = raw_path.parent
    if entry.exists():
        cached = _load_cached(cache_dir, source, date)
        if cached is None:
            raise RuntimeError(f"incomplete immutable cache pair for {source} {date}")
        existing, metadata = cached
        if metadata["raw_file_hash"] != _sha256_bytes(raw) or metadata["parsed_file_hash"] != _sha256_bytes(parsed):
            raise RuntimeError(f"immutable cache collision for {source} {date}")
        return
    entry.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{date}.", dir=entry.parent))
    try:
        _write_immutable(temporary / "raw.json", raw)
        _write_immutable(temporary / "parsed.json", parsed)
        try:
            temporary.rename(entry)
        except FileExistsError:
            cached = _load_cached(cache_dir, source, date)
            if cached is None:
                raise RuntimeError(f"concurrent incomplete cache pair for {source} {date}")
            _, metadata = cached
            if metadata["raw_file_hash"] != _sha256_bytes(raw) or metadata["parsed_file_hash"] != _sha256_bytes(parsed):
                raise RuntimeError(f"concurrent immutable cache collision for {source} {date}")
    finally:
        if temporary.exists():
            for child in temporary.iterdir():
                child.unlink()
            temporary.rmdir()


def _source_metadata(source: str) -> tuple[str, str]:
    return (
        "TWSE" if source.startswith("TWSE") else "TPEX",
        "INSTITUTIONAL" if source.endswith("INSTITUTIONAL") else "MARGIN_SHORT",
    )


def _serialize_parsed(
    source: str,
    date: int,
    rows: dict,
    raw_sha256: str,
    url: str,
    source_rows: int,
    wanted_count: int,
) -> dict:
    market, family = _source_metadata(source)
    names = ("foreign", "investment_trust", "dealer") if family == "INSTITUTIONAL" else ("margin_balance", "short_balance")
    normalized_rows = []
    for code, values in sorted(rows.items()):
        normalized_rows.append({"stock_code": int(code), **{name: int(value) for name, value in zip(names, values)}})
    return {
        "schema_version": 1,
        "source": source,
        "market": market,
        "feature_family": family,
        "date": date,
        "source_url": url,
        "source_row_count": source_rows,
        "wanted_code_count": wanted_count,
        "matched_row_count": len(normalized_rows),
        "raw_sha256": raw_sha256,
        "rows": normalized_rows,
    }


def _load_cached(cache_dir: Path, source: str, date: int) -> tuple[dict, dict] | None:
    raw_path, parsed_path = _cache_paths(cache_dir, source, date)
    if not raw_path.exists() and not parsed_path.exists():
        return None
    if not raw_path.exists() or not parsed_path.exists():
        raise RuntimeError(f"incomplete immutable cache pair for {source} {date}")
    raw = raw_path.read_bytes()
    parsed_bytes = parsed_path.read_bytes()
    try:
        parsed = json.loads(parsed_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid cached parsed JSON for {source} {date}") from exc
    raw_sha = _sha256_bytes(raw)
    if parsed.get("source") != source or int(parsed.get("date", 0)) != date:
        raise RuntimeError(f"cached source/date mismatch for {source} {date}")
    if parsed.get("raw_sha256") != raw_sha:
        raise RuntimeError(f"cached raw hash mismatch for {source} {date}")
    if int(parsed.get("source_row_count", 0)) <= 0:
        raise RuntimeError(f"cached official source is empty for {source} {date}")
    return parsed, {
        "raw_file": str(raw_path.relative_to(cache_dir)),
        "parsed_file": str(parsed_path.relative_to(cache_dir)),
        "raw_file_hash": raw_sha,
        "parsed_file_hash": _sha256_bytes(parsed_bytes),
    }


def _fetch_source(source: str, date: int, wanted: set[int]) -> tuple[dict, bytes]:
    functions = {
        "TWSE_INSTITUTIONAL": _twse_institutional,
        "TPEX_INSTITUTIONAL": _tpex_institutional,
        "TWSE_MARGIN": _twse_margin,
        "TPEX_MARGIN": _tpex_margin,
    }
    rows, digest, url, raw, source_rows = functions[source](date, wanted)
    parsed = _serialize_parsed(source, date, rows, digest, url, source_rows, len(wanted))
    return parsed, raw


def _progress_payload(
    requested: list[int],
    cache_dir: Path,
    stop_reason: str | None = None,
) -> dict:
    complete_sources = 0
    complete_dates = 0
    for date in requested:
        date_complete = True
        for source in SOURCE_ORDER:
            try:
                cached = _load_cached(cache_dir, source, date)
            except RuntimeError:
                cached = None
            complete_sources += int(cached is not None)
            date_complete = date_complete and cached is not None
        complete_dates += int(date_complete)
    return {
        "status": "COMPLETE" if complete_dates == len(requested) else "PARTIAL",
        "official_only": True,
        "updated_at_utc": _utc_now(),
        "expected_dates": len(requested),
        "expected_source_date_pairs": len(requested) * len(SOURCE_ORDER),
        "complete_dates": complete_dates,
        "complete_source_date_pairs": complete_sources,
        "missing_source_date_pairs": len(requested) * len(SOURCE_ORDER) - complete_sources,
        "stop_reason": stop_reason,
    }


def _write_progress(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    temporary.replace(path)


def _assemble_complete_store(
    requested: list[int],
    needed_codes_by_date: dict[int, set[int]],
    cache_dir: Path,
    output: Path,
    manifest_path: Path,
) -> dict:
    records = []
    date_payloads = []
    duplicate_market_code_rows = 0
    for date in requested:
        parsed_by_source = {}
        metadata_by_source = {}
        for source in SOURCE_ORDER:
            cached = _load_cached(cache_dir, source, date)
            if cached is None:
                raise RuntimeError(f"coverage gate failed: missing {source} {date}")
            parsed_by_source[source], metadata_by_source[source] = cached
        institutional = {}
        margin = {}
        for source in SOURCE_ORDER:
            parsed = parsed_by_source[source]
            market_value = 1 if parsed["market"] == "TWSE" else 2
            target = institutional if parsed["feature_family"] == "INSTITUTIONAL" else margin
            for row in parsed["rows"]:
                code = int(row["stock_code"])
                if code in target:
                    duplicate_market_code_rows += 1
                    continue
                if parsed["feature_family"] == "INSTITUTIONAL":
                    target[code] = (row["foreign"], row["investment_trust"], row["dealer"], market_value)
                else:
                    target[code] = (row["margin_balance"], row["short_balance"], market_value)
        if duplicate_market_code_rows:
            raise RuntimeError(f"coverage gate failed: duplicate market code rows={duplicate_market_code_rows}")
        wanted = needed_codes_by_date.get(date, set())
        for code in sorted(wanted):
            inst = institutional.get(code)
            marg = margin.get(code)
            records.append((
                date, code,
                np.nan if inst is None else inst[0],
                np.nan if inst is None else inst[1],
                np.nan if inst is None else inst[2],
                np.nan if marg is None else marg[0],
                np.nan if marg is None else marg[1],
                0 if inst is None else inst[3],
                0 if marg is None else marg[2],
            ))
        date_payloads.append({
            "date": date,
            "wanted_codes": len(wanted),
            "sources": {
                source: {
                    "url": parsed_by_source[source]["source_url"],
                    "source_rows": parsed_by_source[source]["source_row_count"],
                    "matched_rows": parsed_by_source[source]["matched_row_count"],
                    **metadata_by_source[source],
                }
                for source in SOURCE_ORDER
            },
        })
    dtype = np.dtype([
        ("source_date", "<i4"), ("stock_code", "<i4"),
        ("foreign", "<f8"), ("investment_trust", "<f8"), ("dealer", "<f8"),
        ("margin_balance", "<f8"), ("short_balance", "<f8"),
        ("institutional_market", "u1"), ("margin_market", "u1"),
    ])
    table = np.asarray(records, dtype=dtype)
    keys = np.column_stack((table["source_date"], table["stock_code"]))
    duplicate_stock_dates = len(keys) - len(np.unique(keys, axis=0))
    if duplicate_stock_dates:
        raise RuntimeError(f"coverage gate failed: duplicate stock-date={duplicate_stock_dates}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".chip_daily_store.", suffix=".npz", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        np.savez_compressed(temporary, chip_daily=table)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        _write_immutable(output, temporary.read_bytes())
    finally:
        temporary.unlink(missing_ok=True)
    manifest = {
        "status": "COMPLETE",
        "official_only": True,
        "dates_requested": len(requested),
        "source_date_pairs": len(requested) * len(SOURCE_ORDER),
        "records": len(table),
        "coverage_gate": {
            "all_expected_dates_complete": True,
            "twse_coverage_complete": True,
            "tpex_coverage_complete": True,
            "duplicate_stock_date": duplicate_stock_dates,
            "unresolved_parse_errors": 0,
            "pit_lag_mapping_complete": set(requested) == set(needed_codes_by_date),
        },
        "date_payloads": date_payloads,
    }
    if not manifest["coverage_gate"]["pit_lag_mapping_complete"]:
        raise RuntimeError("coverage gate failed: PIT lag mapping is incomplete")
    _write_immutable(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def download_official_chip_store(
    dates: np.ndarray,
    needed_codes_by_date: dict[int, set[int]],
    output: Path,
    manifest_path: Path,
    cache_dir: Path,
    request_interval_seconds: float = 5.0,
    max_attempts: int = 2,
    initial_backoff_seconds: float = 30.0,
    max_source_requests: int | None = 20,
) -> dict:
    if (
        request_interval_seconds < 0
        or max_attempts < 1
        or initial_backoff_seconds < 0
        or (max_source_requests is not None and max_source_requests < 0)
    ):
        raise ValueError("invalid bounded download settings")
    if output.exists() != manifest_path.exists():
        raise RuntimeError("immutable final source store/manifest pair is incomplete")
    if output.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if payload.get("status") != "COMPLETE":
            raise RuntimeError("existing final source manifest is not COMPLETE")
        return payload

    requested = sorted(int(value) for value in np.unique(dates))
    if set(requested) != set(needed_codes_by_date):
        raise RuntimeError("requested dates differ from PIT needed-code mapping")
    persistent_manifest = cache_dir / "download_manifest.jsonl"
    progress_path = cache_dir / "download_progress.json"
    manifest_complete, prior_failures = _manifest_state(persistent_manifest)
    network_requests = 0
    stop_reason = None

    for date in requested:
        wanted = needed_codes_by_date.get(date, set())
        for source in SOURCE_ORDER:
            cached = _load_cached(cache_dir, source, date)
            key = (source, date)
            if cached is not None:
                if key not in manifest_complete:
                    parsed, files = cached
                    _append_manifest(persistent_manifest, {
                        "source": source,
                        "market": parsed["market"],
                        "feature_family": parsed["feature_family"],
                        "date": date,
                        "request_status": "CACHE_RECOVERED",
                        "row_count": parsed["source_row_count"],
                        "matched_row_count": parsed["matched_row_count"],
                        "retrieval_timestamp_utc": _utc_now(),
                        "retry_count": prior_failures.get(key, 0),
                        **files,
                    })
                    manifest_complete.add(key)
                continue
            if max_source_requests is not None and network_requests >= max_source_requests:
                stop_reason = "BOUNDED_BATCH_LIMIT_REACHED"
                progress = _progress_payload(requested, cache_dir, stop_reason)
                _write_progress(progress_path, progress)
                return progress

            market, family = _source_metadata(source)
            for attempt in range(max_attempts):
                if max_source_requests is not None and network_requests >= max_source_requests:
                    stop_reason = "BOUNDED_BATCH_LIMIT_REACHED"
                    progress = _progress_payload(requested, cache_dir, stop_reason)
                    _write_progress(progress_path, progress)
                    return progress
                retry_count = prior_failures.get(key, 0) + attempt
                _append_manifest(persistent_manifest, {
                    "source": source, "market": market, "feature_family": family,
                    "date": date, "request_status": "REQUESTING",
                    "retrieval_timestamp_utc": _utc_now(), "retry_count": retry_count,
                })
                network_requests += 1
                try:
                    parsed, raw = _fetch_source(source, date, wanted)
                    raw_path, parsed_path = _cache_paths(cache_dir, source, date)
                    parsed_bytes = _canonical_json(parsed)
                    _write_cache_pair(cache_dir, source, date, raw, parsed_bytes)
                    files = {
                        "raw_file": str(raw_path.relative_to(cache_dir)),
                        "parsed_file": str(parsed_path.relative_to(cache_dir)),
                        "raw_file_hash": _sha256_bytes(raw),
                        "parsed_file_hash": _sha256_bytes(parsed_bytes),
                    }
                    _append_manifest(persistent_manifest, {
                        "source": source, "market": market, "feature_family": family,
                        "date": date, "request_status": "COMPLETE",
                        "row_count": parsed["source_row_count"],
                        "matched_row_count": parsed["matched_row_count"],
                        "retrieval_timestamp_utc": _utc_now(), "retry_count": retry_count,
                        **files,
                    })
                    manifest_complete.add(key)
                    _write_progress(progress_path, _progress_payload(requested, cache_dir))
                    if request_interval_seconds:
                        time.sleep(request_interval_seconds)
                    break
                except OfficialRateLimitError as exc:
                    status = "RATE_LIMITED"
                    error = f"{type(exc).__name__}: {exc}"
                except Exception as exc:
                    status = "PARSE_ERROR" if isinstance(exc, (KeyError, IndexError, ValueError, OfficialResponseError)) else "REQUEST_ERROR"
                    error = f"{type(exc).__name__}: {exc}"
                _append_manifest(persistent_manifest, {
                    "source": source, "market": market, "feature_family": family,
                    "date": date, "request_status": status,
                    "retrieval_timestamp_utc": _utc_now(), "retry_count": retry_count,
                    "error": error,
                })
                if attempt + 1 < max_attempts:
                    time.sleep(initial_backoff_seconds * (2 ** attempt))
                else:
                    stop_reason = f"{status}:{source}:{date}"
                    progress = _progress_payload(requested, cache_dir, stop_reason)
                    _write_progress(progress_path, progress)
                    return progress

    progress = _progress_payload(requested, cache_dir)
    _write_progress(progress_path, progress)
    if progress["status"] != "COMPLETE":
        return progress
    return _assemble_complete_store(requested, needed_codes_by_date, cache_dir, output, manifest_path)


def phase0_audit_rows() -> list[dict]:
    common = {
        "earliest_date": "2020-01-02",
        "latest_date": "2025-12-31",
        "frequency": "DAILY_TRADING_SESSION",
        "usable_on_same_day_T": False,
        "required_lag_sessions": 1,
        "coverage_pct": None,
        "missing_pct": None,
    }
    rows = []
    for raw in ("foreign_net_buy_sell", "investment_trust_net_buy_sell", "dealer_net_buy_sell"):
        rows.append({
            "feature_family": "INSTITUTIONAL", "raw_field": raw,
            "source": "TWSE_T86_AND_TPEX_3ITRADE_HEDGE", "official_or_third_party": "OFFICIAL",
            **common,
            "publication_timing": "TWSE files produced about 18:00/20:00; TPEx post-close table has no retained per-row published_at",
            "PIT_status": "PASS_CONSERVATIVE_NEXT_SESSION",
            "decision": "PIT_USABLE_WITH_LAG",
        })
    for raw in ("margin_balance", "margin_balance_change", "short_balance", "short_balance_change", "short_margin_ratio"):
        rows.append({
            "feature_family": "MARGIN_SHORT", "raw_field": raw,
            "source": "TWSE_MI_MARGN_AND_TPEX_MARGIN_BALANCE", "official_or_third_party": "OFFICIAL",
            **common,
            "publication_timing": "TWSE compiled after member processing and published in the evening; TPEx post-close table; historical per-row published_at absent",
            "PIT_status": "PASS_CONSERVATIVE_NEXT_SESSION",
            "decision": "PIT_USABLE_WITH_LAG",
        })
    for family, raw, source, status, decision, timing in (
        ("SECURITIES_LENDING", "lending_balance_and_change", "TWSE_TPEX_OFFICIAL_PAGES", "HISTORICAL_FIELD_AND_RELEASE_TIME_NOT_VERIFIED_FOR_BOTH_MARKETS", "NOT_TESTED_DATA_UNAVAILABLE", "not established"),
        ("TDCC_OWNERSHIP", "holder_tier_distribution", "TDCC_OPEN_DATA", "FULL_2020_2025_HISTORY_AND_PUBLISHED_AT_UNAVAILABLE", "REJECTED_PIT_UNSAFE", "weekly cutoff is not publication time"),
        ("BROKER_BRANCH", "branch_concentration", "NO_VERIFIED_OFFICIAL_HISTORICAL_SOURCE", "OFFICIAL_PIT_ARCHIVE_UNAVAILABLE", "NOT_TESTED_DATA_UNAVAILABLE", "not established"),
    ):
        rows.append({
            "feature_family": family, "raw_field": raw, "source": source,
            "official_or_third_party": "OFFICIAL" if family != "BROKER_BRANCH" else "UNAVAILABLE",
            "earliest_date": None, "latest_date": None, "frequency": None,
            "publication_timing": timing, "usable_on_same_day_T": False,
            "required_lag_sessions": None, "coverage_pct": 0.0, "missing_pct": 1.0,
            "PIT_status": status, "decision": decision,
        })
    return rows


__all__ = ["download_official_chip_store", "phase0_audit_rows", "fetch_date"]
