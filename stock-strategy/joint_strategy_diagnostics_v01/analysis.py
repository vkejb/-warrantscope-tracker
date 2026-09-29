"""Joint backtest with causal delayed fills and unchanged broker/live behavior."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any, Mapping

from current_indicator_stop_study_v01.analysis import (
    ONE_R_NET_TWD,
    VARIANTS as CURRENT_VARIANTS,
    simulate_current_indicator_stop,
)
from mfe_profit_protection_study_v01.analysis import (
    ResearchTrade,
    _independent_trade_path,
    build_independent_signal_trades,
    derive_initial_stop_price,
)
from mfe_profit_protection_study_v01.overlay import (
    EntryFill,
    MFEProtectionState,
    PositionBasis,
    VARIANTS as MFE_VARIANTS,
)
from signal_quality_diagnostics_v01.analysis import build_report as build_quality_report
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC,
    _tick_size,
    load_session,
    signal_at,
)


ANALYSIS_ID = "JOINT_INTRADAY_STRATEGY_DIAGNOSTICS_V0_1"
BASELINE_POLICY = CURRENT_VARIANTS[0]
CONFIRMATION_SECONDS = 60
MINIMUM_DIRECTIONAL_VOLUME_DELTA = 0.10
MINIMUM_DIRECTIONAL_LARGE_DELTA = 0.0
MAXIMUM_OPENING_EXTENSION = 0.020
MAXIMUM_VWAP_EXTENSION = 0.0125
EARLY_FAILURE_MINUTES = 5
EARLY_FAILURE_LOSS_R = -0.50
EARLY_FAILURE_MAX_MFE_R = 0.10


def _signed_volume(row: dict[str, Any]) -> float:
    if row["flag"] == "1":
        return float(row["volume"])
    if row["flag"] == "0":
        return -float(row["volume"])
    midpoint = (float(row["bid"]) + float(row["ask"])) / 2
    return float(row["volume"]) if float(row["price"]) >= midpoint else -float(row["volume"])


def _percentile90(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(0.9 * len(ordered)) - 1)
    return ordered[index]


def _anti_chase_pass(row: dict[str, Any]) -> bool:
    return bool(
        float(row["directional_opening_extension"]) <= MAXIMUM_OPENING_EXTENSION
        and float(row["directional_vwap_extension"]) <= MAXIMUM_VWAP_EXTENSION
    )


def _confirmation(
    feature: dict[str, Any], data: dict[str, Any], signal: Any, capital: int,
) -> tuple[ResearchTrade | None, dict[str, Any]]:
    decision = datetime.fromisoformat(str(feature["signal_time"]))
    checkpoint = decision + timedelta(seconds=CONFIRMATION_SECONDS)
    before = [row for row in data["ticks"] if row["time"] <= checkpoint]
    observation = before[-1] if before else None
    execution = next((row for row in data["ticks"] if row["time"] >= checkpoint), None)
    if observation is None or execution is None:
        return None, {"confirmation_reason": "QUOTE_MISSING"}
    observation_age = (checkpoint - observation["time"]).total_seconds()
    execution_delay = (execution["time"] - checkpoint).total_seconds()
    maximum_age = float(SPEC["maximum_tick_staleness_seconds"])
    if not (0 <= observation_age <= maximum_age and 0 <= execution_delay <= maximum_age):
        return None, {
            "confirmation_reason": "QUOTE_STALE",
            "observation_age_seconds": observation_age,
            "execution_delay_seconds": execution_delay,
        }
    window = [row for row in data["ticks"] if decision < row["time"] <= checkpoint]
    if not window or sum(float(row["volume"]) for row in window) <= 0:
        return None, {"confirmation_reason": "FLOW_WINDOW_MISSING"}
    direction = 1.0 if signal.side == "LONG" else -1.0
    total = sum(float(row["volume"]) for row in window)
    volume_delta = direction * sum(_signed_volume(row) for row in window) / total
    reference = [
        row for row in data["ticks"]
        if checkpoint - timedelta(seconds=300) <= row["time"] <= checkpoint
    ]
    threshold = _percentile90([float(row["volume"]) for row in reference])
    large = [row for row in window if float(row["volume"]) >= threshold]
    large_total = sum(float(row["volume"]) for row in large)
    large_delta = (
        direction * sum(_signed_volume(row) for row in large) / large_total
        if large_total > 0 else None
    )
    current = float(observation["price"])
    boundary = float(feature["breakout_boundary_price"])
    structure_held = current >= boundary if signal.side == "LONG" else current <= boundary
    causal = [row for row in data["ticks"] if row["time"] <= checkpoint]
    known_volume = sum(float(row["volume"]) for row in causal)
    vwap = (
        sum(float(row["price"]) * float(row["volume"]) for row in causal) / known_volume
        if known_volume > 0 else None
    )
    first = float(causal[0]["price"])
    if signal.side == "LONG":
        opening_extension = current / first - 1
        vwap_extension = current / float(vwap) - 1 if vwap else None
    else:
        opening_extension = first / current - 1
        vwap_extension = float(vwap) / current - 1 if vwap else None
    diagnostics = {
        "confirmation_time": checkpoint.isoformat(),
        "confirmation_volume_delta": volume_delta,
        "confirmation_large_trade_delta": large_delta,
        "confirmation_structure_held": structure_held,
        "confirmation_opening_extension": opening_extension,
        "confirmation_vwap_extension": vwap_extension,
        "observation_age_seconds": observation_age,
        "execution_delay_seconds": execution_delay,
    }
    checks = {
        "VOLUME_NOT_PERSISTENT": volume_delta >= MINIMUM_DIRECTIONAL_VOLUME_DELTA,
        "LARGE_TRADE_NOT_PERSISTENT": (
            large_delta is not None and large_delta >= MINIMUM_DIRECTIONAL_LARGE_DELTA
        ),
        "BREAKOUT_NOT_HELD": structure_held,
        "OPENING_EXTENSION_TOO_HIGH": opening_extension <= MAXIMUM_OPENING_EXTENSION,
        "VWAP_EXTENSION_TOO_HIGH": (
            vwap_extension is not None and vwap_extension <= MAXIMUM_VWAP_EXTENSION
        ),
    }
    failed = [reason for reason, passed in checks.items() if not passed]
    if failed:
        return None, {**diagnostics, "confirmation_reason": "+".join(failed)}
    entry_price = (
        float(execution["ask"]) + _tick_size(float(execution["ask"]))
        if signal.side == "LONG"
        else max(
            _tick_size(float(execution["bid"])),
            float(execution["bid"]) - _tick_size(float(execution["bid"])),
        )
    )
    quantity = math.floor(capital / (entry_price * 1000)) * 1000
    if quantity <= 0:
        return None, {**diagnostics, "confirmation_reason": "DELAYED_ENTRY_UNAFFORDABLE"}
    trade, path_diagnostic = _independent_trade_path(
        str(feature["session_date"]), data, signal, execution, entry_price, quantity,
    )
    if trade is None:
        return None, {**diagnostics, **path_diagnostic, "confirmation_reason": "EXIT_PATH_UNSCORABLE"}
    return trade, {**diagnostics, **path_diagnostic, "confirmation_reason": "CONFIRMED"}


def _early_failure_execution(trade: ResearchTrade) -> Any | None:
    target = trade.entry_time + timedelta(minutes=EARLY_FAILURE_MINUTES)
    observed = [point for point in trade.points if point.at <= target]
    observation = observed[-1] if observed else None
    execution = next((point for point in trade.points if point.at >= target), None)
    if observation is None or execution is None:
        return None
    maximum_age = float(SPEC["maximum_tick_staleness_seconds"])
    if not (
        0 <= (target - observation.at).total_seconds() <= maximum_age
        and 0 <= (execution.at - target).total_seconds() <= maximum_age
    ):
        return None
    mfe_r = max(
        [0.0, *(max(0.0, trade.basis.favorable_r(point.exit_price)) for point in observed)]
    )
    if (
        float(observation.projected_net_pnl) / ONE_R_NET_TWD <= EARLY_FAILURE_LOSS_R
        and mfe_r <= EARLY_FAILURE_MAX_MFE_R
    ):
        return execution
    return None


def _simulate_cost_aware_joint_exit(trade: ResearchTrade) -> dict[str, Any]:
    initial_stop = derive_initial_stop_price(
        trade.side, trade.entry_price, trade.quantity, ONE_R_NET_TWD,
    )
    basis = PositionBasis.from_fills(
        trade.side,
        [EntryFill(trade.entry_time, trade.entry_price, trade.quantity)],
        initial_stop_price=initial_stop,
    )
    overlay = MFEProtectionState(basis, MFE_VARIANTS["MFE_V1"])
    overlay.observe(
        price=trade.entry_price,
        at=trade.entry_time,
        projected_net_pnl=trade.pnl_at_price(trade.entry_price),
        pnl_at_price=trade.pnl_at_price,
    )
    early = _early_failure_execution(trade)
    hard_hour, hard_minute = map(int, str(SPEC["hard_exit_time"]).split(":"))
    hard_exit = trade.entry_time.replace(
        hour=hard_hour, minute=hard_minute, second=0, microsecond=0,
    )
    for index, point in enumerate(trade.points):
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
        elif overlay.armed and point.projected_net_pnl <= 0:
            reason = "MFE_COST_AWARE_BREAKEVEN"
        elif early is point:
            reason = "EARLY_FAILURE_5M"
        elif point.reversal:
            reason = "SIGNAL_REVERSAL"
        elif point.at >= hard_exit:
            reason = "HARD_EXIT"
        elif trade.force_last_point_exit and index == len(trade.points) - 1:
            reason = "HARD_EXIT"
        if reason is not None:
            return {
                "status": "SCORED",
                "exit_time": point.at.isoformat(),
                "exit_price": point.exit_price,
                "exit_reason": reason,
                "net_pnl": point.projected_net_pnl,
                "holding_seconds": (point.at - trade.entry_time).total_seconds(),
            }
    return {"status": "UNSCORABLE"}


def _max_drawdown(rows: list[dict[str, Any]]) -> float:
    equity = peak = drawdown = 0.0
    for row in sorted(rows, key=lambda item: item["entry_time"]):
        if row["new_pnl"] is None:
            continue
        equity += float(row["new_pnl"])
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _summary(variant: str, rows: list[dict[str, Any]], baseline_net: float) -> dict[str, Any]:
    entered = [row for row in rows if row["action"] == "ENTERED"]
    unscorable_actions = {"UNSCORABLE", "UNSCORABLE_AFTER_DELAY"}
    unscorable = [row for row in rows if row["action"] in unscorable_actions]
    evaluable_opportunities = len(rows) - len(unscorable)
    matched_baseline_net = sum(
        float(row["original_pnl"])
        for row in rows if row["action"] not in unscorable_actions
    )
    pnls = [float(row["new_pnl"]) for row in entered]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    return {
        "variant": variant,
        "opportunities": len(rows),
        "evaluable_opportunities": evaluable_opportunities,
        "unscorable_after_delay": len(unscorable),
        "entered_trades": len(entered),
        "skipped_anti_chase": sum(row["action"] == "SKIPPED_ANTI_CHASE" for row in rows),
        "rejected_confirmation": sum(row["action"] == "REJECTED_CONFIRMATION" for row in rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(entered) if entered else None,
        "net_pnl": sum(pnls),
        "matched_baseline_net_pnl": matched_baseline_net,
        "full_baseline_net_pnl": baseline_net,
        "net_pnl_difference": sum(pnls) - matched_baseline_net,
        "expectancy_per_opportunity": (
            sum(pnls) / evaluable_opportunities if evaluable_opportunities else None
        ),
        "expectancy_per_entry": mean(pnls) if pnls else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "average_winner": mean(wins) if wins else None,
        "average_loser": mean(losses) if losses else None,
        "maximum_drawdown": _max_drawdown(rows),
        "original_winners_not_entered": sum(
            row["original_outcome"] == "WINNER"
            and row["action"] in {"SKIPPED_ANTI_CHASE", "REJECTED_CONFIRMATION"}
            for row in rows
        ),
        "original_losers_not_entered": sum(
            row["original_outcome"] == "LOSER"
            and row["action"] in {"SKIPPED_ANTI_CHASE", "REJECTED_CONFIRMATION"}
            for row in rows
        ),
    }


def build_report(
    session_runs: Mapping[str, list[Path]], capital: int = 190_000,
) -> dict[str, Any]:
    quality = build_quality_report(session_runs, capital)
    features = {
        (str(row["session_date"]), str(row["symbol"])): row
        for row in quality["per_trade"]
        if row["outcome"] in {"WINNER", "LOSER"}
    }
    original_trades, _diagnostics, coverage = build_independent_signal_trades(
        session_runs, capital
    )
    trades = {
        (trade.session_date, trade.symbol): trade for trade in original_trades
        if (trade.session_date, trade.symbol) in features
    }
    stocks_by_date = {}
    for paths in session_runs.values():
        stocks, session_coverage = load_session(paths)
        stocks_by_date[str(session_coverage["session_date"])] = stocks
    baseline_rows, anti_rows, anti_exit_rows, confirm_rows, joint_rows = [], [], [], [], []
    confirmation_cache: dict[tuple[str, str], tuple[ResearchTrade | None, dict[str, Any]]] = {}
    for key, feature in features.items():
        baseline_pnl = float(feature["realized_net_pnl"])
        common = {
            "trade_id": feature["trade_id"],
            "session_date": feature["session_date"],
            "symbol": feature["symbol"],
            "stock_name": feature["stock_name"],
            "side": feature["side"],
            "entry_time": feature["entry_time"],
            "original_outcome": feature["outcome"],
            "original_pnl": baseline_pnl,
        }
        baseline_rows.append({
            **common, "variant": "BASELINE", "action": "ENTERED",
            "new_pnl": baseline_pnl, "exit_reason": feature["exit_reason"],
        })
        if not _anti_chase_pass(feature):
            skipped = {
                **common, "action": "SKIPPED_ANTI_CHASE", "new_pnl": 0.0,
                "exit_reason": None,
            }
            anti_rows.append({**skipped, "variant": "ANTI_CHASE_ONLY"})
            anti_exit_rows.append({**skipped, "variant": "ANTI_CHASE_WITH_EXIT_PROTECTION"})
            confirm_rows.append({**skipped, "variant": "ANTI_CHASE_CONFIRM_60S"})
            joint_rows.append({**skipped, "variant": "JOINT_WITH_EXIT_PROTECTION"})
            continue
        anti_rows.append({
            **common, "variant": "ANTI_CHASE_ONLY", "action": "ENTERED",
            "new_pnl": baseline_pnl, "exit_reason": feature["exit_reason"],
        })
        original_joint = _simulate_cost_aware_joint_exit(trades[key])
        if original_joint["status"] == "SCORED":
            anti_exit_rows.append({
                **common, "variant": "ANTI_CHASE_WITH_EXIT_PROTECTION",
                "action": "ENTERED", "new_pnl": float(original_joint["net_pnl"]),
                "exit_reason": original_joint["exit_reason"],
            })
        else:
            anti_exit_rows.append({
                **common, "variant": "ANTI_CHASE_WITH_EXIT_PROTECTION",
                "action": "UNSCORABLE", "new_pnl": None,
                "exit_reason": "EXIT_OVERLAY_UNSCORABLE",
            })
        data = stocks_by_date[key[0]][key[1]]
        signal = signal_at(key[1], data, datetime.fromisoformat(feature["signal_time"]))
        if signal is None:
            delayed, confirmation = None, {"confirmation_reason": "SIGNAL_REBUILD_FAILED"}
        else:
            delayed, confirmation = _confirmation(feature, data, signal, capital)
        confirmation_cache[key] = (delayed, confirmation)
        if delayed is None:
            rejected = {
                **common, "action": "REJECTED_CONFIRMATION", "new_pnl": 0.0,
                "exit_reason": confirmation["confirmation_reason"], **confirmation,
            }
            confirm_rows.append({**rejected, "variant": "ANTI_CHASE_CONFIRM_60S"})
            joint_rows.append({**rejected, "variant": "JOINT_WITH_EXIT_PROTECTION"})
            continue
        current = simulate_current_indicator_stop(delayed, data, BASELINE_POLICY)
        joint = _simulate_cost_aware_joint_exit(delayed)
        if current["status"] != "SCORED" or joint["status"] != "SCORED":
            unscorable = {
                **common, "action": "UNSCORABLE_AFTER_DELAY", "new_pnl": None,
                "exit_reason": "DELAYED_EXIT_PATH_UNSCORABLE",
                "delayed_entry_time": delayed.entry_time.isoformat(),
                "delayed_entry_price": delayed.entry_price, **confirmation,
            }
            confirm_rows.append({**unscorable, "variant": "ANTI_CHASE_CONFIRM_60S"})
            joint_rows.append({**unscorable, "variant": "JOINT_WITH_EXIT_PROTECTION"})
            continue
        confirm_rows.append({
            **common, "variant": "ANTI_CHASE_CONFIRM_60S", "action": "ENTERED",
            "new_pnl": float(current["net_pnl"]), "exit_reason": current["exit_reason"],
            "delayed_entry_time": delayed.entry_time.isoformat(),
            "delayed_entry_price": delayed.entry_price, **confirmation,
        })
        joint_rows.append({
            **common, "variant": "JOINT_WITH_EXIT_PROTECTION", "action": "ENTERED",
            "new_pnl": float(joint["net_pnl"]), "exit_reason": joint["exit_reason"],
            "delayed_entry_time": delayed.entry_time.isoformat(),
            "delayed_entry_price": delayed.entry_price, **confirmation,
        })
    variants = {
        "BASELINE": baseline_rows,
        "ANTI_CHASE_ONLY": anti_rows,
        "ANTI_CHASE_WITH_EXIT_PROTECTION": anti_exit_rows,
        "ANTI_CHASE_CONFIRM_60S": confirm_rows,
        "JOINT_WITH_EXIT_PROTECTION": joint_rows,
    }
    baseline_net = sum(float(row["new_pnl"]) for row in baseline_rows)
    summaries = [_summary(name, rows, baseline_net) for name, rows in variants.items()]
    impacts = [row for rows in variants.values() for row in rows]
    return {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "BACKTEST_ONLY_CONTROLLED_JOINT_DIAGNOSTIC_NOT_PRODUCTION_VALIDATION",
        "variant_definitions": {
            "BASELINE": "current entry and current -3500/MFE_V1/reversal/EOD exits",
            "ANTI_CHASE_ONLY": "skip if directional opening extension>2% or VWAP extension>1.25%",
            "ANTI_CHASE_WITH_EXIT_PROTECTION": (
                "anti-chase with original entry timing plus 5m early failure and cost-aware MFE breakeven"
            ),
            "ANTI_CHASE_CONFIRM_60S": (
                "anti-chase plus causal 60-second delayed entry requiring persistent signed volume, "
                "persistent large-trade flow, breakout hold, and extension recheck"
            ),
            "JOINT_WITH_EXIT_PROTECTION": (
                "confirmed entry plus 5m -0.5R/<=0.1R-MFE early failure and cost-aware MFE breakeven"
            ),
        },
        "summaries": summaries,
        "trade_impacts": impacts,
        "coverage": coverage,
        "limitations": [
            "17 scorable independent signals across three partial sessions",
            "2026-09-23 includes callback errors",
            "thresholds are diagnostic and in-sample",
            "independent signals overlap and do not represent one executable account",
        ],
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "strategy_changed": False,
        "live_behavior_changed": False,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _fmt(value: Any, digits: int = 0) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):,.{digits}f}"


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Joint intraday strategy diagnostic",
        "",
        "All variants are backtest-only. Delayed variants use a new causal fill after the confirmation checkpoint; they never retain the original entry price.",
        "",
        "| Variant | Entered | Anti-chase skip | Confirmation reject | Unscorable | W/L | Win rate | Net PnL | Delta | PF | Avg loser | Max DD |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["summaries"]:
        rate = "N/A" if row["win_rate"] is None else f"{row['win_rate'] * 100:.1f}%"
        lines.append(
            f"| {row['variant']} | {row['entered_trades']} | {row['skipped_anti_chase']} | "
            f"{row['rejected_confirmation']} | {row['unscorable_after_delay']} | "
            f"{row['wins']}/{row['losses']} | {rate} | "
            f"{_fmt(row['net_pnl'])} | {_fmt(row['net_pnl_difference'])} | "
            f"{_fmt(row['profit_factor'], 2)} | {_fmt(row['average_loser'])} | "
            f"{_fmt(row['maximum_drawdown'])} |"
        )
    lines.extend([
        "",
        "This comparison cannot authorize a live change: the sample is small, all source sessions are partial, and one session has callback errors.",
        "",
    ])
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "report.md").write_text(_markdown(report), encoding="utf-8")
    _write_csv(output_dir / "comparison.csv", report["summaries"])
    _write_csv(output_dir / "per_trade_impacts.csv", report["trade_impacts"])
    artifacts = {
        path.name: sha256_file(path)
        for path in sorted(output_dir.iterdir())
        if path.is_file() and path.name != "run_manifest.json"
    }
    manifest = {
        "analysis_id": ANALYSIS_ID,
        "artifact_hashes": artifacts,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "strategy_changed": False,
        "live_behavior_changed": False,
    }
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
