"""Causal pullback-entry comparison over the frozen independent-signal cohort."""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any, Mapping

from mfe_profit_protection_study_v01.analysis import (
    ResearchTrade,
    SimulatedExit,
    build_independent_signal_trades,
    derive_initial_stop_price,
)
from mfe_profit_protection_study_v01.overlay import (
    EntryFill,
    MFEProtectionState,
    PositionBasis,
    VARIANTS as MFE_VARIANTS,
)
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC,
    _decision_times,
    _entry_tick,
    _exit_quote,
    _projected_net_pnl,
    _reversal_exit_tick,
    _tick_size,
    load_session,
    signal_at,
)
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint


ANALYSIS_ID = "PULLBACK_ENTRY_STUDY_V0_1"
STOP_LOSS_TWD = 3500.0
MFE_VARIANT = "MFE_V1"
REBOUND_R = 0.25
BREAKOUT_LOOKBACK_SECONDS = 30
MAXIMUM_WAIT_SECONDS = 600


@dataclass(frozen=True, slots=True)
class EntryVariant:
    name: str
    pullback_r: float | None
    stability_seconds: int = 0


VARIANTS = (
    EntryVariant("IMMEDIATE", None),
    EntryVariant("PULLBACK_0_5R_60S", 0.5, 60),
    EntryVariant("PULLBACK_1_0R_90S", 1.0, 90),
)


def _entry_price(side: str, row: Mapping[str, Any]) -> float:
    if side == "LONG":
        return float(row["ask"]) + _tick_size(float(row["ask"]))
    return max(
        _tick_size(float(row["bid"])),
        float(row["bid"]) - _tick_size(float(row["bid"])),
    )


def _quote_is_valid(row: Mapping[str, Any]) -> bool:
    bid, ask = float(row["bid"]), float(row["ask"])
    if bid <= 0 or ask <= 0 or ask < bid:
        return False
    midpoint = (bid + ask) / 2
    return midpoint > 0 and (ask - bid) / midpoint * 10_000 <= float(SPEC["maximum_spread_bps"])


def find_pullback_entry(
    ticks: list[dict[str, Any]],
    *,
    side: str,
    original_entry_time: datetime,
    reference_entry_price: float,
    reference_r_per_share: float,
    variant: EntryVariant,
    latest_entry_time: datetime,
) -> dict[str, Any] | None:
    """Return the first causal recovery tick after a qualified adverse pullback.

    The relative extreme is known only after it has remained intact for the
    configured interval.  A later tick must recover 0.25R and break all prior
    trade prices from the preceding 30 seconds; the current tick is excluded
    from that comparison.
    """
    if variant.pullback_r is None:
        raise ValueError("IMMEDIATE does not use pullback entry discovery")
    if side not in {"LONG", "SHORT"} or reference_r_per_share <= 0:
        raise ValueError("side and reference R must be valid")
    deadline = min(
        original_entry_time + timedelta(seconds=MAXIMUM_WAIT_SECONDS),
        latest_entry_time,
    )
    rows = [
        row for row in ticks
        if original_entry_time < row["time"] <= deadline
    ]
    extreme: float | None = None
    extreme_time: datetime | None = None
    threshold = float(variant.pullback_r) * reference_r_per_share
    rebound = REBOUND_R * reference_r_per_share

    for index, row in enumerate(rows):
        price = float(row["price"])
        adverse_move = (
            reference_entry_price - price
            if side == "LONG"
            else price - reference_entry_price
        )
        if extreme is None:
            if adverse_move + 1e-12 < threshold:
                continue
            extreme, extreme_time = price, row["time"]
            continue

        new_extreme = price < extreme if side == "LONG" else price > extreme
        if new_extreme:
            extreme, extreme_time = price, row["time"]
            continue
        assert extreme_time is not None
        if (row["time"] - extreme_time).total_seconds() < variant.stability_seconds:
            continue
        recovered = price - extreme if side == "LONG" else extreme - price
        if recovered + 1e-12 < rebound:
            continue
        window_start = row["time"] - timedelta(seconds=BREAKOUT_LOOKBACK_SECONDS)
        prior = [
            float(item["price"])
            for item in rows[:index]
            if window_start <= item["time"] < row["time"]
        ]
        if not prior:
            continue
        broke_rebound = price > max(prior) if side == "LONG" else price < min(prior)
        if broke_rebound and _quote_is_valid(row):
            return row
    return None


def _trade_from_entry(
    *,
    session_date: str,
    data: dict[str, Any],
    signal: Any,
    entry_row: dict[str, Any],
    entry_price: float,
    quantity: int,
    trade_id: str,
) -> ResearchTrade | None:
    hour, minute = map(int, str(SPEC["hard_exit_time"]).split(":"))
    hard_exit = entry_row["time"].replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    future = [
        row for row in data["ticks"]
        if entry_row["time"] < row["time"] <= hard_exit
    ]
    if not future:
        return None
    reversal_row = _reversal_exit_tick(data, signal, entry_row["time"], hard_exit)
    notional = entry_price * quantity
    points = []
    for row in future:
        exit_price = _exit_quote(signal.side, row)
        pnl = float(_projected_net_pnl(signal.side, entry_price, exit_price, quantity)[3])
        points.append(PathPoint(
            at=row["time"],
            exit_price=exit_price,
            projected_net_pnl=pnl,
            current_return=pnl / notional if notional else 0.0,
            reversal=reversal_row is not None and row["time"] >= reversal_row["time"],
        ))
    staleness = (hard_exit - future[-1]["time"]).total_seconds()
    stop = derive_initial_stop_price(
        signal.side, entry_price, quantity, STOP_LOSS_TWD,
    )
    return ResearchTrade(
        trade_id=trade_id,
        session_date=session_date,
        symbol=signal.stock_id,
        stock_name=signal.stock_name,
        side=signal.side,
        entry_time=entry_row["time"],
        entry_price=entry_price,
        quantity=quantity,
        initial_stop_price=stop,
        points=tuple(points),
        force_last_point_exit=(
            staleness <= float(SPEC["maximum_hard_exit_quote_staleness_seconds"])
        ),
    )


def simulate_current_exit(trade: ResearchTrade) -> SimulatedExit | None:
    """Mirror the current live exit ordering without loss-recovery exit."""
    stop = derive_initial_stop_price(
        trade.side, trade.entry_price, trade.quantity, STOP_LOSS_TWD,
    )
    basis = PositionBasis.from_fills(
        trade.side,
        [EntryFill(trade.entry_time, trade.entry_price, trade.quantity)],
        initial_stop_price=stop,
    )
    overlay = MFEProtectionState(basis, MFE_VARIANTS[MFE_VARIANT])
    overlay.observe(
        price=trade.entry_price,
        at=trade.entry_time,
        projected_net_pnl=trade.pnl_at_price(trade.entry_price),
        pnl_at_price=trade.pnl_at_price,
    )
    hour, minute = map(int, str(SPEC["hard_exit_time"]).split(":"))
    hard_exit = trade.entry_time.replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    for index, point in enumerate(trade.points):
        overlay.observe(
            price=point.exit_price,
            at=point.at,
            projected_net_pnl=point.projected_net_pnl,
            pnl_at_price=trade.pnl_at_price,
        )
        reason = None
        if point.projected_net_pnl <= -STOP_LOSS_TWD:
            reason = "STOP_LOSS"
        elif overlay.triggered(point.exit_price):
            reason = "MFE_PROFIT_PROTECTION"
        elif point.reversal:
            reason = "SIGNAL_REVERSAL"
        elif point.at >= hard_exit:
            reason = "HARD_EXIT"
        elif trade.force_last_point_exit and index == len(trade.points) - 1:
            reason = "HARD_EXIT"
        if reason is not None:
            return SimulatedExit(
                index=index,
                at=point.at,
                price=point.exit_price,
                reason=reason,
                realized_pnl=point.projected_net_pnl,
                state=overlay,
            )
    return None


def _decision_from_trade_id(trade: ResearchTrade) -> datetime:
    clock = trade.trade_id.rsplit("-", 1)[-1]
    return trade.entry_time.replace(
        hour=int(clock[:2]), minute=int(clock[2:4]), second=int(clock[4:6]), microsecond=0,
    )


def _outcome(
    variant: EntryVariant,
    baseline: ResearchTrade,
    candidate: ResearchTrade | None,
    *,
    no_entry: bool = False,
) -> dict[str, Any]:
    common = {
        "variant": variant.name,
        "trade_id": baseline.trade_id,
        "session_date": baseline.session_date,
        "symbol": baseline.symbol,
        "stock_name": baseline.stock_name,
        "side": baseline.side,
        "signal_time": _decision_from_trade_id(baseline).isoformat(),
        "reference_entry_time": baseline.entry_time.isoformat(),
        "reference_entry_price": baseline.entry_price,
    }
    if no_entry or candidate is None:
        return {**common, "status": "NO_ENTRY" if no_entry else "UNSCORABLE"}
    result = simulate_current_exit(candidate)
    if result is None:
        return {
            **common,
            "status": "UNSCORABLE",
            "entry_time": candidate.entry_time.isoformat(),
            "entry_price": candidate.entry_price,
            "quantity": candidate.quantity,
        }
    price_improvement = (
        baseline.entry_price - candidate.entry_price
        if baseline.side == "LONG"
        else candidate.entry_price - baseline.entry_price
    )
    return {
        **common,
        "status": "SCORED",
        "entry_time": candidate.entry_time.isoformat(),
        "entry_price": candidate.entry_price,
        "quantity": candidate.quantity,
        "entry_delay_seconds": (candidate.entry_time - baseline.entry_time).total_seconds(),
        "price_improvement_per_share": price_improvement,
        "exit_time": result.at.isoformat(),
        "exit_price": result.price,
        "exit_reason": result.reason,
        "net_pnl": result.realized_pnl,
        "holding_seconds": (result.at - candidate.entry_time).total_seconds(),
        "mfe_armed": result.state.armed,
        "mfe_r": result.state.mfe_r,
    }


def _max_drawdown(rows: list[dict[str, Any]]) -> float:
    equity = peak = drawdown = 0.0
    for row in sorted(rows, key=lambda item: item["signal_time"]):
        equity += float(row["net_pnl"])
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _summary(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [row for row in rows if row["status"] == "SCORED"]
    pnls = [float(row["net_pnl"]) for row in scored]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    entries = [row for row in rows if row["status"] != "NO_ENTRY"]
    delayed = [row for row in scored if float(row.get("entry_delay_seconds", 0)) > 0]
    return {
        "variant": name,
        "total_signals": len(rows),
        "entries": len(entries),
        "no_entry": sum(row["status"] == "NO_ENTRY" for row in rows),
        "unscorable": sum(row["status"] == "UNSCORABLE" for row in rows),
        "scored_trades": len(scored),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(scored) if scored else None,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_pnl": sum(pnls),
        "expectancy_per_scored_trade": mean(pnls) if pnls else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "average_winner": mean(wins) if wins else None,
        "average_loser": mean(losses) if losses else None,
        "maximum_drawdown": _max_drawdown(scored),
        "average_entry_delay_seconds": mean(float(row["entry_delay_seconds"]) for row in delayed) if delayed else 0.0,
        "median_entry_delay_seconds": median(float(row["entry_delay_seconds"]) for row in delayed) if delayed else 0.0,
        "average_price_improvement_per_share": mean(float(row["price_improvement_per_share"]) for row in delayed) if delayed else 0.0,
        "stop_loss_exits": sum(row.get("exit_reason") == "STOP_LOSS" for row in scored),
        "mfe_exits": sum(row.get("exit_reason") == "MFE_PROFIT_PROTECTION" for row in scored),
        "hard_exits": sum(row.get("exit_reason") == "HARD_EXIT" for row in scored),
    }


def build_report(
    session_runs: Mapping[str, list[Path]],
    capital: int = 190_000,
) -> dict[str, Any]:
    baseline_trades, diagnostics, coverages = build_independent_signal_trades(
        session_runs, capital,
    )
    sessions = {
        date: load_session(paths)[0]
        for date, paths in sorted(session_runs.items())
    }
    outcomes: list[dict[str, Any]] = []
    last_entry_clock = time.fromisoformat(str(SPEC["last_entry_time"]))
    for baseline in baseline_trades:
        data = sessions[baseline.session_date][baseline.symbol]
        decision = _decision_from_trade_id(baseline)
        signal = signal_at(baseline.symbol, data, decision)
        if signal is None:
            raise RuntimeError(f"frozen signal cannot be reconstructed: {baseline.trade_id}")
        outcomes.append(_outcome(VARIANTS[0], baseline, baseline))
        reference_stop = derive_initial_stop_price(
            baseline.side, baseline.entry_price, baseline.quantity, STOP_LOSS_TWD,
        )
        reference_r = abs(baseline.entry_price - reference_stop)
        latest_entry = baseline.entry_time.replace(
            hour=last_entry_clock.hour,
            minute=last_entry_clock.minute,
            second=0,
            microsecond=0,
        )
        for variant in VARIANTS[1:]:
            row = find_pullback_entry(
                data["ticks"],
                side=baseline.side,
                original_entry_time=baseline.entry_time,
                reference_entry_price=baseline.entry_price,
                reference_r_per_share=reference_r,
                variant=variant,
                latest_entry_time=latest_entry,
            )
            if row is None:
                outcomes.append(_outcome(variant, baseline, None, no_entry=True))
                continue
            price = _entry_price(baseline.side, row)
            lots = math.floor(capital / (price * 1000))
            if lots <= 0:
                outcomes.append(_outcome(variant, baseline, None, no_entry=True))
                continue
            candidate = _trade_from_entry(
                session_date=baseline.session_date,
                data=data,
                signal=signal,
                entry_row=row,
                entry_price=price,
                quantity=lots * 1000,
                trade_id=baseline.trade_id,
            )
            outcomes.append(_outcome(variant, baseline, candidate))

    summaries = [
        _summary(variant.name, [row for row in outcomes if row["variant"] == variant.name])
        for variant in VARIANTS
    ]
    by_id: dict[str, list[dict[str, Any]]] = {}
    for row in outcomes:
        by_id.setdefault(row["trade_id"], []).append(row)
    matched_ids = {
        trade_id for trade_id, rows in by_id.items()
        if len(rows) == len(VARIANTS) and all(row["status"] == "SCORED" for row in rows)
    }
    matched = [
        _summary(
            variant.name,
            [
                row for row in outcomes
                if row["variant"] == variant.name and row["trade_id"] in matched_ids
            ],
        )
        for variant in VARIANTS
    ]
    immediate_by_id = {
        row["trade_id"]: row
        for row in outcomes
        if row["variant"] == "IMMEDIATE"
    }
    pairwise = []
    for variant in VARIANTS[1:]:
        variant_rows = [row for row in outcomes if row["variant"] == variant.name]
        paired = [
            row for row in variant_rows
            if row["status"] == "SCORED"
            and immediate_by_id[row["trade_id"]]["status"] == "SCORED"
        ]
        missed = [
            immediate_by_id[row["trade_id"]]
            for row in variant_rows
            if row["status"] == "NO_ENTRY"
            and immediate_by_id[row["trade_id"]]["status"] == "SCORED"
        ]
        immediate_net = sum(float(immediate_by_id[row["trade_id"]]["net_pnl"]) for row in paired)
        variant_net = sum(float(row["net_pnl"]) for row in paired)
        pairwise.append({
            "variant": variant.name,
            "paired_trades": len(paired),
            "immediate_net_on_paired": immediate_net,
            "variant_net_on_paired": variant_net,
            "paired_net_delta": variant_net - immediate_net,
            "paired_improved": sum(
                float(row["net_pnl"]) > float(immediate_by_id[row["trade_id"]]["net_pnl"])
                for row in paired
            ),
            "paired_worse": sum(
                float(row["net_pnl"]) < float(immediate_by_id[row["trade_id"]]["net_pnl"])
                for row in paired
            ),
            "no_entry_with_scored_immediate": len(missed),
            "missed_immediate_net": sum(float(row["net_pnl"]) for row in missed),
            "missed_immediate_winners": sum(float(row["net_pnl"]) > 0 for row in missed),
            "missed_immediate_losses": sum(float(row["net_pnl"]) < 0 for row in missed),
        })
    side_summaries = {
        variant.name: {
            side: _summary(
                variant.name,
                [
                    row for row in outcomes
                    if row["variant"] == variant.name and row["side"] == side
                ],
            )
            for side in ("LONG", "SHORT")
        }
        for variant in VARIANTS
    }
    return {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "OVERLAPPING_INDEPENDENT_SIGNALS; CAUSAL ENTRY CONFIRMATION; NOT AN EXECUTABLE PORTFOLIO",
        "capital_twd": capital,
        "exit_policy": "HARD_3500_PLUS_MFE_V1_NO_LOSS_RECOVERY",
        "variants": [asdict(variant) for variant in VARIANTS],
        "shared_parameters": {
            "rebound_r": REBOUND_R,
            "breakout_lookback_seconds": BREAKOUT_LOOKBACK_SECONDS,
            "maximum_wait_seconds": MAXIMUM_WAIT_SECONDS,
        },
        "coverage": coverages,
        "source_diagnostics": diagnostics,
        "summaries": summaries,
        "side_summaries": side_summaries,
        "matched_trade_ids": sorted(matched_ids),
        "matched_summaries": matched,
        "pairwise_vs_immediate": pairwise,
        "per_trade": outcomes,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "live_behavior_changed": False,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    comparison_fields = [
        "variant", "total_signals", "entries", "no_entry", "unscorable",
        "scored_trades", "wins", "losses", "win_rate", "gross_profit",
        "gross_loss", "net_pnl", "expectancy_per_scored_trade",
        "profit_factor", "average_winner", "average_loser",
        "maximum_drawdown", "average_entry_delay_seconds",
        "median_entry_delay_seconds", "average_price_improvement_per_share",
        "stop_loss_exits", "mfe_exits", "hard_exits",
    ]
    _write_csv(output_dir / "comparison.csv", report["summaries"], comparison_fields)
    _write_csv(output_dir / "matched_comparison.csv", report["matched_summaries"], comparison_fields)
    per_trade_fields = [
        "variant", "trade_id", "session_date", "symbol", "stock_name", "side",
        "status", "signal_time", "reference_entry_time", "reference_entry_price",
        "entry_time", "entry_price", "quantity", "entry_delay_seconds",
        "price_improvement_per_share", "exit_time", "exit_price", "exit_reason",
        "net_pnl", "holding_seconds", "mfe_armed", "mfe_r",
    ]
    _write_csv(output_dir / "per_trade.csv", report["per_trade"], per_trade_fields)
    fmt = lambda value: "N/A" if value is None else f"{float(value):.2f}"
    lines = [
        "# Causal pullback-entry diagnostic", "",
        "Same frozen signals, capital rule, fees, tax, slippage and current exit policy.",
        "A relative extreme is confirmed only by later data; no future low/high is used.", "",
        "| Variant | Signals | Entries | No entry | Unscorable | Net | PF | Win rate | Max DD | Stops |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["summaries"]:
        lines.append(
            f"| {row['variant']} | {row['total_signals']} | {row['entries']} | "
            f"{row['no_entry']} | {row['unscorable']} | {fmt(row['net_pnl'])} | "
            f"{fmt(row['profit_factor'])} | {fmt(row['win_rate'])} | "
            f"{fmt(row['maximum_drawdown'])} | {row['stop_loss_exits']} |"
        )
    lines.extend([
        "", "## Matched cohort", "",
        f"Only the same {len(report['matched_trade_ids'])} fully scored signals are compared below.", "",
        "| Variant | Net | PF | Win rate | Max DD |",
        "|---|---:|---:|---:|---:|",
    ])
    for row in report["matched_summaries"]:
        lines.append(
            f"| {row['variant']} | {fmt(row['net_pnl'])} | {fmt(row['profit_factor'])} | "
            f"{fmt(row['win_rate'])} | {fmt(row['maximum_drawdown'])} |"
        )
    lines.extend([
        "", "## Pairwise effect versus immediate entry", "",
        "| Variant | Paired | Immediate net | Pullback net | Delta | Missed immediate net |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for row in report["pairwise_vs_immediate"]:
        lines.append(
            f"| {row['variant']} | {row['paired_trades']} | "
            f"{fmt(row['immediate_net_on_paired'])} | {fmt(row['variant_net_on_paired'])} | "
            f"{fmt(row['paired_net_delta'])} | {fmt(row['missed_immediate_net'])} |"
        )
    lines.extend([
        "", "## Conclusion", "",
        "Neither pullback rule is supported for promotion. The 0.5R rule slightly improved entry timing on its seven paired trades, but those trades were predominantly weak and five still hit the fixed stop. It also skipped the immediate-entry cohort that contained most of the available profits. The 1R rule produced only one entry, which is not an evaluable sample.",
        "", "Three quality-limited sessions; overlapping independent signals are not one executable portfolio.",
        "Research only: no broker connection, order, fill, or live behavior change.",
    ])
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    artifacts = {
        name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
        for name in (
            "summary.json", "comparison.csv", "matched_comparison.csv",
            "per_trade.csv", "report.md",
        )
    }
    manifest = {
        "analysis_id": ANALYSIS_ID,
        "artifacts": artifacts,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "live_behavior_changed": False,
    }
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
