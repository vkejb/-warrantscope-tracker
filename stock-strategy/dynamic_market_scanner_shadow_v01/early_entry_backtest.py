"""Causal account replay for the frozen early-entry observation states.

This is an offline, broker-free research entry point.  It intentionally reuses
the realtime observer and the existing production-parity exit simulator so an
"early" result cannot be manufactured from end-of-day bars.
"""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
import gzip
import hashlib
import heapq
import json
from pathlib import Path
from statistics import mean, median
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Iterable

from paper_shadow_v01.runner import _load_market_context
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC, _tick_size

from .causal_auction import _verified_selected_quotes
from .realtime_expanded_shadow import (
    EVENTS_NAME, POLICY as OBSERVER_POLICY, POLICY_HASH as OBSERVER_POLICY_HASH,
    RealtimeExpandedShadow,
)
from .scanner import affordable_quantity
from .strict_replay import (
    DEFAULT_PAPER_DIR, DEFAULT_RUNS_DIR, _simulate_exit, _write_csv,
    discover_sources,
)


MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = MODULE_DIR / "early_entry_backtest_results"
VARIANTS = ("EARLY_READY_ACCOUNT", "OFFICIAL_V2_ACCOUNT")
SCENARIOS = ("BASE", "STRESS")
POLICY = {
    "analysis_id": "EARLY_ENTRY_READY_CAUSAL_ACCOUNT_REPLAY_V1",
    "frozen_at_taipei": "2026-11-11T18:00:00+08:00",
    "mode": "SHADOW_ONLY_OFFLINE_CAUSAL_REPLAY_NO_BROKER",
    "early_signal_policy_hash": OBSERVER_POLICY_HASH,
    "selection": (
        "EARLIEST_EARLY_ENTRY_READY_OBSERVED_THEN_HIGHEST_RELATIVE_STRENGTH_5M_"
        "THEN_STOCK_ID"
    ),
    "capital_twd": 190_000,
    "maximum_trades_per_session": 1,
    "maximum_positions": 1,
    "base_fill": "OBSERVED_FRESH_ASK_PLUS_ONE_LEGAL_TICK",
    "stress_fill": "FIRST_TICK_AT_OR_AFTER_5_SECONDS_THEN_ASK_PLUS_TWO_TICKS",
    "exit": "UNCHANGED_EXISTING_LONG_EXIT_MODEL",
    "known_archive_warning": "ALL_DATES_ARE_POST_OUTCOME_EXPLORATORY",
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
POLICY_HASH = hashlib.sha256(canonical_bytes(POLICY)).hexdigest()


def _rows(path: Path, kind: str) -> Iterable[tuple[str, str, dict[str, Any]]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            stamp = str(row.get("received_at", ""))
            if stamp:
                yield stamp, kind, row


def merged_quote_events(run_dir: Path) -> Iterable[tuple[str, dict[str, Any]]]:
    """Merge the four independently persisted callback channels causally."""
    names = (
        ("expanded_ticks.jsonl.gz", "expanded_ticks"),
        ("market_context_ticks.jsonl.gz", "market_context_ticks"),
        ("expanded_books.jsonl.gz", "expanded_books"),
        ("market_context_books.jsonl.gz", "market_context_books"),
    )
    streams = []
    for priority, (name, kind) in enumerate(names):
        path = run_dir / name
        if not path.exists():
            raise RuntimeError(f"required replay artifact missing: {path}")
        streams.append(((stamp, priority, kind, row)
                        for stamp, kind, row in _rows(path, kind)))
    for _stamp, _priority, kind, row in heapq.merge(*streams):
        yield kind, row


def _expanded_items(run_dir: Path) -> list[SimpleNamespace]:
    watchlist = json.loads((run_dir / "watchlist.json").read_text(encoding="utf-8"))
    stocks = (watchlist.get("expanded_shadow_universe") or {}).get("stocks") or []
    return [SimpleNamespace(stock_id=str(row["stock_id"]),
                            stock_name=str(row.get("stock_name") or row["stock_id"]))
            for row in stocks]


def _ready_events(path: Path, session_date: str) -> list[dict[str, Any]]:
    output = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if (row.get("event") != "EARLY_STATE_TRANSITION"
                    or row.get("state") != "EARLY_ENTRY_READY_OBSERVED"):
                continue
            output.append({
                "session_date": session_date,
                "signal_time": row["observed_at"],
                "confirmation_time": row["observed_at"],
                "stock_id": str(row["stock_id"]),
                "stock_name": str(row.get("stock_name") or row["stock_id"]),
                "entry_price": float(row["entry_price"]),
                "relative_strength_5m": float(row["relative_strength_5m"]),
                "market_regime": row.get("market_regime"),
                "source_check_count": int(row["source_check_count"]),
                "volume_gate_reason": row.get("volume_gate_reason"),
                "observation_source": row.get("observation_source"),
                "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
            })
    output.sort(key=lambda row: (row["confirmation_time"],
                                 -row["relative_strength_5m"], row["stock_id"]))
    for rank, row in enumerate(output, 1):
        row["daily_ready_rank"] = rank
        row["trade_selection"] = (
            "SELECTED_EARLIEST_READY" if rank == 1 else "DAILY_SLOT_OCCUPIED"
        )
    return output


def replay_ready_signals(run_dir: Path, session_date: str) -> dict[str, Any]:
    """Replay one sealed session and return every first-per-symbol ready event."""
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "COMPLETE":
        raise RuntimeError(f"source run is not COMPLETE: {run_dir.name}")
    with TemporaryDirectory(prefix=f"early-{session_date}-") as temporary:
        target = Path(temporary)
        observer = RealtimeExpandedShadow(queue_size=200_000)
        observer.start(run_dir=target, run_id=f"offline-{run_dir.name}",
                       expanded_items=_expanded_items(run_dir))
        # Blocking insertion is intentional: historical replay must never turn
        # CPU backpressure into dropped callback evidence.
        for kind, row in merged_quote_events(run_dir):
            observer._queue.put((kind, row))  # research-only deterministic feed
            observer._offered += 1
        observer.close(timeout=180.0)
        summary = json.loads((target / "realtime_expanded_shadow_summary.json").read_text())
        if summary["status"] != "VALID_SHADOW_EVIDENCE":
            raise RuntimeError(f"invalid replay evidence {session_date}: {summary}")
        ready = _ready_events(target / EVENTS_NAME, session_date)
        return {
            "session_date": session_date, "source_run_id": run_dir.name,
            "source_manifest_hash": manifest["manifest_hash"], "signals": ready,
            "processed_events": summary["processed_events"],
            "prefilter_max_selected": summary["early_observation"]["prefilter_max_selected"],
        }


def _stress_fill(signal: dict[str, Any], data: dict[str, Any]) -> tuple[dict | None, str | None]:
    from datetime import datetime, timedelta
    at = datetime.fromisoformat(str(signal["confirmation_time"])) + timedelta(seconds=5)
    row = next((item for item in data["ticks"] if item["time"] >= at), None)
    if row is None:
        return None, "NO_TICK_AT_OR_AFTER_DELAY"
    age = (row["time"] - at).total_seconds()
    if not 0 <= age <= float(SPEC["maximum_tick_staleness_seconds"]):
        return None, "TICK_STALE_AFTER_DELAY"
    price = float(row["ask"]) + 2 * _tick_size(float(row["ask"]))
    quantity, fee, cash = affordable_quantity(price)
    if quantity < 1000:
        return None, "STRESS_ENTRY_UNAFFORDABLE_OR_ABOVE_CAP"
    return {"entry_time": row["time"], "entry_price": price, "quantity": quantity,
            "entry_fee_twd": fee, "cash_required_twd": cash}, None


def _trade(signal: dict[str, Any], scenario: str, run_dir: Path,
           source: dict[str, Any], *, candidates: dict[str, dict[str, Any]] | None = None,
           market: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    symbol = str(signal["stock_id"])
    candidates = candidates or _verified_selected_quotes(run_dir, {symbol})
    if scenario == "BASE":
        quantity, fee, cash = affordable_quantity(float(signal["entry_price"]))
        fill = {"entry_time": signal["confirmation_time"],
                "entry_price": float(signal["entry_price"]), "quantity": quantity,
                "entry_fee_twd": fee, "cash_required_twd": cash}
        failure = None if quantity >= 1000 else "BASE_ENTRY_UNAFFORDABLE_OR_ABOVE_CAP"
    else:
        fill, failure = _stress_fill(signal, candidates[symbol])
    if fill is None or failure:
        return {"session_date": signal["session_date"], "variant": "EARLY_READY_ACCOUNT",
                "scenario": scenario, "stock_id": symbol, "stock_name": signal["stock_name"],
                "status": "UNFILLED_ENTRY", "unfilled_reason": failure, "net_pnl": None,
                "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0}
    market = market or _load_market_context(run_dir, source)
    payload = {**signal,
               "signal_time": datetime.fromisoformat(str(signal["signal_time"])),
               "confirmation_time": (datetime.fromisoformat(fill["entry_time"])
                                     if isinstance(fill["entry_time"], str)
                                     else fill["entry_time"]),
               "delayed_entry_price": fill["entry_price"],
               "delayed_entry_quantity": fill["quantity"]}
    coverage = {"session_date": signal["session_date"], "ended_at_taipei": source["ended_at"]}
    result = _simulate_exit(payload, candidates, market, coverage)
    result.update({"variant": "EARLY_READY_ACCOUNT", "scenario": scenario,
                   "entry_fee_twd": fill["entry_fee_twd"],
                   "cash_required_twd": fill["cash_required_twd"],
                   "early_relative_strength_5m": signal["relative_strength_5m"]})
    return result


def _metrics(variant: str, scenario: str, trades: list[dict[str, Any]], days: list[str]):
    rows = [row for row in trades if row["variant"] == variant and row["scenario"] == scenario]
    scored = [row for row in rows if row.get("status") == "SCORED"]
    values = [float(row["net_pnl"]) for row in scored]
    wins, losses = [x for x in values if x > 0], [x for x in values if x < 0]
    by_day = {str(row["session_date"]): float(row["net_pnl"]) for row in scored}
    equity = peak = 190_000.0
    max_drawdown = 0.0
    for day in days:
        equity += by_day.get(day, 0.0)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {"variant": variant, "scenario": scenario, "session_count": len(days),
            "trade_count": len(rows), "scored_trade_count": len(scored),
            "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / len(values) if values else None,
            "net_pnl_twd": sum(values), "mean_trade_twd": mean(values) if values else None,
            "median_trade_twd": median(values) if values else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "max_drawdown_twd": max_drawdown, "ending_equity_twd": equity,
            "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0}


def _official_trades() -> list[dict[str, Any]]:
    path = MODULE_DIR / "volume_persistence_followup_results" / "trades.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    output = []
    for row in rows:
        if row["variant"] != "V2_RECENT_VOLUME_ACCELERATION":
            continue
        converted = dict(row)
        converted["variant"] = "OFFICIAL_V2_ACCOUNT"
        if converted.get("net_pnl") not in (None, ""):
            converted["net_pnl"] = float(converted["net_pnl"])
        output.append(converted)
    return output


def _score_day(run_dir: Path, replay: dict[str, Any]) -> list[dict[str, Any]]:
    if not replay["signals"]:
        return []
    signal = replay["signals"][0]
    source = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    symbol = str(signal["stock_id"])
    candidates = _verified_selected_quotes(run_dir, {symbol})
    market = _load_market_context(run_dir, source)
    return [_trade(signal, scenario, run_dir, source,
                   candidates=candidates, market=market) for scenario in SCENARIOS]


def run(results_dir: Path = DEFAULT_RESULTS_DIR, workers: int = 3) -> dict[str, Any]:
    sources = [(run_dir, str(paper["session_date"]))
               for run_dir, paper in discover_sources(DEFAULT_PAPER_DIR, DEFAULT_RUNS_DIR)]
    cache_dir = results_dir / "replay_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    replays = []
    pending = []
    for run_dir, day in sources:
        cache_path = cache_dir / f"{day}.json"
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            if (cached.get("source_manifest_hash") == manifest.get("manifest_hash")
                    and cached.get("observer_policy_hash") == OBSERVER_POLICY_HASH):
                replays.append(cached["replay"])
                continue
        pending.append((run_dir, day))
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(replay_ready_signals, run_dir, day): day
                   for run_dir, day in pending}
        for future in as_completed(futures):
            result = future.result()
            replays.append(result)
            cache = {"observer_policy_hash": OBSERVER_POLICY_HASH,
                     "source_manifest_hash": result["source_manifest_hash"],
                     "replay": result}
            (cache_dir / f"{result['session_date']}.json").write_bytes(
                canonical_bytes(cache) + b"\n"
            )
            print(f"replayed {result['session_date']} signals={len(result['signals'])}", flush=True)
    replays.sort(key=lambda row: row["session_date"])
    signals = [signal for replay in replays for signal in replay["signals"]]
    early_trades = []
    by_day = {day: run_dir for run_dir, day in sources}
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        score_futures = {
            pool.submit(_score_day, by_day[replay["session_date"]], replay): replay
            for replay in replays if replay["signals"]
        }
        for future in as_completed(score_futures):
            replay = score_futures[future]
            rows = future.result()
            early_trades.extend(rows)
            print(f"scored {replay['session_date']} symbol={replay['signals'][0]['stock_id']}",
                  flush=True)
    trades = early_trades + _official_trades()
    trades.sort(key=lambda row: (str(row.get("variant")), str(row.get("scenario")),
                                 str(row.get("session_date")), str(row.get("stock_id"))))
    days = [row["session_date"] for row in replays]
    experiments = [_metrics(variant, scenario, trades, days)
                   for variant in VARIANTS for scenario in SCENARIOS]
    sessions = [{key: row[key] for key in (
        "session_date", "source_run_id", "source_manifest_hash", "processed_events",
        "prefilter_max_selected")}
        | {"early_ready_signal_count": len(row["signals"])} for row in replays]
    results_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(results_dir / "signals.csv", signals)
    _write_csv(results_dir / "trades.csv", trades)
    _write_csv(results_dir / "experiments.csv", experiments)
    _write_csv(results_dir / "sessions.csv", sessions)
    summary = {"analysis_id": POLICY["analysis_id"], "policy_hash": POLICY_HASH,
               "observer_policy_hash": OBSERVER_POLICY_HASH, "experiments": experiments,
               "session_dates": days, "early_ready_signal_count": len(signals),
               "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
               "interpretation": "KNOWN_ARCHIVE_EXPLORATORY_NOT_LIVE_APPROVAL"}
    (results_dir / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    manifest = {"policy": POLICY, "observer_policy": OBSERVER_POLICY,
                "summary": summary,
                "source_manifest_hashes": {row["session_date"]: row["source_manifest_hash"]
                                           for row in replays},
                "reproduce": "python3 -m dynamic_market_scanner_shadow_v01.early_entry_backtest --workers 3"}
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (results_dir / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(run(args.results_dir, args.workers), ensure_ascii=False,
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
