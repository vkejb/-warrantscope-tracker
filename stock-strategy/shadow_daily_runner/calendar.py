from __future__ import annotations

import csv
from datetime import date, timedelta
import io

from .io_utils import sha256_bytes
from .sources import SourceSnapshot, decode_json


def _roc_day(value: object) -> date:
    text = str(value or "").strip().replace("/", "").replace("-", "")
    if len(text) != 7 or not text.isdigit():
        raise RuntimeError(f"invalid official ROC calendar date: {value!r}")
    return date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:7]))


def build_trading_calendar(
    snapshot: SourceSnapshot,
    year: int,
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
    }
    return calendar_bytes, metadata


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
