"""Causal Flow x Trailing exit research.

Entry logic remains the production LIVE long-only engine.

Research-only:
- no broker imports
- no order submission
- no LIVE parameter mutation

Flow is evaluated on the existing 30-second decision clock.  Once trailing is
armed, causal flow deterioration may tighten or trigger the exit.
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
class FlowTrailingVariant:
    variant_id: str
    kind: str
    activation: float = 0.015
    base_drawdown: float = 0.008


VARIANTS = (
    FlowTrailingVariant(
        "TRAIL15_FLOW_SELL_GT_BUY",
        "FLOW_SELL_GT_BUY",
    ),
    FlowTrailingVariant(
        "TRAIL15_FLOW_VOLUME_NEG",
        "FLOW_VOLUME_NEG",
    ),
    FlowTrailingVariant(
        "TRAIL15_FLOW_LARGE_NEG",
        "FLOW_LARGE_NEG",
    ),
    FlowTrailingVariant(
        "TRAIL15_FLOW_2OF3_NEG",
        "FLOW_2OF3_NEG",
    ),
    FlowTrailingVariant(
        "TRAIL15_DYNAMIC_1_0_0_8_0_5",
        "DYNAMIC_TRAILING",
    ),
)


def _latest_causal_snapshot(
    snapshots: tuple[FlowSnapshot, ...],
    point_time,
    index: int,
    latest: FlowSnapshot | None,
) -> tuple[int, FlowSnapshot | None]:
    while (
        index < len(snapshots)
        and snapshots[index].at <= point_time
    ):
        latest = snapshots[index]
        index += 1

    if latest is None:
        return index, None

    maximum_age = timedelta(
        seconds=int(SPEC["decision_interval_seconds"])
    )

    if point_time - latest.at > maximum_age:
        return index, None

    return index, latest


def _flow_class(snapshot: FlowSnapshot | None) -> str:
    if snapshot is None:
        return "UNKNOWN"

    negative = _negative_components(snapshot)

    if negative >= 2:
        return "WEAK"

    if (
        snapshot.normalized_delta_60 > 0
        and snapshot.large_trade_delta_60 > 0
        and (
            snapshot.book_imbalance is None
            or snapshot.book_imbalance >= 0
        )
    ):
        return "STRONG"

    return "NEUTRAL"


def _flow_trigger(
    variant: FlowTrailingVariant,
    snapshot: FlowSnapshot | None,
) -> bool:
    if snapshot is None:
        return False

    if variant.kind == "FLOW_SELL_GT_BUY":
        return snapshot.sell_qty_30 > snapshot.buy_qty_30

    if variant.kind == "FLOW_VOLUME_NEG":
        return snapshot.normalized_delta_60 < 0

    if variant.kind == "FLOW_LARGE_NEG":
        return snapshot.large_trade_delta_60 < 0

    if variant.kind == "FLOW_2OF3_NEG":
        return _negative_components(snapshot) >= 2

    return False


def _dynamic_drawdown(snapshot: FlowSnapshot | None) -> float:
    classification = _flow_class(snapshot)

    if classification == "STRONG":
        return 0.010

    if classification == "WEAK":
        return 0.005

    return 0.008


def simulate_flow_trailing_variant(
    path: TradePath,
    snapshots: tuple[FlowSnapshot, ...],
    variant: FlowTrailingVariant,
) -> dict:
    peak_return = 0.0
    worst_return = 0.0
    trailing_armed_at = None

    snapshot_index = 0
    latest_snapshot = None

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

    exit_index = None
    exit_reason = None
    exit_snapshot = None
    active_drawdown = variant.base_drawdown

    for index, point in enumerate(path.points):
        current = float(point.current_return)
        peak_return = max(peak_return, current)
        worst_return = min(worst_return, current)

        snapshot_index, snapshot = _latest_causal_snapshot(
            snapshots,
            point.at,
            snapshot_index,
            latest_snapshot,
        )

        if snapshot is not None:
            latest_snapshot = snapshot

        if (
            trailing_armed_at is None
            and peak_return >= variant.activation
        ):
            trailing_armed_at = point.at

        trailing_armed = trailing_armed_at is not None

        if point.projected_net_pnl <= -float(SPEC["stop_loss_net_twd"]):
            exit_index = index
            exit_reason = "STOP_LOSS"

        elif (
            trailing_armed
            and variant.kind != "DYNAMIC_TRAILING"
            and _flow_trigger(variant, snapshot)
        ):
            exit_index = index
            exit_reason = f"FLOW_TRAILING:{variant.kind}"
            exit_snapshot = snapshot

        elif trailing_armed and variant.kind == "DYNAMIC_TRAILING":
            active_drawdown = _dynamic_drawdown(snapshot)

            if current <= peak_return - active_drawdown:
                exit_index = index
                exit_reason = "DYNAMIC_TRAILING"
                exit_snapshot = snapshot

        elif (
            trailing_armed
            and variant.kind != "DYNAMIC_TRAILING"
            and current <= peak_return - variant.base_drawdown
        ):
            exit_index = index
            exit_reason = "TRAILING_PROFIT"

        elif (
            worst_return < 0
            and current > 0
            and current - worst_return
            >= float(SPEC["loss_recovery_required"])
        ):
            exit_index = index
            exit_reason = "LOSS_RECOVERY_TO_PROFIT"

        elif point.reversal:
            exit_index = index
            exit_reason = "SIGNAL_REVERSAL"

        elif point.at >= hard_exit:
            exit_index = index
            exit_reason = "HARD_EXIT"

        if exit_index is not None:
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

    held = path.points[: exit_index + 1]
    later = path.points[exit_index + 1:]

    peak_before_exit = max(
        held,
        key=lambda item: item.projected_net_pnl,
    )

    full_best = max(
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
        "activation": variant.activation,
        "base_drawdown": variant.base_drawdown,
        "scorable": True,
        "session_date": path.session_date,
        "stock_id": path.stock_id,
        "stock_name": path.stock_name,
        "decision_time": path.decision_time.isoformat(),
        "entry_price": path.entry_price,
        "quantity": path.quantity,
        "notional_used": path.notional_used,
        "trailing_armed": trailing_armed_at is not None,
        "trailing_armed_at": (
            trailing_armed_at.isoformat()
            if trailing_armed_at is not None
            else None
        ),
        "exit_time": point.at.isoformat(),
        "exit_price": point.exit_price,
        "exit_reason": exit_reason,
        "net_pnl": net_pnl,
        "gross_pnl": gross,
        "commission": commission,
        "sell_tax": sell_tax,
        "held_mae_twd": min(
            p.projected_net_pnl for p in held
        ),
        "held_mfe_twd": max(
            p.projected_net_pnl for p in held
        ),
        "peak_before_exit_twd":
            peak_before_exit.projected_net_pnl,
        "giveback_from_peak_twd": round(
            peak_before_exit.projected_net_pnl - net_pnl,
            2,
        ),
        "full_path_best_net_twd":
            full_best.projected_net_pnl,
        "opportunity_gap_to_full_peak_twd": round(
            full_best.projected_net_pnl - net_pnl,
            2,
        ),
        "post_exit_best_net_twd": post_exit_best,
        "post_exit_new_high": (
            post_exit_best is not None
            and post_exit_best
            > peak_before_exit.projected_net_pnl
        ),
        "false_exit_positive_net": (
            post_exit_best is not None
            and post_exit_best > 0
        ),
        "active_drawdown_at_exit": active_drawdown,
        "flow_class_at_exit": _flow_class(exit_snapshot),
    }

    if exit_snapshot is not None:
        result["flow_snapshot"] = {
            "buy_qty_30": exit_snapshot.buy_qty_30,
            "sell_qty_30": exit_snapshot.sell_qty_30,
            "normalized_delta_60":
                exit_snapshot.normalized_delta_60,
            "large_trade_delta_60":
                exit_snapshot.large_trade_delta_60,
            "book_imbalance":
                exit_snapshot.book_imbalance,
            "negative_components":
                _negative_components(exit_snapshot),
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

    if losses == 0:
        return None

    return round(gains / abs(losses), 6)


def build_flow_trailing_report(
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
            row = simulate_flow_trailing_variant(
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
            "trailing_armed_count": sum(
                bool(row["trailing_armed"])
                for row in rows
            ),
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
            "average_giveback_twd": (
                round(
                    sum(
                        float(row["giveback_from_peak_twd"])
                        for row in rows
                    ) / len(rows),
                    2,
                )
                if rows else None
            ),
            "average_opportunity_gap_twd": (
                round(
                    sum(
                        float(
                            row[
                                "opportunity_gap_to_full_peak_twd"
                            ]
                        )
                        for row in rows
                    ) / len(rows),
                    2,
                )
                if rows else None
            ),
            "false_exit_positive_net_count": sum(
                bool(row["false_exit_positive_net"])
                for row in rows
            ),
            "post_exit_new_high_count": sum(
                bool(row["post_exit_new_high"])
                for row in rows
            ),
        })

    return {
        "analysis_id":
            "YUANTA_LONG_ONLY_FLOW_TRAILING_SWEEP_V0_1",
        "capital_twd": capital,
        "entry_logic":
            "PRODUCTION_LIVE_DIRECTION_ENGINE_LONG_ONLY_UNCHANGED",
        "activation": 0.015,
        "research_variants": [
            {
                "variant_id": v.variant_id,
                "kind": v.kind,
                "activation": v.activation,
                "base_drawdown": v.base_drawdown,
            }
            for v in VARIANTS
        ],
        "dynamic_drawdown_definition": {
            "strong": 0.010,
            "neutral": 0.008,
            "weak": 0.005,
        },
        "flow_components": [
            "normalized_delta_60",
            "large_trade_delta_60",
            "book_imbalance",
        ],
        "unchanged_rules": {
            "stop_loss_net_twd":
                SPEC["stop_loss_net_twd"],
            "loss_recovery_required":
                SPEC["loss_recovery_required"],
            "reversal_confirmations":
                SPEC["reversal_confirmations"],
            "hard_exit_time":
                SPEC["hard_exit_time"],
        },
        "diagnostics": diagnostics,
        "summaries": summaries,
        "results": results,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_order_calls": 0,
        "interpretation": (
            "RESEARCH_ONLY_FLOW_X_TRAILING_COMPARISON; "
            "NO_LIVE_PARAMETER_CHANGE"
        ),
    }
