from __future__ import annotations

from collections import Counter
import csv
from dataclasses import dataclass
from datetime import datetime
import io
import json
import math
import re
from typing import Iterable
import zipfile

from .sources import SourceSnapshot, decode_json


COLUMNS = ("date", "code", "name", "volume", "open", "high", "low", "close")
OHLC = ("open", "high", "low", "close")
ORDINARY_STOCK = re.compile(r"^[1-9][0-9]{3}$")
# Keep the direct-official layer identical to the established W01-W35 archive
# producer.  This is a compatibility rule, not an authoritative security
# master: the producer's broad suffix regex also excludes ordinary companies
# whose legal names happen to end in `特`.  Removing that legacy exclusion only
# after W35 would silently expand the frozen research universe.
INELIGIBLE_NAME_SUFFIX = re.compile(
    r"(?:N|DR|R1|R2|特|售[0-9]{2}|購[0-9]{2})$"
)
UNIVERSE_FILTER_DESCRIPTION = (
    "code is 0050 or ^[1-9][0-9]{3}$; name suffix is not "
    "N, DR, R1, R2, 特, 售[0-9]{2}, or 購[0-9]{2}"
)
MISSING = {"", "--", "---", "----", "null", "none", "nan"}

CATEGORY_BLANK_ZERO = "blank_ohlc_zero_or_no_volume"
CATEGORY_BLANK_POSITIVE = "blank_ohlc_positive_volume"
CATEGORY_PARTIAL = "partial_ohlc_missing"
CATEGORY_PARSE = "parse_or_schema_error"
CATEGORIES = (
    CATEGORY_BLANK_ZERO,
    CATEGORY_BLANK_POSITIVE,
    CATEGORY_PARTIAL,
    CATEGORY_PARSE,
)


def eligible_code(value: object) -> bool:
    code = str(value or "").strip()
    return code == "0050" or ORDINARY_STOCK.fullmatch(code) is not None


def eligible_security(code_value: object, name_value: object) -> bool:
    """Return the established release-producer universe membership proxy."""

    return eligible_code(code_value) and INELIGIBLE_NAME_SUFFIX.search(
        str(name_value or "").strip()
    ) is None


def clean_text(value: object) -> str:
    return str(value if value is not None else "").replace(",", "").strip()


def is_missing(value: object) -> bool:
    return clean_text(value).lower() in MISSING


def normalize_date(value: object) -> str:
    result = clean_text(value).replace("/", "").replace("-", "")
    if len(result) != 8 or not result.isdigit():
        raise ValueError(f"invalid date: {value!r}")
    datetime.strptime(result, "%Y%m%d")
    return result


def parse_volume(value: object) -> int | None:
    if is_missing(value):
        return None
    text = clean_text(value)
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(f"invalid volume: {value!r}") from exc
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        raise ValueError(f"invalid volume: {value!r}")
    return int(number)


def parse_price(value: object, field: str) -> float:
    text = clean_text(value)
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"invalid {field}: {value!r}")
    return number


@dataclass(frozen=True, slots=True)
class ParsedSource:
    rows: tuple[dict[str, str], ...]
    excluded: tuple[dict, ...]
    errors: tuple[dict, ...]
    source_metadata: dict
    market: str | None
    requested_date: str | None

    @property
    def category_counts(self) -> dict[str, int]:
        counts = Counter(row["category"] for row in (*self.excluded, *self.errors))
        return {category: counts.get(category, 0) for category in CATEGORIES}


def _event(
    *,
    source: str,
    line: int,
    category: str,
    reason: str,
    row: dict[str, object] | None = None,
    resolved: bool,
    resolution: str = "",
    market: str | None = None,
) -> dict:
    item = row or {}
    return {
        "source": source,
        "line": line,
        "date": clean_text(item.get("date")),
        "code": clean_text(item.get("code")),
        "name": clean_text(item.get("name")),
        "volume": clean_text(item.get("volume")),
        "open": clean_text(item.get("open")),
        "high": clean_text(item.get("high")),
        "low": clean_text(item.get("low")),
        "close": clean_text(item.get("close")),
        "market": market or "",
        "category": category,
        "reason": reason,
        "resolved": resolved,
        "resolution": resolution,
    }


def _validate_row(
    source: str,
    line: int,
    source_row: dict[str, object],
    *,
    market: str | None,
    positive_resolution: dict | None = None,
    direct_official_regular_session: bool = False,
) -> tuple[dict[str, str] | None, dict | None]:
    row = {field: source_row.get(field, "") for field in COLUMNS}
    try:
        day = normalize_date(row["date"])
    except (TypeError, ValueError) as exc:
        return None, _event(
            source=source,
            line=line,
            category=CATEGORY_PARSE,
            reason=str(exc),
            row=row,
            resolved=False,
            market=market,
        )
    row["date"] = day
    code = clean_text(row["code"])
    name = str(row["name"] or "").strip()
    if not eligible_security(code, name):
        return None, None
    row["code"] = code
    row["name"] = name

    try:
        volume = parse_volume(row["volume"])
    except ValueError as exc:
        return None, _event(
            source=source,
            line=line,
            category=CATEGORY_PARSE,
            reason=str(exc),
            row=row,
            resolved=False,
            market=market,
        )
    missing = [is_missing(row[field]) for field in OHLC]
    if all(missing):
        category = CATEGORY_BLANK_POSITIVE if (volume or 0) > 0 else CATEGORY_BLANK_ZERO
        if category == CATEGORY_BLANK_ZERO:
            return None, _event(
                source=source,
                line=line,
                category=category,
                reason="official/source row has no OHLC and zero or no aggregate volume",
                row=row,
                resolved=True,
                resolution="EXCLUDE_NONTRADABLE_NO_PRICE_BAR",
                market=market,
            )
        evidence = positive_resolution or {}
        evidence_matches = (
            evidence.get("date") == day
            and evidence.get("code") == code
            and int(evidence.get("volume", -1)) == volume
            and evidence.get("all_ohlc_missing") is True
        )
        if direct_official_regular_session or evidence_matches:
            reference = evidence.get("source_sha256", "direct official response")
            return None, _event(
                source=source,
                line=line,
                category=category,
                reason=(
                    "official regular-session close source confirms no OHLC; "
                    "aggregate volume is retained only in the exclusion audit"
                ),
                row=row,
                resolved=True,
                resolution=f"OFFICIAL_NO_REGULAR_SESSION_PRICE:{reference}",
                market=market or evidence.get("market"),
            )
        return None, _event(
            source=source,
            line=line,
            category=category,
            reason="positive aggregate volume with no OHLC lacks official reconciliation",
            row=row,
            resolved=False,
            resolution="FAIL_CLOSED_PENDING_OFFICIAL_RECONCILIATION",
            market=market,
        )
    if any(missing):
        return None, _event(
            source=source,
            line=line,
            category=CATEGORY_PARTIAL,
            reason="only part of OHLC is missing",
            row=row,
            resolved=False,
            market=market,
        )

    try:
        prices = {field: parse_price(row[field], field) for field in OHLC}
    except ValueError as exc:
        return None, _event(
            source=source,
            line=line,
            category=CATEGORY_PARSE,
            reason=str(exc),
            row=row,
            resolved=False,
            market=market,
        )
    if (
        prices["high"] < max(prices["open"], prices["close"])
        or prices["low"] > min(prices["open"], prices["close"])
        or prices["high"] < prices["low"]
    ):
        return None, _event(
            source=source,
            line=line,
            category=CATEGORY_PARSE,
            reason="inconsistent OHLC ordering",
            row=row,
            resolved=False,
            market=market,
        )
    if volume is None:
        return None, _event(
            source=source,
            line=line,
            category=CATEGORY_PARSE,
            reason="tradable OHLC row has no volume",
            row=row,
            resolved=False,
            market=market,
        )
    valid = {
        "date": day,
        "code": code,
        "name": str(row["name"]),
        "volume": str(volume),
        "open": clean_text(row["open"]),
        "high": clean_text(row["high"]),
        "low": clean_text(row["low"]),
        "close": clean_text(row["close"]),
    }
    return valid, None


def _deduplicate(
    rows: Iterable[dict[str, str]], source: str
) -> tuple[tuple[dict[str, str], ...], tuple[dict, ...]]:
    ordered: dict[tuple[str, str], dict[str, str]] = {}
    errors: list[dict] = []
    for line, row in enumerate(rows, start=2):
        key = (row["date"], row["code"])
        if key in ordered:
            errors.append(
                _event(
                    source=source,
                    line=line,
                    category=CATEGORY_PARSE,
                    reason=f"duplicate code-date: {key[1]} {key[0]}",
                    row=row,
                    resolved=False,
                )
            )
        else:
            ordered[key] = row
    return tuple(ordered[key] for key in sorted(ordered)), tuple(errors)


def parse_release_archive(
    snapshot: SourceSnapshot,
    resolutions: dict[tuple[str, str], dict] | None = None,
) -> ParsedSource:
    try:
        archive = zipfile.ZipFile(io.BytesIO(snapshot.payload))
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"bad release archive: {snapshot.request_url}") from exc
    with archive:
        members = sorted(name for name in archive.namelist() if name.lower().endswith(".csv"))
        if len(members) != 1:
            raise RuntimeError(f"release archive must contain exactly one CSV: {members}")
        content = archive.read(members[0]).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content, newline=""))
    missing_columns = set(COLUMNS) - set(reader.fieldnames or ())
    if missing_columns:
        raise RuntimeError(f"release archive missing columns: {sorted(missing_columns)}")
    valid: list[dict[str, str]] = []
    excluded: list[dict] = []
    errors: list[dict] = []
    for line, source_row in enumerate(reader, start=2):
        code = clean_text(source_row.get("code"))
        if not eligible_security(code, source_row.get("name")):
            continue
        key_text = (
            clean_text(source_row.get("date")).replace("-", "").replace("/", ""),
            code,
        )
        normalized, event = _validate_row(
            f"{snapshot.path.name}:{members[0]}",
            line,
            source_row,
            market=None,
            positive_resolution=(resolutions or {}).get(key_text),
        )
        if normalized is not None:
            valid.append(normalized)
        elif event is not None:
            (excluded if event["resolved"] else errors).append(event)
    deduped, duplicate_errors = _deduplicate(valid, snapshot.path.name)
    errors.extend(duplicate_errors)
    return ParsedSource(
        rows=deduped,
        excluded=tuple(excluded),
        errors=tuple(errors),
        source_metadata={**snapshot.metadata(), "archive_member": members[0]},
        market=None,
        requested_date=None,
    )


def parse_twse(snapshot: SourceSnapshot, requested_date: str) -> ParsedSource:
    payload = decode_json(snapshot)
    if not isinstance(payload, dict):
        raise RuntimeError("TWSE response is not an object")
    if payload.get("stat") != "OK" or str(payload.get("date")) != requested_date:
        raise RuntimeError(
            f"TWSE EOD not ready for {requested_date}: "
            f"stat={payload.get('stat')!r}, date={payload.get('date')!r}"
        )
    required = {"證券代號", "證券名稱", "成交股數", "開盤價", "最高價", "最低價", "收盤價"}
    matches = [
        table
        for table in payload.get("tables", [])
        if required.issubset(set(table.get("fields", [])))
    ]
    if len(matches) != 1:
        raise RuntimeError(f"TWSE expected one OHLC table, found {len(matches)}")
    table = matches[0]
    fields = table["fields"]
    index = {field: fields.index(field) for field in required}
    valid: list[dict[str, str]] = []
    excluded: list[dict] = []
    errors: list[dict] = []
    universe_excluded: list[dict[str, str]] = []
    for line, values in enumerate(table.get("data", []), start=2):
        if len(values) != len(fields):
            code = clean_text(values[0]) if values else ""
            if eligible_code(code):
                errors.append(
                    _event(
                        source=snapshot.path.name,
                        line=line,
                        category=CATEGORY_PARSE,
                        reason=f"TWSE row length {len(values)} != {len(fields)}",
                        row={"date": requested_date, "code": code},
                        resolved=False,
                        market="TWSE",
                    )
                )
            continue
        row = {
            "date": requested_date,
            "code": values[index["證券代號"]],
            "name": values[index["證券名稱"]],
            "volume": values[index["成交股數"]],
            "open": values[index["開盤價"]],
            "high": values[index["最高價"]],
            "low": values[index["最低價"]],
            "close": values[index["收盤價"]],
        }
        if eligible_code(row["code"]) and not eligible_security(
            row["code"], row["name"]
        ):
            universe_excluded.append(
                {"code": clean_text(row["code"]), "name": str(row["name"]).strip()}
            )
            continue
        normalized, event = _validate_row(
            snapshot.path.name,
            line,
            row,
            market="TWSE",
            direct_official_regular_session=True,
        )
        if normalized is not None:
            valid.append(normalized)
        elif event is not None:
            (excluded if event["resolved"] else errors).append(event)
    deduped, duplicate_errors = _deduplicate(valid, snapshot.path.name)
    errors.extend(duplicate_errors)
    if not deduped:
        raise RuntimeError(f"TWSE returned no eligible price rows for {requested_date}")
    return ParsedSource(
        rows=deduped,
        excluded=tuple(excluded),
        errors=tuple(errors),
        source_metadata={
            **snapshot.metadata(),
            "response_date": str(payload.get("date")),
            "response_status": str(payload.get("stat")),
            "table_title": table.get("title", ""),
            "universe_filter": UNIVERSE_FILTER_DESCRIPTION,
            "universe_excluded_rows": len(universe_excluded),
            "universe_excluded_samples": universe_excluded[:25],
        },
        market="TWSE",
        requested_date=requested_date,
    )


def _decode_tpex(payload: bytes) -> str:
    for encoding in ("big5", "cp950", "utf-8-sig"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RuntimeError("TPEx CSV is not valid Big5/CP950/UTF-8 text")


def _roc_to_gregorian(value: str) -> str:
    parts = value.strip().split("/")
    if len(parts) != 3:
        raise RuntimeError(f"invalid TPEx ROC date: {value!r}")
    return f"{int(parts[0]) + 1911:04d}{int(parts[1]):02d}{int(parts[2]):02d}"


def parse_tpex(snapshot: SourceSnapshot, requested_date: str) -> ParsedSource:
    text = _decode_tpex(snapshot.payload).replace("\r\n", "\n")
    lines = text.splitlines()
    if not lines or "上櫃股票每日收盤行情" not in lines[0]:
        raise RuntimeError(f"TPEx EOD header/status is not ready for {requested_date}")
    date_lines = [line for line in lines[:8] if line.strip().startswith("資料日期:")]
    if len(date_lines) != 1:
        raise RuntimeError("TPEx EOD has no unique source date")
    response_date = _roc_to_gregorian(date_lines[0].split(":", 1)[1])
    if response_date != requested_date:
        raise RuntimeError(
            f"TPEx EOD date mismatch: requested={requested_date}, response={response_date}"
        )
    header_index = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("代號,")),
        None,
    )
    if header_index is None:
        raise RuntimeError("TPEx EOD CSV header not found")
    reader = csv.reader(io.StringIO("\n".join(lines[header_index:]), newline=""))
    raw_header = next(reader)
    header = [field.strip() for field in raw_header]
    required = {"代號", "名稱", "收盤", "開盤", "最高", "最低", "成交股數"}
    if not required.issubset(set(header)):
        raise RuntimeError(f"TPEx EOD missing columns: {sorted(required - set(header))}")
    index = {field: header.index(field) for field in required}
    valid: list[dict[str, str]] = []
    excluded: list[dict] = []
    errors: list[dict] = []
    universe_excluded: list[dict[str, str]] = []
    for relative_line, values in enumerate(reader, start=header_index + 2):
        if not values or not eligible_code(values[0] if values else ""):
            continue
        if len(values) != len(header):
            errors.append(
                _event(
                    source=snapshot.path.name,
                    line=relative_line,
                    category=CATEGORY_PARSE,
                    reason=f"TPEx row length {len(values)} != {len(header)}",
                    row={"date": requested_date, "code": values[0]},
                    resolved=False,
                    market="TPEX",
                )
            )
            continue
        row = {
            "date": requested_date,
            "code": values[index["代號"]],
            "name": values[index["名稱"]],
            "volume": values[index["成交股數"]],
            "open": values[index["開盤"]],
            "high": values[index["最高"]],
            "low": values[index["最低"]],
            "close": values[index["收盤"]],
        }
        if eligible_code(row["code"]) and not eligible_security(
            row["code"], row["name"]
        ):
            universe_excluded.append(
                {"code": clean_text(row["code"]), "name": str(row["name"]).strip()}
            )
            continue
        normalized, event = _validate_row(
            snapshot.path.name,
            relative_line,
            row,
            market="TPEX",
            direct_official_regular_session=True,
        )
        if normalized is not None:
            valid.append(normalized)
        elif event is not None:
            (excluded if event["resolved"] else errors).append(event)
    deduped, duplicate_errors = _deduplicate(valid, snapshot.path.name)
    errors.extend(duplicate_errors)
    if not deduped:
        raise RuntimeError(f"TPEx returned no eligible price rows for {requested_date}")
    return ParsedSource(
        rows=deduped,
        excluded=tuple(excluded),
        errors=tuple(errors),
        source_metadata={
            **snapshot.metadata(),
            "response_date": response_date,
            "response_status": lines[0].strip(),
            "universe_filter": UNIVERSE_FILTER_DESCRIPTION,
            "universe_excluded_rows": len(universe_excluded),
            "universe_excluded_samples": universe_excluded[:25],
        },
        market="TPEX",
        requested_date=requested_date,
    )


def official_exclusion_evidence(parsed: ParsedSource) -> dict[tuple[str, str], dict]:
    result: dict[tuple[str, str], dict] = {}
    for row in parsed.excluded:
        if row["category"] != CATEGORY_BLANK_POSITIVE or not row["resolved"]:
            continue
        key = (row["date"], row["code"])
        result[key] = {
            "date": row["date"],
            "code": row["code"],
            "volume": int(clean_text(row["volume"])),
            "all_ohlc_missing": True,
            "market": parsed.market,
            "source_sha256": parsed.source_metadata["sha256"],
            "request_url": parsed.source_metadata["request_url"],
        }
    return result


def csv_bytes(rows: Iterable[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row[field] for field in COLUMNS})
    return output.getvalue().encode("utf-8")


def deterministic_zip(member_name: str, rows: Iterable[dict[str, str]]) -> bytes:
    payload = csv_bytes(rows)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        info = zipfile.ZipInfo(member_name, date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, payload)
    return output.getvalue()


def parsed_summary(parsed: ParsedSource) -> dict:
    all_events = [*parsed.excluded, *parsed.errors]
    return {
        "source": parsed.source_metadata,
        "market": parsed.market,
        "requested_date": parsed.requested_date,
        "tradable_rows": len(parsed.rows),
        "excluded_rows": len(parsed.excluded),
        "unresolved_rows": len(parsed.errors),
        "classification_counts": parsed.category_counts,
        "resolved_classification_counts": dict(
            Counter(row["category"] for row in parsed.excluded)
        ),
        "unresolved_classification_counts": dict(
            Counter(row["category"] for row in parsed.errors)
        ),
        "events": all_events,
    }


def json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")
