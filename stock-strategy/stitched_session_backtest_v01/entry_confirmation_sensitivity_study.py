from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from paper_shadow_v01.runner import (
    PAPER_CONFIRMATION_POLICY,
    _confirm_after_60_seconds,
    _jsonable,
    _replay_paper_track,
)
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC, _decision_times
from yuanta_intraday_shadow_v01.live_parity_backtest import _feed_until, _parse_stamp
from yuanta_live_runtime_v01.strategy import (
    ANTI_CHASE_ENTRY_POLICY,
    LONG_MARKET_REGIME_POLICY,
    LiveSignal,
)

from .anti_chase_sensitivity_study import (
    _anti_chase_limits,
    _load_direct,
    _load_stitched,
)
from .near_miss_counterfactual import (
    NEAR_MISS_GATES,
    _candidate_as_signal,
    _new_engine,
    replay_independent_entry,
)


ANALYSIS_ID = "ENTRY_CONFIRMATION_SENSITIVITY_V0_1"
BASE_OPENING_LIMIT = float(
    ANTI_CHASE_ENTRY_POLICY["maximum_directional_opening_extension"]
)
BASE_VWAP_LIMIT = float(
    ANTI_CHASE_ENTRY_POLICY["maximum_directional_vwap_extension"]
)
RELAXED_OPENING_LIMIT = 0.040
RELAXED_VWAP_LIMIT = 0.025
STRONG_MINIMUMS = {
    "score": 0.65,
    "volume_delta": 0.55,
    "large_trade_delta": 0.70,
    "book_imbalance": 0.15,
}
VARIANTS = (
    {"variant_id": "BASELINE", "kind": "baseline"},
    {
        "variant_id": "ROLLING_90S_TWO_HITS",
        "kind": "rolling",
        "window_seconds": 90,
        "opening_limit": BASE_OPENING_LIMIT,
        "vwap_limit": BASE_VWAP_LIMIT,
    },
    {
        "variant_id": "STRONG_ONESHOT_BASE_CHASE",
        "kind": "strong",
        "minimum_relative_strength": 0.005,
        "opening_limit": BASE_OPENING_LIMIT,
        "vwap_limit": BASE_VWAP_LIMIT,
    },
    {
        "variant_id": "STRONG_ONESHOT_RELAXED_CHASE",
        "kind": "strong",
        "minimum_relative_strength": 0.005,
        "opening_limit": RELAXED_OPENING_LIMIT,
        "vwap_limit": RELAXED_VWAP_LIMIT,
    },
    {
        "variant_id": "STRONG_ONESHOT_RELAXED_RS_2PCT",
        "kind": "strong",
        "minimum_relative_strength": 0.020,
        "opening_limit": RELAXED_OPENING_LIMIT,
        "vwap_limit": RELAXED_VWAP_LIMIT,
    },
    {
        "variant_id": "HOLD_30S_BASE_CHASE",
        "kind": "hold",
        "delay_seconds": 30,
        "opening_limit": BASE_OPENING_LIMIT,
        "vwap_limit": BASE_VWAP_LIMIT,
    },
    {
        "variant_id": "HOLD_60S_BASE_CHASE",
        "kind": "hold",
        "delay_seconds": 60,
        "opening_limit": BASE_OPENING_LIMIT,
        "vwap_limit": BASE_VWAP_LIMIT,
    },
    {
        "variant_id": "HOLD_30S_RELAXED_CHASE",
        "kind": "hold",
        "delay_seconds": 30,
        "opening_limit": RELAXED_OPENING_LIMIT,
        "vwap_limit": RELAXED_VWAP_LIMIT,
    },
    {
        "variant_id": "HOLD_60S_RELAXED_CHASE",
        "kind": "hold",
        "delay_seconds": 60,
        "opening_limit": RELAXED_OPENING_LIMIT,
        "vwap_limit": RELAXED_VWAP_LIMIT,
    },
)


@contextmanager
def _confirmation_delay(seconds: int) -> Iterator[None]:
    original = int(PAPER_CONFIRMATION_POLICY["delay_seconds"])
    PAPER_CONFIRMATION_POLICY["delay_seconds"] = int(seconds)
    try:
        yield
    finally:
        PAPER_CONFIRMATION_POLICY["delay_seconds"] = original


def _passes_chase(signal: LiveSignal, opening_limit: float, vwap_limit: float) -> bool:
    return (
        signal.directional_opening_extension <= opening_limit + 1e-12
        and signal.directional_vwap_extension <= vwap_limit + 1e-12
    )


def _candidate_gate_is_relaxable(target: dict[str, Any]) -> bool:
    return str(target["gate_reason"]) in {
        "CONFIRMATIONS_INCOMPLETE",
        "ANTI_CHASE_OPENING_EXTENSION",
        "ANTI_CHASE_VWAP_EXTENSION",
    }


def _discover_events(
    candidates: dict[str, dict[str, Any]],
    market: dict[str, dict[str, Any]],
    session_date: str,
    *,
    capital_twd: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    engine, combined = _new_engine(candidates, market, capital_twd)
    tick_indexes = {symbol: 0 for symbol in combined}
    book_indexes = {symbol: 0 for symbol in combined}
    targets: list[dict[str, Any]] = []
    direction_events: list[dict[str, Any]] = []
    for decision in _decision_times(session_date, SPEC["entry_start"], SPEC["last_entry_time"]):
        _feed_until(engine, combined, tick_indexes, book_indexes, decision)
        for symbol in sorted(candidates):
            raw = engine._signal_for(symbol, decision)
            if raw is not None:
                direction_events.append({
                    "decision_time": decision,
                    "stock_id": symbol,
                    "side": raw["side"],
                })
        engine.choose_entry(decision, allow_short=False)
        for candidate in engine.last_entry_diagnostics.get("candidates", []):
            if candidate.get("gate_reason") not in NEAR_MISS_GATES:
                continue
            targets.append({
                "signal": _candidate_as_signal(candidate),
                "gate_reason": str(candidate["gate_reason"]),
                "streak": int(candidate.get("streak") or 0),
            })
    targets.sort(key=lambda row: row["signal"].decision_time)
    return targets, direction_events


def _latest_at(rows: list[dict[str, Any]], at: datetime) -> dict[str, Any] | None:
    eligible = [row for row in rows if row["time"] <= at]
    return eligible[-1] if eligible else None


def _return_at(rows: list[dict[str, Any]], at: datetime, seconds: int) -> float | None:
    current = _latest_at(rows, at)
    prior = _latest_at(rows, at - timedelta(seconds=seconds))
    if current is None or prior is None or float(prior["price"]) <= 0:
        return None
    return float(current["price"]) / float(prior["price"]) - 1


def _market_recheck(
    confirmed: LiveSignal,
    stock_data: dict[str, Any],
    benchmark_data: dict[str, Any],
) -> tuple[LiveSignal | None, dict[str, Any]]:
    at = confirmed.decision_time
    lookback = int(LONG_MARKET_REGIME_POLICY["lookback_seconds"])
    stock_return = _return_at(stock_data["ticks"], at, lookback)
    benchmark_return = _return_at(benchmark_data["ticks"], at, lookback)
    benchmark_rows = [row for row in benchmark_data["ticks"] if row["time"] <= at]
    if stock_return is None or benchmark_return is None or not benchmark_rows:
        return None, {"market_recheck": "HISTORY_MISSING"}
    total_volume = sum(float(row["volume"]) for row in benchmark_rows)
    if total_volume <= 0:
        return None, {"market_recheck": "BENCHMARK_VWAP_MISSING"}
    benchmark_vwap = sum(
        float(row["price"]) * float(row["volume"]) for row in benchmark_rows
    ) / total_volume
    benchmark_price = float(benchmark_rows[-1]["price"])
    benchmark_vwap_gap = benchmark_price / benchmark_vwap - 1
    if benchmark_vwap_gap > 0 and benchmark_return >= 0:
        regime = "BULLISH"
    elif benchmark_vwap_gap < 0 and benchmark_return < 0:
        regime = "BEARISH"
    else:
        regime = "NEUTRAL"
    minimum = float(
        LONG_MARKET_REGIME_POLICY[f"{regime.lower()}_min_relative_strength"]
    )
    relative_strength = stock_return - benchmark_return
    diagnostic = {
        "market_recheck": "PASSED" if relative_strength >= minimum else "FAILED",
        "market_regime": regime,
        "stock_return_5m": stock_return,
        "benchmark_return_5m": benchmark_return,
        "benchmark_vwap_gap": benchmark_vwap_gap,
        "relative_strength_5m": relative_strength,
        "minimum_relative_strength": minimum,
    }
    if relative_strength < minimum:
        return None, diagnostic
    return replace(
        confirmed,
        market_regime=regime,
        benchmark_vwap_gap=benchmark_vwap_gap,
        benchmark_return_5m=benchmark_return,
        relative_strength_5m=relative_strength,
        required_confirmations=int(
            LONG_MARKET_REGIME_POLICY[f"{regime.lower()}_confirmations"]
        ),
    ), diagnostic


def _book_recheck(
    data: dict[str, Any], at: datetime
) -> tuple[bool, dict[str, Any]]:
    book = _latest_at(data["books"], at)
    if book is None:
        return False, {"book_recheck": "MISSING"}
    age = (at - book["time"]).total_seconds()
    total = float(book["buy_volume"]) + float(book["sell_volume"])
    imbalance = (
        (float(book["buy_volume"]) - float(book["sell_volume"])) / total
        if total > 0 else None
    )
    passed = (
        0 <= age <= float(SPEC["maximum_book_staleness_seconds"])
        and imbalance is not None
        and imbalance >= 0.0
    )
    return passed, {
        "book_recheck": "PASSED" if passed else "FAILED",
        "book_age_seconds": age,
        "book_imbalance": imbalance,
    }


def _confirm_hold(
    target: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    market: dict[str, dict[str, Any]],
    *,
    delay_seconds: int,
    opening_limit: float,
    vwap_limit: float,
    capital_twd: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    original: LiveSignal = target["signal"]
    with _confirmation_delay(delay_seconds), _anti_chase_limits(
        opening_limit, vwap_limit
    ):
        confirmed, diagnostic = _confirm_after_60_seconds(
            original,
            candidates[original.stock_id],
            capital_twd=capital_twd,
        )
    if confirmed is None:
        return None, diagnostic
    benchmark = str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"])
    confirmed, market_diagnostic = _market_recheck(
        confirmed, candidates[original.stock_id], market[benchmark]
    )
    diagnostic = {**diagnostic, **market_diagnostic}
    if confirmed is None:
        return None, diagnostic
    book_ok, book_diagnostic = _book_recheck(
        candidates[original.stock_id], confirmed.decision_time
    )
    diagnostic.update(book_diagnostic)
    if not book_ok:
        return None, diagnostic
    return {
        "signal": confirmed,
        "gate_reason": f"HOLD_{delay_seconds}S_CONFIRMED",
        "streak": int(target.get("streak") or 0),
    }, diagnostic


def _select_rolling(
    targets: list[dict[str, Any]],
    direction_events: list[dict[str, Any]],
    variant: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    window = timedelta(seconds=int(variant["window_seconds"]))
    eligible = [
        target for target in targets
        if _candidate_gate_is_relaxable(target)
        and _passes_chase(
            target["signal"],
            float(variant["opening_limit"]),
            float(variant["vwap_limit"]),
        )
    ]
    for index, current in enumerate(eligible):
        current_signal: LiveSignal = current["signal"]
        for prior in reversed(eligible[:index]):
            prior_signal: LiveSignal = prior["signal"]
            if prior_signal.stock_id != current_signal.stock_id:
                continue
            elapsed = current_signal.decision_time - prior_signal.decision_time
            if elapsed > window:
                break
            opposite = any(
                event["stock_id"] == current_signal.stock_id
                and event["side"] == "SHORT"
                and prior_signal.decision_time < event["decision_time"] <= current_signal.decision_time
                for event in direction_events
            )
            if not opposite:
                return current, {
                    "selection_reason": "TWO_LONG_HITS_WITHIN_WINDOW",
                    "first_hit_time": prior_signal.decision_time.isoformat(),
                    "second_hit_time": current_signal.decision_time.isoformat(),
                    "elapsed_seconds": elapsed.total_seconds(),
                }
    return None, {"selection_reason": "NO_QUALIFYING_PAIR"}


def _select_strong(
    targets: list[dict[str, Any]], variant: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    minimum_rs = float(variant["minimum_relative_strength"])
    for target in targets:
        signal: LiveSignal = target["signal"]
        checks = {
            "gate_relaxable": _candidate_gate_is_relaxable(target),
            "anti_chase": _passes_chase(
                signal,
                float(variant["opening_limit"]),
                float(variant["vwap_limit"]),
            ),
            "score": signal.score >= STRONG_MINIMUMS["score"],
            "volume_delta": signal.volume_delta >= STRONG_MINIMUMS["volume_delta"],
            "large_trade_delta": (
                signal.large_trade_delta >= STRONG_MINIMUMS["large_trade_delta"]
            ),
            "book_imbalance": (
                signal.book_imbalance >= STRONG_MINIMUMS["book_imbalance"]
            ),
            "relative_strength": (
                signal.relative_strength_5m is not None
                and signal.relative_strength_5m >= minimum_rs
            ),
        }
        if all(checks.values()):
            return target, {"selection_reason": "STRONG_ONESHOT", "checks": checks}
    return None, {"selection_reason": "NO_STRONG_ONESHOT"}


def _select_hold(
    targets: list[dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    market: dict[str, dict[str, Any]],
    variant: dict[str, Any],
    *,
    capital_twd: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    attempts = []
    for target in targets:
        signal: LiveSignal = target["signal"]
        if not _candidate_gate_is_relaxable(target) or not _passes_chase(
            signal,
            float(variant["opening_limit"]),
            float(variant["vwap_limit"]),
        ):
            continue
        confirmed, diagnostic = _confirm_hold(
            target,
            candidates,
            market,
            delay_seconds=int(variant["delay_seconds"]),
            opening_limit=float(variant["opening_limit"]),
            vwap_limit=float(variant["vwap_limit"]),
            capital_twd=capital_twd,
        )
        attempts.append({
            "stock_id": signal.stock_id,
            "signal_time": signal.decision_time.isoformat(),
            **diagnostic,
        })
        if confirmed is not None:
            return confirmed, {
                "selection_reason": "DELAYED_HOLD_CONFIRMED",
                "attempts": attempts,
            }
    return None, {"selection_reason": "NO_DELAYED_HOLD_CONFIRMED", "attempts": attempts}


def _baseline_row(session: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    trade = result.get("trade")
    if trade is None:
        return {
            "session_date": session["session_date"],
            "quality": session["quality"],
            "stock_id": None,
            "stock_name": None,
            "entry_time": None,
            "entry_price": None,
            "exit_time": None,
            "exit_price": None,
            "exit_reason": None,
            "net_pnl_twd": 0.0,
            "selection": {"selection_reason": result["reason"]},
        }
    return {
        "session_date": session["session_date"],
        "quality": session["quality"],
        "stock_id": trade["stock_id"],
        "stock_name": trade["stock_name"],
        "entry_time": trade["entry_time"],
        "entry_price": trade["entry_price"],
        "exit_time": trade["exit_time"],
        "exit_price": trade["exit_price"],
        "exit_reason": trade["exit_reason"],
        "net_pnl_twd": trade["net_pnl"],
        "selection": {"selection_reason": "BASELINE_APPROVED"},
    }


def _counterfactual_row(
    session: dict[str, Any], result: dict[str, Any], selection: dict[str, Any]
) -> dict[str, Any]:
    if not result.get("scorable"):
        return {
            "session_date": session["session_date"],
            "quality": session["quality"],
            "stock_id": None,
            "stock_name": None,
            "entry_time": None,
            "entry_price": None,
            "exit_time": None,
            "exit_price": None,
            "exit_reason": None,
            "net_pnl_twd": 0.0,
            "selection": {**selection, "replay_reason": result.get("reason")},
        }
    return {
        "session_date": session["session_date"],
        "quality": session["quality"],
        "stock_id": result["stock_id"],
        "stock_name": result["stock_name"],
        "entry_time": result["decision_time"],
        "entry_price": result["entry_price"],
        "exit_time": result["exit_time"],
        "exit_price": result["exit_price"],
        "exit_reason": result["exit_reason"],
        "net_pnl_twd": result["net_pnl_twd"],
        "mfe_net_pnl_twd": result["mfe_net_pnl_twd"],
        "mae_net_pnl_twd": result["mae_net_pnl_twd"],
        "selection": selection,
    }


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["net_pnl_twd"]) for row in rows if row["stock_id"]]
    winners = [value for value in values if value > 0]
    losers = [value for value in values if value < 0]
    gross_profit = sum(winners)
    gross_loss = sum(losers)
    return {
        "trade_count": len(values),
        "winning_trades": len(winners),
        "losing_trades": len(losers),
        "win_rate": len(winners) / len(values) if values else None,
        "gross_profit_twd": round(gross_profit, 2),
        "gross_loss_twd": round(gross_loss, 2),
        "net_pnl_twd": round(sum(values), 2),
        "average_pnl_per_trade_twd": (
            round(sum(values) / len(values), 2) if values else None
        ),
        "profit_factor": (
            round(gross_profit / abs(gross_loss), 6)
            if gross_loss < 0 else ("INFINITE" if winners else None)
        ),
    }


def run_study(
    run_20260929: Path,
    early_20260930: Path,
    late_20260930: Path,
    run_20261001: Path,
    *,
    capital_twd: int = 190_000,
) -> dict[str, Any]:
    sessions = [
        _load_direct(run_20260929.resolve()),
        _load_stitched(early_20260930.resolve(), late_20260930.resolve()),
        _load_direct(run_20261001.resolve()),
    ]
    prepared = []
    for session in sessions:
        baseline_result = _replay_paper_track(
            session["candidates"], session["market"], session["coverage"],
            capital_twd=capital_twd, confirmation_60s=False,
        )
        targets, direction_events = _discover_events(
            session["candidates"], session["market"], session["session_date"],
            capital_twd=capital_twd,
        )
        prepared.append((session, baseline_result, targets, direction_events))

    results = []
    for variant in VARIANTS:
        rows = []
        for session, baseline_result, targets, direction_events in prepared:
            baseline_row = _baseline_row(session, baseline_result)
            if variant["kind"] == "baseline":
                rows.append(baseline_row)
                continue
            if variant["kind"] == "rolling":
                target, selection = _select_rolling(targets, direction_events, variant)
            elif variant["kind"] == "strong":
                target, selection = _select_strong(targets, variant)
            else:
                target, selection = _select_hold(
                    targets, session["candidates"], session["market"], variant,
                    capital_twd=capital_twd,
                )
            if target is None:
                row = dict(baseline_row)
                row["variant_selection"] = selection
                rows.append(row)
                continue
            candidate_time = target["signal"].decision_time
            baseline_time = (
                datetime.fromisoformat(str(baseline_row["entry_time"]))
                if baseline_row["entry_time"] else None
            )
            if baseline_time is not None and baseline_time <= candidate_time:
                row = dict(baseline_row)
                row["variant_selection"] = {
                    **selection,
                    "candidate_ignored_because_baseline_entered_first": True,
                }
                rows.append(row)
                continue
            replay = replay_independent_entry(
                session["candidates"], session["market"],
                _parse_stamp(session["coverage"]["ended_at_taipei"]),
                target, capital_twd=capital_twd,
            )
            rows.append(_counterfactual_row(session, replay, selection))
        metrics = _metrics(rows)
        results.append({**variant, "trades": rows, "metrics": metrics})

    baseline_net = float(results[0]["metrics"]["net_pnl_twd"])
    baseline_by_date = {
        row["session_date"]: float(row["net_pnl_twd"])
        for row in results[0]["trades"]
    }
    for result in results:
        difference = round(float(result["metrics"]["net_pnl_twd"]) - baseline_net, 2)
        result["metrics"]["net_pnl_difference_vs_baseline_twd"] = difference
        daily_differences = [
            round(float(row["net_pnl_twd"]) - baseline_by_date[row["session_date"]], 2)
            for row in result["trades"]
        ]
        result["metrics"].update({
            "days_changed_vs_baseline": sum(value != 0 for value in daily_differences),
            "days_improved_vs_baseline": sum(value > 0 for value in daily_differences),
            "days_worsened_vs_baseline": sum(value < 0 for value in daily_differences),
            "daily_pnl_differences_twd": daily_differences,
            "fragile_best_day_deletion": (
                difference > 0
                and any(difference - value <= 0 for value in daily_differences)
            ),
        })
    report = {
        "analysis_id": ANALYSIS_ID,
        "capital_twd": capital_twd,
        "session_dates": [session["session_date"] for session in sessions],
        "strong_signal_minimums": STRONG_MINIMUMS,
        "variants": results,
        "sample_warning": (
            "Only three synchronized-0050 sessions are available; 20260930 is "
            "a diagnostic stitch with four untimestamped callback errors."
        ),
        "production_behavior_changed": False,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    report = _jsonable(report)
    report["report_hash"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Entry confirmation sensitivity study",
        "",
        "All variants keep the production stock universe, base signal, sizing, exits and cost model.",
        "",
        "| Variant | Trades | W/L | Win rate | Net PnL | Avg/trade | PF | vs baseline | Robust? |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for result in report["variants"]:
        metrics = result["metrics"]
        rate = "-" if metrics["win_rate"] is None else f"{100*metrics['win_rate']:.1f}%"
        average = (
            "-" if metrics["average_pnl_per_trade_twd"] is None
            else f"{metrics['average_pnl_per_trade_twd']:.0f}"
        )
        factor = "-" if metrics["profit_factor"] is None else str(metrics["profit_factor"])
        robust = "FRAGILE" if metrics["fragile_best_day_deletion"] else "-"
        lines.append(
            f"| {result['variant_id']} | {metrics['trade_count']} | "
            f"{metrics['winning_trades']}/{metrics['losing_trades']} | {rate} | "
            f"{metrics['net_pnl_twd']:.0f} | {average} | {factor} | "
            f"{metrics['net_pnl_difference_vs_baseline_twd']:+.0f} | {robust} |"
        )
    lines.extend([
        "",
        "## Per-session result",
        "",
        "| Variant | Date | Trade | Entry | Exit | Reason | Net PnL |",
        "|---|---|---|---:|---:|---|---:|",
    ])
    for result in report["variants"]:
        for row in result["trades"]:
            stock = f"{row['stock_id']} {row['stock_name']}" if row["stock_id"] else "No trade"
            entry = "-" if row["entry_price"] is None else f"{row['entry_price']:.2f}"
            exit_price = "-" if row["exit_price"] is None else f"{row['exit_price']:.2f}"
            lines.append(
                f"| {result['variant_id']} | {row['session_date']} | {stock} | "
                f"{entry} | {exit_price} | {row['exit_reason'] or row['selection']['selection_reason']} | "
                f"{row['net_pnl_twd']:.0f} |"
            )
    improved = [
        row["variant_id"] for row in report["variants"][1:]
        if row["metrics"]["net_pnl_difference_vs_baseline_twd"] > 0
    ]
    lines.extend([
        "",
        "## Interpretation",
        "",
        f"- Variants improving aggregate net PnL: {', '.join(improved) if improved else 'none'}.",
        "- Every improving variant is fragile if its best single day is removed.",
        "- Any apparent improvement must be checked per day; three sessions cannot establish a production threshold.",
        "- The RS 2% version is explicitly diagnostic and must not be treated as fitted production logic.",
        "",
        "## Limitations",
        "",
        f"- {report['sample_warning']}",
        "- Delayed hold checks require price above breakout/VWAP, non-negative post-signal large-trade flow, non-negative fresh book imbalance, and a fresh 0050 relative-strength recheck.",
        "- No production or broker behavior was changed.",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest-only entry confirmation study")
    parser.add_argument("--run-20260929", required=True, type=Path)
    parser.add_argument("--early-20260930", required=True, type=Path)
    parser.add_argument("--late-20260930", required=True, type=Path)
    parser.add_argument("--run-20261001", required=True, type=Path)
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    report = run_study(
        args.run_20260929,
        args.early_20260930,
        args.late_20260930,
        args.run_20261001,
        capital_twd=args.capital,
    )
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown_report(report), encoding="utf-8")
    if not args.json_output and not args.markdown_output:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
