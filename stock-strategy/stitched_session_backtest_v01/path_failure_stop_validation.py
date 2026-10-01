"""Backtest-only, non-time-based early failure stop validation."""
from __future__ import annotations

import argparse
from bisect import bisect_right
import csv
from dataclasses import asdict, dataclass
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from mfe_profit_protection_study_v01.overlay import (
    MFEProtectionState,
    VARIANTS as MFE_VARIANTS,
)
from paper_shadow_v01.runner import _research_trade
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC
from yuanta_intraday_shadow_v01.flow_exit_sweep import build_flow_snapshots
from yuanta_intraday_shadow_v01.live_parity_backtest import _parse_stamp

from intraday_loss_reduction_study_v01.indicator_stop_analysis import (
    _adverse_components,
)
from .anti_chase_sensitivity_study import _load_direct, _load_stitched
from .entry_confirmation_sensitivity_study import _discover_events
from .exit_first_historical_validation import (
    _cohort_summary,
    _historical_cohort,
    _recent_cohort,
)
from .exit_first_profitability_study import (
    ONE_R_NET_TWD,
    ExitVariant,
    _result,
    simulate_exit,
)
from .near_miss_exit_validation import trade_fingerprint


ANALYSIS_ID = "PATH_FAILURE_STOP_VALIDATION_V0_1"
LOSS_THRESHOLDS_R = (0.20, 0.30, 0.40, 0.50)
MFE_PROGRESS_THRESHOLDS_R = (0.10, 0.20, 0.30)
CONFIRMATION_MODES = (
    "PATH_ONLY",
    "BREAKOUT",
    "VWAP_RS",
    "FLOW_2OF3",
    "ANY_1OF3",
    "TWO_OF3",
)
RS_REVERSAL_THRESHOLD = -0.003
TIMED_REFERENCE_NAME = "EARLY_FAILURE_120S_REFERENCE"


@dataclass(frozen=True, slots=True)
class PathFailureVariant:
    name: str
    loss_threshold_r: float
    maximum_mfe_r: float
    confirmation_mode: str


@dataclass(frozen=True, slots=True)
class CausalState:
    breakout_broken: bool
    vwap_broken: bool
    relative_strength_5m: float | None
    vwap_rs_failed: bool
    adverse_flow_components: int
    flow_2of3_failed: bool


def variants() -> tuple[PathFailureVariant, ...]:
    return tuple(
        PathFailureVariant(
            name=(
                f"PATH_LOSS_{int(loss * 100):02d}PCT_R_"
                f"MFE_{int(progress * 100):02d}PCT_R_{mode}"
            ),
            loss_threshold_r=loss,
            maximum_mfe_r=progress,
            confirmation_mode=mode,
        )
        for loss in LOSS_THRESHOLDS_R
        for progress in MFE_PROGRESS_THRESHOLDS_R
        for mode in CONFIRMATION_MODES
    )


def _vwap_at(
    data: dict[str, Any],
    at,
    cumulative_volume: list[float],
    cumulative_price_volume: list[float],
) -> tuple[float, float] | None:
    index = bisect_right(data["tick_times"], at)
    if index <= 0:
        return None
    volume = cumulative_volume[index - 1]
    if volume <= 0:
        return None
    return (
        float(data["ticks"][index - 1]["price"]),
        cumulative_price_volume[index - 1] / volume,
    )


def _indexed_return(data: dict[str, Any], at, seconds: int) -> float | None:
    current_index = bisect_right(data["tick_times"], at) - 1
    prior_index = bisect_right(
        data["tick_times"], at - timedelta(seconds=seconds),
    ) - 1
    if current_index < 0 or prior_index < 0:
        return None
    current = float(data["ticks"][current_index]["price"])
    prior = float(data["ticks"][prior_index]["price"])
    return current / prior - 1 if prior > 0 else None


def build_causal_states(
    trade,
    data: dict[str, Any],
    benchmark: dict[str, Any] | None,
    breakout_boundary_price: float,
) -> tuple[CausalState, ...]:
    snapshots = build_flow_snapshots(data, type("Path", (), {"decision_time": trade.entry_time})())
    cumulative_volume: list[float] = []
    cumulative_price_volume: list[float] = []
    running_volume = running_price_volume = 0.0
    for tick in data["ticks"]:
        volume = float(tick["volume"])
        running_volume += volume
        running_price_volume += float(tick["price"]) * volume
        cumulative_volume.append(running_volume)
        cumulative_price_volume.append(running_price_volume)
    snapshot_index = 0
    current = None
    output = []
    for point in trade.points:
        while snapshot_index < len(snapshots) and snapshots[snapshot_index].at <= point.at:
            current = snapshots[snapshot_index]
            snapshot_index += 1
        breakout_broken = (
            point.exit_price <= breakout_boundary_price
            if trade.side == "LONG" else point.exit_price >= breakout_boundary_price
        )
        vwap_pair = _vwap_at(
            data, point.at, cumulative_volume, cumulative_price_volume,
        )
        vwap_broken = False
        if vwap_pair is not None:
            last, vwap = vwap_pair
            vwap_broken = last < vwap if trade.side == "LONG" else last > vwap
        relative_strength = None
        if benchmark is not None:
            stock_return = _indexed_return(data, point.at, 300)
            benchmark_return = _indexed_return(benchmark, point.at, 300)
            if stock_return is not None and benchmark_return is not None:
                relative_strength = stock_return - benchmark_return
        fresh_flow = (
            current is not None
            and point.at - current.at <= timedelta(seconds=30)
        )
        adverse = _adverse_components(trade.side, current) if fresh_flow else 0
        output.append(CausalState(
            breakout_broken=breakout_broken,
            vwap_broken=vwap_broken,
            relative_strength_5m=relative_strength,
            vwap_rs_failed=(
                vwap_broken
                and relative_strength is not None
                and relative_strength <= RS_REVERSAL_THRESHOLD
            ),
            adverse_flow_components=adverse,
            flow_2of3_failed=adverse >= 2,
        ))
    return tuple(output)


def confirmation_passes(mode: str, state: CausalState) -> bool:
    flags = (
        state.breakout_broken,
        state.vwap_rs_failed,
        state.flow_2of3_failed,
    )
    if mode == "PATH_ONLY":
        return True
    if mode == "BREAKOUT":
        return flags[0]
    if mode == "VWAP_RS":
        return flags[1]
    if mode == "FLOW_2OF3":
        return flags[2]
    if mode == "ANY_1OF3":
        return any(flags)
    if mode == "TWO_OF3":
        return sum(flags) >= 2
    raise ValueError(f"unknown confirmation mode: {mode}")


def simulate_path_failure(
    trade,
    variant: PathFailureVariant,
    states: tuple[CausalState, ...],
) -> dict[str, Any]:
    if len(states) != len(trade.points):
        raise ValueError("causal state length must match trade path")
    mfe_net = trade.pnl_at_price(trade.entry_price)
    overlay = MFEProtectionState(trade.basis, MFE_VARIANTS["MFE_V1"])
    overlay.observe(
        price=trade.entry_price,
        at=trade.entry_time,
        projected_net_pnl=trade.pnl_at_price(trade.entry_price),
        pnl_at_price=trade.pnl_at_price,
    )
    hard_hour, hard_minute = map(int, str(SPEC["hard_exit_time"]).split(":"))
    hard_exit = trade.entry_time.replace(
        hour=hard_hour, minute=hard_minute, second=0, microsecond=0,
    )
    for index, (point, state) in enumerate(zip(trade.points, states)):
        mfe_net = max(mfe_net, float(point.projected_net_pnl))
        overlay.observe(
            price=point.exit_price,
            at=point.at,
            projected_net_pnl=point.projected_net_pnl,
            pnl_at_price=trade.pnl_at_price,
        )
        reason = None
        if point.projected_net_pnl <= -ONE_R_NET_TWD:
            reason = "DISASTER_STOP_NEG_1R"
        elif overlay.triggered(point.exit_price):
            reason = "MFE_PROFIT_PROTECTION"
        elif (
            point.projected_net_pnl
            <= -variant.loss_threshold_r * ONE_R_NET_TWD
            and mfe_net <= variant.maximum_mfe_r * ONE_R_NET_TWD
            and confirmation_passes(variant.confirmation_mode, state)
        ):
            reason = "PATH_FAILURE_STOP"
        elif point.reversal:
            reason = "SIGNAL_REVERSAL"
        elif point.at >= hard_exit:
            reason = "HARD_EXIT"
        elif trade.force_last_point_exit and index == len(trade.points) - 1:
            reason = "HARD_EXIT"
        if reason is not None:
            row = _result(
                trade,
                ExitVariant(variant.name),
                index,
                reason,
                locked_floor=None,
                mfe_net=mfe_net,
            )
            row.update({
                "loss_threshold_r": variant.loss_threshold_r,
                "maximum_mfe_r": variant.maximum_mfe_r,
                "confirmation_mode": variant.confirmation_mode,
                "breakout_broken_at_exit": state.breakout_broken,
                "vwap_broken_at_exit": state.vwap_broken,
                "relative_strength_5m_at_exit": state.relative_strength_5m,
                "vwap_rs_failed_at_exit": state.vwap_rs_failed,
                "adverse_flow_components_at_exit": state.adverse_flow_components,
                "flow_2of3_failed_at_exit": state.flow_2of3_failed,
            })
            return row
    raise RuntimeError(f"no exit for {trade.trade_id}/{variant.name}")


def _prepare_items(
    historical_runs: Mapping[str, list[Path]],
    run_20260929: Path,
    early_20260930: Path,
    late_20260930: Path,
    run_20261001: Path,
    capital_twd: int,
) -> list[dict[str, Any]]:
    historical, _diagnostics, _coverage = _historical_cohort(
        historical_runs, capital_twd,
    )
    recent = _recent_cohort(
        run_20260929, early_20260930, late_20260930, run_20261001,
        capital_twd,
    )
    for item in historical:
        item["source_signal_type"] = "HISTORICAL_BASE_SIGNAL"
        item["benchmark"] = None
    sessions = [
        _load_direct(run_20260929.resolve()),
        _load_stitched(early_20260930.resolve(), late_20260930.resolve()),
        _load_direct(run_20261001.resolve()),
    ]
    session_by_date = {session["session_date"]: session for session in sessions}
    for item in recent:
        item["source_signal_type"] = "RECENT_BASE_SIGNAL"
        item["benchmark"] = session_by_date[item["trade"].session_date]["market"].get("0050")

    items = historical + recent
    existing = {
        trade_fingerprint({
            "session_date": item["trade"].session_date,
            "symbol": item["trade"].symbol,
            "entry_time": item["trade"].entry_time.isoformat(),
            "entry_price": item["trade"].entry_price,
            "quantity": item["trade"].quantity,
        })
        for item in items
    }
    for session in sessions:
        targets, _direction_events = _discover_events(
            session["candidates"], session["market"], session["session_date"],
            capital_twd=capital_twd,
        )
        end_time = _parse_stamp(session["coverage"]["ended_at_taipei"])
        for target in targets:
            signal = target["signal"]
            trade = _research_trade(
                signal, session["candidates"][signal.stock_id], end_time,
            )
            fingerprint = trade_fingerprint({
                "session_date": trade.session_date,
                "symbol": trade.symbol,
                "entry_time": trade.entry_time.isoformat(),
                "entry_price": trade.entry_price,
                "quantity": trade.quantity,
            })
            if fingerprint in existing:
                continue
            existing.add(fingerprint)
            items.append({
                "cohort": "ADDITIONAL_UNIQUE_NEAR_MISS",
                "source_signal_type": "NEAR_MISS",
                "trade": trade,
                "data": session["candidates"][signal.stock_id],
                "benchmark": session["market"].get("0050"),
                "breakout_boundary_price": signal.breakout_boundary_price,
                "gate_reason": target["gate_reason"],
            })
    return items


def _summaries(rows: list[dict[str, Any]], ids: set[str]) -> list[dict[str, Any]]:
    selected = [row for row in rows if row["validation_trade_id"] in ids]
    baseline_rows = {
        row["validation_trade_id"]: row for row in selected
        if row["variant"] == "CURRENT_BASELINE"
    }
    names = ["CURRENT_BASELINE", TIMED_REFERENCE_NAME] + [
        variant.name for variant in variants()
    ]
    return [
        _cohort_summary(
            name,
            [row for row in selected if row["variant"] == name],
            baseline_rows,
        )
        for name in names
    ]


def build_report(
    historical_runs: Mapping[str, list[Path]],
    run_20260929: Path,
    early_20260930: Path,
    late_20260930: Path,
    run_20261001: Path,
    *,
    capital_twd: int = 190_000,
) -> dict[str, Any]:
    items = _prepare_items(
        historical_runs, run_20260929, early_20260930, late_20260930,
        run_20261001, capital_twd,
    )
    candidates = variants()
    rows: list[dict[str, Any]] = []
    ids_by_group: dict[str, set[str]] = {
        "base_signals": set(), "additional_near_misses": set(), "expanded": set(),
    }
    for index, item in enumerate(items):
        trade = item["trade"]
        validation_trade_id = f"{item['source_signal_type']}::{trade.trade_id}::{index}"
        ids_by_group["expanded"].add(validation_trade_id)
        group = (
            "additional_near_misses"
            if item["source_signal_type"] == "NEAR_MISS" else "base_signals"
        )
        ids_by_group[group].add(validation_trade_id)
        states = build_causal_states(
            trade, item["data"], item.get("benchmark"),
            item["breakout_boundary_price"],
        )
        baseline = simulate_exit(
            trade, item["data"], ExitVariant("CURRENT_BASELINE"),
            item["breakout_boundary_price"],
        )
        baseline.update({
            "validation_trade_id": validation_trade_id,
            "source_signal_type": item["source_signal_type"],
            "gate_reason": item.get("gate_reason"),
            "loss_threshold_r": None,
            "maximum_mfe_r": None,
            "confirmation_mode": None,
        })
        rows.append(baseline)
        timed_reference = simulate_exit(
            trade,
            item["data"],
            ExitVariant(
                TIMED_REFERENCE_NAME,
                early_failure_seconds=120,
                maximum_progress_r=0.10,
            ),
            item["breakout_boundary_price"],
        )
        timed_reference.update({
            "validation_trade_id": validation_trade_id,
            "source_signal_type": item["source_signal_type"],
            "gate_reason": item.get("gate_reason"),
            "loss_threshold_r": None,
            "maximum_mfe_r": 0.10,
            "confirmation_mode": "TIME_120S_REFERENCE",
        })
        rows.append(timed_reference)
        for variant in candidates:
            row = simulate_path_failure(trade, variant, states)
            row.update({
                "validation_trade_id": validation_trade_id,
                "source_signal_type": item["source_signal_type"],
                "gate_reason": item.get("gate_reason"),
            })
            rows.append(row)

    lenses = {
        group: _summaries(rows, ids) for group, ids in ids_by_group.items()
    }
    robust_expanded = [
        row for row in lenses["expanded"]
        if row["variant"].startswith("PATH_")
        and row["baseline_winners_harmed"] == 0
        and row["improvement_survives_every_leave_one_out"]
    ]
    robust_expanded.sort(
        key=lambda row: (
            -float(row["net_pnl_twd"]),
            float(row["maximum_drawdown_twd"]),
        )
    )
    report = {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "BACKTEST_ONLY_NON_TIME_PATH_FAILURE_STOP_GRID",
        "capital_twd": capital_twd,
        "one_r_net_twd": ONE_R_NET_TWD,
        "grid": {
            "loss_thresholds_r": LOSS_THRESHOLDS_R,
            "maximum_mfe_thresholds_r": MFE_PROGRESS_THRESHOLDS_R,
            "confirmation_modes": CONFIRMATION_MODES,
            "relative_strength_failure_threshold": RS_REVERSAL_THRESHOLD,
            "variant_count": len(candidates),
        },
        "rule": (
            "current net PnL <= -loss_threshold_R and maximum net MFE so far "
            "<= maximum_MFE_R and causal confirmation mode passes"
        ),
        "confirmation_definitions": {
            "BREAKOUT": "safe exit price is at/below causal breakout boundary for LONG",
            "VWAP_RS": "last trade below causal VWAP and 5m stock-minus-0050 return <= -0.3%",
            "FLOW_2OF3": "at least two of signed volume, large-trade flow, five-level imbalance are adverse",
            "ANY_1OF3": "at least one of BREAKOUT, VWAP_RS, FLOW_2OF3",
            "TWO_OF3": "at least two of BREAKOUT, VWAP_RS, FLOW_2OF3",
        },
        "counts": {group: len(ids) for group, ids in ids_by_group.items()},
        "lenses": lenses,
        "top_robust_expanded": robust_expanded[:20],
        "per_trade": rows,
        "limitations": [
            "The grid is a diagnostic comparison, not fitted production parameters.",
            "Older 20260922-20260924 signals have no synchronized 0050; VWAP_RS cannot trigger for them.",
            "Additional near-miss entries are counterfactual and overlap in clock time.",
            "The 20260930 session is stitched and has four untimestamped callback errors.",
            "Twenty-eight unique signals remain too small for production selection.",
        ],
        "production_behavior_changed": False,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    report["report_hash"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Non-time path-failure stop validation", "",
        "No minimum holding time is used. Existing entries, -1R disaster stop, MFE_V1, costs and slippage remain unchanged.", "",
        "## Cohorts", "",
        f"- Base signals: {report['counts']['base_signals']}",
        f"- Additional unique near-misses: {report['counts']['additional_near_misses']}",
        f"- Expanded unique signals: {report['counts']['expanded']}", "",
        "## Expanded cohort: top robust variants", "",
        "A robust row improves every leave-one-trade-out sample and harms no baseline winner.", "",
        "| Rank | Variant | Net PnL | vs baseline | W/L | PF | Avg loser | Max DD | Stops |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(report["top_robust_expanded"][:15], start=1):
        lines.append(
            f"| {rank} | {row['variant']} | {row['net_pnl_twd']:.0f} | "
            f"{row['net_difference_vs_baseline_twd']:+.0f} | {row['wins']}/{row['losses']} | "
            f"{row['profit_factor']} | {row['average_loser_twd']:.0f} | "
            f"{row['maximum_drawdown_twd']:.0f} | "
            f"{row['exit_reason_counts'].get('PATH_FAILURE_STOP', 0)} |"
        )
    lines.extend(["", "## Best result by cohort", ""])
    for lens, title in (
        ("base_signals", "Base signals"),
        ("additional_near_misses", "Additional near-misses"),
        ("expanded", "Expanded unique signals"),
    ):
        baseline = report["lenses"][lens][0]
        timed = next(
            row for row in report["lenses"][lens]
            if row["variant"] == TIMED_REFERENCE_NAME
        )
        eligible = [
            row for row in report["lenses"][lens]
            if row["variant"].startswith("PATH_")
            if row["baseline_winners_harmed"] == 0
        ]
        best = max(eligible, key=lambda row: float(row["net_pnl_twd"]))
        lines.extend([
            f"### {title}", "",
            f"- Baseline: {baseline['net_pnl_twd']:.0f} TWD, PF {baseline['profit_factor']}, max DD {baseline['maximum_drawdown_twd']:.0f}.",
            f"- Existing 120-second reference: {timed['net_pnl_twd']:.0f} TWD, PF {timed['profit_factor']}, max DD {timed['maximum_drawdown_twd']:.0f}.",
            f"- Best no-winner-harm variant: {best['variant']}.",
            f"- Result: {best['net_pnl_twd']:.0f} TWD ({best['net_difference_vs_baseline_twd']:+.0f}), PF {best['profit_factor']}, max DD {best['maximum_drawdown_twd']:.0f}.", "",
        ])
    best_name = report["top_robust_expanded"][0]["variant"]
    baseline_by_id = {
        row["validation_trade_id"]: row for row in report["per_trade"]
        if row["variant"] == "CURRENT_BASELINE"
    }
    worsened = []
    for row in report["per_trade"]:
        if row["variant"] != best_name:
            continue
        baseline = baseline_by_id[row["validation_trade_id"]]
        difference = float(row["net_pnl_twd"]) - float(baseline["net_pnl_twd"])
        if difference < 0:
            worsened.append((row, baseline, difference))
    lines.extend([
        "## False-exit review for the best non-time variant", "",
        f"The best variant worsened {len(worsened)} trades that later recovered under the baseline path:", "",
    ])
    for row, baseline, difference in worsened:
        lines.append(
            f"- {row['session_date']} {row['symbol']}: baseline "
            f"{baseline['net_pnl_twd']:.0f} TWD versus path stop "
            f"{row['net_pnl_twd']:.0f} TWD ({difference:+.0f})."
        )
    lines.append("")
    lines.extend([
        "## Interpretation", "",
        "- Prefer a configuration only if it preserves baseline winners, improves multiple losses, and survives leave-one-trade-out deletion.",
        "- The 120-second row is a comparison reference only; it is not part of the non-time parameter grid.",
        "- A less-negative result is loss reduction, not proof of positive expectancy.",
        "- No configuration is enabled in production or live trading.", "",
        "## Limitations", "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "path_failure_stop_validation_20260922_20261001.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (output_dir / "report_path_failure_stop_validation.md").write_text(
        markdown_report(report), encoding="utf-8",
    )
    fields = [
        "source_signal_type", "gate_reason", "validation_trade_id", "variant",
        "session_date", "symbol", "stock_name", "entry_time", "entry_price",
        "quantity", "exit_time", "exit_price", "exit_reason", "net_pnl_twd",
        "holding_seconds", "loss_threshold_r", "maximum_mfe_r",
        "confirmation_mode", "mfe_net_pnl_at_exit_twd",
        "full_path_mfe_net_pnl_twd", "full_path_mae_net_pnl_twd",
        "breakout_broken_at_exit", "vwap_broken_at_exit",
        "relative_strength_5m_at_exit", "vwap_rs_failed_at_exit",
        "adverse_flow_components_at_exit", "flow_2of3_failed_at_exit",
    ]
    with (output_dir / "path_failure_stop_validation_per_trade.csv").open(
        "w", encoding="utf-8", newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(
            {field: row.get(field) for field in fields}
            for row in report["per_trade"]
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-20260922", action="append", required=True, type=Path)
    parser.add_argument("--run-20260923", action="append", required=True, type=Path)
    parser.add_argument("--run-20260924", action="append", required=True, type=Path)
    parser.add_argument("--run-20260929", required=True, type=Path)
    parser.add_argument("--early-20260930", required=True, type=Path)
    parser.add_argument("--late-20260930", required=True, type=Path)
    parser.add_argument("--run-20261001", required=True, type=Path)
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    report = build_report(
        {
            "20260922": args.run_20260922,
            "20260923": args.run_20260923,
            "20260924": args.run_20260924,
        },
        args.run_20260929, args.early_20260930, args.late_20260930,
        args.run_20261001, capital_twd=args.capital,
    )
    write_report(report, args.output_dir)
    print(json.dumps(report["top_robust_expanded"][:20], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
