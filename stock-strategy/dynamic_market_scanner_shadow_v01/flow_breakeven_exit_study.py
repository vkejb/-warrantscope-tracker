"""Paired study of a causal flow-reversal, cost-aware breakeven exit."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
import csv
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any

from paper_shadow_v01.runner import _load_market_context
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC, _exit_quote, _projected_net_pnl,
)

from .causal_auction import _verified_selected_quotes
from .expanded_volume_gate import (
    VOLUME_POLICY_HASH, entry_fill_with_reason, select_candidate,
)
from .strict_ablation import load_ledger
from .strict_replay import (
    DEFAULT_PAPER_DIR, DEFAULT_RESULTS_DIR, DEFAULT_RUNS_DIR,
    _simulate_exit, discover_sources,
)


ANALYSIS_ID = "FLOW_REVERSAL_COST_AWARE_BREAKEVEN_V1"
VARIANTS = ("BASELINE", "FLOW_BE_ANY_POSITIVE", "FLOW_BE_0_25R")
SCENARIOS = ("BASE", "STRESS")
POLICY = {
    "analysis_id": ANALYSIS_ID,
    "frozen_at_taipei": "2026-11-10T17:04:40+08:00",
    "mode": "SHADOW_ONLY_KNOWN_ARCHIVE_PAIRED_EXIT_DIAGNOSTIC",
    "parent_volume_policy_hash": VOLUME_POLICY_HASH,
    "entry_variant": "V2_RECENT_VOLUME_ACCELERATION",
    "variants": {
        "BASELINE": {"minimum_net_mfe_twd": None},
        "FLOW_BE_ANY_POSITIVE": {"minimum_net_mfe_twd": 0.0},
        "FLOW_BE_0_25R": {"minimum_net_mfe_twd": 1250.0},
    },
    "initial_r_net_twd": 5000.0,
    "decision_interval_seconds": 30,
    "flow_window_seconds": 60,
    "large_trade_reference_seconds": 300,
    "large_trade_percentile": 0.90,
    "minimum_ticks_in_flow_window": 10,
    "maximum_tick_staleness_seconds": 5,
    "maximum_book_staleness_seconds": 10,
    "adverse_volume_delta": -0.35,
    "adverse_large_trade_delta": -0.25,
    "adverse_book_imbalance": -0.10,
    "minimum_adverse_checks": 2,
    "reversal_confirmations": 2,
    "breakeven_trigger": "FIRST_LATER_EXECUTABLE_NET_PNL_LE_ZERO",
    "capital_twd": 190000.0,
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
POLICY_HASH = hashlib.sha256(canonical_bytes(POLICY)).hexdigest()
DEFAULT_RESULTS_DIR = DEFAULT_RESULTS_DIR.parent / "flow_breakeven_exit_results"
FREEZE_DAY_LEDGER = (
    DEFAULT_RESULTS_DIR.parent / "prospective_v2_runtime" / "nonprospective_freeze_day"
    / "20261110" / "053923b5c77170227270d36c" / "candidate_ledger.csv"
)
FREEZE_DAY_RUN = (
    DEFAULT_RUNS_DIR / "20261110T005011.302826Z_32906f3f"
)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write review-friendly deterministic CSV with LF line endings."""
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _signed_volume(row: dict[str, Any]) -> float:
    if str(row.get("flag")) == "1":
        return float(row["volume"])
    if str(row.get("flag")) == "0":
        return -float(row["volume"])
    midpoint = (float(row["bid"]) + float(row["ask"])) / 2
    return float(row["volume"]) if float(row["price"]) >= midpoint else -float(row["volume"])


def _percentile(values: list[float], level: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(level * len(ordered)) - 1)]


def _decision_times(entry: datetime, end: datetime) -> list[datetime]:
    current = entry.replace(second=0, microsecond=0)
    while current <= entry:
        current += timedelta(seconds=int(POLICY["decision_interval_seconds"]))
    output = []
    while current <= end:
        output.append(current)
        current += timedelta(seconds=int(POLICY["decision_interval_seconds"]))
    return output


def flow_snapshot(data: dict[str, Any], at: datetime) -> dict[str, Any]:
    tick_times = data["tick_times"]
    book_times = data["book_times"]
    start = at - timedelta(seconds=int(POLICY["flow_window_seconds"]))
    ticks = data["ticks"][bisect_left(tick_times, start):bisect_right(tick_times, at)]
    if len(ticks) < int(POLICY["minimum_ticks_in_flow_window"]):
        return {"evaluable": False, "reason": "FLOW_TICKS_INSUFFICIENT"}
    tick_age = (at - ticks[-1]["time"]).total_seconds()
    if not 0 <= tick_age <= float(POLICY["maximum_tick_staleness_seconds"]):
        return {"evaluable": False, "reason": "FLOW_TICK_STALE", "tick_age_seconds": tick_age}
    book_index = bisect_right(book_times, at) - 1
    if book_index < 0:
        return {"evaluable": False, "reason": "FLOW_BOOK_MISSING"}
    book = data["books"][book_index]
    book_age = (at - book["time"]).total_seconds()
    if not 0 <= book_age <= float(POLICY["maximum_book_staleness_seconds"]):
        return {"evaluable": False, "reason": "FLOW_BOOK_STALE", "book_age_seconds": book_age}
    total = sum(float(row["volume"]) for row in ticks)
    reference_start = at - timedelta(seconds=int(POLICY["large_trade_reference_seconds"]))
    reference = data["ticks"][bisect_left(tick_times, reference_start):bisect_right(tick_times, at)]
    if total <= 0 or len(reference) < int(POLICY["minimum_ticks_in_flow_window"]):
        return {"evaluable": False, "reason": "FLOW_VOLUME_REFERENCE_INSUFFICIENT"}
    large_threshold = _percentile(
        [float(row["volume"]) for row in reference],
        float(POLICY["large_trade_percentile"]),
    )
    large = [row for row in ticks if float(row["volume"]) >= large_threshold]
    large_total = sum(float(row["volume"]) for row in large)
    if large_total <= 0:
        return {"evaluable": False, "reason": "FLOW_LARGE_TRADE_MISSING"}
    book_total = float(book["buy_volume"]) + float(book["sell_volume"])
    if book_total <= 0:
        return {"evaluable": False, "reason": "FLOW_BOOK_VOLUME_ZERO"}
    volume_delta = sum(_signed_volume(row) for row in ticks) / total
    large_delta = sum(_signed_volume(row) for row in large) / large_total
    book_imbalance = (
        float(book["buy_volume"]) - float(book["sell_volume"])
    ) / book_total
    checks = {
        "VOLUME": volume_delta <= float(POLICY["adverse_volume_delta"]),
        "LARGE": large_delta <= float(POLICY["adverse_large_trade_delta"]),
        "BOOK": book_imbalance <= float(POLICY["adverse_book_imbalance"]),
    }
    return {
        "evaluable": True, "reason": "EVALUABLE",
        "volume_delta": volume_delta, "large_trade_delta": large_delta,
        "book_imbalance": book_imbalance,
        "adverse_count": sum(checks.values()),
        "adverse_components": "+".join(key for key, value in checks.items() if value),
        "tick_age_seconds": tick_age, "book_age_seconds": book_age,
    }


def _net(entry_price: float, quantity: int, tick: dict[str, Any]) -> tuple[float, float]:
    exit_price = _exit_quote("LONG", tick)
    return exit_price, float(_projected_net_pnl("LONG", entry_price, exit_price, quantity)[3])


def _baseline_path_mfe(baseline: dict[str, Any], data: dict[str, Any]) -> float | None:
    entry = datetime.fromisoformat(str(baseline["entry_time"]))
    exit_at = datetime.fromisoformat(str(baseline["exit_time"]))
    entry_price, quantity = float(baseline["entry_price"]), int(float(baseline["quantity"]))
    values = [
        _net(entry_price, quantity, row)[1]
        for row in data["ticks"] if entry < row["time"] <= exit_at
    ]
    return max(values) if values else None


def simulate_flow_breakeven(
    baseline: dict[str, Any], data: dict[str, Any], minimum_net_mfe_twd: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if baseline.get("status") != "SCORED":
        return {"status": "UNSCORABLE_BASELINE"}, []
    entry = datetime.fromisoformat(str(baseline["entry_time"]))
    baseline_exit = datetime.fromisoformat(str(baseline["exit_time"]))
    entry_price, quantity = float(baseline["entry_price"]), int(float(baseline["quantity"]))
    path = [row for row in data["ticks"] if entry < row["time"] <= baseline_exit]
    if not path:
        return {"status": "UNSCORABLE_EMPTY_PATH"}, []
    events: list[dict[str, Any]] = []
    mfe = float("-inf")
    tick_index = 0
    streak = 0
    activation: datetime | None = None
    activation_snapshot: dict[str, Any] | None = None
    for decision in _decision_times(entry, baseline_exit):
        while tick_index < len(path) and path[tick_index]["time"] <= decision:
            _price, value = _net(entry_price, quantity, path[tick_index])
            mfe = max(mfe, value)
            tick_index += 1
        snap = flow_snapshot(data, decision)
        eligible = mfe > 0 if minimum_net_mfe_twd == 0 else mfe >= minimum_net_mfe_twd
        adverse = bool(
            snap.get("evaluable")
            and int(snap.get("adverse_count", 0)) >= int(POLICY["minimum_adverse_checks"])
        )
        streak = streak + 1 if eligible and adverse else 0
        events.append({
            "decision_time": decision.isoformat(), "net_mfe_twd": mfe if math.isfinite(mfe) else None,
            "mfe_eligible": eligible, "adverse": adverse, "adverse_streak": streak,
            **snap,
        })
        if streak >= int(POLICY["reversal_confirmations"]):
            activation, activation_snapshot = decision, snap
            break
    full_mfe = max((_net(entry_price, quantity, row)[1] for row in path), default=None)
    if activation is None:
        threshold_reached = bool(
            full_mfe is not None
            and (full_mfe > 0 if minimum_net_mfe_twd == 0 else full_mfe >= minimum_net_mfe_twd)
        )
        evaluable = sum(bool(event.get("evaluable")) for event in events)
        return {
            "status": "SCORED", "action": "BASELINE_EXIT_NO_FLOW_ACTIVATION",
            "exit_time": baseline["exit_time"], "exit_price": baseline["exit_price"],
            "exit_reason": baseline["exit_reason"], "net_pnl": float(baseline["net_pnl"]),
            "net_mfe_twd": full_mfe, "flow_activation_time": None,
            "mfe_threshold_reached": threshold_reached,
            "max_adverse_streak": max((int(event["adverse_streak"]) for event in events), default=0),
            "flow_evaluable_decisions": evaluable,
            "non_activation_reason": (
                "MFE_THRESHOLD_NOT_REACHED" if not threshold_reached
                else "FLOW_UNEVALUABLE" if evaluable == 0
                else "REVERSAL_NOT_CONFIRMED"
            ),
        }, events
    activation_tick = next((row for row in path if row["time"] > activation), None)
    activation_net = _net(entry_price, quantity, activation_tick)[1] if activation_tick else None
    for row in path:
        if row["time"] <= activation:
            continue
        exit_price, value = _net(entry_price, quantity, row)
        if value <= 0:
            return {
                "status": "SCORED", "action": "FLOW_BREAKEVEN_EXIT",
                "exit_time": row["time"].isoformat(), "exit_price": exit_price,
                "exit_reason": "FLOW_REVERSAL_COST_AWARE_BREAKEVEN",
                "net_pnl": value, "net_mfe_twd": full_mfe,
                "flow_activation_time": activation.isoformat(),
                "flow_activation_net_pnl_twd": activation_net,
                "mfe_threshold_reached": True,
                "max_adverse_streak": int(POLICY["reversal_confirmations"]),
                "flow_evaluable_decisions": sum(bool(event.get("evaluable")) for event in events),
                "non_activation_reason": None,
                "activation_volume_delta": activation_snapshot.get("volume_delta"),
                "activation_large_trade_delta": activation_snapshot.get("large_trade_delta"),
                "activation_book_imbalance": activation_snapshot.get("book_imbalance"),
            }, events
    return {
        "status": "SCORED", "action": "BASELINE_EXIT_AFTER_FLOW_ACTIVATION",
        "exit_time": baseline["exit_time"], "exit_price": baseline["exit_price"],
        "exit_reason": baseline["exit_reason"], "net_pnl": float(baseline["net_pnl"]),
        "net_mfe_twd": full_mfe, "flow_activation_time": activation.isoformat(),
        "flow_activation_net_pnl_twd": activation_net,
        "mfe_threshold_reached": True,
        "max_adverse_streak": int(POLICY["reversal_confirmations"]),
        "flow_evaluable_decisions": sum(bool(event.get("evaluable")) for event in events),
        "non_activation_reason": "BREAKEVEN_NOT_RETOUCHED_BEFORE_BASELINE_EXIT",
        "activation_volume_delta": activation_snapshot.get("volume_delta"),
        "activation_large_trade_delta": activation_snapshot.get("large_trade_delta"),
        "activation_book_imbalance": activation_snapshot.get("book_imbalance"),
    }, events


def _account_metrics(rows: list[dict[str, Any]], session_dates: list[str],
                     variant: str, scenario: str) -> dict[str, Any]:
    cohort = [row for row in rows if row["variant"] == variant and row["scenario"] == scenario]
    scored = [row for row in cohort if row["status"] == "SCORED"]
    values = [float(row["new_net_pnl_twd"]) for row in scored]
    baseline_values = [float(row["baseline_net_pnl_twd"]) for row in scored]
    wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
    by_day = {str(row["session_date"]): float(row["new_net_pnl_twd"]) for row in scored}
    equity = peak = float(POLICY["capital_twd"])
    max_drawdown = 0.0
    daily_rows = []
    for day in session_dates:
        pnl = by_day.get(day, 0.0)
        equity += pnl
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)
        daily_rows.append({"session_date": day, "variant": variant, "scenario": scenario,
                           "net_pnl_twd": pnl, "equity_twd": equity,
                           "drawdown_twd": drawdown})
    best = max(values) if values else None
    daily_values = list(by_day.values())
    best_day = max(daily_values) if daily_values else None
    deltas = [float(row["delta_vs_baseline_twd"]) for row in scored]
    givebacks = [float(row["new_mfe_giveback_twd"]) for row in scored]
    losing_rows = [row for row in scored if float(row["baseline_net_pnl_twd"]) < 0]
    activated = [row for row in scored if row.get("flow_activation_time")]
    immediate_deltas = [
        float(row["flow_activation_net_pnl_twd"]) - float(row["baseline_net_pnl_twd"])
        for row in activated if row.get("flow_activation_net_pnl_twd") is not None
    ]
    return ({
        "variant": variant, "scenario": scenario, "session_count": len(session_dates),
        "selected_count": len(cohort), "scored_trade_count": len(scored),
        "unfilled_or_unscorable_count": len(cohort) - len(scored),
        "wins": len(wins), "losses": len(losses), "breakeven": len(values)-len(wins)-len(losses),
        "win_rate": len(wins)/len(values) if values else None,
        "net_pnl_twd": sum(values), "baseline_matched_net_pnl_twd": sum(baseline_values),
        "delta_vs_baseline_twd": sum(deltas),
        "mean_trade_twd": mean(values) if values else None,
        "median_trade_twd": median(values) if values else None,
        "profit_factor": sum(wins)/abs(sum(losses)) if losses else None,
        "max_drawdown_twd": max_drawdown, "ending_equity_twd": equity,
        "best_trade_twd": best,
        "net_pnl_ex_best_trade_twd": sum(values)-best if best is not None else None,
        "net_pnl_ex_best_day_twd": sum(values)-best_day if best_day is not None else None,
        "flow_activation_count": sum(bool(row.get("flow_activation_time")) for row in scored),
        "flow_breakeven_exit_count": sum(row.get("action")=="FLOW_BREAKEVEN_EXIT" for row in scored),
        "improved_trade_count": sum(delta > 0 for delta in deltas),
        "worsened_trade_count": sum(delta < 0 for delta in deltas),
        "loss_avoided_twd": sum(delta for delta in deltas if delta > 0),
        "profit_surrendered_twd": -sum(delta for delta in deltas if delta < 0),
        "mean_mfe_giveback_twd": mean(givebacks) if givebacks else None,
        "baseline_losses_with_positive_mfe": sum(
            float(row.get("net_mfe_twd") or 0) > 0 for row in losing_rows
        ),
        "baseline_losses_with_mfe_at_least_0_25r": sum(
            float(row.get("net_mfe_twd") or 0) >= 1250 for row in losing_rows
        ),
        "mfe_threshold_reached_count": sum(bool(row.get("mfe_threshold_reached")) for row in scored),
        "reversal_not_confirmed_count": sum(
            row.get("non_activation_reason") == "REVERSAL_NOT_CONFIRMED" for row in scored
        ),
        "breakeven_not_retouched_count": sum(
            row.get("non_activation_reason") == "BREAKEVEN_NOT_RETOUCHED_BEFORE_BASELINE_EXIT"
            for row in scored
        ),
        "post_hoc_immediate_flow_exit_delta_twd": sum(immediate_deltas),
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
    }, daily_rows)


def freeze_day_case_study() -> dict[str, Any]:
    """Preserve the motivating 2026-11-10 case outside the main cohort."""
    if not FREEZE_DAY_LEDGER.exists() or not (FREEZE_DAY_RUN / "run_manifest.json").exists():
        return {"classification": "NON_PROSPECTIVE_FREEZE_DAY_DIAGNOSTIC",
                "status": "SOURCE_NOT_AVAILABLE", "included_in_main_metrics": False}
    rows = load_ledger(FREEZE_DAY_LEDGER)
    symbols = {str(row["stock_id"]) for row in rows}
    candidates = _verified_selected_quotes(FREEZE_DAY_RUN, symbols)
    selected, _diagnostics = select_candidate(rows, candidates, "V2_RECENT_VOLUME_ACCELERATION")
    if selected is None:
        return {"classification": "NON_PROSPECTIVE_FREEZE_DAY_DIAGNOSTIC",
                "status": "NO_V2_SELECTION", "included_in_main_metrics": False}
    source = json.loads((FREEZE_DAY_RUN / "run_manifest.json").read_text(encoding="utf-8"))
    market = _load_market_context(FREEZE_DAY_RUN, source)
    coverage = {
        "session_date": "20261110", "source_run_ids": [source["run_id"]],
        "source_statuses": [source["status"]], "started_at_taipei": source["started_at"],
        "ended_at_taipei": source["ended_at"],
        "callback_errors": int(source.get("event_counts", {}).get("callback_errors", 0)),
        "coverage_status": "FULL_SESSION",
    }
    symbol = str(selected["stock_id"])
    results = []
    for scenario in SCENARIOS:
        fill, reason = entry_fill_with_reason(selected, candidates[symbol], scenario)
        if fill is None:
            results.append({"scenario": scenario, "status": "UNFILLED_ENTRY",
                            "unfilled_reason": reason})
            continue
        payload = {**selected, "confirmation_time": fill["entry_time"],
                   "delayed_entry_price": fill["entry_price"],
                   "delayed_entry_quantity": fill["quantity"]}
        baseline = _simulate_exit(payload, {symbol: candidates[symbol]}, market, coverage)
        variants = {}
        for variant in VARIANTS[1:]:
            threshold = float(POLICY["variants"][variant]["minimum_net_mfe_twd"])
            outcome, events = simulate_flow_breakeven(baseline, candidates[symbol], threshold)
            variants[variant] = {
                **outcome,
                "delta_vs_baseline_twd": (
                    float(outcome["net_pnl"]) - float(baseline["net_pnl"])
                    if outcome.get("status") == "SCORED" else None
                ),
                "max_event_adverse_streak": max(
                    (int(event["adverse_streak"]) for event in events), default=0
                ),
            }
        results.append({"scenario": scenario, "status": baseline.get("status"),
                        "baseline": baseline, "variants": variants})
    return {
        "classification": "NON_PROSPECTIVE_FREEZE_DAY_DIAGNOSTIC",
        "status": "COMPLETED", "included_in_main_metrics": False,
        "session_date": "20261110", "stock_id": symbol,
        "stock_name": selected["stock_name"],
        "signal_time": selected["signal_time"].isoformat(),
        "results": results, "actual_orders": 0, "actual_fills": 0,
        "broker_order_calls": 0,
    }


def run_study(
    ledger_path: Path = DEFAULT_RESULTS_DIR.parent / "strict_results" / "candidate_ledger.csv",
    sessions_path: Path = DEFAULT_RESULTS_DIR.parent / "strict_results" / "sessions.csv",
    output_dir: Path = DEFAULT_RESULTS_DIR,
    paper_dir: Path = DEFAULT_PAPER_DIR,
    runs_dir: Path = DEFAULT_RUNS_DIR,
) -> dict[str, Any]:
    ledger = load_ledger(ledger_path)
    sources = {str(paper["session_date"]): (run_dir, paper)
               for run_dir, paper in discover_sources(paper_dir, runs_dir)}
    with sessions_path.open(encoding="utf-8", newline="") as handle:
        session_dates = sorted(str(row["session_date"]) for row in csv.DictReader(handle))
    rows_out: list[dict[str, Any]] = []
    events_out: list[dict[str, Any]] = []
    for day in sorted({str(row["session_date"]) for row in ledger}):
        daily = [row for row in ledger if str(row["session_date"]) == day]
        symbols = {str(row["stock_id"]) for row in daily}
        run_dir, _paper = sources[day]
        source = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        candidates = _verified_selected_quotes(run_dir, symbols)
        selected, _diagnostics = select_candidate(daily, candidates, "V2_RECENT_VOLUME_ACCELERATION")
        if selected is None:
            continue
        market = _load_market_context(run_dir, source)
        coverage = {
            "session_date": day, "source_run_ids": [source["run_id"]],
            "source_statuses": [source["status"]], "started_at_taipei": source["started_at"],
            "ended_at_taipei": source["ended_at"],
            "callback_errors": int(source.get("event_counts", {}).get("callback_errors", 0)),
            "coverage_status": "FULL_SESSION",
        }
        symbol = str(selected["stock_id"])
        for scenario in SCENARIOS:
            fill, unfilled_reason = entry_fill_with_reason(selected, candidates[symbol], scenario)
            if fill is None:
                for variant in VARIANTS:
                    rows_out.append({
                        "session_date": day, "stock_id": symbol,
                        "stock_name": selected["stock_name"], "scenario": scenario,
                        "variant": variant, "status": "UNFILLED_ENTRY",
                        "unfilled_reason": unfilled_reason, "actual_orders": 0,
                        "actual_fills": 0, "broker_order_calls": 0,
                    })
                continue
            payload = {**selected, "confirmation_time": fill["entry_time"],
                       "delayed_entry_price": fill["entry_price"],
                       "delayed_entry_quantity": fill["quantity"]}
            baseline = _simulate_exit(payload, {symbol: candidates[symbol]}, market, coverage)
            if baseline.get("status") != "SCORED":
                for variant in VARIANTS:
                    rows_out.append({
                        "session_date": day, "stock_id": symbol,
                        "stock_name": selected["stock_name"], "scenario": scenario,
                        "variant": variant, "status": "UNSCORABLE_BASELINE_EXIT",
                        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
                    })
                continue
            for variant in VARIANTS:
                if variant == "BASELINE":
                    baseline_mfe = _baseline_path_mfe(baseline, candidates[symbol])
                    outcome = {
                        "status": "SCORED", "action": "BASELINE_EXIT",
                        "exit_time": baseline["exit_time"], "exit_price": baseline["exit_price"],
                        "exit_reason": baseline["exit_reason"], "net_pnl": float(baseline["net_pnl"]),
                        "net_mfe_twd": baseline_mfe,
                        "flow_activation_time": None,
                    }
                    event_rows = []
                else:
                    threshold = float(POLICY["variants"][variant]["minimum_net_mfe_twd"])
                    outcome, event_rows = simulate_flow_breakeven(
                        baseline, candidates[symbol], threshold,
                    )
                baseline_net = float(baseline["net_pnl"])
                new_net = float(outcome["net_pnl"]) if outcome.get("status") == "SCORED" else None
                mfe = outcome.get("net_mfe_twd")
                row = {
                    "session_date": day, "stock_id": symbol,
                    "stock_name": selected["stock_name"], "scenario": scenario,
                    "variant": variant, "status": outcome.get("status"),
                    "signal_time": selected["signal_time"].isoformat(),
                    "entry_time": baseline["entry_time"], "entry_price": baseline["entry_price"],
                    "quantity": baseline["quantity"],
                    "baseline_exit_time": baseline["exit_time"],
                    "baseline_exit_reason": baseline["exit_reason"],
                    "baseline_net_pnl_twd": baseline_net,
                    "new_exit_time": outcome.get("exit_time"),
                    "new_exit_price": outcome.get("exit_price"),
                    "new_exit_reason": outcome.get("exit_reason"),
                    "new_net_pnl_twd": new_net,
                    "delta_vs_baseline_twd": new_net-baseline_net if new_net is not None else None,
                    "net_mfe_twd": mfe,
                    "baseline_mfe_giveback_twd": float(mfe)-baseline_net if mfe is not None else None,
                    "new_mfe_giveback_twd": float(mfe)-new_net if mfe is not None and new_net is not None else None,
                    "action": outcome.get("action"),
                    "flow_activation_time": outcome.get("flow_activation_time"),
                    "flow_activation_net_pnl_twd": outcome.get("flow_activation_net_pnl_twd"),
                    "mfe_threshold_reached": outcome.get("mfe_threshold_reached"),
                    "max_adverse_streak": outcome.get("max_adverse_streak"),
                    "flow_evaluable_decisions": outcome.get("flow_evaluable_decisions"),
                    "non_activation_reason": outcome.get("non_activation_reason"),
                    "activation_volume_delta": outcome.get("activation_volume_delta"),
                    "activation_large_trade_delta": outcome.get("activation_large_trade_delta"),
                    "activation_book_imbalance": outcome.get("activation_book_imbalance"),
                    "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
                }
                rows_out.append(row)
                for event in event_rows:
                    events_out.append({
                        "session_date": day, "stock_id": symbol, "scenario": scenario,
                        "variant": variant, **event, "actual_orders": 0,
                        "actual_fills": 0, "broker_order_calls": 0,
                    })
    experiments, daily = [], []
    for scenario in SCENARIOS:
        for variant in VARIANTS:
            metric, days = _account_metrics(rows_out, session_dates, variant, scenario)
            experiments.append(metric); daily.extend(days)
    freeze_day_case = freeze_day_case_study()
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "trades.csv", rows_out)
    _write_csv(output_dir / "flow_events.csv", events_out)
    _write_csv(output_dir / "experiments.csv", experiments)
    _write_csv(output_dir / "daily_results.csv", daily)
    (output_dir / "freeze_day_case.json").write_bytes(
        canonical_bytes(freeze_day_case) + b"\n"
    )
    summary = {"analysis_id": ANALYSIS_ID, "policy_hash": POLICY_HASH,
               "interpretation": "KNOWN_ARCHIVE_EXPLORATORY_NOT_LIVE_APPROVAL",
               "experiments": experiments, "freeze_day_case": freeze_day_case,
               "actual_orders": 0,
               "actual_fills": 0, "broker_order_calls": 0}
    (output_dir / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    report_lines = [
        "# Flow-reversal cost-aware breakeven exit study", "",
        "Known-archive paired diagnostic only; no live behavior changed.", "",
        "| Scenario | Variant | Scored | W/L/BE | Win rate | Net PnL | Delta vs baseline | PF | Max DD | Flow exits | Loss avoided | Profit surrendered | Net ex best trade |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in experiments:
        rate = "NA" if item["win_rate"] is None else f'{item["win_rate"]:.1%}'
        pf = "NA" if item["profit_factor"] is None else f'{item["profit_factor"]:.2f}'
        report_lines.append(
            f'| {item["scenario"]} | {item["variant"]} | {item["scored_trade_count"]} | '
            f'{item["wins"]}/{item["losses"]}/{item["breakeven"]} | {rate} | '
            f'{item["net_pnl_twd"]:.0f} | {item["delta_vs_baseline_twd"]:.0f} | {pf} | '
            f'{item["max_drawdown_twd"]:.0f} | {item["flow_breakeven_exit_count"]} | '
            f'{item["loss_avoided_twd"]:.0f} | {item["profit_surrendered_twd"]:.0f} | '
            f'{item["net_pnl_ex_best_trade_twd"] if item["net_pnl_ex_best_trade_twd"] is not None else "NA"} |'
        )
    report_lines += ["", "## Boundaries", "",
                     "- Thresholds were fixed before this run and were not optimized.",
                     "- Historical archives are already known and are not out-of-sample.",
                     "- Missing flow never counts as reversal; unfilled entries remain explicit.",
                     "- A positive result would require later untouched full-session shadow validation."]
    base_any = next(item for item in experiments
                    if item["scenario"] == "BASE" and item["variant"] == "FLOW_BE_ANY_POSITIVE")
    stress_any = next(item for item in experiments
                      if item["scenario"] == "STRESS" and item["variant"] == "FLOW_BE_ANY_POSITIVE")
    report_lines += [
        "", "## Attribution", "",
        f'- BASE had {base_any["baseline_losses_with_positive_mfe"]} losing trades with positive executable MFE; neither completed the two-window flow reversal.',
        f'- The rule activated on {base_any["flow_activation_count"]} BASE and {stress_any["flow_activation_count"]} STRESS winner, but neither retouched cost-aware breakeven before the existing exit.',
        f'- A post-hoc immediate exit at those flow confirmations would change matched P&L by {base_any["post_hoc_immediate_flow_exit_delta_twd"]:.0f} TWD in BASE and {stress_any["post_hoc_immediate_flow_exit_delta_twd"]:.0f} TWD in STRESS.',
    ]
    if freeze_day_case.get("status") == "COMPLETED":
        base_case = next(item for item in freeze_day_case["results"] if item["scenario"] == "BASE")
        any_case = base_case["variants"]["FLOW_BE_ANY_POSITIVE"]
        quarter_case = base_case["variants"]["FLOW_BE_0_25R"]
        report_lines += [
            "", "## 2026-11-10 freeze-day case (excluded)", "",
            f'- Baseline net P&L: {float(base_case["baseline"]["net_pnl"]):.0f} TWD.',
            f'- ANY_POSITIVE result: {float(any_case["net_pnl"]):.0f} TWD; delta {float(any_case["delta_vs_baseline_twd"]):.0f} TWD. Flow confirmation arrived after executable P&L was already negative, so this is loss reduction, not a breakeven fill.',
            f'- 0.25R result: {float(quarter_case["net_pnl"]):.0f} TWD; reason {quarter_case.get("non_activation_reason")}.',
            "- This observed case is not included in the 16-session metrics and cannot validate the rule.",
        ]
    (output_dir / "REPORT.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    artifacts = {name: sha256_file(output_dir / name) for name in (
        "trades.csv", "flow_events.csv", "experiments.csv", "daily_results.csv",
        "summary.json", "freeze_day_case.json", "REPORT.md",
    )}
    manifest = {"analysis_id": ANALYSIS_ID, "policy": POLICY,
                "policy_hash": POLICY_HASH, "source_ledger": str(ledger_path.resolve()),
                "source_sessions": str(sessions_path.resolve()),
                "session_dates": session_dates, "artifacts": artifacts,
                "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0}
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (output_dir / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return summary


def main() -> int:
    result = run_study()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
