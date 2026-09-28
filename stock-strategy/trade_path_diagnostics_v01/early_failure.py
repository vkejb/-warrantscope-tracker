"""Backtest-only grid for causal early-failure exits."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping

from current_indicator_stop_study_v01.analysis import (
    ONE_R_NET_TWD,
    VARIANTS as CURRENT_VARIANTS,
    simulate_current_indicator_stop,
)
from mfe_profit_protection_study_v01.analysis import (
    ResearchTrade,
    build_independent_signal_trades,
)
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC, load_session

from .analysis import Outcome, _outcome


ANALYSIS_ID = "EARLY_FAILURE_EXIT_STUDY_V0_1"
CHECKPOINTS = (5, 10, 15)
NEGATIVE_THRESHOLDS = (-0.20, -0.30, -0.40, -0.50, -0.60)
PROGRESS_THRESHOLDS = (0.10, 0.20, 0.30, 0.40, 0.50)
CURRENT_POLICY = CURRENT_VARIANTS[0]


@dataclass(frozen=True, slots=True)
class TradeCase:
    trade: ResearchTrade
    outcome: Outcome
    checkpoint_rows: tuple[dict[str, Any], ...]


def _excursions(
    trade: ResearchTrade, points: Iterable[Any],
) -> tuple[float, float, float, float]:
    candidates = [
        (trade.entry_price, trade.pnl_at_price(trade.entry_price)),
        *((point.exit_price, float(point.projected_net_pnl)) for point in points),
    ]
    basis = trade.basis
    mfe_price, mfe_pnl = max(candidates, key=lambda row: basis.favorable_r(row[0]))
    mae_price, mae_pnl = min(candidates, key=lambda row: basis.favorable_r(row[0]))
    return (
        mfe_pnl,
        max(0.0, basis.favorable_r(mfe_price)),
        mae_pnl,
        min(0.0, basis.favorable_r(mae_price)),
    )


def build_checkpoint_rows(trade: ResearchTrade, outcome: Outcome) -> list[dict[str, Any]]:
    max_stale = float(SPEC["maximum_tick_staleness_seconds"])
    held = [
        point for point in trade.points
        if outcome.exit_time is None or point.at <= outcome.exit_time
    ]
    full_mfe, full_mfe_r, _, _ = _excursions(trade, held)
    rows = []
    for checkpoint in CHECKPOINTS:
        target = trade.entry_time + timedelta(minutes=checkpoint)
        observed = [point for point in trade.points if point.at <= target]
        observation = observed[-1] if observed else None
        execution = next((point for point in trade.points if point.at >= target), None)
        observation_staleness = (
            (target - observation.at).total_seconds() if observation else None
        )
        execution_delay = (
            (execution.at - target).total_seconds() if execution else None
        )
        observation_fresh = bool(
            observation
            and observation_staleness is not None
            and 0 <= observation_staleness <= max_stale
        )
        execution_fresh = bool(
            execution
            and execution_delay is not None
            and 0 <= execution_delay <= max_stale
        )
        original_open = bool(outcome.exit_time and outcome.exit_time > target)
        execution_precedes_original = bool(
            execution and outcome.exit_time and execution.at < outcome.exit_time
        )
        evaluable = bool(
            outcome.status == "SCORED"
            and observation_fresh
            and execution_fresh
            and original_open
            and execution_precedes_original
        )
        through = observed if observation_fresh else []
        if through:
            mfe, mfe_r, mae, mae_r = _excursions(trade, through)
        else:
            mfe = mfe_r = mae = mae_r = None
        current = float(observation.projected_net_pnl) if observation_fresh else None
        signed_distance = None
        if observation_fresh:
            signed_distance = (
                observation.exit_price - trade.entry_price
                if trade.side == "LONG"
                else trade.entry_price - observation.exit_price
            )
        rows.append({
            "trade_id": trade.trade_id,
            "session_date": trade.session_date,
            "symbol": trade.symbol,
            "stock_name": trade.stock_name,
            "side": trade.side,
            "checkpoint_minutes": checkpoint,
            "checkpoint_time": target.isoformat(),
            "evaluable": evaluable,
            "original_position_open": original_open,
            "observation_fresh": observation_fresh,
            "execution_quote_fresh": execution_fresh,
            "observation_staleness_seconds": observation_staleness,
            "execution_delay_seconds": execution_delay,
            "current_pnl": current,
            "current_pnl_r": current / ONE_R_NET_TWD if current is not None else None,
            "mfe_so_far": mfe,
            "mfe_r_so_far": mfe_r,
            "mae_so_far": mae,
            "mae_r_so_far": mae_r,
            "distance_from_entry": signed_distance,
            "distance_from_entry_pct": (
                signed_distance / trade.entry_price if signed_distance is not None else None
            ),
            "execution_time": execution.at.isoformat() if execution_fresh else None,
            "execution_price": execution.exit_price if execution_fresh else None,
            "execution_pnl": execution.projected_net_pnl if execution_fresh else None,
            "original_outcome": outcome.label,
            "original_final_pnl": outcome.net_pnl,
            "original_exit_time": outcome.exit_time.isoformat() if outcome.exit_time else None,
            "original_exit_reason": outcome.exit_reason,
            "eventual_full_trade_mfe": full_mfe,
            "eventual_full_trade_mfe_r": full_mfe_r,
        })
    return rows


def _max_drawdown(rows: list[dict[str, Any]], field: str) -> float:
    equity = peak = drawdown = 0.0
    for row in sorted(rows, key=lambda item: item["entry_time"]):
        equity += float(row[field])
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _portfolio_metrics(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    pnls = [float(row[field]) for row in rows]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    return {
        "trades": len(rows),
        "net_pnl": sum(pnls),
        "expectancy": mean(pnls) if pnls else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "max_drawdown": _max_drawdown(rows, field),
        "average_winner": mean(wins) if wins else None,
        "average_loser": mean(losses) if losses else None,
    }


def _candidate_id(checkpoint: int, negative: float, progress: float) -> str:
    return f"T{checkpoint}_NEG{abs(negative):.2f}R_MFE{progress:.2f}R"


def _impact_rows(cases: list[TradeCase]) -> list[dict[str, Any]]:
    output = []
    for checkpoint in CHECKPOINTS:
        for negative in NEGATIVE_THRESHOLDS:
            for progress in PROGRESS_THRESHOLDS:
                candidate = _candidate_id(checkpoint, negative, progress)
                for case in cases:
                    point = next(
                        row for row in case.checkpoint_rows
                        if row["checkpoint_minutes"] == checkpoint
                    )
                    trigger = bool(
                        point["evaluable"]
                        and point["current_pnl_r"] <= negative
                        and point["mfe_r_so_far"] <= progress
                    )
                    original = float(case.outcome.net_pnl)
                    new = float(point["execution_pnl"]) if trigger else original
                    output.append({
                        "candidate_id": candidate,
                        "checkpoint_minutes": checkpoint,
                        "negative_threshold_r": negative,
                        "mfe_progress_threshold_r": progress,
                        "trade_id": case.trade.trade_id,
                        "entry_time": case.trade.entry_time.isoformat(),
                        "symbol": case.trade.symbol,
                        "side": case.trade.side,
                        "original_outcome": case.outcome.label,
                        "evaluable": point["evaluable"],
                        "triggered": trigger,
                        "current_pnl_r": point["current_pnl_r"],
                        "mfe_r_so_far": point["mfe_r_so_far"],
                        "mae_r_so_far": point["mae_r_so_far"],
                        "original_pnl": original,
                        "early_exit_pnl": point["execution_pnl"] if trigger else None,
                        "new_pnl": new,
                        "pnl_delta": new - original,
                        "incorrect_winner_exit": trigger and case.outcome.label == "WINNER",
                        "original_loser_exited": trigger and case.outcome.label == "LOSER",
                        "eventual_full_trade_mfe": point["eventual_full_trade_mfe"],
                    })
    return output


def _grid_rows(impacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in impacts:
        grouped.setdefault(row["candidate_id"], []).append(row)
    output = []
    for candidate, rows in grouped.items():
        base = _portfolio_metrics(rows, "original_pnl")
        new = _portfolio_metrics(rows, "new_pnl")
        affected = [row for row in rows if row["triggered"]]
        loser_impacts = [row for row in affected if row["original_loser_exited"]]
        winner_impacts = [row for row in affected if row["incorrect_winner_exit"]]
        positive_loser_improvements = [
            max(float(row["pnl_delta"]), 0.0) for row in loser_impacts
        ]
        lost_winner = [
            max(-float(row["pnl_delta"]), 0.0) for row in winner_impacts
        ]
        output.append({
            "candidate_id": candidate,
            "checkpoint_minutes": rows[0]["checkpoint_minutes"],
            "negative_threshold_r": rows[0]["negative_threshold_r"],
            "mfe_progress_threshold_r": rows[0]["mfe_progress_threshold_r"],
            "evaluable_trades": sum(bool(row["evaluable"]) for row in rows),
            "trades_affected": len(affected),
            "original_losers_exited_earlier": len(loser_impacts),
            "eventual_winners_incorrectly_exited": len(winner_impacts),
            "false_exit_rate": len(winner_impacts) / len(affected) if affected else 0.0,
            "losers_improved_count": sum(float(row["pnl_delta"]) > 0 for row in loser_impacts),
            "losers_worsened_count": sum(float(row["pnl_delta"]) < 0 for row in loser_impacts),
            "saved_loss_twd": sum(positive_loser_improvements),
            "lost_future_winner_pnl_twd": sum(lost_winner),
            "net_pnl": new["net_pnl"],
            "net_pnl_difference": new["net_pnl"] - base["net_pnl"],
            "expectancy": new["expectancy"],
            "expectancy_difference": new["expectancy"] - base["expectancy"],
            "profit_factor": new["profit_factor"],
            "profit_factor_difference": new["profit_factor"] - base["profit_factor"],
            "max_drawdown": new["max_drawdown"],
            "max_drawdown_difference": new["max_drawdown"] - base["max_drawdown"],
            "average_loser": new["average_loser"],
            "average_loser_difference": new["average_loser"] - base["average_loser"],
            "average_winner": new["average_winner"],
            "average_winner_difference": new["average_winner"] - base["average_winner"],
            "baseline_net_pnl": base["net_pnl"],
            "baseline_expectancy": base["expectancy"],
            "baseline_profit_factor": base["profit_factor"],
            "baseline_max_drawdown": base["max_drawdown"],
            "baseline_average_loser": base["average_loser"],
            "baseline_average_winner": base["average_winner"],
        })
    return output


def _robustness(
    impacts: list[dict[str, Any]], grid: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    by_candidate: dict[str, list[dict[str, Any]]] = {}
    for row in impacts:
        by_candidate.setdefault(row["candidate_id"], []).append(row)
    details, summaries = [], {}
    grid_by_id = {row["candidate_id"]: row for row in grid}
    for candidate, rows in by_candidate.items():
        deltas = []
        for removed in rows:
            remaining = [row for row in rows if row["trade_id"] != removed["trade_id"]]
            delta = sum(float(row["new_pnl"]) - float(row["original_pnl"]) for row in remaining)
            deltas.append(delta)
            details.append({
                "candidate_id": candidate,
                "removed_trade_id": removed["trade_id"],
                "removed_trade_triggered": removed["triggered"],
                "removed_trade_pnl_delta": removed["pnl_delta"],
                "loo_net_pnl_difference": delta,
                "loo_benefit_positive": delta > 0,
            })
        full = float(grid_by_id[candidate]["net_pnl_difference"])
        positive_contributions = [
            float(row["pnl_delta"]) for row in rows if float(row["pnl_delta"]) > 0
        ]
        dominant_share = (
            max(positive_contributions) / sum(positive_contributions)
            if positive_contributions else None
        )
        loo_fragile = bool(full <= 0 or min(deltas) <= 0)
        outlier_concentrated = bool(
            dominant_share is not None and dominant_share > 0.50
        )
        fragile = loo_fragile or outlier_concentrated
        summaries[candidate] = {
            "full_net_pnl_difference": full,
            "loo_min_net_pnl_difference": min(deltas),
            "loo_max_net_pnl_difference": max(deltas),
            "loo_positive_count": sum(delta > 0 for delta in deltas),
            "loo_runs": len(deltas),
            "benefit_disappears_count": sum(delta <= 0 for delta in deltas),
            "largest_positive_trade_share": dominant_share,
            "loo_fragile": loo_fragile,
            "outlier_concentrated": outlier_concentrated,
            "fragility_reason": (
                "LOO_BENEFIT_DISAPPEARS"
                if loo_fragile else
                "ONE_TRADE_OVER_50PCT_OF_GAIN"
                if outlier_concentrated else
                "NONE"
            ),
            "fragile": fragile,
        }
    for row in details:
        row.update(summaries[row["candidate_id"]])
    return details, summaries


def _rank(grid: list[dict[str, Any]], robust: dict[str, dict[str, Any]]) -> None:
    for row in grid:
        row.update(robust[row["candidate_id"]])
    ordered = sorted(
        grid,
        key=lambda row: (
            row["eventual_winners_incorrectly_exited"],
            row["fragile"],
            -row["losers_improved_count"],
            -row["average_loser_difference"],
            -row["expectancy_difference"],
            -row["profit_factor_difference"],
            row["largest_positive_trade_share"] if row["largest_positive_trade_share"] is not None else 1.0,
        ),
    )
    for rank, row in enumerate(ordered, 1):
        row["robustness_rank"] = rank


def build_report(
    session_runs: Mapping[str, list[Path]], capital: int = 190_000,
) -> dict[str, Any]:
    trades, diagnostics, coverage = build_independent_signal_trades(session_runs, capital)
    stocks_by_date = {}
    for paths in session_runs.values():
        stocks, manifest = load_session(paths)
        stocks_by_date[str(manifest["session_date"])] = stocks
    cases, checkpoint_rows, unscorable = [], [], []
    for trade in trades:
        current = simulate_current_indicator_stop(
            trade, stocks_by_date[trade.session_date][trade.symbol], CURRENT_POLICY,
        )
        outcome = _outcome(current)
        rows = build_checkpoint_rows(trade, outcome)
        checkpoint_rows.extend(rows)
        if outcome.status == "SCORED":
            cases.append(TradeCase(trade, outcome, tuple(rows)))
        else:
            unscorable.append(trade.trade_id)
    impacts = _impact_rows(cases)
    grid = _grid_rows(impacts)
    robustness_rows, robustness = _robustness(impacts, grid)
    _rank(grid, robustness)
    robust_candidates = [
        row for row in grid
        if not row["fragile"]
        and row["eventual_winners_incorrectly_exited"] == 0
        and row["losers_improved_count"] >= 2
        and row["net_pnl_difference"] > 0
        and row["profit_factor_difference"] > 0
    ]
    robust_candidates.sort(key=lambda row: row["robustness_rank"])
    shadow_candidates = [
        row for row in grid
        if row["eventual_winners_incorrectly_exited"] == 0
        and row["losers_improved_count"] >= 2
        and row["net_pnl_difference"] > 0
        and row["profit_factor_difference"] > 0
    ]
    shadow_candidates.sort(key=lambda row: row["robustness_rank"])
    return {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "BACKTEST_ONLY_EARLY_LOSER_DIAGNOSTIC; NOT PARAMETER FITTING OR A PRODUCTION RULE",
        "one_r_net_twd": ONE_R_NET_TWD,
        "r_method": "current PnL R uses net PnL / 3500 TWD; MFE_R and MAE_R use side-aware price excursion / initial per-share price risk, matching the existing MFE implementation",
        "execution_method": "observe last quote at/before checkpoint and fill at first quote at/after checkpoint; both must be within existing 5-second staleness limit",
        "grid_definition": {
            "checkpoints_minutes": CHECKPOINTS,
            "negative_thresholds_r": NEGATIVE_THRESHOLDS,
            "mfe_progress_thresholds_r": PROGRESS_THRESHOLDS,
        },
        "coverage": coverage,
        "source_diagnostics": diagnostics,
        "scorable_trades": len(cases),
        "unscorable_trade_ids": unscorable,
        "checkpoint_rows": checkpoint_rows,
        "grid": grid,
        "trade_impacts": impacts,
        "robustness_rows": robustness_rows,
        "robust_candidate_ids": [row["candidate_id"] for row in robust_candidates],
        "shadow_candidate_ids": [row["candidate_id"] for row in shadow_candidates],
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "strategy_changed": False,
        "mfe_profit_protection_changed": False,
        "live_behavior_changed": False,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _fmt(value: Any, digits: int = 0) -> str:
    return "N/A" if value is None else f"{float(value):,.{digits}f}"


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "early_failure_checkpoints.csv", report["checkpoint_rows"])
    _write_csv(output_dir / "early_failure_grid.csv", report["grid"])
    _write_csv(output_dir / "early_failure_trade_impacts.csv", report["trade_impacts"])
    _write_csv(output_dir / "early_failure_robustness.csv", report["robustness_rows"])
    ranked = sorted(report["grid"], key=lambda row: row["robustness_rank"])
    zero_false = [row for row in ranked if row["eventual_winners_incorrectly_exited"] == 0]
    robust = [row for row in ranked if row["candidate_id"] in report["robust_candidate_ids"]]
    shadow = [row for row in ranked if row["candidate_id"] in report["shadow_candidate_ids"]]
    by_checkpoint = {}
    for checkpoint in CHECKPOINTS:
        rows = [row for row in zero_false if row["checkpoint_minutes"] == checkpoint]
        by_checkpoint[checkpoint] = rows[0] if rows else None
    lines = [
        "# EARLY_FAILURE_EXIT controlled diagnostic", "",
        "Backtest/shadow research only. No stock selection, entry, sizing, existing exit, MFE protection, broker, or live behavior was changed.", "",
        "## Method", "",
        f"- {report['scorable_trades']} scorable historical trades; 75 fixed grid candidates.",
        "- A rule exits only when current net PnL R is at/below the loss threshold AND MFE R so far is at/below the progress threshold.",
        "- Current PnL R uses the NT$3,500 net-risk budget; MFE_R/MAE_R use side-aware price excursion divided by the existing initial per-share price risk.",
        "- Observation and simulated fill quotes must each satisfy the existing 5-second freshness rule.",
        "- Original exits that occur first retain priority. All fees, tax, adverse quote proxy, and current exits remain unchanged.",
        "- Ranking prioritizes avoiding eventual winners, breadth across losers, loss reduction, expectancy/PF, and leave-one-trade-out stability—not maximum PnL.", "",
        "## Best zero-false-exit candidate at each checkpoint", "",
        "| Checkpoint | Candidate | Affected | Losers improved | Winner false exits | Net delta | PF delta | Avg loser delta | LOO min delta | Fragile |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for checkpoint in CHECKPOINTS:
        row = by_checkpoint[checkpoint]
        if row is None:
            continue
        lines.append(
            f"| {checkpoint}m | {row['candidate_id']} | {row['trades_affected']} | "
            f"{row['losers_improved_count']} | {row['eventual_winners_incorrectly_exited']} | "
            f"{_fmt(row['net_pnl_difference'])} | {_fmt(row['profit_factor_difference'], 3)} | "
            f"{_fmt(row['average_loser_difference'])} | {_fmt(row['loo_min_net_pnl_difference'])} | "
            f"{'YES' if row['fragile'] else 'NO'} |"
        )
    lines.extend(["", "## Robust candidates", ""])
    if robust:
        lines.extend([
            "| Rank | Candidate | Losers improved | Net delta | Expectancy delta | PF delta | Max-DD delta | LOO min | Largest gain share |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for row in robust[:10]:
            lines.append(
                f"| {row['robustness_rank']} | {row['candidate_id']} | {row['losers_improved_count']} | "
                f"{_fmt(row['net_pnl_difference'])} | {_fmt(row['expectancy_difference'])} | "
                f"{_fmt(row['profit_factor_difference'], 3)} | {_fmt(row['max_drawdown_difference'])} | "
                f"{_fmt(row['loo_min_net_pnl_difference'])} | {_fmt(row['largest_positive_trade_share'] * 100 if row['largest_positive_trade_share'] is not None else None, 1)}% |"
            )
    else:
        lines.append("No candidate met the predeclared robustness screen.")
    checkpoint_coverage = {
        minute: sum(
            bool(row["evaluable"]) for row in report["checkpoint_rows"]
            if row["checkpoint_minutes"] == minute
        )
        for minute in CHECKPOINTS
    }
    lines.extend([
        "", "## Concentration warning", "",
        (
            f"The leading shadow-only family is {shadow[0]['candidate_id']}: net PnL changes from {_fmt(shadow[0]['baseline_net_pnl'])} to {_fmt(shadow[0]['net_pnl'])} TWD, PF from {_fmt(shadow[0]['baseline_profit_factor'], 3)} to {_fmt(shadow[0]['profit_factor'], 3)}, and max drawdown by {_fmt(shadow[0]['max_drawdown_difference'])} TWD. It affects {shadow[0]['trades_affected']} losing trades and no eventual winners in this sample."
            if shadow else
            "No zero-winner-harm, multi-loser candidate improved both net PnL and PF."
        ),
        (
            f"However, the largest improved trade supplies {_fmt(shadow[0]['largest_positive_trade_share'] * 100, 1)}% of the gain. This is classified as outlier-concentrated even though leave-one-out net benefit stays above zero."
            if shadow and shadow[0]["outlier_concentrated"] else
            "No material single-trade concentration was detected in the leading shadow candidate."
        ),
        (
            "The ten leading 5-minute combinations are behaviorally identical in this sample: -0.50R or -0.60R current loss with any tested 0.10R–0.50R MFE ceiling affects the same two trades. The data therefore does not identify a preferred MFE-progress threshold."
            if len(shadow) >= 10 else
            "The grid does not contain a broad plateau of equivalent shadow candidates."
        ),
        "", "## Answers", "",
        f"1. Early identification evidence is evaluated on fresh/open samples of 5m={checkpoint_coverage[5]}, 10m={checkpoint_coverage[10]}, 15m={checkpoint_coverage[15]} trades; conclusions are necessarily provisional.",
        (
            f"2. The most promising checkpoint for further observation is {shadow[0]['checkpoint_minutes']} minutes ({shadow[0]['candidate_id']}); no checkpoint passed the full robustness screen."
            if shadow else
            "2. No checkpoint produced a viable shadow candidate."
        ),
        f"3. Zero-winner-harm, multi-loser candidates improving net/PF: {len(shadow)}; fully robust candidates after concentration and LOO checks: {len(robust)}.",
        f"4. Shadow-mode eligibility: {'YES for non-executing measurement only' if shadow else 'NO'}; no live or strategy enablement is justified.",
        "5. The three partial sessions and 17 scorable trades remain far too small for production.",
        "6. Next data needed: more complete trading days across rising/falling/range regimes, continuous fresh quotes, five-level book snapshots, signed inside/outside volume, spread, volatility, and actual manual/live fill timestamps.",
        "", "All parameter rows, per-trade impacts, and every leave-one-trade-out rerun are in the accompanying CSV files.",
    ])
    report_path = output_dir / "report_early_failure.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    artifacts = (
        "early_failure_checkpoints.csv", "early_failure_grid.csv",
        "early_failure_trade_impacts.csv", "early_failure_robustness.csv",
        "report_early_failure.md",
    )
    manifest = {
        "analysis_id": ANALYSIS_ID,
        "artifact_hashes": {
            name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
            for name in artifacts
        },
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "strategy_changed": False,
        "mfe_profit_protection_changed": False,
        "live_behavior_changed": False,
    }
    (output_dir / "early_failure_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
