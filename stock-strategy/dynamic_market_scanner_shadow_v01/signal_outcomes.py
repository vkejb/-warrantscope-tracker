"""Fixed-horizon executable outcome table for every parent approved signal."""

from __future__ import annotations

from bisect import bisect_right
import csv
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from statistics import mean, median
from typing import Any

from expanded_shadow_universe_v01.paper import _numbers, _read_jsonl
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC, _projected_net_pnl
from yuanta_intraday_shadow_v01.live_parity_backtest import _parse_stamp

from .scanner import affordable_quantity
from .strict_ablation import load_ledger
from .strict_policy import STRICT_POLICY_HASH
from .strict_replay import DEFAULT_PAPER_DIR, DEFAULT_RESULTS_DIR, DEFAULT_RUNS_DIR, _write_csv, discover_sources


OUTCOME_POLICY = {
    "policy_id": "EXPANDED_APPROVED_SIGNAL_OUTCOMES_V1",
    "frozen_at_taipei": "2026-11-10T00:00:00+08:00",
    "parent_policy_hash": STRICT_POLICY_HASH,
    "mode": "KNOWN_ARCHIVE_FIXED_HORIZON_DIAGNOSTIC",
    "horizons_minutes": [1, 3, 5, 15, 30],
    "tracks": {
        "ORIGINAL": "production proxy entry at original approved signal",
        "DELAYED_60S": "ask plus one tick at frozen 60-second checkpoint when evaluable",
    },
    "exit_mark": "latest received best bid at or before exact checkpoint",
    "maximum_quote_age_seconds": float(SPEC["maximum_book_staleness_seconds"]),
    "cost_model": "existing projected round-trip commission and sell tax",
    "interpretation": "all-signal diagnostic is not an executable shared-capital account",
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
OUTCOME_POLICY_HASH = hashlib.sha256(canonical_bytes(OUTCOME_POLICY)).hexdigest()
DEFAULT_OUTCOME_RESULTS_DIR = DEFAULT_RESULTS_DIR.parent / "signal_outcome_results"


def _verified_selected_books(run_dir: Path, symbols: set[str]) -> dict[str, list[dict[str, Any]]]:
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != manifest.get("manifest_hash"):
        raise RuntimeError(f"source manifest hash mismatch: {run_dir.name}")
    book_name = next((name for name in ("expanded_books.jsonl.gz", "expanded_books.jsonl")
                      if name in manifest.get("artifacts", {})), None)
    if book_name is None:
        raise RuntimeError(f"expanded books absent: {run_dir.name}")
    for name in (book_name, "watchlist.json"):
        if sha256_file(run_dir / name) != manifest["artifacts"][name]:
            raise RuntimeError(f"source artifact hash mismatch: {run_dir.name}/{name}")
    watchlist = json.loads((run_dir / "watchlist.json").read_text(encoding="utf-8"))
    expanded = watchlist.get("expanded_shadow_universe") or {}
    if expanded.get("seal_hash") != manifest.get("expanded_universe_seal_hash"):
        raise RuntimeError(f"expanded seal mismatch: {run_dir.name}")
    sealed = {str(row["stock_id"]) for row in expanded.get("stocks", [])}
    missing = symbols - sealed
    if missing:
        raise RuntimeError(f"signals outside sealed universe: {sorted(missing)}")
    output = {symbol: [] for symbol in symbols}
    for row in _read_jsonl(run_dir / book_name):
        symbol = str(row.get("stock_id", ""))
        if symbol not in output:
            continue
        buys, sells = _numbers(row.get("buy_volumes")), _numbers(row.get("sell_volumes"))
        buy_prices, sell_prices = _numbers(row.get("buy_prices")), _numbers(row.get("sell_prices"))
        if not buys or not sells or not buy_prices or not sell_prices:
            continue
        if buy_prices[0] <= 0 or sell_prices[0] < buy_prices[0]:
            continue
        output[symbol].append({
            "time": _parse_stamp(row["received_at"]),
            "best_bid": buy_prices[0], "best_ask": sell_prices[0],
            "buy_volume": sum(buys), "sell_volume": sum(sells),
        })
    for books in output.values():
        books.sort(key=lambda row: row["time"])
    return output


def _mark(books: list[dict[str, Any]], target: datetime) -> dict[str, Any] | None:
    times = [row["time"] for row in books]
    index = bisect_right(times, target) - 1
    if index < 0:
        return None
    row = books[index]
    age = (target - row["time"]).total_seconds()
    if not 0 <= age <= float(OUTCOME_POLICY["maximum_quote_age_seconds"]):
        return None
    return {**row, "age_seconds": age}


def _path(books: list[dict[str, Any]], entry_time: datetime, entry_price: float,
          quantity: int, minutes: int = 30) -> tuple[float | None, float | None]:
    end = entry_time + timedelta(minutes=minutes)
    values = [_projected_net_pnl("LONG", entry_price, float(row["best_bid"]), quantity)[3]
              for row in books if entry_time <= row["time"] <= end]
    return (max(values), min(values)) if values else (None, None)


def _track(row: dict[str, Any], books: list[dict[str, Any]], prefix: str,
           entry_time: datetime | None, entry_price: float | None,
           quantity: int) -> dict[str, Any]:
    output: dict[str, Any] = {
        f"{prefix}_entry_time": entry_time,
        f"{prefix}_entry_price": entry_price,
        f"{prefix}_quantity": quantity,
        f"{prefix}_evaluable": bool(entry_time and entry_price and quantity >= 1000),
    }
    if not output[f"{prefix}_evaluable"]:
        output[f"{prefix}_status"] = "ENTRY_NOT_EVALUABLE"
        return output
    assert entry_time is not None and entry_price is not None
    for minutes in OUTCOME_POLICY["horizons_minutes"]:
        mark = _mark(books, entry_time + timedelta(minutes=int(minutes)))
        key = f"{prefix}_{minutes}m"
        if mark is None:
            output[f"{key}_status"] = "QUOTE_MISSING_OR_STALE"
            output[f"{key}_exit_bid"] = None
            output[f"{key}_net_pnl_twd"] = None
            output[f"{key}_net_return"] = None
            output[f"{key}_quote_age_seconds"] = None
            continue
        net = _projected_net_pnl("LONG", entry_price, float(mark["best_bid"]), quantity)[3]
        output[f"{key}_status"] = "EVALUABLE"
        output[f"{key}_exit_bid"] = mark["best_bid"]
        output[f"{key}_net_pnl_twd"] = net
        output[f"{key}_net_return"] = net / (entry_price * quantity)
        output[f"{key}_quote_age_seconds"] = mark["age_seconds"]
    mfe, mae = _path(books, entry_time, entry_price, quantity)
    output[f"{prefix}_mfe_30m_net_pnl_twd"] = mfe
    output[f"{prefix}_mae_30m_net_pnl_twd"] = mae
    output[f"{prefix}_status"] = "EVALUATED"
    return output


def outcome_row(row: dict[str, Any], books: list[dict[str, Any]]) -> dict[str, Any]:
    original_price = float(row["original_entry_price"])
    original_quantity, _fee, _cash = affordable_quantity(original_price)
    delayed_price = row.get("delayed_entry_price")
    delayed_quantity = int(row.get("delayed_entry_quantity") or 0)
    delayed_time = row["confirmation_time"] if delayed_price and delayed_quantity else None
    return {
        "session_date": row["session_date"], "stock_id": row["stock_id"],
        "stock_name": row["stock_name"], "signal_time": row["signal_time"],
        "signal_market_regime": row.get("signal_market_regime"),
        "production_score": row.get("production_score"),
        "relative_strength_5m": row.get("relative_strength_5m"),
        "parent_strict_reason": row.get("strict_reason"),
        "confirmation_flow_delta": row.get("confirmation_flow_delta"),
        "hypothetical_net_progress_twd": row.get("hypothetical_net_progress_twd"),
        **_track(row, books, "original", row["signal_time"], original_price,
                 original_quantity),
        **_track(row, books, "delayed", delayed_time,
                 float(delayed_price) if delayed_price else None, delayed_quantity),
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
    }


def _metrics(rows: list[dict[str, Any]], track: str, minutes: int) -> dict[str, Any]:
    key = f"{track}_{minutes}m_net_pnl_twd"
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    daily: dict[str, float] = {}
    for row in rows:
        if row.get(key) is not None:
            daily[str(row["session_date"])] = daily.get(str(row["session_date"]), 0) + float(row[key])
    return {
        "track": track, "horizon_minutes": minutes,
        "signal_count": len(rows), "evaluable_count": len(values),
        "positive_count": sum(value > 0 for value in values),
        "win_rate": sum(value > 0 for value in values) / len(values) if values else None,
        "mean_net_pnl_twd": mean(values) if values else None,
        "median_net_pnl_twd": median(values) if values else None,
        "diagnostic_sum_net_pnl_twd": sum(values),
        "positive_day_count": sum(value > 0 for value in daily.values()),
        "evaluable_day_count": len(daily),
        "note": "ALL_SIGNALS_NOT_SHARED_CAPITAL_ACCOUNT",
    }


def _daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for day in sorted({str(row["session_date"]) for row in rows}):
        items = [row for row in rows if str(row["session_date"]) == day]
        base = {"session_date": day, "signal_count": len(items)}
        for track in ("original", "delayed"):
            for minutes in (5, 30):
                key = f"{track}_{minutes}m_net_pnl_twd"
                values = [float(row[key]) for row in items if row.get(key) is not None]
                base[f"{track}_{minutes}m_evaluable_count"] = len(values)
                base[f"{track}_{minutes}m_positive_count"] = sum(value > 0 for value in values)
                base[f"{track}_{minutes}m_mean_net_pnl_twd"] = mean(values) if values else None
        output.append(base)
    return output


def _time_bucket(stamp: datetime) -> str:
    if stamp.hour < 10:
        return "EARLY_BEFORE_1000"
    if (stamp.hour, stamp.minute) < (11, 30):
        return "MID_1000_1129"
    return "LATE_1130_OR_LATER"


def _price_bucket(price: float) -> str:
    if price <= 50:
        return "LE_50"
    if price <= 100:
        return "GT_50_LE_100"
    return "GT_100_LE_190"


def _sign_bucket(value: Any) -> str:
    if value is None or value == "":
        return "MISSING"
    return "POSITIVE" if float(value) > 0 else "NONPOSITIVE"


def _feature_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dimensions = {
        "MARKET_REGIME": lambda row: str(row.get("signal_market_regime") or "MISSING"),
        "SIGNAL_TIME": lambda row: _time_bucket(row["signal_time"]),
        "ENTRY_PRICE": lambda row: _price_bucket(float(row["original_entry_price"])),
        "CONFIRMATION_FLOW_SIGN": lambda row: _sign_bucket(row.get("confirmation_flow_delta")),
        "HYPOTHETICAL_PROGRESS_SIGN": lambda row: _sign_bucket(row.get("hypothetical_net_progress_twd")),
    }
    output = []
    for dimension, classifier in dimensions.items():
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(classifier(row), []).append(row)
        for group, items in sorted(groups.items()):
            for minutes in (5, 30):
                key = f"original_{minutes}m_net_pnl_twd"
                values = [float(row[key]) for row in items if row.get(key) is not None]
                daily: dict[str, float] = {}
                for row in items:
                    if row.get(key) is not None:
                        day = str(row["session_date"])
                        daily[day] = daily.get(day, 0) + float(row[key])
                best_day = max(daily, key=daily.get) if daily else None
                best_day_pnl = daily.get(best_day, 0.0) if best_day else None
                total = sum(values)
                output.append({
                    "dimension": dimension, "group": group,
                    "horizon_minutes": minutes, "signal_count": len(items),
                    "evaluable_count": len(values),
                    "positive_count": sum(value > 0 for value in values),
                    "win_rate": sum(value > 0 for value in values) / len(values) if values else None,
                    "mean_net_pnl_twd": mean(values) if values else None,
                    "median_net_pnl_twd": median(values) if values else None,
                    "diagnostic_sum_net_pnl_twd": total,
                    "best_day": best_day, "best_day_net_pnl_twd": best_day_pnl,
                    "net_pnl_ex_best_day_twd": total - best_day_pnl if best_day_pnl is not None else None,
                    "note": "DESCRIPTIVE_POST_HOC_NOT_SELECTION_RULE",
                })
    return output


def _ranking_diagnostics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for day in sorted({str(row["session_date"]) for row in rows}):
        items = [row for row in rows
                 if str(row["session_date"]) == day
                 and row.get("original_30m_net_pnl_twd") is not None]
        if not items:
            continue
        selectors = {
            "EARLIEST_SIGNAL_CAUSAL": min(items, key=lambda row: row["signal_time"]),
            "HIGHEST_PRODUCTION_SCORE_FULL_DAY_NONCAUSAL": max(
                items, key=lambda row: float(row.get("production_score") or float("-inf"))),
            "HIGHEST_RELATIVE_STRENGTH_FULL_DAY_NONCAUSAL": max(
                items, key=lambda row: float(row.get("relative_strength_5m") or float("-inf"))),
        }
        for selector, selected in selectors.items():
            output.append({
                "session_date": day, "selector": selector,
                "stock_id": selected["stock_id"],
                "signal_time": selected["signal_time"],
                "production_score": selected.get("production_score"),
                "relative_strength_5m": selected.get("relative_strength_5m"),
                "original_30m_net_pnl_twd": selected["original_30m_net_pnl_twd"],
                "causal_strategy_eligible": selector == "EARLIEST_SIGNAL_CAUSAL",
                "note": ("DESCRIPTIVE_ONLY_FULL_DAY_HINDSIGHT"
                         if "NONCAUSAL" in selector else "CAUSAL_BUT_FIXED_HORIZON_NOT_ACCOUNT_EXIT"),
            })
    return output


def run_outcomes(
    ledger_path: Path = DEFAULT_RESULTS_DIR / "candidate_ledger.csv",
    results_dir: Path = DEFAULT_OUTCOME_RESULTS_DIR,
    paper_dir: Path = DEFAULT_PAPER_DIR,
    runs_dir: Path = DEFAULT_RUNS_DIR,
) -> dict[str, Any]:
    ledger = load_ledger(ledger_path)
    source_by_date = {str(paper["session_date"]): run_dir
                      for run_dir, paper in discover_sources(paper_dir, runs_dir)}
    outcomes: list[dict[str, Any]] = []
    for day in sorted({str(row["session_date"]) for row in ledger}):
        day_rows = [row for row in ledger if str(row["session_date"]) == day]
        symbols = {str(row["stock_id"]) for row in day_rows}
        books = _verified_selected_books(source_by_date[day], symbols)
        outcomes.extend(outcome_row(row, books[str(row["stock_id"])]) for row in day_rows)
    metrics = [_metrics(outcomes, track, minutes)
               for track in ("original", "delayed")
               for minutes in OUTCOME_POLICY["horizons_minutes"]]
    parent_summary = json.loads((ledger_path.parent / "summary.json").read_text(encoding="utf-8"))
    results_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(results_dir / "signal_outcomes.csv", outcomes)
    _write_csv(results_dir / "horizon_metrics.csv", metrics)
    _write_csv(results_dir / "daily_diagnostics.csv", _daily(outcomes))
    _write_csv(results_dir / "feature_diagnostics.csv", _feature_groups(outcomes))
    _write_csv(results_dir / "ranking_diagnostics.csv", _ranking_diagnostics(outcomes))
    summary = {
        "analysis_id": OUTCOME_POLICY["policy_id"],
        "outcome_policy_hash": OUTCOME_POLICY_HASH,
        "parent_policy_hash": STRICT_POLICY_HASH,
        "archive_session_count": int(parent_summary["session_count"]),
        "sessions_with_signals": len({str(row["session_date"]) for row in outcomes}),
        "signal_count": len(outcomes), "metrics": metrics,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
        "interpretation": "KNOWN_ARCHIVE_ALL_SIGNAL_DIAGNOSTIC_NOT_ACCOUNT_PERFORMANCE",
    }
    (results_dir / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    manifest = {"policy": OUTCOME_POLICY, "summary": summary,
                "parent_ledger": str(ledger_path)}
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (results_dir / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return summary
