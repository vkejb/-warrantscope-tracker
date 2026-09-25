"""Time-stop and soft/hard hybrid exit research.

Entry remains the production LIVE long-only signal engine.

Research-only:
- no broker imports
- no order submission
- no LIVE parameter mutation
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from statistics import median

from .direction_follow_backtest import SPEC, _projected_net_pnl, load_session
from .exit_parameter_sweep import TradePath, build_trade_path
from .flow_exit_sweep import (
    FlowSnapshot,
    _negative_components,
    build_flow_snapshots,
)


@dataclass(frozen=True)
class HybridVariant:
    variant_id: str
    kind: str
    soft_value: float | None = None
    hard_value: float | None = None
    minutes: int | None = None


VARIANTS = (
    HybridVariant(
        "TIME_3M_NO_POS_MFE_FLOW_2OF3",
        "TIME_NO_POS_MFE_FLOW",
        minutes=3,
    ),
    HybridVariant(
        "TIME_5M_NO_POS_MFE_FLOW_2OF3",
        "TIME_NO_POS_MFE_FLOW",
        minutes=5,
    ),
    HybridVariant(
        "TIME_10M_COST_ZONE",
        "TIME_COST_ZONE",
        minutes=10,
    ),
    HybridVariant(
        "HYBRID_FIXED_1500_3000",
        "HYBRID_FIXED",
        soft_value=1500.0,
        hard_value=3000.0,
    ),
    HybridVariant(
        "HYBRID_FIXED_2000_3000",
        "HYBRID_FIXED",
        soft_value=2000.0,
        hard_value=3000.0,
    ),
    HybridVariant(
        "HYBRID_FIXED_2000_4000",
        "HYBRID_FIXED",
        soft_value=2000.0,
        hard_value=4000.0,
    ),
    HybridVariant(
        "HYBRID_FIXED_2500_4000",
        "HYBRID_FIXED",
        soft_value=2500.0,
        hard_value=4000.0,
    ),
    HybridVariant(
        "HYBRID_PERCENT_1_0_1_5",
        "HYBRID_PERCENT",
        soft_value=0.010,
        hard_value=0.015,
    ),
    HybridVariant(
        "HYBRID_PERCENT_1_2_2_0",
        "HYBRID_PERCENT",
        soft_value=0.012,
        hard_value=0.020,
    ),
)


def _flow_weak(snapshot: FlowSnapshot | None) -> bool:
    return (
        snapshot is not None
        and _negative_components(snapshot) >= 2
    )


def _baseline_reason(
    *,
    projected_net_pnl: float,
    current_return: float,
    peak_return: float,
    worst_return: float,
    reversal: bool,
    hard_exit_reached: bool,
) -> str | None:
    if projected_net_pnl <= -float(SPEC["stop_loss_net_twd"]):
        return "STOP_LOSS"

    if (
        peak_return >= float(SPEC["trailing_profit_activation"])
        and current_return
        <= peak_return - float(SPEC["trailing_profit_drawdown"])
    ):
        return "TRAILING_PROFIT"

    if (
        worst_return < 0
        and current_return > 0
        and current_return - worst_return
        >= float(SPEC["loss_recovery_required"])
    ):
        return "LOSS_RECOVERY_TO_PROFIT"

    if reversal:
        return "SIGNAL_REVERSAL"

    if hard_exit_reached:
        return "HARD_EXIT"

    return None


def _hybrid_stop_reason(
    variant: HybridVariant,
    point,
    snapshot: FlowSnapshot | None,
) -> str | None:
    if variant.kind not in {"HYBRID_FIXED", "HYBRID_PERCENT"}:
        return None

    assert variant.soft_value is not None
    assert variant.hard_value is not None

    if variant.kind == "HYBRID_FIXED":
        loss_value = -float(point.projected_net_pnl)

        if loss_value >= variant.hard_value:
            return "HYBRID_HARD_STOP_FIXED"

        if (
            loss_value >= variant.soft_value
            and _flow_weak(snapshot)
        ):
            return "HYBRID_SOFT_STOP_FIXED_FLOW"

        return None

    loss_rate = -float(point.current_return)

    if loss_rate >= variant.hard_value:
        return "HYBRID_HARD_STOP_PERCENT"

    if (
        loss_rate >= variant.soft_value
        and _flow_weak(snapshot)
    ):
        return "HYBRID_SOFT_STOP_PERCENT_FLOW"

    return None


def simulate_hybrid_variant(
    path: TradePath,
    snapshots: tuple[FlowSnapshot, ...],
    variant: HybridVariant,
) -> dict:
    peak_return = 0.0
    worst_return = 0.0
    positive_mfe_seen = False

    # Flow is calculated on the 30-second decision clock, while executable
    # quote points normally arrive with millisecond timestamps.  Use only the
    # most recent causal flow snapshot at or before each executable point.
    snapshot_index = 0
    latest_snapshot = None
    maximum_flow_age = timedelta(
        seconds=int(SPEC["decision_interval_seconds"])
    )

    hard_hour, hard_minute = map(
        int,
        SPEC["hard_exit_time"].split(":"),
    )
    hard_exit = path.decision_time.replace(
        hour=hard_hour,
        minute=hard_minute,
        second=0,
        microsecond=0,
    )

    time_threshold = (
        path.decision_time + timedelta(minutes=variant.minutes)
        if variant.minutes is not None
        else None
    )

    exit_index = None
    exit_reason = None
    trigger_snapshot = None

    for index, point in enumerate(path.points):
        current = float(point.current_return)

        peak_return = max(peak_return, current)
        worst_return = min(worst_return, current)

        if current > 0:
            positive_mfe_seen = True

        while (
            snapshot_index < len(snapshots)
            and snapshots[snapshot_index].at <= point.at
        ):
            latest_snapshot = snapshots[snapshot_index]
            snapshot_index += 1

        snapshot = latest_snapshot

        if (
            snapshot is not None
            and point.at - snapshot.at > maximum_flow_age
        ):
            snapshot = None

        hybrid_reason = _hybrid_stop_reason(
            variant,
            point,
            snapshot,
        )

        if hybrid_reason is not None:
            exit_index = index
            exit_reason = hybrid_reason
            trigger_snapshot = snapshot
            break

        # For the time-stop variants the existing -5000 stop remains active.
        if (
            variant.kind.startswith("TIME_")
            and point.projected_net_pnl
            <= -float(SPEC["stop_loss_net_twd"])
        ):
            exit_index = index
            exit_reason = "STOP_LOSS"
            break

        if variant.kind == "TIME_NO_POS_MFE_FLOW":
            if (
                time_threshold is not None
                and point.at >= time_threshold
                and not positive_mfe_seen
                and _flow_weak(snapshot)
            ):
                exit_index = index
                exit_reason = f"TIME_{variant.minutes}M_FLOW_STOP"
                trigger_snapshot = snapshot
                break

        elif variant.kind == "TIME_COST_ZONE":
            if (
                time_threshold is not None
                and point.at >= time_threshold
                and point.projected_net_pnl <= 0
            ):
                exit_index = index
                exit_reason = f"TIME_{variant.minutes}M_COST_ZONE"
                break

        # Hybrid replaces the ordinary -5000 stop with its own hard/soft stop.
        # All non-stop LIVE-like exits remain unchanged.
        if variant.kind.startswith("HYBRID_"):
            baseline = _baseline_reason(
                projected_net_pnl=float("inf"),
                current_return=current,
                peak_return=peak_return,
                worst_return=worst_return,
                reversal=bool(point.reversal),
                hard_exit_reached=point.at >= hard_exit,
            )
        else:
            baseline = _baseline_reason(
                projected_net_pnl=float(point.projected_net_pnl),
                current_return=current,
                peak_return=peak_return,
                worst_return=worst_return,
                reversal=bool(point.reversal),
                hard_exit_reached=point.at >= hard_exit,
            )

        if baseline is not None:
            exit_index = index
            exit_reason = baseline
            break

    if exit_index is None or exit_reason is None:
        return {
            "variant_id": variant.variant_id,
            "scorable": False,
            "reason": "NO_EXIT",
        }

    point = path.points[exit_index]

    gross, commission, sell_tax, net_pnl = _projected_net_pnl(
        "LONG",
        path.entry_price,
        point.exit_price,
        path.quantity,
    )

    held_points = path.points[: exit_index + 1]
    later = path.points[exit_index + 1:]

    full_best = max(
        path.points,
        key=lambda item: item.projected_net_pnl,
    )
    full_worst = min(
        path.points,
        key=lambda item: item.projected_net_pnl,
    )

    post_exit_best = max(
        (p.projected_net_pnl for p in later),
        default=None,
    )

    result = {
        "variant_id": variant.variant_id,
        "kind": variant.kind,
        "soft_value": variant.soft_value,
        "hard_value": variant.hard_value,
        "minutes": variant.minutes,
        "scorable": True,
        "session_date": path.session_date,
        "stock_id": path.stock_id,
        "stock_name": path.stock_name,
        "decision_time": path.decision_time.isoformat(),
        "entry_price": path.entry_price,
        "quantity": path.quantity,
        "notional_used": path.notional_used,
        "exit_time": point.at.isoformat(),
        "exit_price": point.exit_price,
        "exit_reason": exit_reason,
        "gross_pnl": gross,
        "commission": commission,
        "sell_tax": sell_tax,
        "net_pnl": net_pnl,
        "held_mae_twd": min(
            p.projected_net_pnl for p in held_points
        ),
        "held_mfe_twd": max(
            p.projected_net_pnl for p in held_points
        ),
        "full_path_best_net_twd": full_best.projected_net_pnl,
        "full_path_worst_net_twd": full_worst.projected_net_pnl,
        "post_exit_best_net_twd": post_exit_best,
        "false_exit_positive_net": (
            post_exit_best is not None
            and post_exit_best > 0
        ),
        "minutes_held": round(
            (point.at - path.decision_time).total_seconds() / 60,
            4,
        ),
        "flow_weak_at_exit": _flow_weak(trigger_snapshot),
    }

    if trigger_snapshot is not None:
        result["flow_snapshot"] = {
            "normalized_delta_60":
                trigger_snapshot.normalized_delta_60,
            "large_trade_delta_60":
                trigger_snapshot.large_trade_delta_60,
            "book_imbalance":
                trigger_snapshot.book_imbalance,
            "negative_components":
                _negative_components(trigger_snapshot),
        }

    return result


def _profit_factor(rows: list[dict]) -> float | None:
    gains = sum(
        max(0.0, float(row["net_pnl"]))
        for row in rows
    )
    losses = sum(
        min(0.0, float(row["net_pnl"]))
        for row in rows
    )

    return (
        None
        if losses == 0
        else round(gains / abs(losses), 6)
    )


def build_hybrid_report(
    session_runs: dict[str, list[Path]],
    capital: int = 190000,
) -> dict:
    results = {
        variant.variant_id: []
        for variant in VARIANTS
    }
    diagnostics = []

    for _date, run_dirs in sorted(session_runs.items()):
        stocks, coverage = load_session(run_dirs)

        path, diag = build_trade_path(
            stocks,
            coverage,
            capital,
        )
        diagnostics.append(diag)

        if path is None:
            continue

        snapshots = build_flow_snapshots(
            stocks[path.stock_id],
            path,
        )

        for variant in VARIANTS:
            row = simulate_hybrid_variant(
                path,
                snapshots,
                variant,
            )

            if row.get("scorable"):
                results[variant.variant_id].append(row)

    summaries = []

    for variant in VARIANTS:
        rows = results[variant.variant_id]
        pnl = [float(row["net_pnl"]) for row in rows]

        summaries.append({
            "variant_id": variant.variant_id,
            "kind": variant.kind,
            "trade_count": len(rows),
            "winning_trades": sum(value > 0 for value in pnl),
            "win_rate": (
                round(
                    sum(value > 0 for value in pnl) / len(pnl),
                    6,
                )
                if pnl else None
            ),
            "net_pnl_twd": round(sum(pnl), 2),
            "average_trade_twd": (
                round(sum(pnl) / len(pnl), 2)
                if pnl else None
            ),
            "median_trade_twd": (
                round(float(median(pnl)), 2)
                if pnl else None
            ),
            "profit_factor": _profit_factor(rows),
            "maximum_single_trade_loss_twd": (
                min(pnl) if pnl else None
            ),
            "average_minutes_held": (
                round(
                    sum(row["minutes_held"] for row in rows)
                    / len(rows),
                    4,
                )
                if rows else None
            ),
            "false_exit_positive_net_count": sum(
                bool(row["false_exit_positive_net"])
                for row in rows
            ),
            "average_held_mae_twd": (
                round(
                    sum(row["held_mae_twd"] for row in rows)
                    / len(rows),
                    2,
                )
                if rows else None
            ),
            "average_held_mfe_twd": (
                round(
                    sum(row["held_mfe_twd"] for row in rows)
                    / len(rows),
                    2,
                )
                if rows else None
            ),
        })

    return {
        "analysis_id":
            "YUANTA_LONG_ONLY_TIME_AND_HYBRID_EXIT_SWEEP_V0_1",
        "capital_twd": capital,
        "entry_logic":
            "PRODUCTION_LIVE_DIRECTION_ENGINE_LONG_ONLY_UNCHANGED",
        "time_stop_definitions": {
            "3m_5m":
                "no positive net MFE observed by threshold and flow 2-of-3 negative",
            "10m":
                "projected net PnL still <= 0 at or after ten-minute threshold",
        },
        "hybrid_flow_definition":
            "2-of-3 negative: volume_delta_60, large_trade_delta_60, book_imbalance",
        "variants": [
            {
                "variant_id": v.variant_id,
                "kind": v.kind,
                "soft_value": v.soft_value,
                "hard_value": v.hard_value,
                "minutes": v.minutes,
            }
            for v in VARIANTS
        ],
        "diagnostics": diagnostics,
        "summaries": summaries,
        "results": results,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_order_calls": 0,
        "interpretation": (
            "RESEARCH_ONLY_TIME_AND_HYBRID_EXIT_COMPARISON; "
            "NO_LIVE_PARAMETER_CHANGE"
        ),
    }
