"""All early-ready signal chart/chip and fixed-horizon audit, offline only."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import datetime
import json
from pathlib import Path
from statistics import mean

from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file

from .causal_auction import _verified_selected_quotes
from .chart_chip_selection import DEFAULT_OUT as HISTORICAL
from .early_entry_backtest import DEFAULT_RESULTS_DIR as EARLY
from .prospective_chart_chip import HORIZONS, horizon_mark
from .scanner import affordable_quantity
from .strict_replay import DEFAULT_RUNS_DIR, _write_csv


HERE = Path(__file__).resolve().parent
OUT = HERE / "raw_signal_comparison_results"
SPEC = HERE / "RAW_SIGNAL_COMPARISON_SPEC.md"


def _read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _bool(value: str) -> bool | None:
    if value == "True":
        return True
    if value == "False":
        return False
    return None


def _group(rows: list[dict], horizon: int, chip_group: str) -> dict:
    selected = [row for row in rows if row["chip_group"] == chip_group]
    values = [float(row[f"net_{horizon}m_twd"]) for row in selected
              if row[f"mark_{horizon}m_status"] == "COMPLETE"]
    return {"chip_group": chip_group, "horizon_minutes": horizon,
            "raw_signal_count": len(selected),
            "distinct_dates": len({row["session_date"] for row in selected}),
            "complete_count": len(values), "not_complete_count": len(selected) - len(values),
            "partial_or_stale_count": sum(row[f"mark_{horizon}m_status"] == "PARTIAL_OR_STALE"
                                          for row in selected),
            "entry_unaffordable_count": sum(row[f"mark_{horizon}m_status"] == "ENTRY_UNAFFORDABLE"
                                            for row in selected),
            "positive_count": sum(value > 0 for value in values),
            "positive_share": sum(value > 0 for value in values) / len(values) if values else None,
            "mean_signal_net_twd": mean(values) if values else None,
            "interpretation": "ALL_SIGNALS_DIAGNOSTIC_NOT_ACCOUNT_PNL"}


def _all_groups(rows: list[dict]) -> list[dict]:
    dimensions = {
        "CHIP": lambda row: row["chip_group"],
        "CHART": lambda row: str(row["chart_pass"]),
        "CHART_X_CHIP": lambda row: f'{row["chart_pass"]}|{row["chip_group"]}',
        "ORIGINAL_RULE_REASON": lambda row: row["original_rule_reason"],
        "SESSION_DATE": lambda row: row["session_date"],
    }
    result = []
    for dimension, classifier in dimensions.items():
        buckets = defaultdict(list)
        for row in rows:
            buckets[classifier(row)].append(row)
        for label, selected in sorted(buckets.items()):
            for horizon in HORIZONS:
                values = [float(row[f"net_{horizon}m_twd"]) for row in selected
                          if row[f"mark_{horizon}m_status"] == "COMPLETE"]
                result.append({"dimension": dimension, "group": label,
                               "horizon_minutes": horizon, "raw_signal_count": len(selected),
                               "distinct_dates": len({row["session_date"] for row in selected}),
                               "original_rule_pass_count": sum(row["original_rule_pass"] == "True"
                                                               for row in selected),
                               "complete_count": len(values),
                               "not_complete_count": len(selected) - len(values),
                               "partial_or_stale_count": sum(row[f"mark_{horizon}m_status"] == "PARTIAL_OR_STALE"
                                                             for row in selected),
                               "entry_unaffordable_count": sum(row[f"mark_{horizon}m_status"] == "ENTRY_UNAFFORDABLE"
                                                               for row in selected),
                               "positive_count": sum(value > 0 for value in values),
                               "negative_count": sum(value < 0 for value in values),
                               "positive_share": (sum(value > 0 for value in values) / len(values)
                                                  if values else None),
                               "mean_signal_net_twd": mean(values) if values else None,
                               "interpretation": "SIGNAL_LABELS_NOT_ACCOUNT_PNL"})
    return result


def _no_wait_ordering(rows: list[dict]) -> list[dict]:
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["session_date"]].append(row)
    result = []
    for day, items in sorted(by_day.items()):
        baseline = sorted(items, key=lambda row: (row["signal_time"],
                          -float(row["relative_strength_5m"]), row["stock_id"]))
        chip = sorted(items, key=lambda row: (row["signal_time"],
                      row["chip_group"] != "POSITIVE",
                      -float(row["relative_strength_5m"]), row["stock_id"]))
        first_time = baseline[0]["signal_time"]
        result.append({"session_date": day, "raw_signal_count": len(items),
                       "first_signal_time": first_time,
                       "same_instant_first_count": sum(row["signal_time"] == first_time
                                                       for row in items),
                       "earliest_stock_id": baseline[0]["stock_id"],
                       "chip_tie_break_stock_id": chip[0]["stock_id"],
                       "selection_changed": baseline[0]["stock_id"] != chip[0]["stock_id"],
                       "account_pnl": None,
                       "note": "RAW_READY_ORDER_NOT_EXECUTABLE_ACCOUNT"})
    return result


def summarize_existing(*, out: Path = OUT) -> list[dict]:
    """Extend private tables from verified prior raw run without rereading quote archives."""
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    if summary["analysis_id"] != "RAW_SIGNAL_CHART_CHIP_COMPARISON_V1":
        raise RuntimeError("unexpected raw-signal study identity")
    if summary["source_signals_sha256"] != sha256_file(EARLY / "signals.csv"):
        raise RuntimeError("raw signal source changed")
    rows = _read(out / "all_raw_signals.csv")
    if len(rows) != summary["raw_signal_count"]:
        raise RuntimeError("raw signal count changed")
    groups = _all_groups(rows)
    _write_csv(out / "chip_groups.csv", [_group(rows, horizon, group)
               for horizon in HORIZONS for group in ("POSITIVE", "NONPOSITIVE", "UNKNOWN")])
    _write_csv(out / "all_groups.csv", groups)
    _write_csv(out / "raw_no_wait_ordering.csv", _no_wait_ordering(rows))
    return groups


def run(*, out: Path = OUT, early: Path = EARLY, historical: Path = HISTORICAL,
        runs: Path = DEFAULT_RUNS_DIR) -> dict:
    source = early / "signals.csv"
    manifest = json.loads((historical / "run_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("source_signals_sha256") != sha256_file(source):
        raise RuntimeError("early-ready signal hash differs from historical comparison")
    signals = _read(source)
    ledger = _read(historical / "candidate_ledger.csv")
    base_rows = [row for row in ledger if row["variant"] == "BASE"]
    joined = {(row["session_date"], row["stock_id"]): row for row in base_rows}
    if len(joined) != len(base_rows) or len(joined) != len(signals):
        raise RuntimeError("BASE ledger must contain exactly one row per raw signal")
    account = {(row["session_date"], row["stock_id"]) for row in
               _read(historical / "trades.csv") if row["variant"] == "BASE"
               and row["scenario"] == "BASE" and row["status"] != "NO_TRADE"}
    by_day = defaultdict(list)
    for signal in signals:
        key = signal["session_date"], signal["stock_id"]
        if key not in joined:
            raise RuntimeError(f"raw signal missing from BASE ledger: {key}")
        by_day[key[0]].append(signal)
    rows = []
    for day in sorted(by_day):
        cache = json.loads((early / "replay_cache" / f"{day}.json").read_text(encoding="utf-8"))
        run_dir = runs / cache["replay"]["source_run_id"]
        run_manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        if run_manifest["manifest_hash"] != cache["source_manifest_hash"]:
            raise RuntimeError(f"quote manifest identity changed: {day}")
        quotes = _verified_selected_quotes(run_dir, {item["stock_id"] for item in by_day[day]})
        for signal in by_day[day]:
            key = day, signal["stock_id"]
            prior = joined[key]
            entry_price = float(signal["entry_price"])
            quantity, _fee, _cash = affordable_quantity(entry_price)
            entry_time = datetime.fromisoformat(signal["signal_time"])
            entry = {"entry_price": entry_price, "entry_time": entry_time,
                     "quantity": quantity}
            chip = _bool(prior["chip_pass"])
            row = {"session_date": day, "stock_id": key[1],
                   "signal_time": signal["signal_time"],
                   "entry_price": entry_price, "whole_lot_quantity": quantity,
                   "relative_strength_5m": signal["relative_strength_5m"],
                   "original_daily_ready_rank": signal["daily_ready_rank"],
                   "chart_pass": prior["chart_pass"], "chip_pass": prior["chip_pass"],
                   "chip_group": ("POSITIVE" if chip is True else
                                  "NONPOSITIVE" if chip is False else "UNKNOWN"),
                   "chip_net_shares": prior["chip_net_shares"],
                   "chip_evidence": prior["chip_evidence"],
                   "original_rule_pass": prior["base_pass"],
                   "original_rule_reason": prior["base_reason"],
                   "account_selected": key in account,
                   "source_run_id": run_dir.name,
                   "source_manifest_hash": run_manifest["manifest_hash"],
                   "actual_orders": 0}
            for horizon in HORIZONS:
                mark = horizon_mark(quotes[key[1]], entry, horizon) if quantity >= 1000 else None
                row[f"mark_{horizon}m_status"] = (mark["mark_status"] if mark else
                                                  "ENTRY_UNAFFORDABLE")
                row[f"net_{horizon}m_twd"] = mark["net_pnl_twd"] if mark and mark["mark_status"] == "COMPLETE" else None
                row[f"trade_change_{horizon}m_pct"] = mark["trade_price_change_pct"] if mark else None
                row[f"bid_time_{horizon}m"] = mark["bid_time"] if mark else None
                row[f"tick_time_{horizon}m"] = mark["tick_time"] if mark else None
            rows.append(row)
    if len(rows) != len(signals):
        raise RuntimeError("raw-signal denominator changed")
    groups = [_group(rows, horizon, group) for horizon in HORIZONS
              for group in ("POSITIVE", "NONPOSITIVE", "UNKNOWN")]
    reasons = Counter(row["original_rule_reason"] for row in rows)
    summary = {"analysis_id": "RAW_SIGNAL_CHART_CHIP_COMPARISON_V1",
               "spec_sha256": sha256_file(SPEC), "source_signals_sha256": sha256_file(source),
               "historical_manifest_hash": manifest["manifest_hash"],
               "historical_ledger_sha256": sha256_file(historical / "candidate_ledger.csv"),
               "raw_signal_count": len(rows), "dates": len(by_day),
               "original_rule_pass_count": sum(row["original_rule_pass"] == "True" for row in rows),
               "account_selected_count": sum(row["account_selected"] for row in rows),
               "chip_groups": dict(Counter(row["chip_group"] for row in rows)),
               "original_rule_reasons": dict(reasons),
               "historical_chip_evidence": "POST_SIGNAL_RECONSTRUCTION",
               "portfolio_pnl": None, "actual_orders": 0, "broker_calls": 0}
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(out / "all_raw_signals.csv", rows)
    _write_csv(out / "chip_groups.csv", groups)
    _write_csv(out / "all_groups.csv", _all_groups(rows))
    _write_csv(out / "raw_no_wait_ordering.csv", _no_wait_ordering(rows))
    (out / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    return summary


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == "--summarize-existing":
        print(json.dumps({"group_rows": len(summarize_existing())}))
    elif len(sys.argv) == 1:
        print(json.dumps(run(), ensure_ascii=False, indent=2))
    else:
        raise SystemExit("usage: raw_signal_comparison [--summarize-existing]")
