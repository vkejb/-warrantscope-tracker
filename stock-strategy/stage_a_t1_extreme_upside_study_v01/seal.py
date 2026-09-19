from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from prospective_shadow_v01.market_data_provider import ExistingDailyDataProvider
from stage_a_prospective_watchlist_v01.seal_store import latest_seal

from .entry_state import RULES, canonical, classify, digest


MODULE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = MODULE_DIR / "runtime"
STAGE_A_RUNTIME = MODULE_DIR.parent / "stage_a_prospective_watchlist_v01" / "runtime"


def _validate_stage_a_seal(path: Path) -> dict:
    stage = json.loads(path.read_text(encoding="utf-8"))
    latest = latest_seal(STAGE_A_RUNTIME)
    if latest is None or latest["signal_date"] != stage["signal_date"] or latest["seal_hash"] != stage["seal_hash"]:
        raise RuntimeError("Stage A source must be the latest verified prospective seal")
    if stage["mode"] != "SHADOW_ONLY" or len(stage["stocks"]) != 30:
        raise RuntimeError("Stage A source is not a sealed 30-stock shadow watchlist")
    return stage


def seal_entry_state(
    stage_a_seal_path: Path,
    archives: list[Path],
    calendar_path: Path,
    *,
    now: datetime | None = None,
    runtime_dir: Path = RUNTIME_DIR,
) -> dict:
    local = (now or datetime.now(ZoneInfo("Asia/Taipei"))).astimezone(ZoneInfo("Asia/Taipei"))
    stage = _validate_stage_a_seal(stage_a_seal_path)
    signal_date = stage["signal_date"]
    if signal_date == "20260918":
        if local.strftime("%Y%m%d") > "20260920":
            raise RuntimeError("2026-09-18 classification cannot first seal after T+1 begins")
    elif local.strftime("%Y%m%d") != signal_date or local.strftime("%H:%M") < "14:25":
        raise RuntimeError("prospective classification only seals on its signal date")
    snapshot = ExistingDailyDataProvider(archives, trading_calendar_path=calendar_path).load_through(signal_date)
    if snapshot.input_manifest_hash != stage["input_hash"]:
        raise RuntimeError("classification input differs from frozen Stage A input")
    stocks = {stock.code: stock for stock in snapshot.prepared_stocks}
    rows = []
    for item in stage["stocks"]:
        stock = stocks[item["stock_id"]]
        if stock.bars[-1].date != signal_date or stock.calendar_indices[-1] - stock.calendar_indices[-25] != 24:
            raise RuntimeError(f"noncontiguous regular-session lookback: {item['stock_id']}")
        state = classify(stock.bars, signal_date)
        rows.append({
            "signal_date": signal_date,
            "stock_id": item["stock_id"],
            "stock_name": item["stock_name"],
            "stage_a_rank": item["rank"],
            "stage_a_score": item["score"],
            **asdict(state),
        })
    if len(rows) != 30 or len({row["stock_id"] for row in rows}) != 30:
        raise RuntimeError("classification must contain exactly 30 unique Stage A stocks")
    content = {
        "schema_version": "1",
        "signal_date": signal_date,
        "mode": "SHADOW_ONLY",
        "evidence_label": "PROSPECTIVE_CLASSIFICATION",
        "rules": RULES,
        "config_hash": digest(RULES),
        "input_hash": stage["input_hash"],
        "stage_a_seal_hash": stage["seal_hash"],
        "stocks": rows,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    result = {**content, "seal_hash": digest(content), "created_at": local.isoformat(), "status": "SEALED"}
    target = runtime_dir / "seals" / f"{signal_date}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing.get("seal_hash") != digest({key: existing.get(key) for key in content}):
            raise RuntimeError("existing entry-state seal hash mismatch")
        # created_at differs on an idempotent rerun, but sealed content cannot.
        if {key: existing.get(key) for key in content} != content:
            raise RuntimeError("existing entry-state seal conflicts with current inputs")
        return {"status": "ALREADY_SEALED", "signal_date": signal_date, "seal_hash": existing["seal_hash"], "count": len(existing["stocks"])}
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(canonical(result).decode("utf-8") + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {"status": "SEALED", "signal_date": signal_date, "seal_hash": result["seal_hash"], "count": 30}


def seal_from_active_inputs(active_inputs_path: Path, *, now: datetime | None = None, runtime_dir: Path = RUNTIME_DIR) -> dict:
    active = json.loads(active_inputs_path.read_text(encoding="utf-8"))
    day = active["target_date"]
    stage_path = STAGE_A_RUNTIME / "seals" / f"{day}.json"
    return seal_entry_state(stage_path, [Path(value) for value in active["archives"]],
                            Path(active["trading_calendar"]), now=now, runtime_dir=runtime_dir)
