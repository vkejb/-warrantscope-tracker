"""Apply the pre-existing frozen V2 volume gate to expanded candidates."""

from __future__ import annotations

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

from .causal_auction import _verified_selected_quotes
from .scanner import affordable_quantity
from .strict_ablation import load_ledger
from .strict_replay import (
    DEFAULT_PAPER_DIR, DEFAULT_RESULTS_DIR, DEFAULT_RUNS_DIR,
    _simulate_exit, _write_csv, discover_sources,
)


SOURCE_CONFIG = load_config()
SOURCE_CONFIG_HASH = hashlib.sha256(canonical_bytes(SOURCE_CONFIG)).hexdigest()
VOLUME_POLICY = {
    "policy_id": "EXPANDED_FROZEN_VOLUME_ACCELERATION_V2",
    "frozen_at_taipei": "2026-11-10T00:00:00+08:00",
    "mode": "SHADOW_ONLY_KNOWN_ARCHIVE_CAUSAL_ACCOUNT_REPLAY",
    "source_policy": "V2_RECENT_VOLUME_ACCELERATION",
    "source_config_hash": SOURCE_CONFIG_HASH,
    "source_parameters": SOURCE_CONFIG["variants"]["V2_RECENT_VOLUME_ACCELERATION"],
    "variants": ["V0_NO_ADDITIONAL_GATE", "V2_RECENT_VOLUME_ACCELERATION"],
    "execution_scenarios": SOURCE_CONFIG["execution_scenarios"],
    "candidate_source": "CURRENT_ENGINE_FIRST_APPROVED_SIGNAL_PER_SYMBOL_LEDGER",
    "selection": "EARLIEST_SIGNAL_TIME_THEN_HIGHEST_PRODUCTION_SCORE",
    "capital_twd": 190_000,
    "maximum_trades_per_session": 1,
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
VOLUME_POLICY_HASH = hashlib.sha256(canonical_bytes(VOLUME_POLICY)).hexdigest()
DEFAULT_VOLUME_RESULTS_DIR = DEFAULT_RESULTS_DIR.parent / "expanded_volume_gate_results"


def gate_result(row: dict[str, Any], data: dict[str, Any], variant: str) -> dict[str, Any]:
    if variant == "V0_NO_ADDITIONAL_GATE":
        return {"evaluable": True, "passed": True,
                "reason": "BASELINE_NO_ADDITIONAL_VOLUME_GATE"}
    if variant != "V2_RECENT_VOLUME_ACCELERATION":
        raise ValueError(f"unknown variant: {variant}")
    return evaluate_volume_gate(data, row["signal_time"], variant, SOURCE_CONFIG)


def select_candidate(rows: list[dict[str, Any]], candidates: dict[str, dict],
                     variant: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    diagnostics = []
    by_time: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        by_time.setdefault(row["signal_time"], []).append(row)
    for at in sorted(by_time):
        passed = []
        for row in by_time[at]:
            result = gate_result(row, candidates[str(row["stock_id"])], variant)
            diagnostics.append({
                "session_date": row["session_date"], "variant": variant,
                "signal_time": row["signal_time"], "stock_id": row["stock_id"],
                "stock_name": row["stock_name"],
                "production_score": row.get("production_score"),
                "gate_evaluable": result["evaluable"], "gate_passed": result["passed"],
                "gate_reason": result["reason"],
                **{key: value for key, value in result.items()
                   if key not in {"evaluable", "passed", "reason"}},
                "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
            })
            if result["passed"]:
                passed.append(row)
        if passed:
            passed.sort(key=lambda row: (-float(row.get("production_score") or 0),
                                         str(row["stock_id"])))
            return passed[0], diagnostics
    return None, diagnostics


def entry_fill(row: dict[str, Any], data: dict[str, Any], scenario: str) -> dict[str, Any] | None:
    if scenario == "BASE":
        at = row["signal_time"]
        price = float(row["original_entry_price"])
    elif scenario == "STRESS":
        target = row["signal_time"] + timedelta(seconds=5)
        tick = next((item for item in data["ticks"] if item["time"] >= target), None)
        if tick is None or not 0 <= (tick["time"] - target).total_seconds() <= float(
            SPEC["maximum_tick_staleness_seconds"]
        ):
            return None
        at = tick["time"]
        price = float(tick["ask"])
        price += _tick_size(price)
        price += _tick_size(price)
    else:
        raise ValueError(f"unknown scenario: {scenario}")
    quantity, fee, cash = affordable_quantity(price)
    if quantity < 1000:
        return None
    return {"entry_time": at, "entry_price": price, "quantity": quantity,
            "entry_fee_twd": fee, "cash_required_twd": cash}


def _metrics(variant: str, scenario: str, selections: int,
             trades: list[dict[str, Any]], session_dates: list[str]) -> dict[str, Any]:
    matching = [row for row in trades
                if row["variant"] == variant and row["scenario"] == scenario]
    scored = [row for row in matching if row.get("status") == "SCORED"]
    by_day = {str(row["session_date"]): float(row["net_pnl"]) for row in scored}
    values = [by_day[day] for day in session_dates if day in by_day]
    wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
    best_trade = max(values) if values else None
    equity, peak, drawdown = 190_000.0, 190_000.0, 0.0
    for day in session_dates:
        equity += by_day.get(day, 0.0)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "variant": variant, "scenario": scenario,
        "session_count": len(session_dates), "selected_signal_count": selections,
        "trade_count": len(matching), "scored_trade_count": len(scored),
        "unfilled_or_unscorable_count": len(matching) - len(scored),
        "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(values) if values else None,
        "net_pnl_twd": sum(values), "mean_trade_twd": mean(values) if values else None,
        "median_trade_twd": median(values) if values else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "best_trade_twd": best_trade,
        "net_pnl_ex_best_trade_twd": sum(values) - best_trade if best_trade is not None else None,
        "max_drawdown_twd": drawdown, "ending_equity_twd": equity,
    }


def run_volume_gate(
    ledger_path: Path = DEFAULT_RESULTS_DIR / "candidate_ledger.csv",
    results_dir: Path = DEFAULT_VOLUME_RESULTS_DIR,
    paper_dir: Path = DEFAULT_PAPER_DIR,
    runs_dir: Path = DEFAULT_RUNS_DIR,
) -> dict[str, Any]:
    ledger = load_ledger(ledger_path)
    sources = {str(paper["session_date"]): (run_dir, paper)
               for run_dir, paper in discover_sources(paper_dir, runs_dir)}
    session_dates = sorted(sources)
    variants = list(VOLUME_POLICY["variants"])
    scenarios = list(VOLUME_POLICY["execution_scenarios"])
    diagnostics, trades = [], []
    selections = {(variant, scenario): 0 for variant in variants for scenario in scenarios}
    for day in sorted({str(row["session_date"]) for row in ledger}):
        rows = [row for row in ledger if str(row["session_date"]) == day]
        symbols = {str(row["stock_id"]) for row in rows}
        run_dir, _paper = sources[day]
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
        for variant in variants:
            selected, gate_rows = select_candidate(rows, candidates, variant)
            diagnostics.extend(gate_rows)
            if selected is None:
                continue
            symbol = str(selected["stock_id"])
            for scenario in scenarios:
                selections[(variant, scenario)] += 1
                fill = entry_fill(selected, candidates[symbol], scenario)
                if fill is None:
                    trades.append({
                        "session_date": day, "variant": variant, "scenario": scenario,
                        "stock_id": symbol, "stock_name": selected["stock_name"],
                        "signal_time": selected["signal_time"],
                        "status": "UNFILLED_ENTRY_QUOTE_OR_CAP",
                        "net_pnl": None, "actual_orders": 0, "actual_fills": 0,
                        "broker_order_calls": 0,
                    })
                    continue
                payload = {
                    **selected, "confirmation_time": fill["entry_time"],
                    "delayed_entry_price": fill["entry_price"],
                    "delayed_entry_quantity": fill["quantity"],
                }
                trade = _simulate_exit(payload, {symbol: candidates[symbol]}, market, coverage)
                trade.update({"variant": variant, "scenario": scenario,
                              "strategy_variant": f"{variant}_{scenario}",
                              "entry_fee_twd": fill["entry_fee_twd"],
                              "cash_required_twd": fill["cash_required_twd"]})
                trades.append(trade)
    metrics = [_metrics(variant, scenario, selections[(variant, scenario)],
                        trades, session_dates)
               for variant in variants for scenario in scenarios]
    results_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(results_dir / "signals.csv", diagnostics)
    _write_csv(results_dir / "trades.csv", trades)
    _write_csv(results_dir / "experiments.csv", metrics)
    summary = {
        "analysis_id": VOLUME_POLICY["policy_id"],
        "volume_policy_hash": VOLUME_POLICY_HASH,
        "source_config_hash": SOURCE_CONFIG_HASH,
        "known_archive_status": "EXPLORATORY_EXISTING_PARAMETERS_NEW_EXPANDED_APPLICATION",
        "experiments": metrics, "actual_orders": 0, "actual_fills": 0,
        "broker_order_calls": 0,
        "interpretation": "KNOWN_ARCHIVE_CAUSAL_REPLAY_NOT_OUT_OF_SAMPLE_NOT_LIVE_APPROVAL",
    }
    (results_dir / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    manifest = {"policy": VOLUME_POLICY, "summary": summary,
                "parent_ledger": str(ledger_path)}
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (results_dir / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return summary
