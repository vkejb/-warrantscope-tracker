"""Causal follow-up for sustained volume and a bounded V2 recovery wait.

Research only.  This module never imports a broker adapter and cannot place an
order.  It keeps the frozen expanded candidate ledger as the signal source.
"""

from __future__ import annotations

from bisect import bisect_right
import argparse
import csv
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from statistics import mean, median
from typing import Any

from paper_shadow_v01.runner import _load_market_context
from volume_flow_entry_study_v01.analysis import evaluate_volume_gate, load_config
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC, _tick_size
from yuanta_live_runtime_v01.strategy import ANTI_CHASE_ENTRY_POLICY

from .causal_auction import _verified_selected_quotes
from .expanded_volume_gate import entry_fill_with_reason
from .scanner import affordable_quantity
from .strict_ablation import load_ledger
from .strict_replay import (
    DEFAULT_PAPER_DIR,
    DEFAULT_RESULTS_DIR,
    DEFAULT_RUNS_DIR,
    _simulate_exit,
    _write_csv,
    discover_sources,
)


SOURCE_CONFIG = load_config()
SOURCE_CONFIG_HASH = hashlib.sha256(canonical_bytes(SOURCE_CONFIG)).hexdigest()
VARIANTS = (
    "V1_SUSTAINED_DISTRIBUTED_BUYING",
    "V2_RECENT_VOLUME_ACCELERATION",
    "V3_SUSTAINED_PLUS_ACCELERATION",
    "V4_WAIT_90S_V2_RECOVERY",
)
SCENARIOS = ("BASE", "STRESS")
POLICY = {
    "analysis_id": "EXPANDED_VOLUME_PERSISTENCE_FOLLOWUP_V1",
    "frozen_at_taipei": "2026-11-11T16:34:45+08:00",
    "mode": "SHADOW_ONLY_OFFLINE_CAUSAL_REPLAY",
    "candidate_source": "FROZEN_CURRENT_ENGINE_FIRST_APPROVED_SIGNAL_PER_SYMBOL_LEDGER",
    "variants": list(VARIANTS),
    "wait_recovery": {
        "checkpoints_seconds": [30, 60, 90],
        "gate": "UNCHANGED_V2_RECENT_VOLUME_ACCELERATION",
        "delayed_fill": "LATEST_FRESH_ASK_PLUS_ONE_TICK",
        "requires_original_breakout_proxy_held": True,
        "breakout_proxy": "LATEST_BID_AT_LEAST_ORIGINAL_ENTRY_PRICE",
        "rechecks_existing_opening_and_vwap_anti_chase": True,
        "maximum_tick_age_seconds": SPEC["maximum_tick_staleness_seconds"],
        "maximum_spread_bps": SPEC["maximum_spread_bps"],
    },
    "capital_twd": 190_000,
    "maximum_trades_per_session": 1,
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
POLICY_HASH = hashlib.sha256(canonical_bytes(POLICY)).hexdigest()
DEFAULT_RESULTS_DIR = DEFAULT_RESULTS_DIR.parent / "volume_persistence_followup_results"


def _latest_tick(data: dict[str, Any], at) -> dict[str, Any] | None:
    times = data.get("tick_times") or [row["time"] for row in data["ticks"]]
    index = bisect_right(times, at) - 1
    return data["ticks"][index] if index >= 0 else None


def _anti_chase(data: dict[str, Any], at, price: float) -> tuple[bool, dict[str, Any]]:
    known = [row for row in data["ticks"] if row["time"] <= at]
    if not known:
        return False, {"recovery_reason": "NO_TICKS_AT_CHECKPOINT"}
    total = sum(float(row["volume"]) for row in known)
    vwap = (sum(float(row["price"]) * float(row["volume"]) for row in known) / total
            if total > 0 else None)
    opening = float(known[0]["price"])
    opening_extension = price / opening - 1
    vwap_extension = price / vwap - 1 if vwap else None
    passed = bool(
        vwap_extension is not None
        and opening_extension <= float(
            ANTI_CHASE_ENTRY_POLICY["maximum_directional_opening_extension"]
        ) + 1e-12
        and vwap_extension <= float(
            ANTI_CHASE_ENTRY_POLICY["maximum_directional_vwap_extension"]
        ) + 1e-12
    )
    return passed, {
        "recovery_opening_extension": opening_extension,
        "recovery_vwap_extension": vwap_extension,
        "recovery_vwap": vwap,
    }


def recovery_opportunity(row: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Return the first causal V2 recovery, or an explicit rejection."""
    original = evaluate_volume_gate(
        data, row["signal_time"], "V2_RECENT_VOLUME_ACCELERATION", SOURCE_CONFIG
    )
    attempts = []
    if original["passed"]:
        return {
            "passed": True,
            "effective_time": row["signal_time"],
            "entry_price": float(row["original_entry_price"]),
            "reason": "ORIGINAL_V2_PASSED",
            "attempts": attempts,
            **{key: value for key, value in original.items()
               if key not in {"passed", "reason"}},
        }
    for seconds in POLICY["wait_recovery"]["checkpoints_seconds"]:
        at = row["signal_time"] + timedelta(seconds=int(seconds))
        gate = evaluate_volume_gate(
            data, at, "V2_RECENT_VOLUME_ACCELERATION", SOURCE_CONFIG
        )
        attempt = {"checkpoint_seconds": seconds, "checkpoint_time": at,
                   "gate_reason": gate["reason"], "gate_passed": gate["passed"]}
        if not gate["passed"]:
            attempts.append(attempt)
            continue
        tick = _latest_tick(data, at)
        if tick is None:
            attempt["recovery_reason"] = "NO_TICK_AT_CHECKPOINT"
            attempts.append(attempt)
            continue
        age = (at - tick["time"]).total_seconds()
        bid, ask = float(tick["bid"]), float(tick["ask"])
        spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000
        price = ask + _tick_size(ask)
        attempt.update({"tick_age_seconds": age, "recovery_bid": bid,
                        "recovery_ask": ask, "spread_bps": spread_bps,
                        "entry_price": price})
        if not 0 <= age <= float(POLICY["wait_recovery"]["maximum_tick_age_seconds"]):
            attempt["recovery_reason"] = "RECOVERY_TICK_STALE"
            attempts.append(attempt)
            continue
        if spread_bps > float(POLICY["wait_recovery"]["maximum_spread_bps"]) + 1e-12:
            attempt["recovery_reason"] = "RECOVERY_SPREAD_TOO_WIDE"
            attempts.append(attempt)
            continue
        if bid + 1e-12 < float(row["original_entry_price"]):
            attempt["recovery_reason"] = "ORIGINAL_BREAKOUT_PROXY_NOT_HELD"
            attempts.append(attempt)
            continue
        anti_chase, features = _anti_chase(data, at, price)
        attempt.update(features)
        if not anti_chase:
            attempt["recovery_reason"] = "RECOVERY_ANTI_CHASE_FAILED"
            attempts.append(attempt)
            continue
        quantity, fee, cash = affordable_quantity(price)
        if quantity < 1000:
            attempt["recovery_reason"] = "RECOVERY_UNAFFORDABLE_OR_ABOVE_CAP"
            attempts.append(attempt)
            continue
        attempt["recovery_reason"] = "V2_RECOVERED_WITHIN_90S"
        attempts.append(attempt)
        return {
            "passed": True, "effective_time": at, "entry_price": price,
            "quantity": quantity, "entry_fee_twd": fee,
            "cash_required_twd": cash, "reason": "V2_RECOVERED_WITHIN_90S",
            "attempts": attempts, **attempt,
        }
    return {"passed": False, "effective_time": None, "entry_price": None,
            "reason": "V2_NOT_RECOVERED_WITHIN_90S", "attempts": attempts}


def _opportunity(row: dict[str, Any], data: dict[str, Any], variant: str) -> dict[str, Any]:
    if variant == "V4_WAIT_90S_V2_RECOVERY":
        return recovery_opportunity(row, data)
    gate = evaluate_volume_gate(data, row["signal_time"], variant, SOURCE_CONFIG)
    return {
        "passed": bool(gate["passed"]),
        "effective_time": row["signal_time"] if gate["passed"] else None,
        "entry_price": float(row["original_entry_price"]) if gate["passed"] else None,
        "reason": gate["reason"],
        **{key: value for key, value in gate.items()
           if key not in {"passed", "reason"}},
    }


def _select(rows: list[dict[str, Any]], candidates: dict[str, dict], variant: str):
    diagnostics, passed = [], []
    for row in rows:
        result = _opportunity(row, candidates[str(row["stock_id"])], variant)
        diagnostic = {
            "session_date": row["session_date"], "variant": variant,
            "signal_time": row["signal_time"], "stock_id": row["stock_id"],
            "stock_name": row["stock_name"],
            "production_score": row.get("production_score"),
            "gate_passed": result["passed"], "gate_reason": result["reason"],
            "effective_time": result.get("effective_time"),
            "recovery_attempts": json.dumps(result.get("attempts", []),
                                             default=str, ensure_ascii=False),
            **{key: value for key, value in result.items()
               if key not in {"passed", "reason", "attempts", "effective_time",
                              "gate_reason"}},
            "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
        }
        diagnostics.append(diagnostic)
        if result["passed"]:
            passed.append((row, result))
    if not passed:
        return None, None, diagnostics
    passed.sort(key=lambda item: (
        item[1]["effective_time"], -float(item[0].get("production_score") or 0),
        str(item[0]["stock_id"]),
    ))
    return passed[0][0], passed[0][1], diagnostics


def _stress_recovery_fill(result: dict[str, Any], data: dict[str, Any]):
    target = result["effective_time"] + timedelta(seconds=5)
    row = next((item for item in data["ticks"] if item["time"] >= target), None)
    if row is None:
        return None, "NO_TICK_AT_OR_AFTER_DELAY"
    age = (row["time"] - target).total_seconds()
    if not 0 <= age <= float(SPEC["maximum_tick_staleness_seconds"]):
        return None, "TICK_STALE_AFTER_DELAY"
    price = float(row["ask"])
    price += _tick_size(price)
    price += _tick_size(price)
    quantity, fee, cash = affordable_quantity(price)
    if quantity < 1000:
        return None, "STRESS_RECOVERY_UNAFFORDABLE_OR_ABOVE_CAP"
    return {"entry_time": row["time"], "entry_price": price, "quantity": quantity,
            "entry_fee_twd": fee, "cash_required_twd": cash}, None


def replay_day(day: str, rows: list[dict[str, Any]], candidates: dict[str, dict],
               market: dict[str, dict], coverage: dict[str, Any]):
    signals, trades = [], []
    for variant in VARIANTS:
        selected, result, diagnostics = _select(rows, candidates, variant)
        signals.extend(diagnostics)
        if selected is None or result is None:
            continue
        symbol = str(selected["stock_id"])
        for scenario in SCENARIOS:
            if variant == "V4_WAIT_90S_V2_RECOVERY" and result["reason"] != "ORIGINAL_V2_PASSED":
                if scenario == "BASE":
                    quantity, fee, cash = affordable_quantity(float(result["entry_price"]))
                    fill = {"entry_time": result["effective_time"],
                            "entry_price": float(result["entry_price"]),
                            "quantity": quantity, "entry_fee_twd": fee,
                            "cash_required_twd": cash}
                    unfilled = None
                else:
                    fill, unfilled = _stress_recovery_fill(result, candidates[symbol])
            else:
                fill, unfilled = entry_fill_with_reason(
                    selected, candidates[symbol], scenario
                )
            if fill is None:
                trades.append({"session_date": day, "variant": variant,
                               "scenario": scenario, "stock_id": symbol,
                               "stock_name": selected["stock_name"],
                               "status": "UNFILLED_ENTRY", "unfilled_reason": unfilled,
                               "net_pnl": None, "actual_orders": 0,
                               "actual_fills": 0, "broker_order_calls": 0})
                continue
            payload = {
                **selected, "confirmation_time": fill["entry_time"],
                "delayed_entry_price": fill["entry_price"],
                "delayed_entry_quantity": fill["quantity"],
            }
            trade = _simulate_exit(payload, {symbol: candidates[symbol]}, market, coverage)
            trade.update({"variant": variant, "scenario": scenario,
                          "entry_fee_twd": fill["entry_fee_twd"],
                          "cash_required_twd": fill["cash_required_twd"],
                          "gate_reason": result["reason"]})
            trades.append(trade)
    return signals, trades


def _metrics(variant: str, scenario: str, trades: list[dict[str, Any]], days: list[str]):
    matching = [row for row in trades if row["variant"] == variant and row["scenario"] == scenario]
    scored = [row for row in matching if row.get("status") == "SCORED"]
    by_day = {str(row["session_date"]): float(row["net_pnl"]) for row in scored}
    values = [by_day.get(day, 0.0) for day in days]
    trade_values = [float(row["net_pnl"]) for row in scored]
    wins = [value for value in trade_values if value > 0]
    losses = [value for value in trade_values if value < 0]
    equity = peak = 190_000.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    best = max(trade_values) if trade_values else None
    return {
        "variant": variant, "scenario": scenario, "session_count": len(days),
        "trade_count": len(matching), "scored_trade_count": len(scored),
        "no_trade_days": len(days) - len(matching),
        "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trade_values) if trade_values else None,
        "net_pnl_twd": sum(trade_values),
        "mean_trade_twd": mean(trade_values) if trade_values else None,
        "median_trade_twd": median(trade_values) if trade_values else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "best_trade_twd": best,
        "net_pnl_ex_best_trade_twd": (sum(trade_values) - best if best is not None else None),
        "max_drawdown_twd": drawdown, "ending_equity_twd": equity,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
    }


def _today_rows(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "realtime_expanded_shadow_events.jsonl"
    output = []
    if not path.exists():
        return output
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("event") not in {"CANDIDATE_SEEN", "SHADOW_SIGNAL_SELECTED"}:
            continue
        output.append({
            "session_date": str(row["decision_time"])[:10].replace("-", ""),
            "signal_time": __import__("datetime").datetime.fromisoformat(row["decision_time"]),
            "confirmation_time": __import__("datetime").datetime.fromisoformat(row["decision_time"]),
            "stock_id": str(row["stock_id"]), "stock_name": row["stock_name"],
            "original_entry_price": float(row["entry_price"]),
            "production_score": float(row.get("score") or 0),
        })
    unique = {}
    for row in output:
        unique[(row["stock_id"], row["signal_time"])] = row
    return list(unique.values())


def run(results_dir: Path = DEFAULT_RESULTS_DIR, prospective_run: Path | None = None):
    ledger = load_ledger(DEFAULT_RESULTS_DIR.parent / "strict_results" / "candidate_ledger.csv")
    sources = {str(paper["session_date"]): (run_dir, paper)
               for run_dir, paper in discover_sources(DEFAULT_PAPER_DIR, DEFAULT_RUNS_DIR)}
    if prospective_run is not None:
        source = json.loads((prospective_run / "run_manifest.json").read_text(encoding="utf-8"))
        if source.get("status") != "COMPLETE":
            raise RuntimeError("prospective run is not COMPLETE")
        day = source["started_at"][:10].replace("-", "")
        ledger.extend(_today_rows(prospective_run))
        sources[day] = (prospective_run, {"session_date": day})
    days = sorted(sources)
    signals, trades = [], []
    for day in sorted({str(row["session_date"]) for row in ledger}):
        if day not in sources:
            continue
        rows = [row for row in ledger if str(row["session_date"]) == day]
        symbols = {str(row["stock_id"]) for row in rows}
        run_dir, _paper = sources[day]
        source = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        candidates = _verified_selected_quotes(run_dir, symbols)
        market = _load_market_context(run_dir, source)
        coverage = {"session_date": day, "ended_at_taipei": source["ended_at"]}
        day_signals, day_trades = replay_day(day, rows, candidates, market, coverage)
        signals.extend(day_signals)
        trades.extend(day_trades)
    metrics = [_metrics(variant, scenario, trades, days)
               for variant in VARIANTS for scenario in SCENARIOS]
    results_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(results_dir / "signals.csv", signals)
    _write_csv(results_dir / "trades.csv", trades)
    _write_csv(results_dir / "experiments.csv", metrics)
    summary = {"analysis_id": POLICY["analysis_id"], "policy_hash": POLICY_HASH,
               "source_config_hash": SOURCE_CONFIG_HASH, "experiments": metrics,
               "session_dates": days, "actual_orders": 0, "actual_fills": 0,
               "broker_order_calls": 0,
               "interpretation": "KNOWN_ARCHIVE_EXPLORATORY_PLUS_20261111_POST_OUTCOME_NOT_LIVE_APPROVAL"}
    (results_dir / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    manifest = {"policy": POLICY, "summary": summary,
                "prospective_run": str(prospective_run) if prospective_run else None}
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (results_dir / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--prospective-run", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.results_dir, args.prospective_run), ensure_ascii=False,
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
