"""Exact official daily price limits; never infer a limit from a 9.9% cutoff."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
from decimal import Decimal, ROUND_FLOOR

from shadow_daily_runner.normalize import _decode_tpex


TWSE_URL = "https://www.twse.com.tw/exchangeReport/TWT84U?date={day}&response=json&selectType=ALL"
TWSE_EX_RIGHT_URL = "https://www.twse.com.tw/rwd/zh/exRight/TWT49U?response=json&startDate={start}&endDate={end}"


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def twse_limits(day: str, cache_dir: Path) -> tuple[dict[str, float], dict]:
    """Official all-securities limit-price table, including ordinary shares."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    pointer = cache_dir / f"{day}.source.json"
    url = TWSE_URL.format(day=day)
    if pointer.exists():
        provenance = json.loads(pointer.read_text(encoding="utf-8"))
        if provenance["url"] != url:
            raise RuntimeError("TWSE limit-source URL changed")
        raw = cache_dir / provenance["filename"]
        payload = raw.read_bytes()
        if _hash(payload) != provenance["sha256"]:
            raise RuntimeError("TWSE official price-limit cache hash mismatch")
    else:
        result = subprocess.run(
            ["/usr/bin/curl", "-fsSL", "--max-time", "40", "-A", "WarrantScope-Research/0.1", url],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"TWSE TWT84U download failed: exit {result.returncode}")
        payload = result.stdout
        provenance = {
            "url": url,
            "sha256": _hash(payload),
            "filename": f"{_hash(payload)}.json",
        }
        # Validate before any cache is committed. Both files remain immutable.
        _parse_twse(payload, day)
        raw = cache_dir / provenance["filename"]
        if raw.exists() and raw.read_bytes() != payload:
            raise RuntimeError("TWSE content-addressed cache collision")
        if not raw.exists():
            with raw.open("xb") as handle:
                handle.write(payload)
        with pointer.open("x", encoding="utf-8") as handle:
            json.dump(provenance, handle, ensure_ascii=False, sort_keys=True)
    return _parse_twse(payload, day), provenance


def _parse_twse(payload: bytes, day: str) -> dict[str, float]:
    source = json.loads(payload.decode("utf-8-sig"))
    if source.get("stat") != "OK" or str(source.get("date")) != day:
        raise RuntimeError("TWSE TWT84U date/status mismatch")
    fields = source["fields"]
    if "證券代號" not in fields or "漲停價" not in fields:
        raise RuntimeError("TWSE TWT84U schema missing official price limit")
    code_index, limit_index = fields.index("證券代號"), fields.index("漲停價")
    values: dict[str, float] = {}
    for row in source["data"]:
        code = str(row[code_index]).strip()
        raw = str(row[limit_index]).replace(",", "").strip()
        if not raw or raw in {"--", "---"}:
            continue
        value = float(raw)
        if code in values or value <= 0:
            raise RuntimeError(f"TWSE TWT84U duplicate/invalid code {code}")
        values[code] = value
    if len(values) < 1000:
        raise RuntimeError("TWSE all-securities limit-price coverage too low")
    return values


def twse_ex_right_limits(start: str, end: str, cache_dir: Path) -> tuple[dict[tuple[str, str], float], dict]:
    """Official TWT49U ex-right/dividend overrides for the queried dates."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    pointer = cache_dir / f"ex_right_{start}_{end}.source.json"
    url = TWSE_EX_RIGHT_URL.format(start=start, end=end)
    if pointer.exists():
        provenance = json.loads(pointer.read_text(encoding="utf-8"))
        if provenance["url"] != url:
            raise RuntimeError("TWSE ex-right URL changed")
        payload = (cache_dir / provenance["filename"]).read_bytes()
        if _hash(payload) != provenance["sha256"]:
            raise RuntimeError("TWSE ex-right source hash mismatch")
    else:
        result = subprocess.run(
            ["/usr/bin/curl", "-fsSL", "--max-time", "40", "-A", "WarrantScope-Research/0.1", url],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"TWSE TWT49U download failed: exit {result.returncode}")
        payload = result.stdout
        _parse_ex_right(payload)
        provenance = {"url": url, "sha256": _hash(payload), "filename": f"{_hash(payload)}.json"}
        raw = cache_dir / provenance["filename"]
        if not raw.exists():
            with raw.open("xb") as handle:
                handle.write(payload)
        with pointer.open("x", encoding="utf-8") as handle:
            json.dump(provenance, handle, ensure_ascii=False, sort_keys=True)
    return _parse_ex_right(payload), provenance


def _parse_ex_right(payload: bytes) -> dict[tuple[str, str], float]:
    source = json.loads(payload.decode("utf-8-sig"))
    if source.get("stat") != "OK":
        raise RuntimeError("TWSE TWT49U status failed")
    fields = source["fields"]
    if not {"資料日期", "股票代號", "漲停價格"}.issubset(fields):
        raise RuntimeError("TWSE TWT49U schema mismatch")
    indexes = [fields.index(name) for name in ("資料日期", "股票代號", "漲停價格")]
    out: dict[tuple[str, str], float] = {}
    for row in source["data"]:
        date_text, code, limit_text = (str(row[index]).strip() for index in indexes)
        parts = date_text.replace("年", " ").replace("月", " ").replace("日", "").split()
        if len(parts) != 3:
            raise RuntimeError("TWSE TWT49U date format changed")
        day = f"{int(parts[0]) + 1911:04d}{int(parts[1]):02d}{int(parts[2]):02d}"
        key = (day, code)
        if key in out:
            raise RuntimeError("TWSE TWT49U duplicate code-date")
        out[key] = float(limit_text.replace(",", ""))
    return out


def ordinary_share_limit_from_reference(reference: float) -> float:
    """TWSE/TPEx ordinary-stock 10% limit rounded down to a legal tick."""
    value = Decimal(str(reference)) * Decimal("1.10")
    for lower, upper, tick in (
        (Decimal("0"), Decimal("10"), Decimal("0.01")),
        (Decimal("10"), Decimal("50"), Decimal("0.05")),
        (Decimal("50"), Decimal("100"), Decimal("0.10")),
        (Decimal("100"), Decimal("500"), Decimal("0.50")),
        (Decimal("500"), Decimal("1000"), Decimal("1")),
        (Decimal("1000"), Decimal("Infinity"), Decimal("5")),
    ):
        if lower <= value < upper:
            return float((value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick)
    raise AssertionError("unreachable price band")


def tpex_next_limits(signal_day: str, direct_audit: dict) -> tuple[dict[str, float], dict]:
    """TPEx T-day regular-session CSV publishes its official next-day limit."""
    expected = f"tpex_eod_{signal_day}"
    sources = [
        item
        for archive in direct_audit["archives"]
        for item in archive["sources"]
        if item["source"] == expected
    ]
    if len(sources) != 1:
        raise RuntimeError(f"TPEx source is not unique on {signal_day}")
    source = sources[0]
    payload = Path(source["path"]).read_bytes()
    if _hash(payload) != source["sha256"]:
        raise RuntimeError("TPEx raw source hash mismatch")
    lines = _decode_tpex(payload).replace("\r\n", "\n").splitlines()
    roc = f"{int(signal_day[:4]) - 1911:03d}/{signal_day[4:6]}/{signal_day[6:]}"
    if f"資料日期:{roc}" not in lines[:8]:
        raise RuntimeError("TPEx next-limit source-date mismatch")
    header_index = next((i for i, line in enumerate(lines) if line.lstrip().startswith("代號,")), None)
    if header_index is None:
        raise RuntimeError("TPEx next-limit header missing")
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])))
    if reader.fieldnames is None:
        raise RuntimeError("TPEx next-limit schema missing")
    names = {name.strip(): name for name in reader.fieldnames}
    if not {"代號", "次日漲停價"}.issubset(names):
        raise RuntimeError("TPEx official next-limit field missing")
    limits: dict[str, float] = {}
    for row in reader:
        code = str(row[names["代號"]]).strip()
        raw_value = row[names["次日漲停價"]]
        raw = str(raw_value).replace(",", "").strip() if raw_value is not None else ""
        if not code.isdigit() or not raw or raw in {"----", "--", "-"}:
            continue
        value = float(raw)
        if code in limits or value <= 0:
            raise RuntimeError(f"TPEx duplicate/invalid next-limit code {code}")
        limits[code] = value
    if len(limits) < 500:
        raise RuntimeError("TPEx official price-limit coverage too low")
    return limits, {"source": source["source"], "sha256": source["sha256"], "path": source["path"]}
