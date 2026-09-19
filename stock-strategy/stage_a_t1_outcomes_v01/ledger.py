"""Append-only T+1 outcome revisions, independent of immutable signal seals."""

from __future__ import annotations

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from prospective_shadow_v01.market_data_provider import ExistingDailyDataProvider
from stage_a_prospective_watchlist_v01.seal_store import RUNTIME_DIR as STAGE_A_RUNTIME
from stage_a_t1_extreme_upside_study_v01.entry_state import canonical, digest
from stage_a_t1_extreme_upside_study_v01.official_limits import twse_limits, tpex_next_limits
from stage_a_t1_extreme_upside_study_v01.outcomes import evaluate, next_session_bar


RUNTIME = Path(__file__).resolve().parent / "runtime"


def verify(path: Path) -> tuple[set[tuple[str, str]], str, int]:
    keys = set()
    previous = "GENESIS"
    count = 0
    if not path.exists():
        return keys, previous, count
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        content = {key: value for key, value in item.items() if key != "outcome_hash"}
        if item["previous_hash"] != previous or item["outcome_hash"] != digest(content):
            raise RuntimeError("Stage A outcome ledger hash chain invalid")
        key = (item["signal_date"], item["stock_id"])
        if key in keys:
            raise RuntimeError("Stage A outcome ledger duplicate key")
        keys.add(key)
        previous = item["outcome_hash"]
        count += 1
    return keys, previous, count


def update(active_inputs: Path, official_audit: Path, *, now: datetime | None = None, runtime: Path = RUNTIME) -> dict:
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "update.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _update_locked(active_inputs, official_audit, now=now, runtime=runtime)


def _update_locked(active_inputs: Path, official_audit: Path, *, now: datetime | None = None, runtime: Path = RUNTIME) -> dict:
    local = (now or datetime.now(ZoneInfo("Asia/Taipei"))).astimezone(ZoneInfo("Asia/Taipei"))
    active = json.loads(active_inputs.read_text(encoding="utf-8"))
    through = active["target_date"]
    if local.strftime("%Y%m%d") < through or (local.strftime("%Y%m%d") == through and local.strftime("%H:%M") < "14:25"):
        raise RuntimeError("T+1 regular session has not ended")
    snapshot = ExistingDailyDataProvider([Path(x) for x in active["archives"]], trading_calendar_path=Path(active["trading_calendar"])).load_through(through)
    audit = json.loads(official_audit.read_text(encoding="utf-8"))
    stocks = {stock.code: stock for stock in snapshot.prepared_stocks}
    ledger_path = runtime / "outcomes.jsonl"
    existing, previous, before = verify(ledger_path)
    pending = []
    for path in sorted((STAGE_A_RUNTIME / "seals").glob("*.json")):
        seal = json.loads(path.read_text(encoding="utf-8"))
        signal_date = seal["signal_date"]
        if signal_date not in snapshot.benchmark.calendar:
            continue
        session_index = snapshot.benchmark.calendar.index(signal_date)
        if session_index + 1 >= len(snapshot.benchmark.calendar):
            continue
        outcome_date = snapshot.benchmark.calendar[session_index + 1]
        if outcome_date > through:
            continue
        keys = ("schema_version", "signal_date", "setup", "mode", "stocks", "model_hash", "model_spec_hash", "config_hash", "input_hash", "eligible_stock_count")
        if seal["seal_hash"] != digest({key: seal[key] for key in keys}) or len(seal["stocks"]) != 30:
            raise RuntimeError(f"Stage A signal seal invalid: {signal_date}")
        # Reuse the content-addressed official study cache; no refetch needed.
        official_cache = Path(__file__).resolve().parents[1] / "stage_a_t1_extreme_upside_study_v01" / "runtime" / "official_limits_all"
        official_twse, provenance = twse_limits(outcome_date, official_cache)
        official_tpex, tpex_source = tpex_next_limits(signal_date, audit)
        if set(official_twse).intersection(official_tpex):
            raise RuntimeError("official market price-limit identity collision")
        limits = {**official_twse, **official_tpex}
        state_path = Path(__file__).resolve().parents[1] / "stage_a_t1_extreme_upside_study_v01" / "runtime" / "seals" / f"{signal_date}.json"
        state = None
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            fields = ("schema_version", "signal_date", "mode", "evidence_label", "rules", "config_hash", "input_hash", "stage_a_seal_hash", "stocks", "actual_orders", "actual_fills", "broker_connections")
            if state["seal_hash"] != digest({key: state[key] for key in fields}) or state["stage_a_seal_hash"] != seal["seal_hash"]:
                raise RuntimeError("entry-state classification seal mismatch")
        state_by_code = {item["stock_id"]: item["classification"] for item in state["stocks"]} if state else {}
        for item in seal["stocks"]:
            key = (signal_date, item["stock_id"])
            if key in existing:
                continue
            stock = stocks.get(item["stock_id"])
            bar = next_session_bar(stock, signal_date, snapshot.benchmark.calendar) if stock else None
            if stock is None or bar is None or item["stock_id"] not in limits:
                raise RuntimeError(f"T+1 outcome incomplete: {key}")
            signal_bar = next((row for row in stock.bars if row.date == signal_date), None)
            if signal_bar is None:
                raise RuntimeError(f"signal close missing: {key}")
            value = evaluate(signal_bar.close, bar, limits[item["stock_id"]])
            pending.append({"signal_date": signal_date, "outcome_date": outcome_date, "stock_id": item["stock_id"], "stage_a_rank": item["rank"], "stage_a_score": item["score"], "entry_state": state_by_code.get(item["stock_id"], "NOT_PROSPECTIVELY_CLASSIFIED"), **value, "signal_seal_hash": seal["seal_hash"], "official_twse_source_sha256": provenance["sha256"], "official_tpex_source_sha256": tpex_source["sha256"], "matured_at": local.isoformat()})
    if pending:
        runtime.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(ledger_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            for item in pending:
                content = {**item, "previous_hash": previous}
                hashed = {**content, "outcome_hash": digest(content)}
                handle.write(canonical(hashed) + b"\n")
                previous = hashed["outcome_hash"]
            handle.flush()
            os.fsync(handle.fileno())
    _, final, count = verify(ledger_path)
    if count != before + len(pending):
        raise RuntimeError("outcome ledger append count mismatch")
    return {"status": "OK", "appended": len(pending), "rows": count, "last_hash": final, "actual_orders": 0, "actual_fills": 0, "broker_connections": 0}
