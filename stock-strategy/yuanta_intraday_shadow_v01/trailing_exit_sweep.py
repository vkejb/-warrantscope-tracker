"""Causal trailing-profit parameter research for LIVE-parity long entries.

Only trailing activation/drawdown vary.  All other LIVE-like exit rules remain
unchanged: -5000 TWD net stop, loss recovery, signal reversal and 13:20 exit.

Research-only.  No broker imports or order submission.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import median

from .direction_follow_backtest import SPEC, _projected_net_pnl, load_session
from .exit_parameter_sweep import TradePath, build_trade_path


@dataclass(frozen=True)
class TrailingVariant:
    variant_id: str
    activation: float
    drawdown: float


TRAILING_VARIANTS = (
    TrailingVariant("TRAIL_1_5_0_6", 0.015, 0.006),
    TrailingVariant("TRAIL_1_5_0_8", 0.015, 0.008),
    TrailingVariant("TRAIL_1_5_1_0", 0.015, 0.010),
    TrailingVariant("TRAIL_2_0_2_0", 0.020, 0.020),
)

ORIGINAL_VARIANT_ID = "TRAIL_2_0_2_0"


def simulate_trailing_variant(
    path: TradePath,
    variant: TrailingVariant,
) -> dict:
    peak_return = 0.0
    worst_return = 0.0

    trailing_armed_at = None
    exit_index = None
    exit_reason = None

    peak_net_before_exit = None
    peak_net_before_exit_at = None

    hard_hour, hard_minute = map(int, SPEC["hard_exit_time"].split(":"))
    hard_exit = path.decision_time.replace(
        hour=hard_hour,
        minute=hard_minute,
        second=0,
        microsecond=0,
    )

    for index, point in enumerate(path.points):
        current = point.current_return

        peak_return = max(peak_return, current)
        worst_return = min(worst_return, current)

        if (
            peak_net_before_exit is None
            or point.projected_net_pnl > peak_net_before_exit
        ):
            peak_net_before_exit = point.projected_net_pnl
            peak_net_before_exit_at = point.at

        if (
            trailing_armed_at is None
            and peak_return >= variant.activation
        ):
            trailing_armed_at = point.at

        if point.projected_net_pnl <= -float(SPEC["stop_loss_net_twd"]):
            exit_index = index
            exit_reason = "STOP_LOSS"
        elif (
            peak_return >= variant.activation
            and current <= peak_return - variant.drawdown
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

    full_best = max(
        path.points,
        key=lambda item: item.projected_net_pnl,
    )
    full_worst = min(
        path.points,
        key=lambda item: item.projected_net_pnl,
    )

    later = path.points[exit_index + 1:]

    post_exit_new_high = (
        any(
            p.projected_net_pnl > float(peak_net_before_exit)
            for p in later
        )
        if peak_net_before_exit is not None
        else False
    )

    giveback = (
        float(peak_net_before_exit) - net_pnl
        if peak_net_before_exit is not None
        else 0.0
    )

    opportunity_gap = full_best.projected_net_pnl - net_pnl

    capture_ratio = (
        net_pnl / full_best.projected_net_pnl
        if full_best.projected_net_pnl > 0
        else None
    )

    time_from_peak = (
        (point.at - peak_net_before_exit_at).total_seconds()
        if peak_net_before_exit_at is not None
        else None
    )

    return {
        "variant_id": variant.variant_id,
        "activation": variant.activation,
        "drawdown": variant.drawdown,
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
        "trailing_armed": trailing_armed_at is not None,
        "trailing_armed_at": (
            trailing_armed_at.isoformat()
            if trailing_armed_at is not None
            else None
        ),
        "trailing_triggered": exit_reason == "TRAILING_PROFIT",
        "highest_net_before_exit_twd": peak_net_before_exit,
        "highest_net_before_exit_time": (
            peak_net_before_exit_at.isoformat()
            if peak_net_before_exit_at is not None
            else None
        ),
        "giveback_from_peak_to_exit_twd": round(giveback, 2),
        "time_from_peak_to_exit_seconds": time_from_peak,
        "full_path_best_net_twd": full_best.projected_net_pnl,
        "full_path_best_time": full_best.at.isoformat(),
        "full_path_worst_net_twd": full_worst.projected_net_pnl,
        "opportunity_gap_to_full_path_peak_twd":
            round(opportunity_gap, 2),
        "high_point_capture_ratio": (
            round(capture_ratio, 6)
            if capture_ratio is not None
            else None
        ),
        "post_exit_new_high": post_exit_new_high,
    }


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


def build_trailing_report(
    session_runs: dict[str, list[Path]],
    capital: int = 190000,
) -> dict:
    results = {
        variant.variant_id: []
        for variant in TRAILING_VARIANTS
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

        session_rows = {}

        for variant in TRAILING_VARIANTS:
            row = simulate_trailing_variant(path, variant)
            session_rows[variant.variant_id] = row

        original = session_rows[ORIGINAL_VARIANT_ID]
        original_net = (
            float(original["net_pnl"])
            if original.get("scorable")
            else None
        )

        for variant in TRAILING_VARIANTS:
            row = session_rows[variant.variant_id]

            if not row.get("scorable"):
                continue

            row["original_trailing_net_pnl"] = original_net
            row["delta_vs_original_twd"] = (
                round(float(row["net_pnl"]) - original_net, 2)
                if original_net is not None
                else None
            )

            results[variant.variant_id].append(row)

    summaries = []

    for variant in TRAILING_VARIANTS:
        rows = results[variant.variant_id]
        pnl = [float(row["net_pnl"]) for row in rows]

        summaries.append({
            "variant_id": variant.variant_id,
            "activation": variant.activation,
            "drawdown": variant.drawdown,
            "trade_count": len(rows),
            "trailing_armed_count": sum(
                bool(row["trailing_armed"])
                for row in rows
            ),
            "trailing_trigger_count": sum(
                bool(row["trailing_triggered"])
                for row in rows
            ),
            "winning_trades": sum(value > 0 for value in pnl),
            "win_rate": (
                round(sum(value > 0 for value in pnl) / len(pnl), 6)
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
                min(pnl)
                if pnl else None
            ),
            "maximum_giveback_twd": (
                max(
                    (
                        float(row["giveback_from_peak_to_exit_twd"])
                        for row in rows
                    ),
                    default=None,
                )
            ),
            "average_giveback_twd": (
                round(
                    sum(
                        float(row["giveback_from_peak_to_exit_twd"])
                        for row in rows
                    ) / len(rows),
                    2,
                )
                if rows else None
            ),
            "average_opportunity_gap_to_full_peak_twd": (
                round(
                    sum(
                        float(
                            row[
                                "opportunity_gap_to_full_path_peak_twd"
                            ]
                        )
                        for row in rows
                    ) / len(rows),
                    2,
                )
                if rows else None
            ),
            "post_exit_new_high_count": sum(
                bool(row["post_exit_new_high"])
                for row in rows
            ),
            "average_delta_vs_original_twd": (
                round(
                    sum(
                        float(row["delta_vs_original_twd"])
                        for row in rows
                        if row["delta_vs_original_twd"] is not None
                    ) / len(rows),
                    2,
                )
                if rows else None
            ),
        })

    return {
        "analysis_id": "YUANTA_LONG_ONLY_TRAILING_EXIT_SWEEP_V0_1",
        "capital_twd": capital,
        "entry_logic":
            "PRODUCTION_LIVE_DIRECTION_ENGINE_LONG_ONLY_UNCHANGED",
        "variants": [
            {
                "variant_id": variant.variant_id,
                "activation": variant.activation,
                "drawdown": variant.drawdown,
            }
            for variant in TRAILING_VARIANTS
        ],
        "unchanged_exit_rules": {
            "stop_loss_net_twd": SPEC["stop_loss_net_twd"],
            "loss_recovery_required":
                SPEC["loss_recovery_required"],
            "reversal_confirmations":
                SPEC["reversal_confirmations"],
            "hard_exit_time": SPEC["hard_exit_time"],
        },
        "original_variant_id": ORIGINAL_VARIANT_ID,
        "diagnostics": diagnostics,
        "summaries": summaries,
        "results": results,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_order_calls": 0,
        "interpretation": (
            "RESEARCH_ONLY_TRAILING_PARAMETER_COMPARISON; "
            "NO_LIVE_PARAMETER_CHANGE"
        ),
    }
