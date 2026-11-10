"""Frozen five-minute causal candidate auction for expanded LONG signals."""

from __future__ import annotations

import csv
from datetime import datetime, time, timedelta
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any

from expanded_shadow_universe_v01.paper import _numbers, _read_jsonl
from paper_shadow_v01.runner import _load_market_context
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC, _archived_exchange_time
from yuanta_intraday_shadow_v01.live_parity_backtest import _feed_until, _parse_stamp

from .scanner import affordable_quantity
from .strict_ablation import load_ledger
from .strict_policy import STRICT_POLICY_HASH
from .strict_replay import (
    DEFAULT_PAPER_DIR, DEFAULT_RESULTS_DIR, DEFAULT_RUNS_DIR,
    PASS_REASONS, _engine, _simulate_exit, _write_csv, discover_sources,
)


AUCTION_POLICY = {
    "policy_id": "CAUSAL_RELATIVE_STRENGTH_AUCTION_5M_V1",
    "frozen_at_taipei": "2026-11-10T00:00:00+08:00",
    "mode": "SHADOW_ONLY_KNOWN_ARCHIVE_CAUSAL_ACCOUNT_REPLAY",
    "parent_policy_hash": STRICT_POLICY_HASH,
    "direction": "LONG_ONLY",
    "auction_start": "09:05",
    "auction_last_close": "13:10",
    "window_seconds": 300,
    "window_membership": "START_INCLUSIVE_CLOSE_EXCLUSIVE",
    "candidate_source": "FIRST_PRODUCTION_APPROVED_SIGNAL_PER_SYMBOL",
    "close_revalidation": "FULL_CURRENT_PRODUCTION_ENGINE_ENTRY_GATES",
    "ranking": "CURRENT_RELATIVE_STRENGTH_5M_THEN_CURRENT_PRODUCTION_SCORE",
    "selection": "FIRST_WINDOW_WITH_AT_LEAST_ONE_REVALIDATED_CANDIDATE",
    "maximum_entry_price_twd": 190.0,
    "capital_twd": 190_000,
    "maximum_trades_per_session": 1,
    "maximum_positions": 1,
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
AUCTION_POLICY_HASH = hashlib.sha256(canonical_bytes(AUCTION_POLICY)).hexdigest()
DEFAULT_AUCTION_RESULTS_DIR = DEFAULT_RESULTS_DIR.parent / "causal_auction_results"


def auction_close(signal_time: datetime) -> datetime | None:
    start_clock = time.fromisoformat(str(AUCTION_POLICY["auction_start"]))
    last_clock = time.fromisoformat(str(AUCTION_POLICY["auction_last_close"]))
    start = signal_time.replace(hour=start_clock.hour, minute=start_clock.minute,
                                second=0, microsecond=0)
    last = signal_time.replace(hour=last_clock.hour, minute=last_clock.minute,
                               second=0, microsecond=0)
    if signal_time < start or signal_time >= last:
        return None
    seconds = (signal_time - start).total_seconds()
    window = int(seconds // int(AUCTION_POLICY["window_seconds"]))
    close = start + timedelta(seconds=(window + 1) * int(AUCTION_POLICY["window_seconds"]))
    return close if close <= last else None


def _verified_selected_quotes(run_dir: Path, symbols: set[str]) -> dict[str, dict[str, Any]]:
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != manifest.get("manifest_hash"):
        raise RuntimeError(f"source manifest hash mismatch: {run_dir.name}")
    tick_name = next((name for name in ("expanded_ticks.jsonl.gz", "expanded_ticks.jsonl")
                      if name in manifest.get("artifacts", {})), None)
    book_name = next((name for name in ("expanded_books.jsonl.gz", "expanded_books.jsonl")
                      if name in manifest.get("artifacts", {})), None)
    if tick_name is None or book_name is None:
        raise RuntimeError(f"expanded quote artifacts absent: {run_dir.name}")
    for name in (tick_name, book_name, "watchlist.json"):
        if sha256_file(run_dir / name) != manifest["artifacts"][name]:
            raise RuntimeError(f"source artifact hash mismatch: {run_dir.name}/{name}")
    watchlist = json.loads((run_dir / "watchlist.json").read_text(encoding="utf-8"))
    expanded = watchlist.get("expanded_shadow_universe") or {}
    if expanded.get("seal_hash") != manifest.get("expanded_universe_seal_hash"):
        raise RuntimeError(f"expanded seal mismatch: {run_dir.name}")
    metadata = {str(row["stock_id"]): row for row in expanded.get("stocks", [])}
    missing = symbols - set(metadata)
    if missing:
        raise RuntimeError(f"signals outside sealed universe: {sorted(missing)}")
    output = {symbol: {"ticks": [], "books": [], "meta": metadata[symbol]}
              for symbol in symbols}
    for row in _read_jsonl(run_dir / tick_name):
        symbol = str(row.get("stock_id", ""))
        if symbol not in output:
            continue
        try:
            received = _parse_stamp(row["received_at"])
            item = {
                "time": received, "received_at": received,
                "exchange_time": _archived_exchange_time(row.get("quote_time"), received),
                "price": float(row["deal_price"]), "volume": float(row["deal_volume"]),
                "bid": float(row["buy_price"]), "ask": float(row["sell_price"]),
                "flag": str(row.get("in_out_flag", "")),
                "serial": int(row.get("serial_no", 0)),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if min(item["price"], item["bid"], item["ask"]) > 0 and item["volume"] >= 0:
            output[symbol]["ticks"].append(item)
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
        output[symbol]["books"].append({
            "time": _parse_stamp(row["received_at"]),
            "buy_volume": sum(buys), "sell_volume": sum(sells),
            "best_bid": buy_prices[0], "best_ask": sell_prices[0],
        })
    for data in output.values():
        data["ticks"].sort(key=lambda row: (row["time"], row["serial"]))
        data["books"].sort(key=lambda row: row["time"])
        data["tick_times"] = [row["time"] for row in data["ticks"]]
        data["book_times"] = [row["time"] for row in data["books"]]
    return output


def _revalidate(cohort: list[dict[str, Any]], candidates: dict[str, dict],
                market: dict[str, dict], close: datetime) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    symbols = {str(row["stock_id"]) for row in cohort}
    subset = {symbol: candidates[symbol] for symbol in symbols}
    engine = _engine(subset, market)
    combined = {**subset, **market}
    tick_indexes = {symbol: 0 for symbol in combined}
    book_indexes = {symbol: 0 for symbol in combined}
    _feed_until(engine, combined, tick_indexes, book_indexes, close)
    engine.choose_entry(close, allow_short=False)
    diagnostic = engine.last_entry_diagnostics or {}
    passed = [row for row in diagnostic.get("candidates") or []
              if str(row.get("gate_reason")) in PASS_REASONS]
    passed.sort(key=lambda row: (
        -float(row.get("relative_strength_5m") or float("-inf")),
        -float(row.get("score") or 0), str(row["stock_id"]),
    ))
    if not passed:
        return None, diagnostic
    selected = dict(passed[0])
    quantity, fee, cash = affordable_quantity(float(selected["entry_price"]))
    if quantity < 1000 or float(selected["entry_price"]) > float(AUCTION_POLICY["maximum_entry_price_twd"]):
        diagnostic["auction_selection_rejection"] = "FEE_AWARE_CAP_OR_PRICE_REJECTED"
        return None, diagnostic
    original = next(row for row in cohort if str(row["stock_id"]) == str(selected["stock_id"]))
    selected.update({
        "signal_time": original["signal_time"], "confirmation_time": close,
        "delayed_entry_price": float(selected["entry_price"]),
        "delayed_entry_quantity": quantity, "delayed_entry_fee_twd": fee,
        "delayed_entry_cash_required_twd": cash,
        "auction_candidate_count": len(cohort),
        "auction_revalidated_count": len(passed),
    })
    return selected, diagnostic


def replay_day(day: str, rows: list[dict[str, Any]], candidates: dict[str, dict],
               market: dict[str, dict], coverage: dict[str, Any]) -> dict[str, Any]:
    by_close: dict[datetime, list[dict[str, Any]]] = {}
    for row in rows:
        close = auction_close(row["signal_time"])
        if close is not None:
            by_close.setdefault(close, []).append(row)
    diagnostics = []
    selected = None
    for close in sorted(by_close):
        selected, diagnostic = _revalidate(by_close[close], candidates, market, close)
        diagnostics.append({
            "session_date": day, "auction_close": close,
            "candidate_count": len(by_close[close]),
            "revalidated_count": (selected or {}).get("auction_revalidated_count", 0),
            "selected_stock_id": (selected or {}).get("stock_id"),
            "engine_decision": diagnostic.get("decision"),
            "engine_reason": diagnostic.get("reason"),
            "market_regime": (diagnostic.get("market_context") or {}).get("market_regime"),
            "candidate_gate_reasons": "+".join(sorted({
                str(item.get("gate_reason")) for item in diagnostic.get("candidates") or []
            })),
            "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
        })
        if selected is not None:
            break
    trade = (_simulate_exit(selected, {str(selected["stock_id"]): candidates[str(selected["stock_id"])]},
                            market, coverage) if selected else None)
    if trade:
        trade["strategy_variant"] = AUCTION_POLICY["policy_id"]
        trade["auction_close"] = selected["confirmation_time"].isoformat()
        trade["auction_candidate_count"] = selected["auction_candidate_count"]
        trade["auction_revalidated_count"] = selected["auction_revalidated_count"]
        trade["current_relative_strength_5m"] = selected.get("relative_strength_5m")
        trade["current_production_score"] = selected.get("score")
    return {"trade": trade, "diagnostics": diagnostics}


def _account_metrics(trades: list[dict[str, Any]], session_dates: list[str]) -> dict[str, Any]:
    scored = [row for row in trades if row.get("status") == "SCORED"]
    by_day = {str(row["session_date"]): float(row["net_pnl"]) for row in scored}
    values = [by_day[day] for day in session_dates if day in by_day]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    equity, peak, drawdown = 190_000.0, 190_000.0, 0.0
    for day in session_dates:
        equity += by_day.get(day, 0.0)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "session_count": len(session_dates), "trade_count": len(trades),
        "scored_trade_count": len(scored), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(values) if values else None,
        "net_pnl_twd": sum(values), "mean_trade_twd": mean(values) if values else None,
        "median_trade_twd": median(values) if values else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "max_drawdown_twd": drawdown, "ending_equity_twd": equity,
    }


def _baseline(paper_dir: Path, session_dates: list[str]) -> dict[str, Any]:
    daily = {}
    trades = 0
    for path in paper_dir.glob("*/*/manifest.json"):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        day = str(manifest["session_date"])
        daily[day] = float(manifest.get("production_strategy_net_pnl", 0) or 0)
        trades += int(manifest.get("production_strategy_trade_count", 0) or 0)
    values = [daily.get(day, 0.0) for day in session_dates]
    wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
    equity, peak, drawdown = 190_000.0, 190_000.0, 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "strategy": "EXPANDED_PRODUCTION_FIRST_SIGNAL_BASELINE",
        "session_count": len(session_dates), "trade_count": trades,
        "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / trades if trades else None,
        "net_pnl_twd": sum(values),
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "max_drawdown_twd": drawdown, "ending_equity_twd": equity,
    }


def run_auction(
    ledger_path: Path = DEFAULT_RESULTS_DIR / "candidate_ledger.csv",
    results_dir: Path = DEFAULT_AUCTION_RESULTS_DIR,
    paper_dir: Path = DEFAULT_PAPER_DIR,
    runs_dir: Path = DEFAULT_RUNS_DIR,
) -> dict[str, Any]:
    ledger = load_ledger(ledger_path)
    sources = {str(paper["session_date"]): (run_dir, paper)
               for run_dir, paper in discover_sources(paper_dir, runs_dir)}
    session_dates = sorted(sources)
    trades, diagnostics = [], []
    for day in sorted({str(row["session_date"]) for row in ledger}):
        day_rows = [row for row in ledger if str(row["session_date"]) == day]
        symbols = {str(row["stock_id"]) for row in day_rows}
        run_dir, paper = sources[day]
        source = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        candidates = _verified_selected_quotes(run_dir, symbols)
        market = _load_market_context(run_dir, source)
        coverage = {
            "session_date": day, "source_run_ids": [source["run_id"]],
            "source_statuses": [source["status"]],
            "started_at_taipei": source["started_at"], "ended_at_taipei": source["ended_at"],
            "callback_errors": int(source.get("event_counts", {}).get("callback_errors", 0)),
            "coverage_status": "FULL_SESSION",
        }
        result = replay_day(day, day_rows, candidates, market, coverage)
        diagnostics.extend(result["diagnostics"])
        if result["trade"]:
            trades.append(result["trade"])
    auction_metrics = _account_metrics(trades, session_dates)
    baseline_metrics = _baseline(paper_dir, session_dates)
    results_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(results_dir / "trades.csv", trades)
    _write_csv(results_dir / "auction_diagnostics.csv", diagnostics)
    _write_csv(results_dir / "comparison.csv", [baseline_metrics, {
        "strategy": AUCTION_POLICY["policy_id"], **auction_metrics,
    }])
    summary = {
        "analysis_id": AUCTION_POLICY["policy_id"],
        "auction_policy_hash": AUCTION_POLICY_HASH,
        "known_archive_status": "EXPLORATORY_POST_HOC_HYPOTHESIS",
        "auction": auction_metrics, "baseline": baseline_metrics,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
        "interpretation": "KNOWN_ARCHIVE_CAUSAL_REPLAY_NOT_OUT_OF_SAMPLE_NOT_LIVE_APPROVAL",
    }
    (results_dir / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    manifest = {"policy": AUCTION_POLICY, "summary": summary,
                "parent_ledger": str(ledger_path)}
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (results_dir / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return summary
