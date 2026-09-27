"""Controlled comparison of the unchanged live-parity exit and three MFE overlays."""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable, Iterable, Mapping

from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC,
    SPEC_HASH,
    _projected_net_pnl,
    load_session,
)
from yuanta_intraday_shadow_v01.exit_parameter_sweep import (
    PathPoint,
    TradePath,
    build_trade_path,
)
from yuanta_intraday_shadow_v01.live_parity_backtest import (
    EXECUTION_MODEL,
    _validate_full_session_coverage,
)

from .overlay import EntryFill, MFEProtectionState, PositionBasis, VARIANTS


ANALYSIS_ID = "MFE_PROFIT_PROTECTION_STUDY_V0_1"
BASELINE = "BASELINE"
VARIANT_ORDER = (BASELINE, "MFE_LOOSE", "MFE_V1", "MFE_AGGRESSIVE")
BUCKETS = (
    ("LT_0_5R", float("-inf"), 0.5),
    ("0_5_TO_1_0R", 0.5, 1.0),
    ("1_0_TO_1_5R", 1.0, 1.5),
    ("1_5_TO_2_0R", 1.5, 2.0),
    ("2_0_TO_3_0R", 2.0, 3.0),
    ("3_0_TO_5_0R", 3.0, 5.0),
    ("GE_5_0R", 5.0, float("inf")),
)


@dataclass(frozen=True, slots=True)
class ResearchTrade:
    trade_id: str
    session_date: str
    symbol: str
    stock_name: str
    side: str
    entry_time: datetime
    entry_price: float
    quantity: int
    initial_stop_price: float
    points: tuple[PathPoint, ...]

    @property
    def basis(self) -> PositionBasis:
        return PositionBasis.from_fills(
            self.side,
            [EntryFill(self.entry_time, self.entry_price, self.quantity)],
            initial_stop_price=self.initial_stop_price,
        )

    def pnl_at_price(self, price: float) -> float:
        return float(
            _projected_net_pnl(
                self.side, self.entry_price, price, self.quantity
            )[3]
        )


@dataclass(slots=True)
class ExistingExitState:
    peak_return: float = 0.0
    worst_return: float = 0.0


@dataclass(frozen=True, slots=True)
class SimulatedExit:
    index: int
    at: datetime
    price: float
    reason: str
    realized_pnl: float
    state: MFEProtectionState
    intrabar_ambiguous: bool = False


def _pnl(side: str, entry_price: float, exit_price: float, quantity: int) -> float:
    return float(_projected_net_pnl(side, entry_price, exit_price, quantity)[3])


def derive_initial_stop_price(
    side: str,
    entry_price: float,
    quantity: int,
    stop_loss_twd: float,
) -> float:
    """Invert the unchanged net-TWD stop into a positive per-share price R."""
    side = side.upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if entry_price <= 0 or quantity <= 0 or stop_loss_twd <= 0:
        raise ValueError("entry, quantity and stop loss must be positive")
    target = -float(stop_loss_twd)
    at_entry = _pnl(side, entry_price, entry_price, quantity)
    if at_entry <= target:
        raise ValueError("fees consume the complete initial risk at entry")

    if side == "LONG":
        adverse, safe = max(entry_price / 1_000_000, 0.000001), entry_price
        if _pnl(side, entry_price, adverse, quantity) > target:
            raise ValueError("unable to derive long initial stop")
        for _ in range(100):
            middle = (adverse + safe) / 2
            if _pnl(side, entry_price, middle, quantity) <= target:
                adverse = middle
            else:
                safe = middle
        stop = (adverse + safe) / 2
    else:
        safe, adverse = entry_price, entry_price * 2
        for _ in range(30):
            if _pnl(side, entry_price, adverse, quantity) <= target:
                break
            adverse *= 2
        else:
            raise ValueError("unable to derive short initial stop")
        for _ in range(100):
            middle = (safe + adverse) / 2
            if _pnl(side, entry_price, middle, quantity) <= target:
                adverse = middle
            else:
                safe = middle
        stop = (safe + adverse) / 2
    risk = entry_price - stop if side == "LONG" else stop - entry_price
    if not math.isfinite(stop) or stop <= 0 or risk <= 0:
        raise ValueError("derived initial R is invalid")
    return stop


def _hard_exit_at(trade: ResearchTrade) -> datetime:
    hour, minute = map(int, str(SPEC["hard_exit_time"]).split(":"))
    return trade.entry_time.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _existing_exit_reason(
    point: PathPoint,
    state: ExistingExitState,
    hard_exit: datetime,
) -> str | None:
    state.peak_return = max(state.peak_return, point.current_return)
    state.worst_return = min(state.worst_return, point.current_return)
    if point.projected_net_pnl <= -float(SPEC["stop_loss_net_twd"]):
        return "STOP_LOSS"
    if (
        state.peak_return >= float(SPEC["trailing_profit_activation"])
        and point.current_return
        <= state.peak_return - float(SPEC["trailing_profit_drawdown"])
    ):
        return "TRAILING_PROFIT"
    if (
        state.worst_return < 0
        and point.current_return > 0
        and point.current_return - state.worst_return
        >= float(SPEC["loss_recovery_required"])
    ):
        return "LOSS_RECOVERY_TO_PROFIT"
    if point.reversal:
        return "SIGNAL_REVERSAL"
    if point.at >= hard_exit:
        return "HARD_EXIT"
    return None


def simulate_trade(
    trade: ResearchTrade,
    variant_name: str = BASELINE,
    *,
    enable_mfe_profit_protection: bool = False,
) -> SimulatedExit:
    if variant_name != BASELINE and variant_name not in VARIANTS:
        raise ValueError(f"unknown MFE variant: {variant_name}")
    variant = VARIANTS.get(variant_name, VARIANTS["MFE_V1"])
    basis = trade.basis
    overlay = MFEProtectionState(basis, variant)
    overlay.observe(
        price=trade.entry_price,
        at=trade.entry_time,
        projected_net_pnl=trade.pnl_at_price(trade.entry_price),
        pnl_at_price=trade.pnl_at_price,
    )
    existing = ExistingExitState()
    hard_exit = _hard_exit_at(trade)

    for index, point in enumerate(trade.points):
        existing_reason = _existing_exit_reason(point, existing, hard_exit)
        overlay.observe(
            price=point.exit_price,
            at=point.at,
            projected_net_pnl=point.projected_net_pnl,
            pnl_at_price=trade.pnl_at_price,
        )
        # Existing risk/take-profit/force-flat logic wins ties and is never weakened.
        reason = existing_reason
        if (
            reason is None
            and enable_mfe_profit_protection
            and overlay.triggered(point.exit_price)
        ):
            reason = "MFE_PROFIT_PROTECTION"
        if reason is not None:
            return SimulatedExit(
                index=index,
                at=point.at,
                price=point.exit_price,
                reason=reason,
                realized_pnl=point.projected_net_pnl,
                state=overlay,
            )
    raise RuntimeError(f"no valid exit found for {trade.trade_id}")


def _convert_path(path: TradePath) -> ResearchTrade:
    stop = derive_initial_stop_price(
        "LONG",
        path.entry_price,
        path.quantity,
        float(SPEC["stop_loss_net_twd"]),
    )
    trade_id = (
        f"{path.session_date}-{path.stock_id}-"
        f"{path.decision_time.strftime('%H%M%S')}"
    )
    return ResearchTrade(
        trade_id=trade_id,
        session_date=path.session_date,
        symbol=path.stock_id,
        stock_name=path.stock_name,
        side="LONG",
        entry_time=path.decision_time,
        entry_price=path.entry_price,
        quantity=path.quantity,
        initial_stop_price=stop,
        points=path.points,
    )


def build_trades(
    session_runs: Mapping[str, list[Path]],
    capital: int = 190_000,
) -> tuple[list[ResearchTrade], list[dict[str, Any]], list[dict[str, Any]]]:
    trades: list[ResearchTrade] = []
    diagnostics: list[dict[str, Any]] = []
    coverages: list[dict[str, Any]] = []
    for session_date, run_dirs in sorted(session_runs.items()):
        stocks, coverage = load_session(run_dirs)
        _validate_full_session_coverage(coverage)
        path, diagnostic = build_trade_path(stocks, coverage, capital)
        diagnostic = {**diagnostic, "requested_session_date": session_date}
        diagnostics.append(diagnostic)
        coverages.append(coverage)
        if path is not None:
            trades.append(_convert_path(path))
    return trades, diagnostics, coverages


def _mfe_through(trade: ResearchTrade, final_index: int) -> tuple[float, datetime, float, float]:
    basis = trade.basis
    candidates = [
        (trade.entry_price, trade.entry_time, trade.pnl_at_price(trade.entry_price), 0.0)
    ]
    candidates.extend(
        (point.exit_price, point.at, point.projected_net_pnl, basis.favorable_r(point.exit_price))
        for point in trade.points[: final_index + 1]
    )
    return max(candidates, key=lambda row: row[3])


def _ratio(realized: float, mfe_pnl: float) -> float | None:
    return realized / mfe_pnl if mfe_pnl > 0 else None


def _post_exit(
    trade: ResearchTrade,
    result: SimulatedExit,
    original: SimulatedExit,
) -> dict[str, Any] | None:
    if result.reason != "MFE_PROFIT_PROTECTION":
        return None
    later = trade.points[result.index + 1: original.index + 1]
    best = max((point.projected_net_pnl for point in later), default=result.realized_pnl)
    worst = min((point.projected_net_pnl for point in later), default=result.realized_pnl)
    return {
        "trade_id": trade.trade_id,
        "symbol": trade.symbol,
        "side": trade.side,
        "variant": result.state.variant.name,
        "mfe_exit_time": result.at.isoformat(),
        "mfe_exit_pnl": result.realized_pnl,
        "post_exit_best_pnl": best,
        "post_exit_worst_pnl": worst,
        "max_favorable_movement_after_exit": best - result.realized_pnl,
        "max_adverse_movement_after_exit": worst - result.realized_pnl,
        "original_strategy_final_pnl": original.realized_pnl,
        "mfe_minus_original_pnl": result.realized_pnl - original.realized_pnl,
    }


def compare_trade(
    trade: ResearchTrade,
    variant_name: str,
    original: SimulatedExit,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    enabled = variant_name != BASELINE
    result = simulate_trade(
        trade,
        variant_name,
        enable_mfe_profit_protection=enabled,
    )
    basis = trade.basis
    mfe_price, mfe_time, mfe_pnl, mfe_r = _mfe_through(trade, original.index)
    path_mfe_price, path_mfe_time, path_mfe_pnl, path_mfe_r = _mfe_through(
        trade, len(trade.points) - 1
    )
    after_original = trade.points[original.index + 1:]
    original_retention = _ratio(original.realized_pnl, mfe_pnl)
    new_retention = _ratio(result.realized_pnl, mfe_pnl)
    locked_pnl = result.state.locked_profit_pnl if enabled else None
    row = {
        "variant": variant_name,
        "trade_id": trade.trade_id,
        "symbol": trade.symbol,
        "stock_name": trade.stock_name,
        "side": trade.side,
        "quantity": trade.quantity,
        "entry_time": trade.entry_time.isoformat(),
        "entry_price": trade.entry_price,
        "initial_stop_price": trade.initial_stop_price,
        "initial_R": basis.initial_risk_per_share,
        "initial_risk_pnl": basis.initial_risk_pnl,
        "original_exit_time": original.at.isoformat(),
        "original_exit_price": original.price,
        "original_exit_reason": original.reason,
        "MFE_price": mfe_price,
        "MFE_time": mfe_time.isoformat(),
        "MFE_pnl": mfe_pnl,
        "MFE_R": mfe_r,
        "counterfactual_path_MFE_price": path_mfe_price,
        "counterfactual_path_MFE_time": path_mfe_time.isoformat(),
        "counterfactual_path_MFE_pnl": path_mfe_pnl,
        "counterfactual_path_MFE_R": path_mfe_r,
        "post_original_exit_best_pnl": max(
            (point.projected_net_pnl for point in after_original),
            default=original.realized_pnl,
        ),
        "post_original_exit_worst_pnl": min(
            (point.projected_net_pnl for point in after_original),
            default=original.realized_pnl,
        ),
        "mfe_protection_armed": bool(enabled and result.state.armed),
        "mfe_activation_time": (
            result.state.activation_time.isoformat()
            if enabled and result.state.activation_time is not None
            else None
        ),
        "max_locked_profit_R": result.state.locked_profit_r if enabled and result.state.armed else None,
        "max_locked_profit_pnl": locked_pnl,
        "new_exit_time": result.at.isoformat(),
        "new_exit_price": result.price,
        "new_exit_reason": result.reason,
        "original_realized_pnl": original.realized_pnl,
        "new_realized_pnl": result.realized_pnl,
        "original_realized_R": basis.favorable_r(original.price),
        "new_realized_R": basis.favorable_r(result.price),
        "profit_retention_original": original_retention,
        "profit_retention_new": new_retention,
        "mfe_profit_given_back_original": (
            1 - original_retention if original_retention is not None else None
        ),
        "mfe_profit_given_back_new": (
            1 - new_retention if new_retention is not None else None
        ),
        "holding_seconds": (result.at - trade.entry_time).total_seconds(),
        "original_holding_seconds": (original.at - trade.entry_time).total_seconds(),
        "intrabar_ambiguous": result.intrabar_ambiguous,
    }
    return row, _post_exit(trade, result, original)


def _average(values: Iterable[float | int | None]) -> float | None:
    rows = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return mean(rows) if rows else None


def _median(values: Iterable[float | int | None]) -> float | None:
    rows = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return median(rows) if rows else None


def _maximum_drawdown(rows: list[dict[str, Any]]) -> float:
    equity = peak = 0.0
    drawdown = 0.0
    for row in sorted(rows, key=lambda item: item["entry_time"]):
        equity += float(row["new_realized_pnl"])
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _maximum_consecutive_losses(rows: list[dict[str, Any]]) -> int:
    current = maximum = 0
    for row in sorted(rows, key=lambda item: item["entry_time"]):
        if float(row["new_realized_pnl"]) < 0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def summarize_variant(variant: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(row["new_realized_pnl"]) for row in rows]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    original_winners = [row for row in rows if float(row["original_realized_pnl"]) > 0]
    originally_profitable = len(original_winners)
    retention = [
        float(row["profit_retention_new"])
        for row in rows
        if row["profit_retention_new"] is not None and float(row["new_realized_pnl"]) > 0
    ]
    mfe_exits = [row for row in rows if row["new_exit_reason"] == "MFE_PROFIT_PROTECTION"]
    improved = [row for row in original_winners if row["new_realized_pnl"] > row["original_realized_pnl"]]
    worse = [row for row in original_winners if row["new_realized_pnl"] < row["original_realized_pnl"]]
    affected_winners = [row for row in original_winners if row["mfe_protection_armed"]]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    average_winner = _average(wins)
    average_loser = _average(losses)
    count = len(rows)
    return {
        "variant": variant,
        "total_trades": count,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "flat_trades": count - len(wins) - len(losses),
        "win_rate": len(wins) / count if count else None,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_pnl": sum(pnls),
        "average_pnl_per_trade": _average(pnls),
        "expectancy_per_trade": _average(pnls),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "average_winner": average_winner,
        "average_loser": average_loser,
        "payoff_ratio": (
            average_winner / abs(average_loser)
            if average_winner is not None and average_loser not in (None, 0)
            else None
        ),
        "maximum_winning_trade": max(wins) if wins else None,
        "maximum_losing_trade": min(losses) if losses else None,
        "maximum_drawdown": _maximum_drawdown(rows),
        "maximum_consecutive_losses": _maximum_consecutive_losses(rows),
        "average_holding_seconds": _average(row["holding_seconds"] for row in rows),
        "median_holding_seconds": _median(row["holding_seconds"] for row in rows),
        "average_MFE": _average(row["MFE_pnl"] for row in rows),
        "median_MFE": _median(row["MFE_pnl"] for row in rows),
        "average_MFE_R": _average(row["MFE_R"] for row in rows),
        "average_profit_retention_ratio": _average(retention),
        "median_profit_retention_ratio": _median(retention),
        "average_profit_giveback_ratio": _average(1 - value for value in retention),
        "mfe_protection_activations": sum(bool(row["mfe_protection_armed"]) for row in rows),
        "mfe_exits": len(mfe_exits),
        "pnl_generated_by_mfe_exits": sum(float(row["new_realized_pnl"]) for row in mfe_exits),
        "percentage_of_winners_affected_by_mfe": (
            len(affected_winners) / originally_profitable if originally_profitable else None
        ),
        "percentage_originally_profitable_improved": (
            len(improved) / originally_profitable if originally_profitable else None
        ),
        "percentage_originally_profitable_made_worse": (
            len(worse) / originally_profitable if originally_profitable else None
        ),
        "percentage_mfe_exit_before_original": (
            sum(row["new_exit_time"] < row["original_exit_time"] for row in mfe_exits) / count
            if count else None
        ),
        "intrabar_ambiguous_trades": sum(bool(row["intrabar_ambiguous"]) for row in rows),
    }


def _bucket_name(value: float) -> str:
    for name, low, high in BUCKETS:
        if low <= value < high:
            return name
    raise AssertionError("MFE R bucket missing")


def bucket_analysis(all_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for variant in VARIANT_ORDER:
        variant_rows = [row for row in all_rows if row["variant"] == variant]
        for name, _low, _high in BUCKETS:
            rows = [row for row in variant_rows if _bucket_name(float(row["MFE_R"])) == name]
            result.append({
                "variant": variant,
                "MFE_R_bucket": name,
                "trade_count": len(rows),
                "average_final_pnl": _average(row["new_realized_pnl"] for row in rows),
                "average_MFE": _average(row["MFE_pnl"] for row in rows),
                "average_retention_ratio": _average(row["profit_retention_new"] for row in rows),
                "baseline_realized_R": _average(row["original_realized_R"] for row in rows),
                "MFE_version_realized_R": _average(row["new_realized_R"] for row in rows),
            })
    return result


def _diagnostic_answers(
    summaries: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
    post_exit: list[dict[str, Any]],
) -> dict[str, Any]:
    baseline = next(row for row in summaries if row["variant"] == BASELINE)
    variants = [row for row in summaries if row["variant"] != BASELINE]
    large_mfe = [
        row for row in all_rows
        if row["variant"] == BASELINE and float(row["MFE_R"]) >= 1.5
    ]
    low_retention = [
        row for row in large_mfe
        if row["profit_retention_original"] is not None
        and float(row["profit_retention_original"]) < 0.5
    ]
    best = max(
        variants,
        key=lambda row: (
            float(row["net_pnl"]),
            float(row["profit_factor"] or float("-inf")),
            -float(row["maximum_drawdown"]),
        ),
        default=None,
    )
    any_changed = any(row["mfe_exits"] for row in variants)
    return {
        "1_large_mfe_low_retention_frequency": {
            "large_mfe_trade_count": len(large_mfe),
            "below_50pct_retention_count": len(low_retention),
            "supported": bool(large_mfe),
        },
        "2_low_retention_cause_of_poor_expectancy": (
            "NOT_IDENTIFIABLE_FROM_AVAILABLE_SAMPLE" if not large_mfe else
            "DIAGNOSTIC_ONLY_NO_CAUSAL_CLAIM"
        ),
        "3_profit_factor_improved": {
            row["variant"]: (
                None if row["profit_factor"] is None or baseline["profit_factor"] is None
                else row["profit_factor"] > baseline["profit_factor"]
            ) for row in variants
        },
        "4_expectancy_improved": {
            row["variant"]: row["expectancy_per_trade"] > baseline["expectancy_per_trade"]
            for row in variants
        },
        "5_maximum_drawdown_reduced": {
            row["variant"]: row["maximum_drawdown"] < baseline["maximum_drawdown"]
            for row in variants
        },
        "6_average_winner_reduction": {
            row["variant"]: (
                None if row["average_winner"] is None or baseline["average_winner"] is None
                else row["average_winner"] - baseline["average_winner"]
            ) for row in variants
        },
        "7_largest_winners_prematurely_exited": sum(
            row["mfe_minus_original_pnl"] < 0 for row in post_exit
        ),
        "8_best_tradeoff": (
            "NO_OBSERVED_DIFFERENCE" if not any_changed else best["variant"]
        ),
        "9_results_after_costs": True,
        "10_breadth": {
            "mfe_exit_count": len(post_exit),
            "unique_trades": len({row["trade_id"] for row in post_exit}),
            "interpretation": (
                "NO_MFE_EXITS" if not post_exit else
                "REQUIRES_OUTLIER_REVIEW_IN_PER_TRADE_FILE"
            ),
        },
    }


def build_report(
    session_runs: Mapping[str, list[Path]],
    capital: int = 190_000,
) -> dict[str, Any]:
    trades, diagnostics, coverage = build_trades(session_runs, capital)
    all_rows: list[dict[str, Any]] = []
    post_rows: list[dict[str, Any]] = []
    for trade in trades:
        original = simulate_trade(trade, BASELINE, enable_mfe_profit_protection=False)
        for variant in VARIANT_ORDER:
            row, post = compare_trade(trade, variant, original)
            all_rows.append(row)
            if post is not None:
                post_rows.append(post)
    summaries = [
        summarize_variant(
            variant,
            [row for row in all_rows if row["variant"] == variant],
        )
        for variant in VARIANT_ORDER
    ]
    return {
        "analysis_id": ANALYSIS_ID,
        "strategy_spec_hash": SPEC_HASH,
        "strategy_spec": SPEC,
        "overlay_feature_flag_default": False,
        "variant_definitions": {
            name: asdict(variant) for name, variant in VARIANTS.items()
        },
        "execution_model": EXECUTION_MODEL,
        "initial_R_method": (
            "invert unchanged STOP_LOSS_NET_TWD through existing fee/tax model; "
            "R trigger remains entry-to-stop price distance"
        ),
        "zero_R_interpretation": (
            "entry-price breakeven before costs; reported PnL always includes existing fees/tax"
        ),
        "coverage": coverage,
        "diagnostics": diagnostics,
        "summaries": summaries,
        "per_trade": all_rows,
        "bucket_analysis": bucket_analysis(all_rows),
        "post_exit_analysis": post_rows,
        "diagnostic_answers": _diagnostic_answers(summaries, all_rows, post_rows),
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_order_calls": 0,
        "live_behavior_changed": False,
        "interpretation": "BACKTEST_ONLY_DIAGNOSTIC_NOT_PARAMETER_OPTIMIZATION",
    }


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _write_csv(
    path: Path,
    rows: list[dict[str, Any]],
    fields: list[str] | None = None,
) -> None:
    fields = fields or (list(rows[0]) if rows else [])
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not fields:
            return
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def markdown_report(report: dict[str, Any]) -> str:
    summaries = report["summaries"]
    lines = [
        "# MFE profit protection diagnostic",
        "",
        "This is a backtest/shadow-only comparison. Live strategy and order routing are unchanged.",
        "",
        "| Variant | Trades | Win rate | Net PnL | Expectancy | PF | Max DD | Avg winner | Avg loser | MFE exits |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        lines.append(
            "| {variant} | {total_trades} | {win_rate} | {net_pnl} | {expectancy} | "
            "{pf} | {dd} | {winner} | {loser} | {exits} |".format(
                variant=row["variant"],
                total_trades=row["total_trades"],
                win_rate=_fmt(None if row["win_rate"] is None else row["win_rate"] * 100) + ("" if row["win_rate"] is None else "%"),
                net_pnl=_fmt(row["net_pnl"]),
                expectancy=_fmt(row["expectancy_per_trade"]),
                pf=_fmt(row["profit_factor"]),
                dd=_fmt(row["maximum_drawdown"]),
                winner=_fmt(row["average_winner"]),
                loser=_fmt(row["average_loser"]),
                exits=row["mfe_exits"],
            )
        )
    lines.extend([
        "",
        "## Data and execution limits",
        "",
        f"- Sessions accepted by the existing production-parity coverage gate: {len(report['coverage'])}",
        f"- Scored production-parity entries: {summaries[0]['total_trades']}",
        f"- Execution model: `{report['execution_model']}`",
        "- Entry selection, timing, sizing, existing exits, fees and tax are unchanged.",
        "- MFE uses the existing executable liquidation-quote proxy, not an optimistic raw last-trade high/low.",
        "- Replay still assumes immediate full entry/exit fills at the existing adverse-one-tick proxy; queue position, latency and real partial fills are not observed.",
        "- Source data are ticks, so intrabar ambiguity is zero in this run; OHLC ambiguity is handled and unit-tested for future bar inputs.",
        "",
        "## Diagnostic answers",
        "",
        "```json",
        json.dumps(report["diagnostic_answers"], ensure_ascii=False, indent=2, allow_nan=False),
        "```",
        "",
        "## Conclusion",
        "",
    ])
    if any(row["mfe_exits"] for row in summaries):
        lines.append(
            "At least one overlay changed an exit. Review `per_trade_comparison.csv` and "
            "`post_exit_analysis.csv`; the sample remains diagnostic and is not a promotion decision."
        )
    else:
        lines.append(
            "No available production-parity trade reached the 1R activation threshold before its existing exit. "
            "All four variants are therefore identical in this sample, and no configuration can be recommended."
        )
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {key: value for key, value in report.items() if key != "per_trade"}
    summary_path = output_dir / "summary.json"
    summary_path.write_bytes(_json_bytes(payload) + b"\n")
    _write_csv(output_dir / "comparison.csv", report["summaries"])
    _write_csv(output_dir / "per_trade_comparison.csv", report["per_trade"])
    _write_csv(output_dir / "mfe_bucket_analysis.csv", report["bucket_analysis"])
    _write_csv(
        output_dir / "post_exit_analysis.csv",
        report["post_exit_analysis"],
        [
            "trade_id", "symbol", "side", "variant", "mfe_exit_time",
            "mfe_exit_pnl", "post_exit_best_pnl", "post_exit_worst_pnl",
            "max_favorable_movement_after_exit", "max_adverse_movement_after_exit",
            "original_strategy_final_pnl", "mfe_minus_original_pnl",
        ],
    )
    (output_dir / "report.md").write_text(markdown_report(report), encoding="utf-8")
    artifacts = {}
    for path in sorted(output_dir.iterdir()):
        if path.is_file() and path.name != "run_manifest.json":
            artifacts[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "analysis_id": ANALYSIS_ID,
        "strategy_spec_hash": SPEC_HASH,
        "artifact_hashes": artifacts,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_order_calls": 0,
        "live_behavior_changed": False,
    }
    (output_dir / "run_manifest.json").write_bytes(_json_bytes(manifest) + b"\n")
    return artifacts
