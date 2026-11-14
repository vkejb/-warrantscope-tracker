"""Offline readiness and no-wait chip-order audit; never invokes trading code."""
from __future__ import annotations

from collections import defaultdict
import csv
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import zipfile

from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from prospective_notifications_v01.chip_watch import _parse

from .chart_chip_selection import ACTIVE, DEFAULT_OUT as HISTORICAL, _previous_date
from .prospective_chart_chip import HORIZONS, OUT as PROSPECTIVE, SOURCES
from .strict_replay import _write_csv


HERE = Path(__file__).resolve().parent
OUT = HERE / "next_step_audit_results"
SEALS = HERE.parent / "expanded_shadow_universe_v01/runtime/seals"
RUNS = HERE.parent / "yuanta_intraday_shadow_v01/runtime/runs"


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _daily_source_coverage(active: dict, source_day: str) -> dict:
    found = set()
    last_date = None
    for item in active["archive_hashes"]:
        path = Path(item["path"])
        if sha256_file(path) != item["sha256"]:
            raise RuntimeError(f"daily archive hash mismatch: {path}")
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".csv")]
            if len(names) != 1:
                raise RuntimeError(f"unexpected daily archive format: {path}")
            with archive.open(names[0]) as raw:
                for row in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig")):
                    date = row["date"].replace("-", "")
                    last_date = max(last_date or date, date)
                    if date == source_day:
                        found.add(row["code"])
    return {"status": "PRESENT" if found else "MISSING", "symbol_count": len(found),
            "latest_archived_date": last_date}


def _capture_coverage(target_day: str, source_day: str, base: Path) -> dict:
    from zoneinfo import ZoneInfo
    market_open = datetime.strptime(target_day, "%Y%m%d").replace(
        hour=9, tzinfo=ZoneInfo("Asia/Taipei"))
    result = {}
    for name in SOURCES:
        folder = base / "presignal_captures" / target_day / name
        raw, metadata = folder / "raw.json", folder / "metadata.json"
        if not raw.is_file() or not metadata.is_file():
            result[name] = {"status": "MISSING"}
            continue
        meta = json.loads(metadata.read_text(encoding="utf-8"))
        valid = (meta.get("source") == name and meta.get("source_day") == source_day
                 and meta.get("target_day") == target_day
                 and meta.get("sha256") == sha256_file(raw)
                 and meta.get("status") == "PRESIGNAL_CAPTURE"
                 and datetime.fromisoformat(meta["captured_at_utc"]) < market_open)
        if valid:
            try:
                _parse(name, source_day, raw.read_bytes(), set(), meta["url"])
            except (KeyError, ValueError, TypeError, RuntimeError):
                valid = False
        result[name] = {"status": "VERIFIED_PRESIGNAL" if valid else "INVALID",
                        "sha256": sha256_file(raw),
                        "captured_at_utc": meta.get("captured_at_utc"),
                        "source_rows": meta.get("source_rows")}
    return result


def _same_time_rank(rows: list[dict], rs_by_key: dict[tuple[str, str], float]) -> list[dict]:
    """Only reorder candidates available at the same instant; never wait for later signals."""
    return sorted(rows, key=lambda row: (row["entry_time"],
                  -(row["chip_pass"] == "True"),
                  -rs_by_key[(row["session_date"], row["stock_id"])], row["stock_id"]))


def audit(target_day: str, *, out: Path = OUT, historical: Path = HISTORICAL,
          prospective: Path = PROSPECTIVE, active_path: Path = ACTIVE,
          seals: Path = SEALS, runs: Path = RUNS) -> dict:
    if len(target_day) != 8 or not target_day.isdigit():
        raise ValueError("target day must be YYYYMMDD")
    active = json.loads(active_path.read_text(encoding="utf-8"))
    source_day = _previous_date(active, target_day)
    if source_day is None:
        raise RuntimeError("no preceding trading day in calendar")
    captures = _capture_coverage(target_day, source_day, prospective)
    daily = _daily_source_coverage(active, source_day)
    seal_path = seals / f"{target_day}.json"
    if seal_path.is_file():
        from expanded_shadow_universe_v01.universe import _validate_seal
        seal = _validate_seal(json.loads(seal_path.read_text(encoding="utf-8")), target_day)
        universe = {"status": "VERIFIED", "seal_hash": seal["seal_hash"],
                    "selected_count": seal["selected_count"]}
    else:
        universe = {"status": "MISSING"}
    target_runs = sorted(path for path in runs.glob(f"{target_day}*/run_manifest.json"))
    session = {"status": "PENDING" if not target_runs else "EXISTS_NEEDS_QUALITY_REVIEW",
               "manifest_paths": [str(path) for path in target_runs]}

    known_manifest = json.loads((prospective / "known_horizon_manifest.json").read_text(encoding="utf-8"))
    for key, path in (("candidate_ledger_sha256", historical / "candidate_ledger.csv"),
                      ("account_trades_sha256", historical / "trades.csv")):
        if sha256_file(path) != known_manifest[key]:
            raise RuntimeError(f"known research source changed: {path}")
    known = [row for row in _rows(prospective / "known_horizon_labels.csv")
             if row["horizon_minutes"] == str(HORIZONS[0])]
    from .early_entry_backtest import DEFAULT_RESULTS_DIR as EARLY
    early = _rows(EARLY / "signals.csv")
    if sha256_file(EARLY / "signals.csv") != known_manifest["early_signals_sha256"]:
        raise RuntimeError("known signal source changed")
    rs = {(row["session_date"], row["stock_id"]): float(row["relative_strength_5m"])
          for row in early}
    by_day = defaultdict(list)
    for row in known:
        by_day[row["session_date"]].append(row)
    account = {(row["session_date"], row["scenario"]): row
               for row in _rows(historical / "trades.csv") if row["variant"] == "BASE"}
    ordering = []
    historical_manifest = json.loads((historical / "run_manifest.json").read_text(encoding="utf-8"))
    if historical_manifest["manifest_hash"] != known_manifest["upstream_manifest_hash"]:
        raise RuntimeError("historical manifest identity changed")
    for day in historical_manifest["dates"]:
        rows = by_day[day]
        base = sorted(rows, key=lambda row: (row["entry_time"],
                     -rs[(day, row["stock_id"])], row["stock_id"]))
        chip = _same_time_rank(rows, rs)
        old = account.get((day, "BASE"))
        if old and base and old.get("stock_id") != base[0]["stock_id"]:
            raise RuntimeError(f"baseline selection mismatch: {day}")
        ordering.append({"session_date": day, "eligible_count": len(rows),
                         "same_instant_competition": any(
                             a["entry_time"] == b["entry_time"] for a, b in zip(base, base[1:])),
                         "baseline_stock_id": base[0]["stock_id"] if base else None,
                         "chip_order_stock_id": chip[0]["stock_id"] if chip else None,
                         "selection_changed": bool(base and chip and
                                                   base[0]["stock_id"] != chip[0]["stock_id"]),
                         "baseline_account_net_pnl_twd": old.get("net_pnl_twd") if old else None,
                         "ordering_pnl_status": "SAME_TRADE_REUSED" if base == chip else "REPLAY_REQUIRED",
                         "chip_evidence": "POST_SIGNAL_HISTORICAL_RECONSTRUCTION",
                         "actual_orders": 0})
    readiness = {"target_day": target_day, "source_day": source_day,
                 "official_captures": captures, "daily_bars": daily,
                 "premarket_universe": universe, "intraday_session": session,
                 "status": "READY_FOR_LATER_SESSION" if (
                     all(x["status"] == "VERIFIED_PRESIGNAL" for x in captures.values())
                     and daily["status"] == "PRESENT" and universe["status"] == "VERIFIED")
                     else "PENDING_INPUTS", "actual_orders": 0}
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(out / "known_no_wait_ordering.csv", ordering)
    (out / f"readiness_{target_day}.json").write_bytes(canonical_bytes(readiness) + b"\n")
    summary = {"mode": "SHADOW_ONLY", "no_live_order": True, "actual_orders": 0,
               "known_dates": len(ordering), "eligible_signals": len(known),
               "same_instant_competition_days": sum(x["same_instant_competition"] for x in ordering),
               "selection_changes": sum(x["selection_changed"] for x in ordering),
               "new_date_validation": "PENDING", "readiness_status": readiness["status"],
               "historical_manifest_hash": known_manifest["upstream_manifest_hash"],
               "known_labels_sha256": sha256_file(prospective / "known_horizon_labels.csv")}
    (out / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    return {"readiness": readiness, "summary": summary}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m dynamic_market_scanner_shadow_v01.next_step_audit YYYYMMDD")
    print(json.dumps(audit(sys.argv[1]), ensure_ascii=False, indent=2))
