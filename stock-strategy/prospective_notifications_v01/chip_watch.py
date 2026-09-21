from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import urlencode

RUNTIME = Path(__file__).resolve().parent / "runtime" / "chip_watch"
TWSE_INSTITUTIONAL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_INSTITUTIONAL = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
TWSE_MARGIN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
TPEX_MARGIN = "https://www.tpex.org.tw/www/zh-tw/margin/balance"
SOURCE_ORDER = ("TWSE_INSTITUTIONAL", "TPEX_INSTITUTIONAL", "TWSE_MARGIN", "TPEX_MARGIN")


class OfficialNotReady(RuntimeError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise RuntimeError(f"immutable chip-watch cache collision: {path.name}")
        return
    path.write_bytes(payload)


def _roc(date: int) -> str:
    text = str(date)
    return f"{int(text[:4]) - 1911:03d}/{text[4:6]}/{text[6:]}"


def _get(url: str, params: dict[str, str]) -> tuple[bytes, str]:
    full_url = f"{url}?{urlencode(params)}"
    command = [
        "/usr/bin/curl", "-sS", "--compressed", "-L", "--max-time", "45",
        "-A", "WarrantScopeResearch/1.0 (official daily chip watch)",
        "-H", "Accept: application/json,text/plain,*/*", "--get", url,
    ]
    for key, value in params.items():
        command.extend(["--data-urlencode", f"{key}={value}"])
    completed = subprocess.run(command, capture_output=True, timeout=50, check=False)
    if completed.returncode:
        raise OfficialNotReady(f"official curl exit {completed.returncode}")
    try:
        payload = json.loads(completed.stdout.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OfficialNotReady("official source is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise OfficialNotReady("official source JSON is not an object")
    return completed.stdout, full_url


def _request(source: str, date: str) -> tuple[bytes, str]:
    day = int(date)
    if source == "TWSE_INSTITUTIONAL":
        raw, url = _get(TWSE_INSTITUTIONAL, {"response": "json", "date": date, "selectType": "ALLBUT0999"})
    elif source == "TPEX_INSTITUTIONAL":
        raw, url = _get(TPEX_INSTITUTIONAL, {"l": "zh-tw", "o": "json", "se": "EW", "t": "D", "d": _roc(day), "s": "0,asc"})
    elif source == "TWSE_MARGIN":
        raw, url = _get(TWSE_MARGIN, {"response": "json", "date": date, "selectType": "ALL"})
    elif source == "TPEX_MARGIN":
        text = date
        raw, url = _get(TPEX_MARGIN, {"date": f"{text[:4]}/{text[4:6]}/{text[6:]}", "id": "", "response": "json"})
    else:  # pragma: no cover
        raise ValueError(source)
    return raw, url


def _integer(value: object) -> int:
    text = str(value).replace(",", "").strip()
    return int(text) if text not in {"", "--"} else 0


def _parse(source: str, date: str, raw: bytes, wanted: set[str], url: str) -> dict:
    payload = json.loads(raw.decode("utf-8-sig"))
    if int(payload.get("date", 0)) != int(date):
        raise OfficialNotReady(f"{source} returned a different date")
    if source == "TWSE_INSTITUTIONAL":
        if payload.get("stat") != "OK":
            raise OfficialNotReady(f"{source} not published: {payload.get('stat')}")
        fields, source_rows = payload["fields"], payload.get("data", [])
        indexes = (
            next(i for i, field in enumerate(fields) if "外陸資買賣超股數(不含外資自營商)" in field),
            fields.index("投信買賣超股數"), fields.index("自營商買賣超股數"),
        )
        names = ("foreign", "investment_trust", "dealer")
    elif source == "TPEX_INSTITUTIONAL":
        if payload.get("stat") != "ok":
            raise OfficialNotReady(f"{source} not published: {payload.get('stat')}")
        source_rows = payload.get("tables", [{}])[0].get("data", [])
        indexes, names = (10, 13, 22), ("foreign", "investment_trust", "dealer")
    elif source == "TWSE_MARGIN":
        if payload.get("stat") != "OK":
            raise OfficialNotReady(f"{source} not published: {payload.get('stat')}")
        table = next((item for item in payload.get("tables", []) if "融資融券彙總" in item.get("title", "")), None)
        if table is None:
            raise OfficialNotReady("TWSE margin detail table is absent")
        source_rows = table.get("data", [])
        indexes, names = (6, 12), ("margin_balance", "short_balance")
    else:
        if payload.get("stat") != "ok":
            raise OfficialNotReady(f"{source} not published: {payload.get('stat')}")
        source_rows = payload.get("tables", [{}])[0].get("data", [])
        indexes, names = (6, 14), ("margin_balance", "short_balance")
    if not source_rows:
        raise OfficialNotReady(f"{source} has no official market rows")
    market = "TWSE" if source.startswith("TWSE") else "TPEX"
    rows = []
    seen: set[str] = set()
    for raw_row in source_rows:
        code = str(raw_row[0]).strip()  # Leading zero is identity-significant.
        if code in seen:
            raise RuntimeError(f"duplicate exact official identity {market}:{code}")
        seen.add(code)
        if code not in wanted:
            continue
        rows.append({
            "market": market, "security_code": code, "security_type": "COMMON_STOCK",
            **{name: _integer(raw_row[index]) for name, index in zip(names, indexes)},
        })
    return {
        "schema_version": 1, "identity_schema_version": "EXACT_STRING_CODE",
        "source": source, "source_date": int(date), "source_url": url,
        "source_row_count": len(source_rows), "matched_row_count": len(rows), "rows": rows,
    }


def _load_source(source: str, date: str, wanted: set[str], runtime: Path) -> tuple[dict, str]:
    folder = runtime / date / source
    raw_path = folder / "raw.json"
    metadata_path = folder / "metadata.json"
    if raw_path.exists() != metadata_path.exists():
        raise RuntimeError(f"incomplete chip-watch cache: {source}")
    if raw_path.exists():
        raw = raw_path.read_bytes()
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if hashlib.sha256(raw).hexdigest() != metadata["raw_sha256"]:
            raise RuntimeError(f"chip-watch raw hash mismatch: {source}")
        url = metadata["source_url"]
    else:
        raw, url = _request(source, date)
    parsed = _parse(source, date, raw, wanted, url)
    metadata = {
        "source": source,
        "source_date": int(date),
        "source_url": url,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "source_row_count": parsed["source_row_count"],
        "matched_row_count": parsed["matched_row_count"],
        "parser_schema_version": parsed["schema_version"],
        "identity_schema_version": parsed["identity_schema_version"],
        "excluded_non_common_security_count": 0,
    }
    _write_immutable(raw_path, raw)
    _write_immutable(metadata_path, _canonical(metadata))
    return parsed, metadata["raw_sha256"]


def select_observation_candidates(stage: dict, entry_state: dict, chip_rows: dict[str, dict]) -> list[dict]:
    """Fixed, unvalidated watch rule: first five Stage-A-ranked OVERHEATED names.

    Chip values annotate the list; they never alter membership or sealed classifications.
    """
    states = {str(row["stock_id"]): row for row in entry_state["stocks"]}
    selected = []
    for stock in sorted(stage["stocks"], key=lambda row: int(row["rank"])):
        code = str(stock["stock_id"])
        state = states.get(code)
        if state is None or state["classification"] != "OVERHEATED":
            continue
        chip = chip_rows.get(code, {})
        foreign = chip.get("foreign")
        trust = chip.get("investment_trust")
        dealer = chip.get("dealer")
        combined = None if None in (foreign, trust, dealer) else int(foreign) + int(trust) + int(dealer)
        tags = []
        if combined is not None:
            tags.append(f"三大法人合計{'買超' if combined > 0 else '賣超' if combined < 0 else '持平'} {combined:+,}股")
        if foreign is not None:
            tags.append(f"外資 {int(foreign):+,}股")
        if trust is not None:
            tags.append(f"投信 {int(trust):+,}股")
        if chip.get("margin_balance") is not None:
            tags.append(f"融資餘額 {int(chip['margin_balance']):,}股")
        selected.append({
            "stock_id": code,
            "stock_name": stock["stock_name"],
            "stage_a_rank": int(stock["rank"]),
            "classification": state["classification"],
            "chip_tags": tags or ["官方資料無該檔可用列"],
        })
        if len(selected) == 5:
            break
    return selected


def prepare_chip_watch(date: str, stage: dict, entry_state: dict, *, runtime: Path = RUNTIME) -> dict:
    if stage["signal_date"] != date or entry_state["signal_date"] != date:
        raise RuntimeError("chip-watch date does not match sealed observations")
    if entry_state["stage_a_seal_hash"] != stage["seal_hash"]:
        raise RuntimeError("chip-watch entry-state seal mismatch")
    wanted = {str(row["stock_id"]) for row in stage["stocks"]}
    parsed_sources: list[dict] = []
    hashes: dict[str, str] = {}
    try:
        for source in SOURCE_ORDER:
            parsed, raw_hash = _load_source(source, date, wanted, runtime)
            parsed_sources.append(parsed)
            hashes[source] = raw_hash
    except (OfficialNotReady, RuntimeError, KeyError, ValueError) as exc:
        return {"status": "NOT_READY", "signal_date": date, "reason": f"{type(exc).__name__}: {exc}"}

    rows: dict[str, dict] = {}
    identities: dict[str, tuple[str, str]] = {}
    for parsed in parsed_sources:
        for row in parsed["rows"]:
            code = row["security_code"]
            identity = (row["market"], row["security_type"])
            if code in identities and identities[code] != identity:
                raise RuntimeError(f"cross-market identity collision for {code}")
            identities[code] = identity
            rows.setdefault(code, {}).update({key: row[key] for key in ("foreign", "investment_trust", "dealer", "margin_balance", "short_balance") if key in row})
    source_hash = hashlib.sha256(_canonical({"date": date, "sources": hashes, "stage_a_seal": stage["seal_hash"], "entry_state_seal": entry_state["seal_hash"]})).hexdigest()
    snapshot = {
        "schema_version": 1,
        "signal_date": date,
        "status": "COMPLETE",
        "rule": "TOP5_BY_FROZEN_STAGE_A_RANK_WITHIN_FROZEN_OVERHEATED__CHIP_ANNOTATION_ONLY",
        "evidence_status": "UNVALIDATED_RESEARCH_WATCHLIST_NOT_TRADING_SIGNAL",
        "stage_a_seal_hash": stage["seal_hash"],
        "entry_state_seal_hash": entry_state["seal_hash"],
        "source_hashes": hashes,
        "source_hash": source_hash,
        "candidates": select_observation_candidates(stage, entry_state, rows),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    snapshot_path = runtime / date / "snapshot.json"
    _write_immutable(snapshot_path, _canonical(snapshot))
    return snapshot
