from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np


TWSE_INSTITUTIONAL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_INSTITUTIONAL = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
TWSE_MARGIN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
TPEX_MARGIN = "https://www.tpex.org.tw/www/zh-tw/margin/balance"
_THREAD = threading.local()


def _int(value: object) -> int:
    text = str(value).replace(",", "").strip()
    return int(text) if text not in {"", "--"} else 0


def _roc(date: int) -> str:
    text = str(date)
    return f"{int(text[:4]) - 1911:03d}/{text[4:6]}/{text[6:]}"


def _get(url: str, params: dict[str, object], attempts: int = 5) -> tuple[dict, str, str]:
    query = urlencode(params)
    full_url = f"{url}?{query}"
    for attempt in range(attempts):
        try:
            command = ["/usr/bin/curl", "-sS", "-L", "--max-time", "35", "--get", url]
            for key, value in params.items():
                command.extend(["--data-urlencode", f"{key}={value}"])
            raw = subprocess.check_output(command, timeout=40)
            return json.loads(raw.decode("utf-8-sig")), hashlib.sha256(raw).hexdigest(), full_url
        except Exception:
            if attempt + 1 == attempts:
                raise
            time.sleep(1.0 + attempt * 1.5)
    raise AssertionError("unreachable")


def _twse_institutional(date: int, wanted: set[int]) -> tuple[dict[int, tuple[int, int, int]], str, str]:
    payload, digest, url = _get(TWSE_INSTITUTIONAL, {
        "response": "json", "date": str(date), "selectType": "ALLBUT0999",
    })
    if payload.get("stat") != "OK" or int(payload.get("date", 0)) != date:
        raise RuntimeError(f"TWSE institutional invalid response for {date}: {payload.get('stat')}")
    fields = payload["fields"]
    positions = (
        fields.index("證券代號"),
        next(i for i, field in enumerate(fields) if "外陸資買賣超股數(不含外資自營商)" in field),
        fields.index("投信買賣超股數"),
        fields.index("自營商買賣超股數"),
    )
    rows = {}
    for row in payload.get("data", []):
        code = row[positions[0]].strip()
        if code.isdigit() and int(code) in wanted:
            rows[int(code)] = tuple(_int(row[index]) for index in positions[1:])
    return rows, digest, url


def _tpex_institutional(date: int, wanted: set[int]) -> tuple[dict[int, tuple[int, int, int]], str, str]:
    payload, digest, url = _get(TPEX_INSTITUTIONAL, {
        "l": "zh-tw", "o": "json", "se": "EW", "t": "D",
        "d": _roc(date), "s": "0,asc",
    })
    if payload.get("stat") != "ok" or int(payload.get("date", 0)) != date:
        raise RuntimeError(f"TPEx institutional invalid response for {date}: {payload.get('stat')}")
    tables = payload.get("tables", [])
    data = tables[0].get("data", []) if tables else []
    rows = {}
    for row in data:
        code = row[0].strip()
        if code.isdigit() and int(code) in wanted:
            rows[int(code)] = (_int(row[10]), _int(row[13]), _int(row[22]))
    return rows, digest, url


def _twse_margin(date: int, wanted: set[int]) -> tuple[dict[int, tuple[int, int]], str, str]:
    payload, digest, url = _get(TWSE_MARGIN, {
        "response": "json", "date": str(date), "selectType": "ALL",
    })
    if payload.get("stat") != "OK" or int(payload.get("date", 0)) != date:
        raise RuntimeError(f"TWSE margin invalid response for {date}: {payload.get('stat')}")
    table = next((item for item in payload.get("tables", []) if "融資融券彙總" in item.get("title", "")), None)
    if table is None:
        raise RuntimeError(f"TWSE margin detail missing for {date}")
    rows = {}
    for row in table.get("data", []):
        code = row[0].strip()
        if code.isdigit() and int(code) in wanted:
            rows[int(code)] = (_int(row[6]), _int(row[12]))
    return rows, digest, url


def _tpex_margin(date: int, wanted: set[int]) -> tuple[dict[int, tuple[int, int]], str, str]:
    text = str(date)
    payload, digest, url = _get(TPEX_MARGIN, {
        "date": f"{text[:4]}/{text[4:6]}/{text[6:]}", "id": "", "response": "json",
    })
    if payload.get("stat") != "ok" or int(payload.get("date", 0)) != date:
        raise RuntimeError(f"TPEx margin invalid response for {date}: {payload.get('stat')}")
    tables = payload.get("tables", [])
    data = tables[0].get("data", []) if tables else []
    rows = {}
    for row in data:
        code = row[0].strip()
        if code.isdigit() and int(code) in wanted:
            rows[int(code)] = (_int(row[6]), _int(row[14]))
    return rows, digest, url


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
        rows, digest, url = function(date, wanted)
        result["sources"][name] = {"sha256": digest, "url": url, "matched_rows": len(rows)}
        target = institutional if "INSTITUTIONAL" in name else margin
        market = "TWSE" if name.startswith("TWSE") else "TPEX"
        for code, values in rows.items():
            if code in target:
                raise RuntimeError(f"duplicate market code {code} on {date} for {name}")
            target[code] = (*values, market)
    result["institutional"] = institutional
    result["margin"] = margin
    return result


def download_official_chip_store(
    dates: np.ndarray,
    needed_codes_by_date: dict[int, set[int]],
    output: Path,
    manifest_path: Path,
    workers: int = 8,
) -> dict:
    if output.exists() or manifest_path.exists():
        raise FileExistsError("refusing to overwrite immutable chip source store")
    requested = sorted(int(value) for value in np.unique(dates))
    results: dict[int, dict] = {}
    failures: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_date, date, needed_codes_by_date.get(date, set())): date
            for date in requested
        }
        for number, future in enumerate(as_completed(futures), 1):
            date = futures[future]
            try:
                results[date] = future.result()
            except Exception as exc:
                failures[date] = f"{type(exc).__name__}: {exc}"
            if number % 50 == 0:
                print(json.dumps({"processed_dates": number, "total_dates": len(requested), "failures": len(failures)}), flush=True)
    if failures:
        raise RuntimeError(f"official chip download failed closed: {json.dumps(failures, ensure_ascii=False)}")

    records = []
    manifest_dates = []
    for date in requested:
        item = results[date]
        wanted = needed_codes_by_date.get(date, set())
        for code in sorted(wanted):
            inst = item["institutional"].get(code)
            margin = item["margin"].get(code)
            records.append((
                date, code,
                np.nan if inst is None else inst[0],
                np.nan if inst is None else inst[1],
                np.nan if inst is None else inst[2],
                np.nan if margin is None else margin[0],
                np.nan if margin is None else margin[1],
                0 if inst is None else (1 if inst[3] == "TWSE" else 2),
                0 if margin is None else (1 if margin[2] == "TWSE" else 2),
            ))
        manifest_dates.append({
            "date": date,
            "retrieved_at_utc": item["retrieved_at_utc"],
            "wanted_codes": len(wanted),
            "sources": item["sources"],
        })
    dtype = np.dtype([
        ("source_date", "<i4"), ("stock_code", "<i4"),
        ("foreign", "<f8"), ("investment_trust", "<f8"), ("dealer", "<f8"),
        ("margin_balance", "<f8"), ("short_balance", "<f8"),
        ("institutional_market", "u1"), ("margin_market", "u1"),
    ])
    table = np.asarray(records, dtype=dtype)
    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output, chip_daily=table)
    manifest = {
        "status": "COMPLETE",
        "official_only": True,
        "dates_requested": len(requested),
        "records": len(table),
        "retrieval_started_utc": min(item["retrieved_at_utc"] for item in results.values()),
        "retrieval_finished_utc": max(item["retrieved_at_utc"] for item in results.values()),
        "date_payloads": manifest_dates,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


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
