from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zipfile


TAIFEX_FUT = "https://www.taifex.com.tw/cht/3/futDataDown"
TAIFEX_OPT = "https://www.taifex.com.tw/cht/3/optDataDown"
TWSE_TAIEX = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _request(url: str, data: dict[str, str] | None = None, attempts: int = 3) -> bytes:
    body = urlencode(data).encode() if data else None
    for attempt in range(attempts):
        try:
            req = Request(url, data=body, headers={"User-Agent": "WarrantScope-research/0.1", "Accept": "*/*"})
            with urlopen(req, timeout=120) as response:
                payload = response.read()
                if response.status != 200 or not payload:
                    raise RuntimeError(f"HTTP {response.status} or empty payload")
                return payload
        except Exception as error:
            # TWSE's public CDN intermittently returns 428 to urllib while the
            # same official URL succeeds through the repository's established
            # curl transport.  This is a transport fallback, not a data source
            # fallback; redirects remain visible to curl and non-200 exits fail.
            if data is None:
                try:
                    return subprocess.check_output(
                        ["/usr/bin/curl", "-f", "-sS", "-L", "--max-time", "120", url],
                        timeout=130,
                    )
                except Exception:
                    pass
            if attempt + 1 == attempts:
                raise error
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def acquire(runtime: Path) -> dict:
    raw = runtime / "official_raw"
    raw.mkdir(parents=True, exist_ok=True)
    records = []
    for year in range(2020, 2026):
        for family, url in (("futures", TAIFEX_FUT), ("options", TAIFEX_OPT)):
            path = raw / f"taifex_{family}_{year}.zip"
            if not path.exists():
                payload = _request(url, {"down_type": "2", "his_year": str(year)})
                if not payload.startswith(b"PK"):
                    raise RuntimeError(f"official {family} {year} response is not ZIP")
                path.write_bytes(payload)
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                if bad:
                    raise RuntimeError(f"corrupt ZIP member: {bad}")
            records.append({"source": family, "source_url": url, "year": year, "path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size})
    # The public TWSE endpoint began returning a 200 security HTML page before
    # a complete 2020-2025 TAIEX archive was obtained. Basis is therefore
    # excluded rather than patched with a third-party source.
    manifest = {
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "official_only": True,
        "records": records,
    }
    (runtime / "source_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def _decode_csv(payload: bytes) -> list[list[str]]:
    for encoding in ("utf-8-sig", "cp950", "big5"):
        try:
            text = payload.decode(encoding)
            return list(csv.reader(io.StringIO(text)))
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("csv", b"", 0, 1, "unsupported TAIFEX encoding")


def _num(value: str) -> float | None:
    value = value.strip().replace(",", "")
    if value in {"", "-", "NULL"}:
        return None
    try:
        return float(value.rstrip("%"))
    except ValueError:
        return None


def parse_futures(raw: Path) -> dict[int, dict]:
    result: dict[int, dict] = {}
    for path in sorted(raw.glob("taifex_futures_*.zip")):
        with zipfile.ZipFile(path) as z:
            for member in z.namelist():
                rows = _decode_csv(z.read(member))
                for row in rows[1:]:
                    if len(row) < 18 or row[1].strip() != "TX" or row[17].strip() != "一般":
                        continue
                    date = int(row[0].replace("/", ""))
                    expiry = row[2].strip()
                    if len(expiry) != 6 or not expiry.isdigit():
                        continue
                    settlement, close, oi = _num(row[10]), _num(row[6]), _num(row[11])
                    if oi is None:
                        oi = 0.0
                    day = result.setdefault(date, {"contracts": [], "tx_total_oi": 0.0})
                    day["tx_total_oi"] += oi
                    day["contracts"].append((expiry, settlement if settlement is not None else close))
    out = {}
    for date, day in result.items():
        valid = [(e, p) for e, p in day["contracts"] if p is not None]
        if not valid:
            continue
        expiry, price = min(valid, key=lambda x: x[0])
        out[date] = {"tx_near_price": price, "tx_near_contract": expiry, "tx_total_oi": day["tx_total_oi"]}
    return out


def parse_options(raw: Path) -> dict[int, dict]:
    result: dict[int, dict] = {}
    for path in sorted(raw.glob("taifex_options_*.zip")):
        with zipfile.ZipFile(path) as z:
            for member in z.namelist():
                rows = _decode_csv(z.read(member))
                for row in rows[1:]:
                    if len(row) < 18 or row[1].strip() != "TXO" or row[17].strip() != "一般":
                        continue
                    date = int(row[0].replace("/", ""))
                    side = row[4].strip()
                    volume, oi = _num(row[9]) or 0.0, _num(row[11]) or 0.0
                    day = result.setdefault(date, {"put_volume": 0.0, "call_volume": 0.0, "put_oi": 0.0, "call_oi": 0.0})
                    prefix = "put" if side == "賣權" else "call" if side == "買權" else None
                    if prefix:
                        day[f"{prefix}_volume"] += volume
                        day[f"{prefix}_oi"] += oi
    return result


def parse_taiex(raw: Path) -> dict[int, float]:
    result = {}
    for path in sorted(raw.glob("twse_taiex_*.json")):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        for row in data["data"]:
            roc, month, day = (int(x) for x in row[0].split("/"))
            date = (roc + 1911) * 10000 + month * 100 + day
            result[date] = float(row[4].replace(",", ""))
    return result


def build_context(runtime: Path) -> list[dict]:
    raw = runtime / "official_raw"
    fut, opt = parse_futures(raw), parse_options(raw)
    dates = sorted(set(fut) & set(opt))
    rows = []
    for date in dates:
        f, o = fut[date], opt[date]
        if o["call_volume"] <= 0 or o["call_oi"] <= 0:
            continue
        rows.append({
            "date": date,
            "tx_near_contract": f["tx_near_contract"],
            "tx_near_price": f["tx_near_price"],
            "tx_total_oi": f["tx_total_oi"],
            "txo_volume_pc_ratio": o["put_volume"] / o["call_volume"],
            "txo_oi_pc_ratio": o["put_oi"] / o["call_oi"],
        })
    for i, row in enumerate(rows):
        names = {
            "tx_total_oi": "tx_total_oi",
            "txo_volume_pc_ratio": "txo_volume_pc",
            "txo_oi_pc_ratio": "txo_oi_pc",
        }
        for name, output_prefix in names.items():
            row[f"{output_prefix}_change_1"] = row[name] - rows[i - 1][name] if i >= 1 else None
            row[f"{output_prefix}_change_5"] = row[name] - rows[i - 5][name] if i >= 5 else None
    path = runtime / "derivatives_context_daily.csv"
    fields = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader(); w.writerows(rows)
    return rows
