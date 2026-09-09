from __future__ import annotations

import csv
from datetime import date, timedelta
import io
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from .io_utils import sha256_bytes, sha256_file
from .sources import SourceSnapshot, decode_json


AD_HOC_CLOSURE_TITLE = re.compile(
    r"^臺灣證券交易所集中交易市場(?P<roc_year>\d{3})年"
    r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日休市一天$"
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _roc_day(value: object) -> date:
    text = str(value or "").strip().replace("/", "").replace("-", "")
    if len(text) != 7 or not text.isdigit():
        raise RuntimeError(f"invalid official ROC calendar date: {value!r}")
    return date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:7]))


def build_trading_calendar(
    snapshot: SourceSnapshot,
    year: int,
    *,
    ad_hoc_closures: list[dict] | tuple[dict, ...] = (),
) -> tuple[bytes, dict]:
    payload = decode_json(snapshot)
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("official TWSE holiday schedule is empty or malformed")

    nontrading: set[date] = set()
    source_rows: list[dict] = []
    for source in payload:
        if not isinstance(source, dict):
            raise RuntimeError("official TWSE holiday schedule contains a non-object row")
        if not {"Name", "Date", "Description"}.issubset(source):
            raise RuntimeError("official TWSE holiday schedule row has missing fields")
        day = _roc_day(source["Date"])
        if day.year != year:
            continue
        name = str(source.get("Name", "")).strip()
        description = str(source.get("Description", "")).strip()
        combined = f"{name} {description}"
        explicitly_open = "開始交易" in combined or "最後交易" in combined
        if day.weekday() < 5 and not explicitly_open:
            nontrading.add(day)
        source_rows.append(
            {
                "date": day.isoformat(),
                "name": name,
                "description": description,
                "explicitly_open": explicitly_open,
                "excluded_from_weekday_calendar": day in nontrading,
            }
        )

    closure_rows: list[dict] = []
    for item in ad_hoc_closures:
        if not isinstance(item, dict):
            raise RuntimeError("ad-hoc market closure evidence must be an object")
        try:
            closure_day = date.fromisoformat(str(item["date"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("invalid ad-hoc market closure date") from exc
        if closure_day.year != year:
            continue
        title = str(item.get("title", "")).strip()
        match = AD_HOC_CLOSURE_TITLE.fullmatch(title)
        if match is None:
            raise RuntimeError("unrecognized TWSE ad-hoc closure title")
        title_day = date(
            int(match.group("roc_year")) + 1911,
            int(match.group("month")),
            int(match.group("day")),
        )
        if title_day != closure_day:
            raise RuntimeError("TWSE ad-hoc closure title/date mismatch")
        source_sha256 = str(item.get("source_sha256", ""))
        record_sha256 = str(item.get("record_sha256", ""))
        if len(source_sha256) != 64 or len(record_sha256) != 64:
            raise RuntimeError("ad-hoc closure evidence hashes are invalid")
        if closure_day.weekday() < 5:
            nontrading.add(closure_day)
        closure_rows.append(dict(item))

    first = date(year, 1, 1)
    last = date(year, 12, 31)
    sessions: list[str] = []
    cursor = first
    while cursor <= last:
        if cursor.weekday() < 5 and cursor not in nontrading:
            sessions.append(cursor.isoformat())
        cursor += timedelta(days=1)
    if sessions != sorted(set(sessions)):
        raise RuntimeError("derived trading calendar is not unique and ascending")

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["date"])
    writer.writerows([[day] for day in sessions])
    calendar_bytes = output.getvalue().encode("utf-8")
    metadata = {
        "year": year,
        "method": (
            "Gregorian Monday-Friday dates minus official TWSE weekday holiday/"
            "no-trading rows; rows explicitly labelled first/last trading day stay open"
        ),
        "source": snapshot.metadata(),
        "source_page_url": (
            "https://www.twse.com.tw/holidaySchedule/holidaySchedule"
        ),
        "calendar_sha256": sha256_bytes(calendar_bytes),
        "calendar_rows": len(sessions),
        "first_date": sessions[0],
        "last_date": sessions[-1],
        "official_schedule_rows": source_rows,
        "official_ad_hoc_closure_rows": closure_rows,
    }
    return calendar_bytes, metadata


def discover_ad_hoc_closures(snapshot: SourceSnapshot, year: int) -> list[dict]:
    """Extract official, title-bound emergency closure evidence from TWSE news.

    The annual holiday schedule omits typhoon closures announced at short notice.
    Only the exact TWSE market-closure title is accepted; general news or an empty
    EOD response can never remove a session from the calendar.
    """

    payload = decode_json(snapshot)
    if not isinstance(payload, list):
        raise RuntimeError("official TWSE news list is malformed")
    rows: list[dict] = []
    for source in payload:
        if not isinstance(source, dict):
            raise RuntimeError("official TWSE news list contains a non-object row")
        title = str(source.get("Title", "")).strip()
        match = AD_HOC_CLOSURE_TITLE.fullmatch(title)
        if match is None:
            continue
        closure_day = date(
            int(match.group("roc_year")) + 1911,
            int(match.group("month")),
            int(match.group("day")),
        )
        if closure_day.year != year:
            continue
        canonical_record = (
            f"{source.get('Date', '')}\n{title}\n{source.get('Url', '')}\n"
        ).encode("utf-8")
        rows.append(
            {
                "date": closure_day.isoformat(),
                "title": title,
                "announcement_date_roc": str(source.get("Date", "")),
                "url": str(source.get("Url", "")),
                "source_request_url": snapshot.request_url,
                "source_retrieved_at_utc": snapshot.retrieved_at_utc,
                "source_sha256": snapshot.sha256,
                "source_path": str(snapshot.path),
                "record_sha256": sha256_bytes(canonical_record),
            }
        )
    rows.sort(key=lambda item: item["date"])
    dates = [item["date"] for item in rows]
    if len(dates) != len(set(dates)):
        raise RuntimeError("duplicate TWSE ad-hoc closure evidence")
    return rows


def _official_twse_url(value: object) -> bool:
    host = (urlparse(str(value or "")).hostname or "").lower()
    return host == "twse.com.tw" or host.endswith(".twse.com.tw")


def validate_ad_hoc_closure_evidence(item: dict, year: int) -> dict:
    """Validate that a saved closure row is linked to its immutable news raw."""

    if not isinstance(item, dict):
        raise RuntimeError("saved ad-hoc closure evidence is not an object")
    try:
        closure_day = date.fromisoformat(str(item["date"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("saved ad-hoc closure date is invalid") from exc
    if closure_day.year != year:
        raise RuntimeError("saved ad-hoc closure has the wrong calendar year")
    title = str(item.get("title", "")).strip()
    match = AD_HOC_CLOSURE_TITLE.fullmatch(title)
    if match is None:
        raise RuntimeError("saved ad-hoc closure title is not exact")
    title_day = date(
        int(match.group("roc_year")) + 1911,
        int(match.group("month")),
        int(match.group("day")),
    )
    if title_day != closure_day:
        raise RuntimeError("saved ad-hoc closure title/date mismatch")

    source_digest = str(item.get("source_sha256", ""))
    record_digest = str(item.get("record_sha256", ""))
    if not HEX64.fullmatch(source_digest) or not HEX64.fullmatch(record_digest):
        raise RuntimeError("saved ad-hoc closure digest is invalid")
    raw_path = Path(str(item.get("source_path", "")))
    if not raw_path.is_file() or sha256_file(raw_path) != source_digest:
        raise RuntimeError("saved ad-hoc closure raw source/hash is invalid")
    if not _official_twse_url(item.get("source_request_url")):
        raise RuntimeError("saved ad-hoc closure source URL is not official TWSE")
    if not _official_twse_url(item.get("url")):
        raise RuntimeError("saved ad-hoc closure record URL is not official TWSE")
    if not item.get("source_retrieved_at_utc"):
        raise RuntimeError("saved ad-hoc closure retrieval timestamp is missing")

    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("saved ad-hoc closure raw source is malformed") from exc
    if not isinstance(raw, list):
        raise RuntimeError("saved ad-hoc closure raw source is not a news list")
    wanted = (
        str(item.get("announcement_date_roc", "")),
        title,
        str(item.get("url", "")),
    )
    matches = [
        source
        for source in raw
        if isinstance(source, dict)
        and (
            str(source.get("Date", "")),
            str(source.get("Title", "")).strip(),
            str(source.get("Url", "")),
        )
        == wanted
    ]
    if len(matches) != 1:
        raise RuntimeError("saved ad-hoc closure record is not uniquely in its raw source")
    canonical = f"{wanted[0]}\n{wanted[1]}\n{wanted[2]}\n".encode("utf-8")
    if sha256_bytes(canonical) != record_digest:
        raise RuntimeError("saved ad-hoc closure record hash is invalid")
    return dict(item)


def collect_ad_hoc_closures(
    snapshot: SourceSnapshot,
    year: int,
    *,
    previous_calendar_metadata_path: Path | None = None,
) -> list[dict]:
    """Union current official news with previously verified immutable evidence.

    TWSE's news-list API can roll old announcements out of its latest response.
    A previously proven emergency closure therefore stays in the point-in-time
    calendar, but only while its original raw snapshot and record hash validate.
    """

    current = discover_ad_hoc_closures(snapshot, year)
    previous: list[dict] = []
    path = previous_calendar_metadata_path
    if path is not None and path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("previous calendar metadata is malformed") from exc
        rows = payload.get("official_ad_hoc_closure_rows", [])
        if not isinstance(rows, list):
            raise RuntimeError("previous calendar closure evidence is malformed")
        for item in rows:
            if str(item.get("date", ""))[:4] == str(year):
                previous.append(validate_ad_hoc_closure_evidence(item, year))

    merged: dict[str, dict] = {item["date"]: item for item in previous}
    for item in current:
        existing = merged.get(item["date"])
        if existing is not None:
            identity = ("title", "url", "record_sha256")
            if any(existing.get(key) != item.get(key) for key in identity):
                raise RuntimeError("conflicting official closure evidence for one date")
            continue
        merged[item["date"]] = item
    return [merged[day] for day in sorted(merged)]


def read_sessions(payload: bytes) -> list[str]:
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
    if reader.fieldnames != ["date"]:
        raise RuntimeError("trading_calendar.csv must contain only the date column")
    sessions = [str(row["date"]).strip() for row in reader]
    for item in sessions:
        date.fromisoformat(item)
    if sessions != sorted(set(sessions)):
        raise RuntimeError("trading_calendar.csv dates are not unique and ascending")
    return sessions
