"""Exit-parameter research on top of the production long-only entry engine.

Entry selection is frozen: historical data is replayed through
LiveDirectionEngine with allow_short=False.  Only exit thresholds vary.

This module is research-only and never imports or calls the broker adapter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
import math

from yuanta_live_runtime_v01.strategy import LiveDirectionEngine, ManagedPosition

from .direction_follow_backtest import (
    SPEC,
    _decision_times,
    _projected_net_pnl,
    load_session,
)
from .live_parity_backtest import (
    EXECUTION_MODEL,
    _parse_stamp,
    _record_book,
    _record_tick,
    _feed_until,
    _validate_full_session_coverage,
)


FIXED_STOP_TWD = (1500, 2000, 2500, 3000, 4000, 5000)
PERCENT_STOP_RATES = (0.008, 0.010, 0.012, 0.015, 0.020, 0.025)


@dataclass(frozen=True)
class PathPoint:
    at: datetime
    exit_price: float
    projected_net_pnl: float
    current_return: float
    reversal: bool


@dataclass(frozen=True)
class TradePath:
    session_date: str
    stock_id: str
    stock_name: str
    decision_time: datetime
    entry_price: float
    quantity: int
    notional_used: float
    points: tuple[PathPoint, ...]


@dataclass(frozen=True)
class ExitVariant:
    variant_id: str
    stop_type: str
    stop_value: float


def variants() -> tuple[ExitVariant, ...]:
    result = [
        ExitVariant(f"FIXED_{value}", "FIXED_TWD", float(value))
        for value in FIXED_STOP_TWD
    ]
    result.extend(
        ExitVariant(
            f"PERCENT_{rate * 100:.1f}",
            "PERCENT_NET_RETURN",
            float(rate),
        )
        for rate in PERCENT_STOP_RATES
    )
    return tuple(result)


def build_trade_path(
    stocks: dict[str, dict],
    coverage: dict,
    capital: int = 190000,
    *,
    exit_quote_staleness_seconds: float = 3.0,
) -> tuple[TradePath | None, dict]:
    _validate_full_session_coverage(coverage)

    session_date = str(coverage["session_date"])
    metadata = {
        symbol: str(data.get("meta", {}).get("stock_name", symbol))
        for symbol, data in stocks.items()
    }

    engine = LiveDirectionEngine(metadata, capital_twd=capital)
    tick_indexes = {symbol: 0 for symbol in stocks}
    book_indexes = {symbol: 0 for symbol in stocks}

    candidate = None

    for decision in _decision_times(
        session_date,
        SPEC["entry_start"],
        SPEC["last_entry_time"],
    ):
        _feed_until(
            engine,
            stocks,
            tick_indexes,
            book_indexes,
            decision,
        )
        candidate = engine.choose_entry(decision, allow_short=False)
        if candidate is not None:
            break

    diagnostics = {
        "session_date": session_date,
        "long_only": True,
        "execution_model": EXECUTION_MODEL,
    }

    if candidate is None:
        diagnostics["reason"] = "NO_LONG_ENTRY"
        return None, diagnostics

    position = ManagedPosition(
        stock_id=candidate.stock_id,
        stock_name=candidate.stock_name,
        side="LONG",
        quantity=candidate.quantity,
        entry_price=candidate.entry_price,
        entry_order_id="BACKTEST_PROXY",
        entry_time=candidate.decision_time,
    )

    data = stocks[candidate.stock_id]

    events: list[tuple[datetime, int, str, dict | None]] = []

    for row in data["ticks"][tick_indexes[candidate.stock_id]:]:
        events.append((row["time"], 0, "tick", row))

    for row in data["books"][book_indexes[candidate.stock_id]:]:
        events.append((row["time"], 1, "book", row))

    end_time = _parse_stamp(coverage["ended_at_taipei"])
    interval = timedelta(seconds=int(SPEC["decision_interval_seconds"]))
    next_decision = candidate.decision_time + interval

    while next_decision <= end_time:
        events.append((next_decision, 2, "decision", None))
        next_decision += interval

    events.sort(key=lambda item: (item[0], item[1]))

    hard_hour, hard_minute = map(int, SPEC["hard_exit_time"].split(":"))
    hard_exit = candidate.decision_time.replace(
        hour=hard_hour,
        minute=hard_minute,
        second=0,
        microsecond=0,
    )

    points: list[PathPoint] = []

    for at, _priority, kind, row in events:
        reversal = False

        if kind == "tick":
            assert row is not None
            _record_tick(engine, candidate.stock_id, row)
        elif kind == "book":
            assert row is not None
            _record_book(engine, candidate.stock_id, row)
        else:
            reversal = engine.opposite_signal(position, at)
            engine.last_decision = at

        quote = engine.safe_exit_quote(
            position,
            at,
            max_age_seconds=exit_quote_staleness_seconds,
        )
        if quote is None:
            continue

        projected = engine.projected_net(position, quote.price)
        denominator = candidate.entry_price * candidate.quantity
        current_return = projected / denominator if denominator else 0.0

        points.append(
            PathPoint(
                at=at,
                exit_price=quote.price,
                projected_net_pnl=projected,
                current_return=current_return,
                reversal=reversal,
            )
        )

        # Counterfactual path only needs to extend to the first executable
        # hard-exit opportunity at/after 13:20.
        if at >= hard_exit:
            break

    diagnostics.update({
        "stock_id": candidate.stock_id,
        "stock_name": candidate.stock_name,
        "decision_time": candidate.decision_time.isoformat(),
        "entry_price": candidate.entry_price,
        "quantity": candidate.quantity,
        "path_points": len(points),
    })

    if not points:
        diagnostics["reason"] = "NO_EXECUTABLE_EXIT_PATH"
        return None, diagnostics

    return TradePath(
        session_date=session_date,
        stock_id=candidate.stock_id,
        stock_name=candidate.stock_name,
        decision_time=candidate.decision_time,
        entry_price=candidate.entry_price,
        quantity=candidate.quantity,
        notional_used=candidate.entry_price * candidate.quantity,
        points=tuple(points),
    ), diagnostics


def simulate_variant(path: TradePath, variant: ExitVariant) -> dict:
    peak_return = 0.0
    worst_return = 0.0
    exit_index = None
    exit_reason = None

    hard_hour, hard_minute = map(int, SPEC["hard_exit_time"].split(":"))
    hard_exit = path.decision_time.replace(
        hour=hard_hour,
        minute=hard_minute,
        second=0,
        microsecond=0,
    )

    mae = 0.0
    mfe = 0.0

    for index, point in enumerate(path.points):
        current = point.current_return
        peak_return = max(peak_return, current)
        worst_return = min(worst_return, current)
        mae = min(mae, current)
        mfe = max(mfe, current)

        stopped = False

        if variant.stop_type == "FIXED_TWD":
            stopped = point.projected_net_pnl <= -variant.stop_value
        elif variant.stop_type == "PERCENT_NET_RETURN":
            stopped = current <= -variant.stop_value
        else:
            raise ValueError(f"unsupported stop type: {variant.stop_type}")

        if stopped:
            exit_index = index
            exit_reason = (
                "STOP_LOSS_FIXED"
                if variant.stop_type == "FIXED_TWD"
                else "STOP_LOSS_PERCENT"
            )
        elif (
            peak_return >= float(SPEC["trailing_profit_activation"])
            and current
            <= peak_return - float(SPEC["trailing_profit_drawdown"])
        ):
            exit_index = index
            exit_reason = "TRAILING_PROFIT"
        elif (
            worst_return < 0
            and current > 0
            and current - worst_return >= float(SPEC["loss_recovery_required"])
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
            "stop_type": variant.stop_type,
            "stop_value": variant.stop_value,
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

    is_stop = exit_reason.startswith("STOP_LOSS")
    later = path.points[exit_index + 1:] if is_stop else ()

    recovered_positive_net = (
        any(p.projected_net_pnl > 0 for p in later)
        if is_stop
        else False
    )
    recovered_entry_price = (
        any(p.exit_price >= path.entry_price for p in later)
        if is_stop
        else False
    )

    full_path_mae = min(
        (p.current_return for p in path.points),
        default=0.0,
    )
    full_path_mfe = max(
        (p.current_return for p in path.points),
        default=0.0,
    )
    post_exit_best_net = (
        max((p.projected_net_pnl for p in later), default=None)
        if is_stop
        else None
    )
    post_exit_best_return = (
        max((p.current_return for p in later), default=None)
        if is_stop
        else None
    )

    return {
        "variant_id": variant.variant_id,
        "stop_type": variant.stop_type,
        "stop_value": variant.stop_value,
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
        "held_mae_net_return": mae,
        "held_mfe_net_return": mfe,
        "held_mae_twd": round(mae * path.notional_used, 2),
        "held_mfe_twd": round(mfe * path.notional_used, 2),
        "full_path_mae_net_return": full_path_mae,
        "full_path_mfe_net_return": full_path_mfe,
        "full_path_mae_twd": round(full_path_mae * path.notional_used, 2),
        "full_path_mfe_twd": round(full_path_mfe * path.notional_used, 2),
        "post_exit_best_net_pnl": post_exit_best_net,
        "post_exit_best_net_return": post_exit_best_return,
        "stopped_out": is_stop,
        "recovered_positive_net_after_stop": recovered_positive_net,
        "recovered_entry_price_after_stop": recovered_entry_price,
    }


def _profit_factor(results: list[dict]) -> float | None:
    gains = sum(max(0.0, float(row["net_pnl"])) for row in results)
    losses = sum(min(0.0, float(row["net_pnl"])) for row in results)
    if losses == 0:
        return None
    return round(gains / abs(losses), 6)


def build_sweep_report(
    session_runs: dict[str, list[Path]],
    capital: int = 190000,
) -> dict:
    all_results: dict[str, list[dict]] = {
        variant.variant_id: [] for variant in variants()
    }
    diagnostics = []

    for _date, run_dirs in sorted(session_runs.items()):
        stocks, coverage = load_session(run_dirs)
        path, diag = build_trade_path(stocks, coverage, capital)
        diagnostics.append(diag)

        if path is None:
            continue

        for variant in variants():
            result = simulate_variant(path, variant)
            if result.get("scorable"):
                all_results[variant.variant_id].append(result)

    summaries = []

    for variant in variants():
        rows = all_results[variant.variant_id]
        stopped = [row for row in rows if row["stopped_out"]]
        false_positive = [
            row for row in stopped
            if row["recovered_positive_net_after_stop"]
        ]
        entry_recovered = [
            row for row in stopped
            if row["recovered_entry_price_after_stop"]
        ]

        summaries.append({
            "variant_id": variant.variant_id,
            "stop_type": variant.stop_type,
            "stop_value": variant.stop_value,
            "trade_count": len(rows),
            "winning_trades": sum(row["net_pnl"] > 0 for row in rows),
            "win_rate": (
                round(sum(row["net_pnl"] > 0 for row in rows) / len(rows), 6)
                if rows else None
            ),
            "net_pnl_twd": round(sum(row["net_pnl"] for row in rows), 2),
            "average_trade_twd": (
                round(sum(row["net_pnl"] for row in rows) / len(rows), 2)
                if rows else None
            ),
            "average_losing_trade_twd": (
                round(
                    sum(row["net_pnl"] for row in rows if row["net_pnl"] < 0)
                    / sum(row["net_pnl"] < 0 for row in rows),
                    2,
                )
                if any(row["net_pnl"] < 0 for row in rows)
                else None
            ),
            "maximum_single_trade_loss_twd": (
                min((row["net_pnl"] for row in rows), default=None)
            ),
            "profit_factor": _profit_factor(rows),
            "average_held_mae_twd": (
                round(sum(row["held_mae_twd"] for row in rows) / len(rows), 2)
                if rows else None
            ),
            "average_held_mfe_twd": (
                round(sum(row["held_mfe_twd"] for row in rows) / len(rows), 2)
                if rows else None
            ),
            "average_full_path_mae_twd": (
                round(sum(row["full_path_mae_twd"] for row in rows) / len(rows), 2)
                if rows else None
            ),
            "average_full_path_mfe_twd": (
                round(sum(row["full_path_mfe_twd"] for row in rows) / len(rows), 2)
                if rows else None
            ),
            "stop_loss_count": len(stopped),
            "false_stop_positive_net_count": len(false_positive),
            "false_stop_positive_net_rate": (
                round(len(false_positive) / len(stopped), 6)
                if stopped else None
            ),
            "entry_price_recovery_after_stop_count": len(entry_recovered),
            "entry_price_recovery_after_stop_rate": (
                round(len(entry_recovered) / len(stopped), 6)
                if stopped else None
            ),
        })

    return {
        "analysis_id": "YUANTA_LONG_ONLY_EXIT_STOP_SWEEP_V0_1",
        "capital_twd": capital,
        "entry_logic": "PRODUCTION_LIVE_DIRECTION_ENGINE_LONG_ONLY_UNCHANGED",
        "execution_model": EXECUTION_MODEL,
        "fixed_stop_twd": list(FIXED_STOP_TWD),
        "percent_stop_rates": list(PERCENT_STOP_RATES),
        "unchanged_exit_rules": {
            "trailing_profit_activation": SPEC["trailing_profit_activation"],
            "trailing_profit_drawdown": SPEC["trailing_profit_drawdown"],
            "loss_recovery_required": SPEC["loss_recovery_required"],
            "reversal_confirmations": SPEC["reversal_confirmations"],
            "hard_exit_time": SPEC["hard_exit_time"],
        },
        "diagnostics": diagnostics,
        "summaries": summaries,
        "results": all_results,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_order_calls": 0,
        "interpretation": (
            "RESEARCH_ONLY_EXIT_PARAMETER_COMPARISON; "
            "DO_NOT_CHANGE_LIVE_THRESHOLDS_FROM SMALL SAMPLE RESULTS"
        ),
    }
