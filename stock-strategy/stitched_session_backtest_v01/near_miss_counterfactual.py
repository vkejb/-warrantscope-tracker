from __future__ import annotations

import argparse
from dataclasses import fields
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

from paper_shadow_v01.runner import _load_market_context, _jsonable
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC,
    _decision_times,
    _projected_net_pnl,
    load_session,
)
from yuanta_intraday_shadow_v01.live_parity_backtest import (
    _feed_until,
    _parse_stamp,
    _record_book,
    _record_tick,
)
from yuanta_live_runtime_v01.strategy import (
    LIVE_EXIT_POLICY,
    LONG_MARKET_REGIME_POLICY,
    LiveDirectionEngine,
    LiveSignal,
    ManagedPosition,
)

from .analysis import _manifest, _validate_pair, stitch_streams


ANALYSIS_ID = "NEAR_MISS_COUNTERFACTUAL_V0_1"
NEAR_MISS_GATES = {
    "ANTI_CHASE_OPENING_EXTENSION",
    "ANTI_CHASE_VWAP_EXTENSION",
    "CONFIRMATIONS_INCOMPLETE",
    "RELATIVE_STRENGTH_BELOW_THRESHOLD",
    "STOCK_5M_HISTORY_MISSING",
}


def _new_engine(
    candidates: dict[str, dict[str, Any]],
    market: dict[str, dict[str, Any]],
    capital_twd: int,
) -> tuple[LiveDirectionEngine, dict[str, dict[str, Any]]]:
    combined = {**candidates, **market}
    metadata = {
        symbol: str(data.get("meta", {}).get("stock_name", symbol))
        for symbol, data in combined.items()
    }
    engine = LiveDirectionEngine(
        metadata,
        capital_twd=capital_twd,
        candidate_symbols=set(candidates),
        benchmark_symbol=str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"]),
    )
    return engine, combined


def _candidate_as_signal(candidate: dict[str, Any]) -> LiveSignal:
    names = {item.name for item in fields(LiveSignal)}
    values = {name: candidate[name] for name in names if name in candidate}
    return LiveSignal(**values)


def discover_near_misses(
    candidates: dict[str, dict[str, Any]],
    market: dict[str, dict[str, Any]],
    session_date: str,
    *,
    capital_twd: int,
) -> list[dict[str, Any]]:
    engine, combined = _new_engine(candidates, market, capital_twd)
    tick_indexes = {symbol: 0 for symbol in combined}
    book_indexes = {symbol: 0 for symbol in combined}
    output: list[dict[str, Any]] = []
    for decision in _decision_times(session_date, SPEC["entry_start"], SPEC["last_entry_time"]):
        _feed_until(engine, combined, tick_indexes, book_indexes, decision)
        engine.choose_entry(decision, allow_short=False)
        for candidate in engine.last_entry_diagnostics.get("candidates", []):
            if candidate.get("gate_reason") not in NEAR_MISS_GATES:
                continue
            signal = _candidate_as_signal(candidate)
            output.append({
                "signal": signal,
                "gate_reason": str(candidate["gate_reason"]),
                "streak": int(candidate.get("streak") or 0),
                "minimum_relative_strength": candidate.get("minimum_relative_strength"),
            })
    return output


def replay_independent_entry(
    candidates: dict[str, dict[str, Any]],
    market: dict[str, dict[str, Any]],
    coverage_end: datetime,
    target: dict[str, Any],
    *,
    capital_twd: int,
) -> dict[str, Any]:
    """Force one rejected candidate through entry, keeping production exits intact."""
    signal: LiveSignal = target["signal"]
    engine, combined = _new_engine(candidates, market, capital_twd)
    tick_indexes = {symbol: 0 for symbol in combined}
    book_indexes = {symbol: 0 for symbol in combined}

    # Rebuild all causal state and confirmation streaks exactly through the
    # target decision. Only the final entry gate is overridden.
    for decision in _decision_times(
        signal.decision_time.strftime("%Y%m%d"),
        SPEC["entry_start"],
        SPEC["last_entry_time"],
    ):
        if decision > signal.decision_time:
            break
        _feed_until(engine, combined, tick_indexes, book_indexes, decision)
        engine.choose_entry(decision, allow_short=False)

    position = ManagedPosition(
        stock_id=signal.stock_id,
        stock_name=signal.stock_name,
        side=signal.side,
        quantity=signal.quantity,
        entry_price=signal.entry_price,
        entry_order_id="COUNTERFACTUAL_ONLY_NO_ORDER",
        entry_time=signal.decision_time,
    )
    data = candidates[signal.stock_id]
    events: list[tuple[datetime, int, str, dict[str, Any] | None]] = []
    for row in data["ticks"][tick_indexes[signal.stock_id]:]:
        events.append((row["time"], 0, "tick", row))
    for row in data["books"][book_indexes[signal.stock_id]:]:
        events.append((row["time"], 1, "book", row))
    decision = signal.decision_time + timedelta(seconds=int(SPEC["decision_interval_seconds"]))
    while decision <= coverage_end:
        events.append((decision, 2, "decision", None))
        decision += timedelta(seconds=int(SPEC["decision_interval_seconds"]))
    events.sort(key=lambda item: (item[0], item[1]))

    exit_decision = None
    exit_time = None
    mfe_net = float("-inf")
    mae_net = float("inf")
    for at, _priority, kind, row in events:
        reversal = False
        if kind == "tick":
            assert row is not None
            _record_tick(engine, signal.stock_id, row)
        elif kind == "book":
            assert row is not None
            _record_book(engine, signal.stock_id, row)
        else:
            reversal = engine.opposite_signal(position, at)
            engine.last_decision = at
        quote = engine.safe_exit_quote(position, at, max_age_seconds=3.0)
        if quote is not None:
            projected = engine.projected_net(position, quote.price)
            mfe_net = max(mfe_net, projected)
            mae_net = min(mae_net, projected)
        exit_decision = engine.evaluate_exit(
            position,
            at,
            reversal=reversal,
            max_quote_age_seconds=3.0,
        )
        if exit_decision is not None:
            exit_time = at
            break

    base = {
        "stock_id": signal.stock_id,
        "stock_name": signal.stock_name,
        "decision_time": signal.decision_time.isoformat(),
        "gate_reason": target["gate_reason"],
        "score": signal.score,
        "entry_price": signal.entry_price,
        "quantity": signal.quantity,
        "notional_twd": signal.entry_price * signal.quantity,
        "market_regime": signal.market_regime,
        "relative_strength_5m": signal.relative_strength_5m,
        "required_confirmations": signal.required_confirmations,
        "streak": target["streak"],
        "opening_extension": signal.directional_opening_extension,
        "vwap_extension": signal.directional_vwap_extension,
    }
    if exit_decision is None or exit_time is None:
        return {**base, "scorable": False, "reason": "NO_FRESH_EXIT_QUOTE"}
    gross, commission, tax, net = _projected_net_pnl(
        signal.side,
        signal.entry_price,
        exit_decision.price,
        signal.quantity,
    )
    return {
        **base,
        "scorable": True,
        "exit_time": exit_time.isoformat(),
        "exit_price": exit_decision.price,
        "exit_reason": exit_decision.reason,
        "gross_pnl_twd": gross,
        "commission_twd": commission,
        "tax_twd": tax,
        "net_pnl_twd": net,
        "mfe_net_pnl_twd": None if mfe_net == float("-inf") else round(mfe_net, 2),
        "mae_net_pnl_twd": None if mae_net == float("inf") else round(mae_net, 2),
        "holding_seconds": (exit_time - signal.decision_time).total_seconds(),
        "mfe_protection_armed": position.mfe_protection_armed,
        "maximum_locked_profit_r": position.locked_profit_r,
    }


def run_counterfactual(
    early_run: Path,
    late_run: Path,
    *,
    capital_twd: int = 190_000,
    cutover_text: str = "09:27:15.505",
) -> dict[str, Any]:
    early_run, late_run = early_run.resolve(), late_run.resolve()
    early_manifest, late_manifest = _manifest(early_run), _manifest(late_run)
    _validate_pair(early_manifest, late_manifest)
    early_candidates, early_coverage = load_session([early_run])
    late_candidates, late_coverage = load_session([late_run])
    early_market = _load_market_context(early_run, early_manifest)
    late_market = _load_market_context(late_run, late_manifest)
    session_date = datetime.fromisoformat(str(late_coverage["started_at_taipei"])).strftime("%Y%m%d")
    cutover = datetime.strptime(
        f"{session_date} {cutover_text}", "%Y%m%d %H:%M:%S.%f"
    ).replace(tzinfo=signal_timezone(early_candidates, late_candidates))
    candidates = stitch_streams(early_candidates, late_candidates, cutover)
    market = stitch_streams(early_market, late_market, cutover)
    targets = discover_near_misses(
        candidates, market, session_date, capital_twd=capital_twd
    )
    coverage_end = _parse_stamp(late_coverage["ended_at_taipei"])
    trades = [
        replay_independent_entry(
            candidates,
            market,
            coverage_end,
            target,
            capital_twd=capital_twd,
        )
        for target in targets
    ]
    scored = [row for row in trades if row["scorable"]]
    winners = [row for row in scored if float(row["net_pnl_twd"]) > 0]
    losers = [row for row in scored if float(row["net_pnl_twd"]) < 0]
    first = scored[0] if scored else None
    report = {
        "analysis_id": ANALYSIS_ID,
        "session_date": session_date,
        "capital_twd": capital_twd,
        "cutover_time": cutover.isoformat(),
        "exit_policy": LIVE_EXIT_POLICY,
        "method": (
            "Each rejected near-miss is independently forced through entry at the engine's "
            "suggested adverse-one-tick price; all current exit logic and costs remain enabled."
        ),
        "trades": trades,
        "summary": {
            "near_miss_count": len(trades),
            "scorable_count": len(scored),
            "winning_count": len(winners),
            "losing_count": len(losers),
            "flat_count": len(scored) - len(winners) - len(losers),
            "independent_arithmetic_net_pnl_twd": round(
                sum(float(row["net_pnl_twd"]) for row in scored), 2
            ),
            "average_net_pnl_twd": round(
                sum(float(row["net_pnl_twd"]) for row in scored) / len(scored), 2
            ) if scored else None,
            "first_chronological_only_net_pnl_twd": (
                first["net_pnl_twd"] if first else None
            ),
            "first_chronological_only_stock_id": first["stock_id"] if first else None,
        },
        "limitations": [
            "The nine entries are independent counterfactuals and cannot be summed as a feasible one-position trading day.",
            "The production strategy would accept at most one trade; first-chronological-only is the feasible mechanical override scenario.",
            "The early source has four aggregate callback errors without timestamps, so this remains diagnostic-only.",
            "No broker connection or order submission was performed.",
        ],
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    report["report_hash"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


def signal_timezone(
    early: dict[str, dict[str, Any]], late: dict[str, dict[str, Any]]
):
    for collection in (early, late):
        for data in collection.values():
            if data.get("ticks"):
                return data["ticks"][0]["time"].tzinfo
    raise RuntimeError("source has no timestamped ticks")


def markdown_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# 2026-09-30 near-miss entry counterfactual",
        "",
        "Each row is an independent forced entry. Production entry gates were not changed.",
        "",
        "| Time | Stock | Blocked by | Entry x qty | Exit | Reason | Net PnL | MFE | MAE |",
        "|---|---|---|---:|---:|---|---:|---:|---:|",
    ]
    for row in report["trades"]:
        if not row["scorable"]:
            lines.append(
                f"| {row['decision_time'][11:19]} | {row['stock_id']} {row['stock_name']} | "
                f"{row['gate_reason']} | {row['entry_price']:.2f} x {row['quantity']} | - | "
                f"{row['reason']} | - | - | - |"
            )
            continue
        lines.append(
            f"| {row['decision_time'][11:19]} | {row['stock_id']} {row['stock_name']} | "
            f"{row['gate_reason']} | {row['entry_price']:.2f} x {row['quantity']} | "
            f"{row['exit_price']:.2f} | {row['exit_reason']} | {row['net_pnl_twd']:.0f} | "
            f"{row['mfe_net_pnl_twd']:.0f} | {row['mae_net_pnl_twd']:.0f} |"
        )
    lines.extend([
        "",
        "## Summary",
        "",
        f"- Scorable: {summary['scorable_count']} / {summary['near_miss_count']}",
        f"- Winners / losers: {summary['winning_count']} / {summary['losing_count']}",
        f"- Independent arithmetic sum (not executable as one day): NT${summary['independent_arithmetic_net_pnl_twd']}",
        f"- Average per independent opportunity: NT${summary['average_net_pnl_twd']}",
        f"- First chronological opportunity only: {summary['first_chronological_only_stock_id']} / NT${summary['first_chronological_only_net_pnl_twd']}",
        "",
        "## Limitations",
        "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Counterfactual replay of rejected near-miss entries")
    parser.add_argument("--early-run", required=True, type=Path)
    parser.add_argument("--late-run", required=True, type=Path)
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    report = run_counterfactual(args.early_run, args.late_run, capital_twd=args.capital)
    rendered = json.dumps(_jsonable(report), ensure_ascii=False, indent=2) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(rendered, encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown_report(report), encoding="utf-8")
    if not args.json_output:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
