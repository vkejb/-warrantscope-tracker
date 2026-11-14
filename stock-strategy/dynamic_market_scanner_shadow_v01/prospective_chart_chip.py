"""Immutable public-data capture and causal horizon labels for chart/chip research.

No broker imports, order calls, subscriptions, or scheduling.
"""
from __future__ import annotations

from bisect import bisect_right
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

from prospective_notifications_v01.chip_watch import _parse, _request
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import _projected_net_pnl

from .causal_auction import _verified_selected_quotes
from .chart_chip_selection import DEFAULT_OUT as HISTORICAL, _previous_date
from .early_entry_backtest import DEFAULT_RESULTS_DIR as EARLY
from .signal_outcomes import _mark, OUTCOME_POLICY
from .signal_selection_rule_impact import current_opportunity
from .strict_replay import DEFAULT_RUNS_DIR, _write_csv


HERE = Path(__file__).resolve().parent
SPEC = HERE / "PROSPECTIVE_CHART_CHIP_SPEC.md"
OUT = HERE / "prospective_chart_chip_results"
SOURCES = ("TWSE_INSTITUTIONAL", "TPEX_INSTITUTIONAL")
HORIZONS = (5, 15, 30)


def _read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _write_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise RuntimeError(f"immutable research capture collision: {path}")
        return
    path.write_bytes(payload)


def capture(source_day: str, target_day: str, *, out: Path = OUT,
            now: datetime | None = None, fetch=_request) -> dict:
    """Fetch both public daily files; never infer a missing source as zero."""
    if not (source_day.isdigit() and target_day.isdigit()
            and len(source_day) == len(target_day) == 8):
        raise ValueError("dates must be YYYYMMDD")
    active = json.loads((HERE.parent / "shadow_daily_runner/runtime/active_inputs.json").read_text(encoding="utf-8"))
    with Path(active["trading_calendar"]).open(encoding="utf-8", newline="") as stream:
        trading_days = {row["date"].replace("-", "") for row in csv.DictReader(stream)}
    if target_day not in trading_days:
        raise ValueError("target is not in the trading calendar")
    if _previous_date(active, target_day) != source_day:
        raise ValueError("source is not the calendar's preceding trading session")
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise ValueError("now must have a timezone")
    from zoneinfo import ZoneInfo
    market_open = datetime.strptime(target_day, "%Y%m%d").replace(
        hour=9, tzinfo=ZoneInfo("Asia/Taipei"))
    if observed >= market_open:
        raise ValueError("capture after target open cannot be presignal")
    result = {"source_day": source_day, "target_day": target_day, "sources": {},
              "mode": "SHADOW_ONLY", "actual_orders": 0}
    for source in SOURCES:
        folder = out / "presignal_captures" / target_day / source
        raw_path, meta_path = folder / "raw.json", folder / "metadata.json"
        if raw_path.exists() != meta_path.exists():
            raise RuntimeError(f"partial research capture: {folder}")
        if meta_path.exists():
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            if (metadata["source_day"] != source_day or metadata["target_day"] != target_day
                    or metadata["source"] != source or sha256_file(raw_path) != metadata["sha256"]):
                raise RuntimeError(f"existing research capture identity/hash mismatch: {folder}")
            if (metadata.get("status") != "PRESIGNAL_CAPTURE" or
                    datetime.fromisoformat(metadata["captured_at_utc"]) >= market_open):
                raise RuntimeError(f"existing research capture is not presignal: {folder}")
            _parse(source, source_day, raw_path.read_bytes(), set(), metadata["url"])
        else:
            raw, url = fetch(source, source_day)
            parsed = _parse(source, source_day, raw, set(), url)
            completed = now or datetime.now(timezone.utc)
            if completed >= market_open:
                raise RuntimeError("download completed after target open")
            metadata = {"source": source, "source_day": source_day, "target_day": target_day,
                        "url": url, "captured_at_utc": completed.astimezone(timezone.utc).isoformat(),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "source_rows": parsed["source_row_count"],
                        "status": "PRESIGNAL_CAPTURE", "actual_orders": 0}
            _write_once(raw_path, raw)
            _write_once(meta_path, canonical_bytes(metadata) + b"\n")
        result["sources"][source] = {"sha256": metadata["sha256"],
                                      "captured_at_utc": metadata["captured_at_utc"],
                                      "source_rows": metadata["source_rows"]}
    return result


def _fresh_tick(data: dict, target: datetime) -> dict | None:
    index = bisect_right(data["tick_times"], target) - 1
    if index < 0:
        return None
    row = data["ticks"][index]
    age = (target - row["time"]).total_seconds()
    return row if 0 <= age <= float(OUTCOME_POLICY["maximum_quote_age_seconds"]) else None


def horizon_mark(data: dict, entry: dict, minutes: int) -> dict:
    target = entry["entry_time"] + timedelta(minutes=minutes)
    book, tick = _mark(data["books"], target), _fresh_tick(data, target)
    result = {"horizon_minutes": minutes, "target_time": target.isoformat(),
              "mark_status": "COMPLETE" if book and tick else "PARTIAL_OR_STALE",
              "bid_time": book["time"].isoformat() if book else None,
              "tick_time": tick["time"].isoformat() if tick else None,
              "trade_price_change_pct": ((tick["price"] / entry["entry_price"] - 1) * 100
                                         if tick else None),
              "bid_roundtrip_change_pct": ((book["best_bid"] / entry["entry_price"] - 1) * 100
                                           if book else None),
              "net_pnl_twd": (_projected_net_pnl("LONG", entry["entry_price"],
                                                book["best_bid"], entry["quantity"])[3]
                              if book else None)}
    return result


def analyze_known(*, out: Path = OUT) -> dict:
    """Replay only frozen old candidates for labels; never alter upstream outputs."""
    source = HISTORICAL
    prior_manifest = json.loads((source / "run_manifest.json").read_text(encoding="utf-8"))
    if prior_manifest["analysis_id"] != "CHART_CHIP_SELECTION_V1":
        raise RuntimeError("unexpected upstream analysis")
    ledger = [row for row in _read(source / "candidate_ledger.csv")
              if row["variant"] == "BASE" and row["base_pass"] == "True"]
    early = {(row["session_date"], row["stock_id"]): row
             for row in _read(EARLY / "signals.csv")}
    account = {(row["session_date"], row["stock_id"]): row
               for row in _read(source / "trades.csv")
               if row["variant"] == "BASE" and row["scenario"] == "BASE"
               and row["status"] != "NO_TRADE"}
    rows = []
    for dated in sorted({row["session_date"] for row in ledger}):
        daily = [row for row in ledger if row["session_date"] == dated]
        cache = json.loads((EARLY / "replay_cache" / f"{dated}.json").read_text(encoding="utf-8"))["replay"]
        run_dir = DEFAULT_RUNS_DIR / cache["source_run_id"]
        run_manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        if run_manifest["manifest_hash"] != cache["source_manifest_hash"]:
            raise RuntimeError(f"intraday manifest changed: {dated}")
        quotes = _verified_selected_quotes(run_dir, {row["stock_id"] for row in daily})
        for row in daily:
            key = dated, row["stock_id"]
            raw = early[key]
            signal = {**raw, "signal_time": datetime.fromisoformat(raw["signal_time"]),
                      "entry_price": float(raw["entry_price"]),
                      "relative_strength_5m": float(raw["relative_strength_5m"])}
            data = quotes[row["stock_id"]]
            entry = current_opportunity(signal, data)
            if not entry["passed"]:
                raise RuntimeError(f"upstream candidate no longer passes: {key}")
            trade = account.get(key)
            for minutes in HORIZONS:
                mark = horizon_mark(data, entry, minutes)
                rows.append({"session_date": dated, "stock_id": row["stock_id"],
                             "signal_time": raw["signal_time"],
                             "entry_time": entry["entry_time"].isoformat(),
                             "entry_price": entry["entry_price"],
                             "quantity": entry["quantity"],
                             "chart_pass": row["chart_pass"], "chip_pass": row["chip_pass"],
                             "chip_evidence": "HISTORICAL_RECONSTRUCTION",
                             "account_selected": trade is not None,
                             "account_net_pnl_twd": trade.get("net_pnl_twd") if trade else None,
                             "account_exit_reason": trade.get("exit_reason") if trade else None,
                             **mark, "actual_orders": 0})
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(out / "known_horizon_labels.csv", rows)
    summary = {"analysis_id": "PROSPECTIVE_CHART_CHIP_V1", "scope": "KNOWN_EXPLORATORY",
               "spec_sha256": sha256_file(SPEC),
               "code_sha256": sha256_file(Path(__file__)),
               "upstream_manifest_hash": prior_manifest["manifest_hash"],
               "candidate_ledger_sha256": sha256_file(source / "candidate_ledger.csv"),
               "account_trades_sha256": sha256_file(source / "trades.csv"),
               "early_signals_sha256": sha256_file(EARLY / "signals.csv"),
               "candidate_count": len(ledger), "horizon_rows": len(rows),
               "complete_by_horizon": {str(minutes): sum(row["mark_status"] == "COMPLETE"
                                                        for row in rows if row["horizon_minutes"] == minutes)
                                       for minutes in HORIZONS},
               "historical_presignal_chip_evidence": "UNKNOWN_NOT_CAPTURED",
               "new_date_validation": "PENDING", "actual_orders": 0, "broker_calls": 0}
    (out / "known_horizon_manifest.json").write_bytes(canonical_bytes(summary) + b"\n")
    return summary


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "capture" and len(sys.argv) == 4:
        print(json.dumps(capture(sys.argv[2], sys.argv[3]), ensure_ascii=False, indent=2))
    elif command == "analyze-known" and len(sys.argv) == 2:
        print(json.dumps(analyze_known(), ensure_ascii=False, indent=2))
    else:
        raise SystemExit("usage: prospective_chart_chip capture SOURCE_YYYYMMDD TARGET_YYYYMMDD | analyze-known")
