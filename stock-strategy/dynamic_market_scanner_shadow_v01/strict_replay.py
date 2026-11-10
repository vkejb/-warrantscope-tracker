"""Causal, broker-free replay for STRICT_MARKET_WIDE_V1."""

from __future__ import annotations

from bisect import bisect_right
import csv
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any

from expanded_shadow_universe_v01.paper import load_expanded_candidates
from paper_shadow_v01.runner import _load_market_context
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC, _decision_times, _projected_net_pnl, _tick_size,
)
from yuanta_intraday_shadow_v01.live_parity_backtest import (
    _feed_until, _parse_stamp, _record_book, _record_tick,
    _validate_full_session_coverage,
)
from yuanta_live_runtime_v01.strategy import (
    ANTI_CHASE_ENTRY_POLICY, LONG_MARKET_REGIME_POLICY,
    LiveDirectionEngine, ManagedPosition,
)

from .scanner import affordable_quantity
from .strict_policy import STRICT_POLICY, STRICT_POLICY_HASH


MODULE_DIR = Path(__file__).resolve().parent
STOCK_STRATEGY_DIR = MODULE_DIR.parent
DEFAULT_RUNS_DIR = STOCK_STRATEGY_DIR / "yuanta_intraday_shadow_v01" / "runtime" / "runs"
DEFAULT_PAPER_DIR = STOCK_STRATEGY_DIR / "expanded_shadow_universe_v01" / "runtime" / "days"
DEFAULT_RESULTS_DIR = MODULE_DIR / "strict_results"
PASS_REASONS = frozenset({"BASE_SIGNAL_CONFIRMED", "MARKET_GATE_PASSED"})


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _signed_volume(row: dict[str, Any]) -> float:
    if str(row.get("flag", "")) == "1":
        return float(row["volume"])
    if str(row.get("flag", "")) == "0":
        return -float(row["volume"])
    midpoint = (float(row["bid"]) + float(row["ask"])) / 2
    return float(row["volume"]) if float(row["price"]) >= midpoint else -float(row["volume"])


def _latest(items: list[dict[str, Any]], times: list[datetime], at: datetime) -> dict[str, Any] | None:
    index = bisect_right(times, at) - 1
    return items[index] if index >= 0 else None


def evaluate_observation(
    candidate: dict[str, Any], data: dict[str, Any], checkpoint: datetime, *,
    confirmation_market_regime: str | None,
) -> dict[str, Any]:
    """Evaluate only information received by checkpoint; no future quote is used."""
    signal_time = candidate["decision_time"]
    ticks, books = data["ticks"], data["books"]
    latest_tick = _latest(ticks, data["tick_times"], checkpoint)
    latest_book = _latest(books, data["book_times"], checkpoint)
    base = {"confirmation_time": checkpoint,
            "confirmation_market_regime": confirmation_market_regime}
    if latest_tick is None or latest_book is None:
        return {**base, "strict_passed": False, "strict_reason": "QUOTE_OR_BOOK_MISSING"}
    tick_age = (checkpoint - latest_tick["time"]).total_seconds()
    book_age = (checkpoint - latest_book["time"]).total_seconds()
    if not 0 <= tick_age <= float(SPEC["maximum_tick_staleness_seconds"]):
        return {**base, "strict_passed": False, "strict_reason": "TICK_STALE",
                "tick_age_seconds": tick_age}
    if not 0 <= book_age <= float(SPEC["maximum_book_staleness_seconds"]):
        return {**base, "strict_passed": False, "strict_reason": "BOOK_STALE",
                "book_age_seconds": book_age}
    bid, ask = float(latest_book["best_bid"]), float(latest_book["best_ask"])
    mid = (bid + ask) / 2
    spread_bps = (ask - bid) / mid * 10_000 if mid > 0 else math.inf
    if spread_bps > float(SPEC["maximum_spread_bps"]) + 1e-12:
        return {**base, "strict_passed": False, "strict_reason": "SPREAD_TOO_WIDE",
                "spread_bps": spread_bps}
    known = [row for row in ticks if row["time"] <= checkpoint and row["received_at"] <= checkpoint]
    total_volume = sum(float(row["volume"]) for row in known)
    vwap = (sum(float(row["price"]) * float(row["volume"]) for row in known) /
            total_volume if total_volume > 0 else None)
    window = [row for row in known if signal_time < row["time"] <= checkpoint]
    window_volume = sum(float(row["volume"]) for row in window)
    flow_delta = (sum(_signed_volume(row) for row in window) / window_volume
                  if window_volume > 0 else None)
    original_price = float(candidate["entry_price"])
    original_quantity, _fee, _cash = affordable_quantity(original_price)
    hypothetical_net = (_projected_net_pnl("LONG", original_price, bid,
                                            original_quantity)[3]
                        if original_quantity > 0 else None)
    opening = float(known[0]["price"]) if known else None
    delayed_entry = ask + _tick_size(ask)
    delayed_quantity, delayed_fee, delayed_cash = affordable_quantity(delayed_entry)
    opening_extension = delayed_entry / opening - 1 if opening else None
    vwap_extension = delayed_entry / vwap - 1 if vwap else None
    checks = [
        ("MARKET_BEARISH", confirmation_market_regime is not None and confirmation_market_regime != "BEARISH"),
        ("FLOW_WINDOW_MISSING", flow_delta is not None),
        ("FLOW_TURNED_NEGATIVE", flow_delta is not None and flow_delta >= float(STRICT_POLICY["minimum_confirmation_flow_delta"])),
        ("BREAKOUT_NOT_HELD", bid >= float(candidate["breakout_boundary_price"])),
        ("VWAP_NOT_HELD", vwap is not None and bid >= vwap),
        ("NET_PROGRESS_NOT_POSITIVE", hypothetical_net is not None and hypothetical_net > float(STRICT_POLICY["minimum_hypothetical_net_progress_twd"])),
        ("DELAYED_ENTRY_ABOVE_190", delayed_entry <= float(STRICT_POLICY["maximum_entry_price_twd"]) + 1e-12),
        ("DELAYED_ENTRY_UNAFFORDABLE", delayed_quantity >= int(STRICT_POLICY["board_lot_shares"])),
        ("ANTI_CHASE_OPENING_EXTENSION", opening_extension is not None and opening_extension <= float(ANTI_CHASE_ENTRY_POLICY["maximum_directional_opening_extension"]) + 1e-12),
        ("ANTI_CHASE_VWAP_EXTENSION", vwap_extension is not None and vwap_extension <= float(ANTI_CHASE_ENTRY_POLICY["maximum_directional_vwap_extension"]) + 1e-12),
    ]
    failed = [reason for reason, passed in checks if not passed]
    return {
        **base, "strict_passed": not failed,
        "strict_reason": "+".join(failed) if failed else "STRICT_CONFIRMATION_PASSED",
        "tick_age_seconds": tick_age, "book_age_seconds": book_age,
        "spread_bps": spread_bps, "confirmation_bid": bid,
        "confirmation_ask": ask, "confirmation_vwap": vwap,
        "confirmation_flow_delta": flow_delta,
        "hypothetical_original_quantity": original_quantity,
        "hypothetical_net_progress_twd": hypothetical_net,
        "delayed_entry_price": delayed_entry,
        "delayed_entry_quantity": delayed_quantity,
        "delayed_entry_fee_twd": delayed_fee,
        "delayed_entry_cash_required_twd": delayed_cash,
        "delayed_opening_extension": opening_extension,
        "delayed_vwap_extension": vwap_extension,
    }


def _engine(candidates: dict[str, dict], market: dict[str, dict]) -> LiveDirectionEngine:
    combined = {**candidates, **market}
    metadata = {symbol: str(data.get("meta", {}).get("stock_name", symbol))
                for symbol, data in combined.items()}
    return LiveDirectionEngine(
        metadata, capital_twd=int(STRICT_POLICY["capital_twd"]),
        candidate_symbols=set(candidates),
        benchmark_symbol=str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"]),
    )


def _simulate_exit(selected: dict[str, Any], candidates: dict[str, dict],
                   market: dict[str, dict], coverage: dict[str, Any]) -> dict[str, Any]:
    engine = _engine(candidates, market)
    combined = {**candidates, **market}
    tick_indexes = {symbol: 0 for symbol in combined}
    book_indexes = {symbol: 0 for symbol in combined}
    entry_time = selected["confirmation_time"]
    _feed_until(engine, combined, tick_indexes, book_indexes, entry_time)
    position = ManagedPosition(
        selected["stock_id"], selected["stock_name"], "LONG",
        int(selected["delayed_entry_quantity"]), float(selected["delayed_entry_price"]),
        "STRICT_SHADOW_ONLY_NO_ORDER", entry_time,
    )
    data = candidates[position.stock_id]
    events: list[tuple[datetime, int, str, dict[str, Any] | None]] = []
    events += [(row["time"], 0, "tick", row)
               for row in data["ticks"][tick_indexes[position.stock_id]:]]
    events += [(row["time"], 1, "book", row)
               for row in data["books"][book_indexes[position.stock_id]:]]
    end = _parse_stamp(coverage["ended_at_taipei"])
    decision = entry_time + timedelta(seconds=int(SPEC["decision_interval_seconds"]))
    while decision <= end:
        events.append((decision, 2, "decision", None))
        decision += timedelta(seconds=int(SPEC["decision_interval_seconds"]))
    events.sort(key=lambda item: (item[0], item[1]))
    held_values: list[float] = []
    exit_decision = exit_time = None
    for at, _priority, kind, row in events:
        reversal = False
        if kind == "tick":
            assert row is not None
            _record_tick(engine, position.stock_id, row)
        elif kind == "book":
            assert row is not None
            _record_book(engine, position.stock_id, row)
        else:
            reversal = engine.opposite_signal(position, at)
            engine.last_decision = at
        quote = engine.safe_exit_quote(position, at, max_age_seconds=3.0)
        if quote is not None:
            held_values.append(engine.projected_net(position, quote.price))
        proposed = engine.evaluate_exit(position, at, reversal=reversal,
                                        max_quote_age_seconds=3.0)
        if proposed is not None:
            exit_decision, exit_time = proposed, at
            break
    trade = {
        "session_date": coverage["session_date"], "stock_id": position.stock_id,
        "stock_name": position.stock_name, "side": "LONG",
        "strategy_variant": STRICT_POLICY["policy_id"],
        "signal_time": selected["signal_time"].isoformat(),
        "entry_time": entry_time.isoformat(), "entry_price": position.entry_price,
        "quantity": position.quantity, "actual_orders": 0, "actual_fills": 0,
        "broker_order_calls": 0,
    }
    if exit_decision is None or exit_time is None:
        return {**trade, "status": "UNSCORABLE_EXIT_FRESH_QUOTE_MISSING",
                "exit_time": None, "exit_price": None, "exit_reason": None,
                "net_pnl": None, "mfe_net_pnl": max(held_values) if held_values else None,
                "mae_net_pnl": min(held_values) if held_values else None}
    gross, commission, tax, net = _projected_net_pnl(
        "LONG", position.entry_price, exit_decision.price, position.quantity)
    return {**trade, "status": "SCORED", "exit_time": exit_time.isoformat(),
            "exit_price": exit_decision.price, "exit_reason": exit_decision.reason,
            "gross_pnl": gross, "commission": commission, "sell_tax": tax,
            "net_pnl": net, "holding_seconds": (exit_time - entry_time).total_seconds(),
            "mfe_net_pnl": max(held_values) if held_values else None,
            "mae_net_pnl": min(held_values) if held_values else None}


def replay_session(candidates: dict[str, dict], market: dict[str, dict],
                   coverage: dict[str, Any]) -> dict[str, Any]:
    _validate_full_session_coverage(coverage)
    engine = _engine(candidates, market)
    combined = {**candidates, **market}
    tick_indexes = {symbol: 0 for symbol in combined}
    book_indexes = {symbol: 0 for symbol in combined}
    pending: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    ledger: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    for decision in _decision_times(coverage["session_date"], SPEC["entry_start"],
                                    SPEC["last_entry_time"]):
        _feed_until(engine, combined, tick_indexes, book_indexes, decision)
        engine.choose_entry(decision, allow_short=False)
        diagnostic = engine.last_entry_diagnostics or {}
        market_regime = (diagnostic.get("market_context") or {}).get("market_regime")
        due: list[dict[str, Any]] = []
        for symbol, observation in list(pending.items()):
            if observation["confirmation_time"] <= decision:
                result = evaluate_observation(
                    observation["candidate"], candidates[symbol], decision,
                    confirmation_market_regime=market_regime)
                due.append({**observation["ledger"], **result})
                del pending[symbol]
        passed = [row for row in due if row["strict_passed"]]
        if passed:
            passed.sort(key=lambda row: (
                -float(row.get("hypothetical_net_progress_twd") or -math.inf),
                -float(row.get("relative_strength_5m") or -math.inf),
                -float(row.get("production_score") or 0), str(row["stock_id"])))
            for rank, row in enumerate(passed, 1):
                row["confirmation_rank"] = rank
                if selected is None and rank == 1:
                    selected = row
                    row["trade_selection"] = "SELECTED_EARLIEST_CHECKPOINT_RANK_1"
                else:
                    row["trade_selection"] = "DAILY_TRADE_SLOT_OCCUPIED"
        ledger.extend(due)
        for candidate in diagnostic.get("candidates") or []:
            symbol = str(candidate["stock_id"])
            if symbol in seen or str(candidate.get("gate_reason")) not in PASS_REASONS:
                continue
            seen.add(symbol)
            signal_market = str(candidate.get("market_regime") or market_regime or "UNKNOWN")
            base = {
                "session_date": coverage["session_date"], "stock_id": symbol,
                "stock_name": str(candidate.get("stock_name", symbol)),
                "signal_time": decision,
                "confirmation_time": decision + timedelta(seconds=int(STRICT_POLICY["observation_seconds"])),
                "signal_market_regime": signal_market,
                "production_score": candidate.get("score"),
                "relative_strength_5m": candidate.get("relative_strength_5m"),
                "original_entry_price": candidate.get("entry_price"),
                "original_gate_reason": candidate.get("gate_reason"),
                "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
            }
            if signal_market == "BEARISH":
                ledger.append({**base, "strict_passed": False,
                               "strict_reason": "MARKET_BEARISH_AT_SIGNAL",
                               "trade_selection": "REJECTED"})
            else:
                pending[symbol] = {"candidate": candidate,
                                   "confirmation_time": base["confirmation_time"],
                                   "ledger": base}
    for observation in pending.values():
        ledger.append({**observation["ledger"], "strict_passed": False,
                       "strict_reason": "OBSERVATION_NOT_MATURE_BEFORE_ENTRY_CUTOFF",
                       "trade_selection": "REJECTED"})
    trade = _simulate_exit(selected, candidates, market, coverage) if selected else None
    return {"ledger": ledger, "trade": trade, "selected": selected}


def discover_sources(paper_dir: Path = DEFAULT_PAPER_DIR,
                     runs_dir: Path = DEFAULT_RUNS_DIR) -> list[tuple[Path, dict[str, Any]]]:
    sources = []
    for path in sorted(paper_dir.glob("*/*/manifest.json")):
        paper = json.loads(path.read_text(encoding="utf-8"))
        run_dir = runs_dir / str(paper["source_run_id"])
        if run_dir.is_dir():
            sources.append((run_dir, paper))
    return sources


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows([{key: _jsonable(row.get(key)) for key in fields} for row in rows])


def run_all(results_dir: Path = DEFAULT_RESULTS_DIR,
            session_dates: set[str] | None = None) -> dict[str, Any]:
    results_dir.mkdir(parents=True, exist_ok=True)
    ledger: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    for run_dir, paper in discover_sources():
        if session_dates is not None and str(paper["session_date"]) not in session_dates:
            continue
        source = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        candidates, expanded_coverage = load_expanded_candidates(run_dir)
        market = _load_market_context(run_dir, source)
        coverage = {
            "session_date": str(paper["session_date"]),
            "source_run_ids": [source["run_id"]], "source_statuses": [source["status"]],
            "started_at_taipei": source["started_at"], "ended_at_taipei": source["ended_at"],
            "callback_errors": int(source.get("event_counts", {}).get("callback_errors", 0)),
            "coverage_status": "FULL_SESSION",
        }
        result = replay_session(candidates, market, coverage)
        ledger.extend(result["ledger"])
        if result["trade"]:
            trades.append(result["trade"])
        sessions.append({
            "session_date": paper["session_date"], "source_run_id": source["run_id"],
            "source_manifest_hash": source["manifest_hash"], **expanded_coverage,
            "candidate_signals": len(result["ledger"]),
            "strict_pass_count": sum(bool(row.get("strict_passed")) for row in result["ledger"]),
            "trade_count": int(result["trade"] is not None),
            "net_pnl": result["trade"].get("net_pnl") if result["trade"] else 0,
        })
    _write_csv(results_dir / "candidate_ledger.csv", ledger)
    _write_csv(results_dir / "trades.csv", trades)
    _write_csv(results_dir / "sessions.csv", sessions)
    scored = [row for row in trades if row.get("status") == "SCORED"]
    values = [float(row["net_pnl"]) for row in scored]
    equity = float(STRICT_POLICY["capital_twd"])
    peak, max_drawdown = equity, 0.0
    for session in sessions:
        equity += float(session.get("net_pnl") or 0)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    summary = {
        "analysis_id": STRICT_POLICY["policy_id"],
        "strict_policy_hash": STRICT_POLICY_HASH, "session_count": len(sessions),
        "candidate_signal_count": len(ledger),
        "strict_pass_count": sum(bool(row.get("strict_passed")) for row in ledger),
        "trade_count": len(trades), "scored_trade_count": len(scored),
        "wins": sum(value > 0 for value in values),
        "losses": sum(value < 0 for value in values),
        "net_pnl_twd": sum(values), "mean_trade_twd": mean(values) if values else None,
        "median_trade_twd": median(values) if values else None,
        "max_drawdown_twd": max_drawdown, "ending_equity_twd": equity,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
        "interpretation": "EXPLORATORY_KNOWN_ARCHIVES_SHADOW_ONLY_NOT_LIVE_APPROVAL",
    }
    (results_dir / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    manifest = {"policy": STRICT_POLICY, "summary": summary,
                "source_manifest_hashes": {row["session_date"]: row["source_manifest_hash"]
                                           for row in sessions}}
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (results_dir / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return summary


def aggregate_result_dirs(source_dirs: list[Path], results_dir: Path) -> dict[str, Any]:
    """Merge independently replayed date groups without recomputing market data."""
    results_dir.mkdir(parents=True, exist_ok=True)
    ledger: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    for source_dir in source_dirs:
        for name, target in (("candidate_ledger.csv", ledger),
                             ("trades.csv", trades), ("sessions.csv", sessions)):
            with (source_dir / name).open(encoding="utf-8", newline="") as handle:
                target.extend(dict(row) for row in csv.DictReader(handle))
    sessions.sort(key=lambda row: row["session_date"])
    if len({row["session_date"] for row in sessions}) != len(sessions):
        raise RuntimeError("duplicate session date across strict result groups")
    _write_csv(results_dir / "candidate_ledger.csv", ledger)
    _write_csv(results_dir / "trades.csv", trades)
    _write_csv(results_dir / "sessions.csv", sessions)
    scored = [row for row in trades if row.get("status") == "SCORED"]
    values = [float(row["net_pnl"]) for row in scored]
    equity = float(STRICT_POLICY["capital_twd"])
    peak, max_drawdown = equity, 0.0
    by_date = {row["session_date"]: row for row in sessions}
    for day in sorted(by_date):
        equity += float(by_date[day].get("net_pnl") or 0)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    summary = {
        "analysis_id": STRICT_POLICY["policy_id"], "strict_policy_hash": STRICT_POLICY_HASH,
        "session_count": len(sessions), "candidate_signal_count": len(ledger),
        "strict_pass_count": sum(str(row.get("strict_passed", "")).lower() == "true" for row in ledger),
        "trade_count": len(trades), "scored_trade_count": len(scored),
        "wins": sum(value > 0 for value in values), "losses": sum(value < 0 for value in values),
        "net_pnl_twd": sum(values), "mean_trade_twd": mean(values) if values else None,
        "median_trade_twd": median(values) if values else None,
        "max_drawdown_twd": max_drawdown, "ending_equity_twd": equity,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
        "interpretation": "EXPLORATORY_KNOWN_ARCHIVES_SHADOW_ONLY_NOT_LIVE_APPROVAL",
    }
    (results_dir / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    source_hashes = {row["session_date"]: row["source_manifest_hash"] for row in sessions}
    manifest = {"policy": STRICT_POLICY, "summary": summary,
                "source_manifest_hashes": source_hashes}
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (results_dir / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return summary
