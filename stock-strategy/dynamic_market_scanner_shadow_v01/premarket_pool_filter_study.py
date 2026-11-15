"""Offline premarket pool-gate diagnostic on immutable expanded seals."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import datetime
import json
from pathlib import Path

from expanded_shadow_universe_v01.universe import _validate_seal
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file

from .chart_chip_selection import (
    ACTIVE, DEFAULT_OUT as HISTORICAL, VARIANTS, _chips, _daily_bars,
    _previous_date, chart_pass, chip_pass, gate, prior_bars,
)
from .early_entry_backtest import DEFAULT_RESULTS_DIR as EARLY
from .raw_signal_comparison import OUT as RAW
from .strict_replay import _write_csv


HERE = Path(__file__).resolve().parent
OUT = HERE / "premarket_pool_filter_results"
SPEC = HERE / "PREMARKET_POOL_FILTER_SPEC.md"
SEALS = HERE.parent / "expanded_shadow_universe_v01/runtime/seals"


def _read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _gate_status(chart: bool | None, chip: bool | None, variant: str) -> str:
    accepted, reason = gate(chart, chip, variant)
    return "PASS" if accepted else "UNKNOWN" if reason == "DATA_UNKNOWN" else "REJECT"


def _before_open(created_at: str, target_day: str) -> bool:
    from zoneinfo import ZoneInfo
    stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    market_open = datetime.strptime(target_day, "%Y%m%d").replace(
        hour=9, tzinfo=ZoneInfo("Asia/Taipei"))
    return stamp < market_open


def run(*, out: Path = OUT, historical: Path = HISTORICAL, raw: Path = RAW,
        seals: Path = SEALS, active_path: Path = ACTIVE) -> dict:
    known_manifest = json.loads((historical / "run_manifest.json").read_text(encoding="utf-8"))
    if known_manifest["source_signals_sha256"] != sha256_file(EARLY / "signals.csv"):
        raise RuntimeError("early signal source differs from known comparison")
    raw_summary = json.loads((raw / "summary.json").read_text(encoding="utf-8"))
    if raw_summary["source_signals_sha256"] != known_manifest["source_signals_sha256"]:
        raise RuntimeError("raw signal study source differs")
    raw_signals = _read(raw / "all_raw_signals.csv")
    if len(raw_signals) != raw_summary["raw_signal_count"]:
        raise RuntimeError("raw signal row count changed")
    by_day = defaultdict(list)
    for row in raw_signals:
        by_day[row["session_date"]].append(row)
    dates = known_manifest["dates"]
    if set(dates) != set(by_day):
        raise RuntimeError("seal dates and raw signal dates differ")
    active = json.loads(active_path.read_text(encoding="utf-8"))
    seal_by_day = {}
    all_symbols = set()
    for day in dates:
        source_day = _previous_date(active, day)
        if source_day is None:
            raise RuntimeError(f"no prior trading date for {day}")
        seal_path = seals / f"{source_day}.json"
        seal = _validate_seal(json.loads(seal_path.read_text(encoding="utf-8")), source_day)
        if not _before_open(seal["created_at"], day):
            raise RuntimeError(f"expanded seal created after target open: {day}")
        stocks = {str(item["stock_id"]) for item in seal["stocks"]}
        missing = {row["stock_id"] for row in by_day[day]} - stocks
        if missing:
            raise RuntimeError(f"raw signals outside sealed pool {day}: {sorted(missing)}")
        all_symbols.update(stocks)
        seal_by_day[day] = source_day, seal_path, seal
    bars, _active = _daily_bars(all_symbols, active_path)
    pool, impact, daily, ordering = [], [], [], []
    for day in dates:
        source_day, seal_path, seal = seal_by_day[day]
        stocks = {str(item["stock_id"]) for item in seal["stocks"]}
        chips, chip_status = _chips(historical, source_day, stocks)
        assessments = {}
        for item in seal["stocks"]:
            symbol = str(item["stock_id"])
            history = prior_bars(bars.get(symbol, []), day, source_day)
            chart, chip = chart_pass(history), chip_pass(chips, symbol)
            assessments[symbol] = {"chart": chart, "chip": chip}
            for variant in VARIANTS:
                pool.append({"session_date": day, "source_date": source_day,
                             "stock_id": symbol, "original_pool_rank": item["rank"],
                             "source_seal_hash": seal["seal_hash"],
                             "source_seal_path": str(seal_path),
                             "chart_pass": chart, "chip_pass": chip,
                             "chip_evidence": chip_status,
                             "variant": variant,
                             "status": _gate_status(chart, chip, variant),
                             "actual_orders": 0})
        for signal in by_day[day]:
            symbol = signal["stock_id"]
            assessment = assessments[symbol]
            for variant in VARIANTS:
                impact.append({"session_date": day, "stock_id": symbol,
                               "signal_time": signal["signal_time"],
                               "original_rule_pass": signal["original_rule_pass"],
                               "original_account_selected": signal["account_selected"],
                               "chip_group": signal["chip_group"],
                               "chart_pass": assessment["chart"],
                               "variant": variant,
                               "pool_status": _gate_status(assessment["chart"], assessment["chip"], variant),
                               "mark_30m_status": signal["mark_30m_status"],
                               "net_30m_twd": signal["net_30m_twd"],
                               "actual_orders": 0})
        for variant in VARIANTS:
            member = [row for row in pool if row["session_date"] == day and row["variant"] == variant]
            selected = [row for row in impact if row["session_date"] == day and row["variant"] == variant]
            counts = Counter(row["status"] for row in member)
            retained = [row for row in selected if row["pool_status"] == "PASS"]
            daily.append({"session_date": day, "source_date": source_day,
                          "variant": variant, "original_pool_count": len(stocks),
                          "pool_pass": counts["PASS"], "pool_reject": counts["REJECT"],
                          "pool_unknown": counts["UNKNOWN"],
                          "original_ready_signals": len(by_day[day]),
                          "ready_retained": len(retained),
                          "original_rule_pass_retained": sum(row["original_rule_pass"] == "True" for row in retained),
                          "original_account_selected_retained": sum(row["original_account_selected"] == "True" for row in retained),
                          "actual_orders": 0})
            first = sorted(retained, key=lambda row: (row["signal_time"], row["stock_id"]))
            ordering.append({"session_date": day, "variant": variant,
                             "first_retained_ready_stock_id": first[0]["stock_id"] if first else None,
                             "first_retained_ready_time": first[0]["signal_time"] if first else None,
                             "interpretation": "OLD_SIGNAL_SUBSET_NOT_REPLAYED_ACCOUNT"})
    metrics = []
    for variant in VARIANTS:
        rows = [row for row in impact if row["variant"] == variant]
        pool_days = [row for row in daily if row["variant"] == variant]
        blocked = [row for row in rows if row["pool_status"] != "PASS"]
        marked = [row for row in blocked if row["mark_30m_status"] == "COMPLETE"]
        metrics.append({"variant": variant, "dates": len(pool_days),
                        "pool_original_total_stock_days": sum(row["original_pool_count"] for row in pool_days),
                        "pool_pass_stock_days": sum(row["pool_pass"] for row in pool_days),
                        "pool_reject_stock_days": sum(row["pool_reject"] for row in pool_days),
                        "pool_unknown_stock_days": sum(row["pool_unknown"] for row in pool_days),
                        "raw_ready_signals": len(rows),
                        "ready_retained": sum(row["pool_status"] == "PASS" for row in rows),
                        "ready_rejected": sum(row["pool_status"] == "REJECT" for row in rows),
                        "ready_unknown": sum(row["pool_status"] == "UNKNOWN" for row in rows),
                        "original_rule_pass_retained": sum(row["pool_status"] == "PASS" and row["original_rule_pass"] == "True" for row in rows),
                        "original_account_selected_retained": sum(row["pool_status"] == "PASS" and row["original_account_selected"] == "True" for row in rows),
                        "blocked_30m_evaluable": len(marked),
                        "blocked_30m_positive": sum(float(row["net_30m_twd"]) > 0 for row in marked),
                        "blocked_30m_negative": sum(float(row["net_30m_twd"]) < 0 for row in marked),
                        "account_pnl": None, "actual_orders": 0})
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("pool_ledger.csv", pool), ("raw_signal_impact.csv", impact),
                       ("daily_pool_counts.csv", daily), ("first_ready_subset.csv", ordering),
                       ("experiments.csv", metrics)):
        _write_csv(out / name, rows)
    result = {"analysis_id": "PREMARKET_POOL_FILTER_DIAGNOSTIC_V1",
              "spec_sha256": sha256_file(SPEC), "source_signal_sha256": raw_summary["source_signals_sha256"],
              "source_raw_signals_sha256": sha256_file(raw / "all_raw_signals.csv"),
              "historical_manifest_hash": known_manifest["manifest_hash"],
              "dates": dates, "unique_pool_symbols": len(all_symbols),
              "chip_provenance": "POST_SIGNAL_HISTORICAL_RECONSTRUCTION",
              "actual_orders": 0, "broker_calls": 0,
              "account_pnl": None}
    (out / "summary.json").write_bytes(canonical_bytes(result) + b"\n")
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
