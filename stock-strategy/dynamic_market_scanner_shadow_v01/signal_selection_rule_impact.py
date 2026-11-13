"""Causal comparison of the current 1% gate and a bounded dual-lane rescue."""

from __future__ import annotations

from bisect import bisect_right
import csv
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from statistics import mean, median
from typing import Any

from paper_shadow_v01.runner import _load_market_context
from volume_flow_entry_study_v01.analysis import evaluate_volume_gate
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC, _projected_net_pnl, _tick_size

from .causal_auction import _verified_selected_quotes
from .early_entry_backtest import DEFAULT_RESULTS_DIR as EARLY_RESULTS_DIR
from .scanner import affordable_quantity
from .signal_outcomes import _mark
from .strict_replay import DEFAULT_RUNS_DIR, _simulate_exit, _write_csv
from .volume_persistence_followup import SOURCE_CONFIG, _anti_chase


MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = MODULE_DIR / "signal_selection_rule_impact_results"
VARIANTS = ("CURRENT_RS1_180S", "DUAL_LANE_PERSISTENCE_90S")
SCENARIOS = ("BASE", "STRESS")
POLICY = {
    "analysis_id": "SIGNAL_SELECTION_RULE_IMPACT_V1",
    "frozen_at_taipei": "2026-11-13T09:36:00+08:00",
    "mode": "KNOWN_ARCHIVE_POST_OUTCOME_EXPLORATORY_NO_BROKER",
    "candidate_source": "ALL_FIRST_PER_SYMBOL_EARLY_ENTRY_READY_SIGNALS",
    "variants": {
        "CURRENT_RS1_180S": {
            "relative_strength_minimum": 0.01,
            "confirmation_seconds": 180,
            "confirmation": "ORIGINAL_ENTRY_HYPOTHETICAL_NET_PNL_GT_ZERO",
        },
        "DUAL_LANE_PERSISTENCE_90S": {
            "relative_strength": "CONTINUOUS_RANKING_NOT_HARD_GATE",
            "checkpoints_seconds": [30, 60, 90],
            "confirmation": (
                "UNCHANGED_V2_VOLUME_GATE_AND_ORIGINAL_BREAKOUT_HELD_AND_"
                "UNCHANGED_ANTI_CHASE"
            ),
        },
    },
    "entry": "LATEST_FRESH_ASK_PLUS_ONE_TICK_AT_CONFIRMATION",
    "stress_entry": "FIVE_SECONDS_LATER_ASK_PLUS_TWO_TICKS",
    "all_signal_outcome": "AFTER_COST_NET_PNL_AT_30_MINUTES_AFTER_ENTRY",
    "account": "EARLIEST_CONFIRMATION_THEN_RELATIVE_STRENGTH_ONE_TRADE_PER_DAY",
    "capital_twd": 190_000,
    "maximum_trades_per_session": 1,
    "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
}
POLICY_HASH = hashlib.sha256(canonical_bytes(POLICY)).hexdigest()


def _latest(rows: list[dict[str, Any]], times: list[datetime], at: datetime):
    index = bisect_right(times, at) - 1
    return rows[index] if index >= 0 else None


def _fresh_entry(data: dict[str, Any], at: datetime) -> tuple[dict[str, Any] | None, str]:
    tick = _latest(data["ticks"], data["tick_times"], at)
    book = _latest(data["books"], data["book_times"], at)
    if tick is None or book is None:
        return None, "QUOTE_MISSING"
    tick_age = (at - tick["time"]).total_seconds()
    book_age = (at - book["time"]).total_seconds()
    if not 0 <= tick_age <= float(SPEC["maximum_tick_staleness_seconds"]):
        return None, "TICK_STALE"
    if not 0 <= book_age <= float(SPEC["maximum_book_staleness_seconds"]):
        return None, "BOOK_STALE"
    bid, ask = float(book["best_bid"]), float(book["best_ask"])
    spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000
    if spread_bps > float(SPEC["maximum_spread_bps"]) + 1e-12:
        return None, "SPREAD_TOO_WIDE"
    price = ask + _tick_size(ask)
    quantity, fee, cash = affordable_quantity(price)
    if quantity < 1000:
        return None, "PRICE_CAP_OR_BOARD_LOT"
    return {
        "entry_time": at, "entry_price": price, "quantity": quantity,
        "entry_fee_twd": fee, "cash_required_twd": cash,
        "best_bid": bid, "best_ask": ask, "spread_bps": spread_bps,
        "tick_age_seconds": tick_age, "book_age_seconds": book_age,
    }, "PASSED"


def current_opportunity(signal: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    if float(signal["relative_strength_5m"]) < 0.01:
        return {"passed": False, "reason": "RS_BELOW_1PCT"}
    at = signal["signal_time"] + timedelta(seconds=180)
    fill, reason = _fresh_entry(data, at)
    if fill is None:
        return {"passed": False, "reason": reason, "effective_time": at}
    original_quantity, _fee, _cash = affordable_quantity(float(signal["entry_price"]))
    progress = _projected_net_pnl(
        "LONG", float(signal["entry_price"]), float(fill["best_bid"]),
        original_quantity,
    )[3]
    if progress <= 0:
        return {"passed": False, "reason": "THREE_MINUTE_NET_NOT_POSITIVE",
                "effective_time": at, "hypothetical_net_progress_twd": progress}
    return {"passed": True, "reason": "CURRENT_RS_AND_180S_PASSED",
            "effective_time": at, "hypothetical_net_progress_twd": progress, **fill}


def dual_lane_opportunity(signal: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    attempts = []
    for seconds in (30, 60, 90):
        at = signal["signal_time"] + timedelta(seconds=seconds)
        gate = evaluate_volume_gate(
            data, at, "V2_RECENT_VOLUME_ACCELERATION", SOURCE_CONFIG,
        )
        attempt = {"checkpoint_seconds": seconds, "gate_reason": gate["reason"]}
        if not gate["passed"]:
            attempts.append(attempt)
            continue
        fill, reason = _fresh_entry(data, at)
        if fill is None:
            attempt["reason"] = reason
            attempts.append(attempt)
            continue
        if float(fill["best_bid"]) + 1e-12 < float(signal["entry_price"]):
            attempt["reason"] = "ORIGINAL_BREAKOUT_NOT_HELD"
            attempts.append(attempt)
            continue
        anti_chase, features = _anti_chase(data, at, float(fill["entry_price"]))
        attempt.update(features)
        if not anti_chase:
            attempt["reason"] = "ANTI_CHASE_FAILED"
            attempts.append(attempt)
            continue
        return {"passed": True, "reason": "PERSISTENCE_RESCUE_PASSED",
                "effective_time": at, "checkpoint_seconds": seconds,
                "attempts": attempts + [attempt], **fill, **features}
    return {"passed": False, "reason": "NO_PERSISTENT_CONFIRMATION_WITHIN_90S",
            "attempts": attempts}


def opportunity(signal: dict[str, Any], data: dict[str, Any], variant: str):
    if variant == "CURRENT_RS1_180S":
        return current_opportunity(signal, data)
    if variant == "DUAL_LANE_PERSISTENCE_90S":
        return dual_lane_opportunity(signal, data)
    raise ValueError(f"unknown variant: {variant}")


def _stress_fill(base: dict[str, Any], data: dict[str, Any]):
    target = base["effective_time"] + timedelta(seconds=5)
    tick = next((row for row in data["ticks"] if row["time"] >= target), None)
    if tick is None:
        return None, "NO_TICK_AFTER_STRESS_DELAY"
    if (tick["time"] - target).total_seconds() > float(SPEC["maximum_tick_staleness_seconds"]):
        return None, "STRESS_TICK_STALE"
    price = float(tick["ask"]) + 2 * _tick_size(float(tick["ask"]))
    quantity, fee, cash = affordable_quantity(price)
    if quantity < 1000:
        return None, "STRESS_PRICE_CAP_OR_BOARD_LOT"
    return {"entry_time": tick["time"], "entry_price": price,
            "quantity": quantity, "entry_fee_twd": fee,
            "cash_required_twd": cash}, "PASSED"


def _fill(base: dict[str, Any], data: dict[str, Any], scenario: str):
    if scenario == "BASE":
        return {key: base[key] for key in (
            "entry_time", "entry_price", "quantity", "entry_fee_twd",
            "cash_required_twd",
        )}, "PASSED"
    return _stress_fill(base, data)


def _fixed_30m(signal: dict[str, Any], fill: dict[str, Any], data: dict[str, Any]):
    mark = _mark(data["books"], fill["entry_time"] + timedelta(minutes=30))
    if mark is None:
        return None
    return float(_projected_net_pnl(
        "LONG", float(fill["entry_price"]), float(mark["best_bid"]),
        int(fill["quantity"]),
    )[3])


def _metrics(rows: list[dict[str, Any]], scope: str, variant: str, scenario: str,
             session_count: int) -> dict[str, Any]:
    items = [row for row in rows if row["scope"] == scope
             and row["variant"] == variant and row["scenario"] == scenario]
    values = [float(row["net_pnl_twd"]) for row in items
              if row.get("net_pnl_twd") is not None]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    return {
        "scope": scope, "variant": variant, "scenario": scenario,
        "session_count": session_count, "trade_count": len(items),
        "scored_trade_count": len(values), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(values) if values else None,
        "net_pnl_twd": sum(values), "mean_trade_twd": mean(values) if values else None,
        "median_trade_twd": median(values) if values else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
        "note": ("OVERLAPPING_DIAGNOSTIC_NOT_ACCOUNT_PNL"
                 if scope == "ALL_SIGNALS_FIXED_30M" else "ONE_TRADE_PER_DAY_ACCOUNT_REPLAY"),
    }


def run(
    signals_path: Path = EARLY_RESULTS_DIR / "signals.csv",
    replay_cache_dir: Path = EARLY_RESULTS_DIR / "replay_cache",
    runs_dir: Path = DEFAULT_RUNS_DIR,
    results_dir: Path = DEFAULT_RESULTS_DIR,
) -> dict[str, Any]:
    with signals_path.open(encoding="utf-8", newline="") as handle:
        source_signals = list(csv.DictReader(handle))
    signals = []
    trades = []
    days = sorted({row["session_date"] for row in source_signals})
    for day in days:
        cached = json.loads((replay_cache_dir / f"{day}.json").read_text(encoding="utf-8"))["replay"]
        run_dir = runs_dir / cached["source_run_id"]
        source = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        if source["manifest_hash"] != cached["source_manifest_hash"]:
            raise RuntimeError(f"source manifest changed: {day}")
        day_rows = []
        for raw in source_signals:
            if raw["session_date"] != day:
                continue
            row = dict(raw)
            row["signal_time"] = datetime.fromisoformat(raw["signal_time"])
            row["entry_price"] = float(raw["entry_price"])
            row["relative_strength_5m"] = float(raw["relative_strength_5m"])
            day_rows.append(row)
        candidates = _verified_selected_quotes(run_dir, {row["stock_id"] for row in day_rows})
        market = _load_market_context(run_dir, source)
        coverage = {"session_date": day, "ended_at_taipei": source["ended_at"]}
        passed_by_variant: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {
            variant: [] for variant in VARIANTS
        }
        for row in day_rows:
            data = candidates[row["stock_id"]]
            for variant in VARIANTS:
                result = opportunity(row, data, variant)
                signals.append({
                    "session_date": day, "variant": variant,
                    "stock_id": row["stock_id"], "stock_name": row["stock_name"],
                    "signal_time": row["signal_time"],
                    "relative_strength_5m": row["relative_strength_5m"],
                    "gate_passed": result["passed"], "gate_reason": result["reason"],
                    "effective_time": result.get("effective_time"),
                    "checkpoint_seconds": result.get("checkpoint_seconds"),
                    "attempts": json.dumps(result.get("attempts", []), default=str,
                                           ensure_ascii=False),
                    "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
                })
                if result["passed"]:
                    passed_by_variant[variant].append((row, result))
                    for scenario in SCENARIOS:
                        fill, _reason = _fill(result, data, scenario)
                        net = _fixed_30m(row, fill, data) if fill else None
                        trades.append({
                            "scope": "ALL_SIGNALS_FIXED_30M", "session_date": day,
                            "variant": variant, "scenario": scenario,
                            "stock_id": row["stock_id"], "stock_name": row["stock_name"],
                            "signal_time": row["signal_time"],
                            "entry_time": fill["entry_time"] if fill else None,
                            "entry_price": fill["entry_price"] if fill else None,
                            "quantity": fill["quantity"] if fill else None,
                            "net_pnl_twd": net, "status": "SCORED" if net is not None else "NA",
                            "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
                        })
        for variant, passed in passed_by_variant.items():
            if not passed:
                continue
            passed.sort(key=lambda item: (
                item[1]["effective_time"], -float(item[0]["relative_strength_5m"]),
                item[0]["stock_id"],
            ))
            row, result = passed[0]
            data = candidates[row["stock_id"]]
            for scenario in SCENARIOS:
                fill, reason = _fill(result, data, scenario)
                if fill is None:
                    trades.append({
                        "scope": "ACCOUNT_ONE_TRADE_PER_DAY", "session_date": day,
                        "variant": variant, "scenario": scenario,
                        "stock_id": row["stock_id"], "stock_name": row["stock_name"],
                        "status": "UNFILLED", "reason": reason, "net_pnl_twd": None,
                        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
                    })
                    continue
                payload = {
                    **row, "confirmation_time": fill["entry_time"],
                    "delayed_entry_price": fill["entry_price"],
                    "delayed_entry_quantity": fill["quantity"],
                }
                trade = _simulate_exit(payload, {row["stock_id"]: data}, market, coverage)
                trades.append({
                    **trade, "scope": "ACCOUNT_ONE_TRADE_PER_DAY",
                    "variant": variant, "scenario": scenario,
                    "net_pnl_twd": trade.get("net_pnl"),
                })
    metrics = [_metrics(trades, scope, variant, scenario, len(days))
               for scope in ("ALL_SIGNALS_FIXED_30M", "ACCOUNT_ONE_TRADE_PER_DAY")
               for variant in VARIANTS for scenario in SCENARIOS]
    results_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(results_dir / "signals.csv", signals)
    _write_csv(results_dir / "trades.csv", trades)
    _write_csv(results_dir / "experiments.csv", metrics)
    summary = {
        "analysis_id": POLICY["analysis_id"], "policy_hash": POLICY_HASH,
        "session_dates": days, "source_signal_count": len(source_signals),
        "experiments": metrics, "actual_orders": 0, "actual_fills": 0,
        "broker_order_calls": 0,
        "interpretation": "KNOWN_ARCHIVE_EXPLORATORY_NOT_LIVE_APPROVAL",
    }
    (results_dir / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    manifest = {"policy": POLICY, "summary": summary}
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (results_dir / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
