"""Build a sealed expanded universe from official EOD and company masters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from shadow_daily_runner.normalize import parse_tpex, parse_twse
from yuanta_intraday_shadow_v01.collector import canonical_bytes, _source_snapshot

from .policy import POLICY


MODULE_DIR = Path(__file__).resolve().parent
STOCK_STRATEGY_DIR = MODULE_DIR.parent
DEFAULT_RUNTIME_DIR = MODULE_DIR / "runtime"
DEFAULT_AUDIT_DIR = STOCK_STRATEGY_DIR / "shadow_daily_runner" / "runtime" / "audit"
TWSE_MASTER_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_MASTER_URL = "https://www.tpex.org.tw/www/zh-tw/company/otcSearch"


@dataclass(frozen=True, slots=True)
class ExpandedWatchItem:
    stock_id: str
    stock_name: str
    market: str
    rank: int
    close: float
    volume: int
    turnover_twd: float
    industry: str
    industry_code: str | None


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _get(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "WarrantScope-expanded-shadow/1.0"})
    with urlopen(request, timeout=30) as response:
        return response.read()


def _post(url: str, fields: dict[str, str]) -> bytes:
    request = Request(
        url,
        data=urlencode(fields).encode("ascii"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "WarrantScope-expanded-shadow/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        return response.read()


def _official_eod(signal_date: str, audit_dir: Path) -> tuple[dict[str, dict], dict]:
    audit_path = audit_dir / f"official_eod_through_{signal_date}.json"
    if not audit_path.is_file():
        raise RuntimeError(f"official EOD audit missing for {signal_date}")
    audit_bytes = audit_path.read_bytes()
    audit = json.loads(audit_bytes)
    matches: dict[str, dict] = {}
    for archive in audit.get("archives", []):
        for source in archive.get("sources", []):
            if str(source.get("response_date")) != signal_date:
                continue
            name = str(source.get("source", ""))
            if name == f"twse_eod_{signal_date}":
                matches["TWSE"] = source
            elif name == f"tpex_eod_{signal_date}":
                matches["TPEX"] = source
    if set(matches) != {"TWSE", "TPEX"}:
        raise RuntimeError(f"official TWSE/TPEx EOD sources unavailable for {signal_date}")
    rows: dict[str, dict] = {}
    for market, source in matches.items():
        parsed = (
            parse_twse(_source_snapshot(source), signal_date)
            if market == "TWSE"
            else parse_tpex(_source_snapshot(source), signal_date)
        )
        if parsed.errors:
            raise RuntimeError(f"official {market} EOD parse contains errors")
        for row in parsed.rows:
            code = str(row["code"])
            if code in rows:
                raise RuntimeError(f"duplicate official EOD identity: {code}")
            rows[code] = {**row, "market": market}
    return rows, {
        "audit_path": str(audit_path.resolve()),
        "audit_sha256": _sha(audit_bytes),
        "sources": {
            market: {
                "path": str(source["path"]),
                "sha256": str(source["sha256"]),
                "request_url": str(source["request_url"]),
            }
            for market, source in sorted(matches.items())
        },
    }


def _parse_twse_master(payload: bytes) -> dict[str, dict]:
    rows = json.loads(payload)
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("TWSE company master is empty")
    result = {}
    for row in rows:
        code = str(row.get("公司代號", "")).strip()
        if code:
            result[code] = {
                "stock_name": str(row.get("公司簡稱", "")).strip(),
                "industry": "",
                "industry_code": str(row.get("產業別", "")).strip(),
            }
    return result


def _parse_tpex_master(payload: bytes) -> dict[str, dict]:
    document = json.loads(payload)
    if document.get("stat") != "ok":
        raise RuntimeError(f"TPEx company master failed: {document.get('stat')}")
    tables = document.get("tables", [])
    if len(tables) != 1 or not isinstance(tables[0].get("data"), list):
        raise RuntimeError("TPEx company master schema changed")
    result = {}
    for row in tables[0]["data"]:
        if not isinstance(row, list) or len(row) < 4:
            raise RuntimeError("TPEx company master row schema changed")
        code = str(row[0]).strip()
        if code:
            result[code] = {
                "stock_name": str(row[1]).strip(),
                "industry": str(row[3]).strip(),
                "industry_code": None,
            }
    return result


def _store_source(runtime_dir: Path, label: str, payload: bytes) -> dict:
    digest = _sha(payload)
    target = runtime_dir / "sources" / label / f"{digest}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != payload:
        raise RuntimeError(f"source hash collision: {target}")
    if not target.exists():
        target.write_bytes(payload)
    return {"path": str(target.resolve()), "sha256": digest}


def _select_candidates(eod: dict[str, dict], masters: dict[str, dict]) -> tuple[list[dict], dict[str, int]]:
    """Apply the frozen policy without network or filesystem side effects."""
    excluded_codes = set(POLICY["excluded_twse_industry_codes"])
    excluded_names = set(POLICY["excluded_industry_names"])
    code_pattern = re.compile(str(POLICY["security_code_pattern"]))
    candidates = []
    reason_counts: dict[str, int] = {}
    for code, row in sorted(eod.items()):
        reason = ""
        master = masters.get(code)
        try:
            close = float(row["close"])
            volume = int(float(row["volume"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            close, volume, reason = 0.0, 0, "INVALID_EOD"
        turnover = close * volume
        if not reason and code_pattern.fullmatch(code) is None:
            reason = "NON_COMMON_STOCK_CODE"
        elif not reason and master is None:
            reason = "MISSING_OFFICIAL_INDUSTRY"
        elif not reason and (
            master["industry_code"] in excluded_codes
            or master["industry"] in excluded_names
        ):
            reason = "EXCLUDED_INDUSTRY"
        elif not reason and close > float(POLICY["maximum_close_twd"]):
            reason = "ABOVE_PRICE_CAP"
        elif not reason and turnover < float(POLICY["minimum_turnover_twd"]):
            reason = "BELOW_TURNOVER_FLOOR"
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            continue
        candidates.append({
            "stock_id": code,
            "stock_name": master["stock_name"] or str(row.get("name", code)),
            "market": str(row["market"]),
            "close": close,
            "volume": volume,
            "turnover_twd": turnover,
            "industry": master["industry"],
            "industry_code": master["industry_code"],
        })
    candidates.sort(key=lambda row: (-float(row["turnover_twd"]), str(row["stock_id"])))
    return candidates, reason_counts


def build_universe(
    signal_date: str,
    *,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    audit_dir: Path = DEFAULT_AUDIT_DIR,
    twse_payload: bytes | None = None,
    tpex_payload: bytes | None = None,
    fetch_get: Callable[[str], bytes] = _get,
    fetch_post: Callable[[str, dict[str, str]], bytes] = _post,
) -> dict:
    if not re.fullmatch(r"[0-9]{8}", signal_date):
        raise ValueError("signal_date must be YYYYMMDD")
    eod, eod_provenance = _official_eod(signal_date, audit_dir)
    twse_payload = twse_payload if twse_payload is not None else fetch_get(TWSE_MASTER_URL)
    tpex_payload = tpex_payload if tpex_payload is not None else fetch_post(
        TPEX_MASTER_URL, {"type": "stkType", "stkType": " ", "response": "json"}
    )
    source_provenance = {
        "TWSE_COMPANY_MASTER": {
            "url": TWSE_MASTER_URL,
            **_store_source(runtime_dir, "twse_company_master", twse_payload),
        },
        "TPEX_COMPANY_MASTER": {
            "url": TPEX_MASTER_URL,
            "post_fields": {"type": "stkType", "stkType": " ", "response": "json"},
            **_store_source(runtime_dir, "tpex_company_master", tpex_payload),
        },
    }
    masters = _parse_twse_master(twse_payload)
    for code, row in _parse_tpex_master(tpex_payload).items():
        if code in masters:
            raise RuntimeError(f"company master market identity collision: {code}")
        masters[code] = row
    candidates, reason_counts = _select_candidates(eod, masters)
    maximum = int(POLICY["maximum_symbols"])
    selected = [{**row, "rank": rank} for rank, row in enumerate(candidates[:maximum], 1)]
    if not selected:
        raise RuntimeError("expanded universe is empty")
    seal = {
        "schema_version": 1,
        "signal_date": signal_date,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": "SEALED",
        "mode": POLICY["mode"],
        "policy": POLICY,
        "policy_hash": _sha(canonical_bytes(POLICY)),
        "official_eod": eod_provenance,
        "company_master_sources": source_provenance,
        "eligible_before_cap": len(candidates),
        "selected_count": len(selected),
        "excluded_reason_counts": dict(sorted(reason_counts.items())),
        "stocks": selected,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_order_calls": 0,
    }
    seal["seal_hash"] = _sha(canonical_bytes(seal))
    target = runtime_dir / "seals" / f"{signal_date}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        # Rebuilding may have a different retrieval timestamp.  Never silently
        # overwrite an already sealed daily universe.
        if existing.get("seal_hash") != seal["seal_hash"]:
            return existing
        return existing
    target.write_bytes(canonical_bytes(seal) + b"\n")
    return seal


def _validate_seal(seal: dict, expected_date: str | None = None) -> dict:
    unsigned = {key: value for key, value in seal.items() if key != "seal_hash"}
    if _sha(canonical_bytes(unsigned)) != seal.get("seal_hash"):
        raise RuntimeError("expanded universe seal hash mismatch")
    if expected_date is not None and seal.get("signal_date") != expected_date:
        raise RuntimeError("expanded universe date mismatch")
    if seal.get("policy") != POLICY or seal.get("policy_hash") != _sha(canonical_bytes(POLICY)):
        raise RuntimeError("expanded universe policy does not match deployed policy")
    stocks = seal.get("stocks", [])
    if not stocks or len(stocks) > int(POLICY["maximum_symbols"]):
        raise RuntimeError("expanded universe count is invalid")
    if len({str(row["stock_id"]) for row in stocks}) != len(stocks):
        raise RuntimeError("expanded universe contains duplicate stocks")
    if [int(row["rank"]) for row in stocks] != list(range(1, len(stocks) + 1)):
        raise RuntimeError("expanded universe ranks are invalid")
    return seal


def load_universe(
    signal_date: str, *, runtime_dir: Path = DEFAULT_RUNTIME_DIR,
) -> tuple[dict, list[ExpandedWatchItem]]:
    path = runtime_dir / "seals" / f"{signal_date}.json"
    if not path.is_file():
        raise RuntimeError(f"expanded universe seal missing for {signal_date}")
    seal = _validate_seal(json.loads(path.read_text(encoding="utf-8")), signal_date)
    return seal, [
        ExpandedWatchItem(
            stock_id=str(row["stock_id"]), stock_name=str(row["stock_name"]),
            market=str(row["market"]), rank=int(row["rank"]),
            close=float(row["close"]), volume=int(row["volume"]),
            turnover_twd=float(row["turnover_twd"]), industry=str(row["industry"]),
            industry_code=(None if row.get("industry_code") is None else str(row["industry_code"])),
        )
        for row in seal["stocks"]
    ]


def load_or_build_universe(
    signal_date: str, *, runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    audit_dir: Path = DEFAULT_AUDIT_DIR,
) -> tuple[dict, list[ExpandedWatchItem]]:
    try:
        return load_universe(signal_date, runtime_dir=runtime_dir)
    except RuntimeError as exc:
        if "missing" not in str(exc):
            raise
    build_universe(signal_date, runtime_dir=runtime_dir, audit_dir=audit_dir)
    return load_universe(signal_date, runtime_dir=runtime_dir)
