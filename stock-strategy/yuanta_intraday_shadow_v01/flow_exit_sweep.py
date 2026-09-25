"""Causal tick-flow exit research for the long-only LIVE-parity entry.

The production entry engine is unchanged.  Flow exits are evaluated on the
same 30-second decision clock using only ticks/books received by that time.

If a flow rule does not exit earlier, the existing LIVE-like baseline remains:
-5000 TWD hard stop, current trailing rule, loss recovery, signal reversal,
and 13:20 hard exit.

Research-only: no broker imports and no order submission.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from pathlib import Path

from .direction_follow_backtest import (
    SPEC,
    _projected_net_pnl,
    load_session,
)
from .exit_parameter_sweep import (
    ExitVariant,
    TradePath,
    build_trade_path,
    simulate_variant,
)


@dataclass(frozen=True)
class FlowSnapshot:
    at: datetime
    buy_qty_30: float
    sell_qty_30: float
    buy_qty_60: float
    sell_qty_60: float
    buy_sell_ratio_30: float | None
    sell_buy_ratio_30: float | None
    net_aggressive_30: float
    normalized_delta_30: float
    net_aggressive_60: float
    normalized_delta_60: float
    buy_qty_30_previous: float
    sell_qty_30_previous: float
    buy_decay_from_peak: float
    sell_increase_vs_previous: float | None
    large_threshold: float | None
    large_buy_qty_60: float
    large_sell_qty_60: float
    large_buy_sell_ratio_60: float | None
    large_trade_delta_60: float
    max_consecutive_sell_ticks_30: int
    average_buy_trade_size_30: float | None
    average_sell_trade_size_30: float | None
    book_imbalance: float | None


@dataclass(frozen=True)
class FlowVariant:
    variant_id: str
    rule: str


FLOW_VARIANTS = (
    FlowVariant("SELL_GT_BUY_30", "SELL_GT_BUY_30"),
    FlowVariant("SELL_BUY_RATIO_1_25_30", "SELL_BUY_RATIO_1_25_30"),
    FlowVariant("SELL_BUY_RATIO_1_50_30", "SELL_BUY_RATIO_1_50_30"),
    FlowVariant("VOLUME_DELTA_LT_0_60", "VOLUME_DELTA_LT_0_60"),
    FlowVariant("VOLUME_DELTA_LE_M20_60", "VOLUME_DELTA_LE_M20_60"),
    FlowVariant("VOLUME_DELTA_LE_M35_60", "VOLUME_DELTA_LE_M35_60"),
    FlowVariant("BUY_DECAY_30PCT", "BUY_DECAY_30PCT"),
    FlowVariant("BUY_DECAY_50PCT", "BUY_DECAY_50PCT"),
    FlowVariant("BUY_RATIO_FROM_1_5_TO_LT_1", "BUY_RATIO_FROM_1_5_TO_LT_1"),
    FlowVariant("LARGE_SELL_GT_BUY_60", "LARGE_SELL_GT_BUY_60"),
    FlowVariant("LARGE_DELTA_LT_0_60", "LARGE_DELTA_LT_0_60"),
    FlowVariant("LARGE_DELTA_LE_M25_60", "LARGE_DELTA_LE_M25_60"),
    FlowVariant("BOOK_IMBALANCE_LT_0", "BOOK_IMBALANCE_LT_0"),
    FlowVariant("FLOW_2_OF_3_NEG", "FLOW_2_OF_3_NEG"),
    FlowVariant("FLOW_2_OF_3_NEG_2X", "FLOW_2_OF_3_NEG_2X"),
    FlowVariant("LARGE_NEG_2X", "LARGE_NEG_2X"),
)


def _signed_side(row: dict) -> str:
    flag = str(row.get("flag", ""))
    if flag == "1":
        return "BUY"
    if flag == "0":
        return "SELL"
    midpoint = (float(row["bid"]) + float(row["ask"])) / 2
    return "BUY" if float(row["price"]) >= midpoint else "SELL"


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator > 0:
        return numerator / denominator
    if numerator > 0:
        return math.inf
    return None


def _normalized(buy: float, sell: float) -> float:
    total = buy + sell
    return (buy - sell) / total if total > 0 else 0.0


def _percentile90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(
        0,
        math.ceil(float(SPEC["large_trade_percentile"]) * len(ordered)) - 1,
    )
    return ordered[index]


def _window(data: dict, start: datetime, end: datetime) -> list[dict]:
    times = data["tick_times"]
    return data["ticks"][
        bisect_left(times, start):
        bisect_right(times, end)
    ]


def _buy_sell(rows: list[dict]) -> tuple[float, float, int, int]:
    buy = sell = 0.0
    buy_count = sell_count = 0
    for row in rows:
        if _signed_side(row) == "BUY":
            buy += float(row["volume"])
            buy_count += 1
        else:
            sell += float(row["volume"])
            sell_count += 1
    return buy, sell, buy_count, sell_count


def _latest_book(data: dict, at: datetime) -> dict | None:
    index = bisect_right(data["book_times"], at) - 1
    if index < 0:
        return None
    book = data["books"][index]
    age = (at - book["time"]).total_seconds()
    if age < 0 or age > float(SPEC["maximum_book_staleness_seconds"]):
        return None
    return book


def _max_sell_burst(rows: list[dict]) -> int:
    current = best = 0
    for row in rows:
        if _signed_side(row) == "SELL":
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def flow_snapshot(
    data: dict,
    at: datetime,
    *,
    peak_buy_qty_30: float,
) -> FlowSnapshot | None:
    rows30 = _window(data, at - timedelta(seconds=30), at)
    rows60 = _window(data, at - timedelta(seconds=60), at)

    # A flow decision must be based on a recently received trade.  Otherwise
    # an inactive/stale symbol could look like buy flow collapsed to zero.
    if not rows60:
        return None
    latest_age = (at - rows60[-1]["time"]).total_seconds()
    if (
        latest_age < 0
        or latest_age > float(SPEC["maximum_tick_staleness_seconds"])
    ):
        return None

    previous30 = _window(
        data,
        at - timedelta(seconds=60),
        at - timedelta(seconds=30, microseconds=1),
    )
    reference = _window(
        data,
        at - timedelta(seconds=int(SPEC["large_trade_reference_seconds"])),
        at,
    )

    buy30, sell30, buy_count30, sell_count30 = _buy_sell(rows30)
    buy60, sell60, _buy_count60, _sell_count60 = _buy_sell(rows60)
    previous_buy30, previous_sell30, _, _ = _buy_sell(previous30)

    threshold = _percentile90([float(row["volume"]) for row in reference])
    large_rows = (
        [row for row in rows60 if float(row["volume"]) >= threshold]
        if threshold is not None
        else []
    )
    large_buy, large_sell, _, _ = _buy_sell(large_rows)

    book = _latest_book(data, at)
    book_imbalance = None
    if book is not None:
        total = float(book["buy_volume"]) + float(book["sell_volume"])
        if total > 0:
            book_imbalance = (
                float(book["buy_volume"]) - float(book["sell_volume"])
            ) / total

    effective_peak = max(float(peak_buy_qty_30), buy30)
    buy_decay = (
        1.0 - buy30 / effective_peak
        if effective_peak > 0
        else 0.0
    )

    sell_increase = None
    if previous_sell30 > 0:
        sell_increase = sell30 / previous_sell30 - 1.0
    elif sell30 > 0:
        sell_increase = math.inf

    return FlowSnapshot(
        at=at,
        buy_qty_30=buy30,
        sell_qty_30=sell30,
        buy_qty_60=buy60,
        sell_qty_60=sell60,
        buy_sell_ratio_30=_ratio(buy30, sell30),
        sell_buy_ratio_30=_ratio(sell30, buy30),
        net_aggressive_30=buy30 - sell30,
        normalized_delta_30=_normalized(buy30, sell30),
        net_aggressive_60=buy60 - sell60,
        normalized_delta_60=_normalized(buy60, sell60),
        buy_qty_30_previous=previous_buy30,
        sell_qty_30_previous=previous_sell30,
        buy_decay_from_peak=buy_decay,
        sell_increase_vs_previous=sell_increase,
        large_threshold=threshold,
        large_buy_qty_60=large_buy,
        large_sell_qty_60=large_sell,
        large_buy_sell_ratio_60=_ratio(large_buy, large_sell),
        large_trade_delta_60=_normalized(large_buy, large_sell),
        max_consecutive_sell_ticks_30=_max_sell_burst(rows30),
        average_buy_trade_size_30=(
            buy30 / buy_count30 if buy_count30 else None
        ),
        average_sell_trade_size_30=(
            sell30 / sell_count30 if sell_count30 else None
        ),
        book_imbalance=book_imbalance,
    )


def build_flow_snapshots(
    data: dict,
    path: TradePath,
) -> tuple[FlowSnapshot, ...]:
    interval = timedelta(seconds=int(SPEC["decision_interval_seconds"]))
    hard_hour, hard_minute = map(int, SPEC["hard_exit_time"].split(":"))
    hard_exit = path.decision_time.replace(
        hour=hard_hour,
        minute=hard_minute,
        second=0,
        microsecond=0,
    )

    result = []
    peak_buy = 0.0
    at = path.decision_time + interval

    while at <= hard_exit:
        snapshot = flow_snapshot(
            data,
            at,
            peak_buy_qty_30=peak_buy,
        )
        if snapshot is not None:
            peak_buy = max(peak_buy, snapshot.buy_qty_30)
            result.append(snapshot)
        at += interval

    return tuple(result)


def _negative_components(snapshot: FlowSnapshot) -> int:
    values = [
        snapshot.normalized_delta_60 < 0,
        snapshot.large_trade_delta_60 < 0,
        (
            snapshot.book_imbalance is not None
            and snapshot.book_imbalance < 0
        ),
    ]
    return sum(bool(value) for value in values)


def rule_triggered(
    variant: FlowVariant,
    snapshot: FlowSnapshot,
    *,
    previous: FlowSnapshot | None,
    seen_buy_ratio_above_1_5: bool,
) -> bool:
    rule = variant.rule

    if rule == "SELL_GT_BUY_30":
        return snapshot.sell_qty_30 > snapshot.buy_qty_30

    if rule == "SELL_BUY_RATIO_1_25_30":
        ratio = snapshot.sell_buy_ratio_30
        return ratio is not None and ratio > 1.25

    if rule == "SELL_BUY_RATIO_1_50_30":
        ratio = snapshot.sell_buy_ratio_30
        return ratio is not None and ratio > 1.50

    if rule == "VOLUME_DELTA_LT_0_60":
        return snapshot.normalized_delta_60 < 0

    if rule == "VOLUME_DELTA_LE_M20_60":
        return snapshot.normalized_delta_60 <= -0.20

    if rule == "VOLUME_DELTA_LE_M35_60":
        return snapshot.normalized_delta_60 <= -0.35

    if rule == "BUY_DECAY_30PCT":
        return snapshot.buy_decay_from_peak >= 0.30

    if rule == "BUY_DECAY_50PCT":
        return snapshot.buy_decay_from_peak >= 0.50

    if rule == "BUY_RATIO_FROM_1_5_TO_LT_1":
        ratio = snapshot.buy_sell_ratio_30
        return (
            seen_buy_ratio_above_1_5
            and ratio is not None
            and ratio < 1.0
        )

    if rule == "LARGE_SELL_GT_BUY_60":
        return snapshot.large_sell_qty_60 > snapshot.large_buy_qty_60

    if rule == "LARGE_DELTA_LT_0_60":
        return snapshot.large_trade_delta_60 < 0

    if rule == "LARGE_DELTA_LE_M25_60":
        return snapshot.large_trade_delta_60 <= -0.25

    if rule == "BOOK_IMBALANCE_LT_0":
        return (
            snapshot.book_imbalance is not None
            and snapshot.book_imbalance < 0
        )

    if rule == "FLOW_2_OF_3_NEG":
        return _negative_components(snapshot) >= 2

    if rule == "FLOW_2_OF_3_NEG_2X":
        return (
            previous is not None
            and _negative_components(previous) >= 2
            and _negative_components(snapshot) >= 2
            and snapshot.at - previous.at
            == timedelta(seconds=int(SPEC["decision_interval_seconds"]))
        )

    if rule == "LARGE_NEG_2X":
        return (
            previous is not None
            and previous.large_trade_delta_60 < 0
            and snapshot.large_trade_delta_60 < 0
            and snapshot.at - previous.at
            == timedelta(seconds=int(SPEC["decision_interval_seconds"]))
        )

    raise ValueError(f"unsupported flow rule: {rule}")


def _first_executable_point_at_or_after(
    path: TradePath,
    at: datetime,
):
    # A decision can occur between quote callbacks.  Once the causal exit
    # condition fires, keep the exit intent pending and use the first later
    # point that already passed the same fresh/safe quote checks used when
    # TradePath was constructed.
    for point in path.points:
        if point.at >= at:
            return point
    return None


def simulate_flow_variant(
    path: TradePath,
    snapshots: tuple[FlowSnapshot, ...],
    variant: FlowVariant,
) -> dict:
    baseline = simulate_variant(
        path,
        ExitVariant("BASELINE_5000", "FIXED_TWD", 5000.0),
    )
    if not baseline.get("scorable"):
        return {
            "variant_id": variant.variant_id,
            "scorable": False,
            "reason": "BASELINE_UNSCORABLE",
        }

    baseline_exit = datetime.fromisoformat(baseline["exit_time"])
    previous = None
    seen_ratio = False
    triggered_snapshot = None

    for snapshot in snapshots:
        ratio = snapshot.buy_sell_ratio_30
        if ratio is not None and ratio > 1.5:
            seen_ratio = True

        if snapshot.at >= baseline_exit:
            break

        if rule_triggered(
            variant,
            snapshot,
            previous=previous,
            seen_buy_ratio_above_1_5=seen_ratio,
        ):
            triggered_snapshot = snapshot
            break

        previous = snapshot

    if triggered_snapshot is None:
        return {
            **baseline,
            "variant_id": variant.variant_id,
            "flow_triggered": False,
            "baseline_net_pnl": baseline["net_pnl"],
            "delta_vs_baseline_twd": 0.0,
        }

    point = _first_executable_point_at_or_after(
        path,
        triggered_snapshot.at,
    )
    if point is None:
        return {
            "variant_id": variant.variant_id,
            "scorable": False,
            "reason": "FLOW_TRIGGER_WITHOUT_LATER_EXECUTABLE_QUOTE",
        }

    # If the ordinary LIVE-like baseline would already have exited before
    # the flow intent could obtain an executable quote, baseline wins.
    if point.at >= baseline_exit:
        return {
            **baseline,
            "variant_id": variant.variant_id,
            "flow_triggered": True,
            "flow_trigger_time": triggered_snapshot.at.isoformat(),
            "flow_executable_before_baseline": False,
            "baseline_net_pnl": baseline["net_pnl"],
            "delta_vs_baseline_twd": 0.0,
        }

    gross, commission, sell_tax, net_pnl = _projected_net_pnl(
        "LONG",
        path.entry_price,
        point.exit_price,
        path.quantity,
    )

    later = [p for p in path.points if p.at > point.at]
    post_exit_best_net = max(
        (p.projected_net_pnl for p in later),
        default=None,
    )
    post_exit_best_return = max(
        (p.current_return for p in later),
        default=None,
    )

    held = [p for p in path.points if p.at <= point.at]

    return {
        "variant_id": variant.variant_id,
        "scorable": True,
        "session_date": path.session_date,
        "stock_id": path.stock_id,
        "stock_name": path.stock_name,
        "decision_time": path.decision_time.isoformat(),
        "entry_price": path.entry_price,
        "quantity": path.quantity,
        "exit_time": point.at.isoformat(),
        "exit_price": point.exit_price,
        "exit_reason": f"FLOW:{variant.rule}",
        "gross_pnl": gross,
        "commission": commission,
        "sell_tax": sell_tax,
        "net_pnl": net_pnl,
        "flow_triggered": True,
        "flow_trigger_time": triggered_snapshot.at.isoformat(),
        "flow_exit_latency_seconds": round(
            (point.at - triggered_snapshot.at).total_seconds(),
            6,
        ),
        "flow_executable_before_baseline": True,
        "baseline_net_pnl": baseline["net_pnl"],
        "delta_vs_baseline_twd": round(
            net_pnl - float(baseline["net_pnl"]),
            2,
        ),
        "held_mae_twd": round(
            min((p.current_return for p in held), default=0.0)
            * path.notional_used,
            2,
        ),
        "held_mfe_twd": round(
            max((p.current_return for p in held), default=0.0)
            * path.notional_used,
            2,
        ),
        "post_exit_best_net_pnl": post_exit_best_net,
        "post_exit_best_net_return": post_exit_best_return,
        "false_exit_positive_net": (
            post_exit_best_net is not None
            and post_exit_best_net > 0
        ),
        "flow_snapshot": {
            "buy_qty_30": triggered_snapshot.buy_qty_30,
            "sell_qty_30": triggered_snapshot.sell_qty_30,
            "buy_qty_60": triggered_snapshot.buy_qty_60,
            "sell_qty_60": triggered_snapshot.sell_qty_60,
            "buy_sell_ratio_30": triggered_snapshot.buy_sell_ratio_30,
            "sell_buy_ratio_30": triggered_snapshot.sell_buy_ratio_30,
            "normalized_delta_30": triggered_snapshot.normalized_delta_30,
            "normalized_delta_60": triggered_snapshot.normalized_delta_60,
            "buy_decay_from_peak": triggered_snapshot.buy_decay_from_peak,
            "large_threshold": triggered_snapshot.large_threshold,
            "large_buy_qty_60": triggered_snapshot.large_buy_qty_60,
            "large_sell_qty_60": triggered_snapshot.large_sell_qty_60,
            "large_trade_delta_60": triggered_snapshot.large_trade_delta_60,
            "book_imbalance": triggered_snapshot.book_imbalance,
            "max_consecutive_sell_ticks_30":
                triggered_snapshot.max_consecutive_sell_ticks_30,
        },
    }


def _profit_factor(rows: list[dict]) -> float | None:
    gains = sum(max(0.0, float(row["net_pnl"])) for row in rows)
    losses = sum(min(0.0, float(row["net_pnl"])) for row in rows)
    return None if losses == 0 else round(gains / abs(losses), 6)


def build_flow_report(
    session_runs: dict[str, list[Path]],
    capital: int = 190000,
) -> dict:
    results = {variant.variant_id: [] for variant in FLOW_VARIANTS}
    diagnostics = []

    for _date, run_dirs in sorted(session_runs.items()):
        stocks, coverage = load_session(run_dirs)
        path, diag = build_trade_path(stocks, coverage, capital)
        diagnostics.append(diag)

        if path is None:
            continue

        snapshots = build_flow_snapshots(
            stocks[path.stock_id],
            path,
        )

        for variant in FLOW_VARIANTS:
            row = simulate_flow_variant(path, snapshots, variant)
            if row.get("scorable"):
                results[variant.variant_id].append(row)

    summaries = []

    for variant in FLOW_VARIANTS:
        rows = results[variant.variant_id]
        triggered = [row for row in rows if row["flow_triggered"]]

        summaries.append({
            "variant_id": variant.variant_id,
            "trade_count": len(rows),
            "flow_trigger_count": len(triggered),
            "winning_trades": sum(row["net_pnl"] > 0 for row in rows),
            "win_rate": (
                round(
                    sum(row["net_pnl"] > 0 for row in rows) / len(rows),
                    6,
                )
                if rows else None
            ),
            "net_pnl_twd": round(
                sum(row["net_pnl"] for row in rows),
                2,
            ),
            "average_trade_twd": (
                round(
                    sum(row["net_pnl"] for row in rows) / len(rows),
                    2,
                )
                if rows else None
            ),
            "profit_factor": _profit_factor(rows),
            "average_delta_vs_baseline_twd": (
                round(
                    sum(row["delta_vs_baseline_twd"] for row in rows)
                    / len(rows),
                    2,
                )
                if rows else None
            ),
            "false_exit_positive_net_count": sum(
                bool(row.get("false_exit_positive_net"))
                for row in triggered
            ),
            "false_exit_positive_net_rate": (
                round(
                    sum(
                        bool(row.get("false_exit_positive_net"))
                        for row in triggered
                    ) / len(triggered),
                    6,
                )
                if triggered else None
            ),
        })

    return {
        "analysis_id": "YUANTA_LONG_ONLY_CAUSAL_FLOW_EXIT_SWEEP_V0_1",
        "capital_twd": capital,
        "entry_logic": "PRODUCTION_LIVE_DIRECTION_ENGINE_LONG_ONLY_UNCHANGED",
        "flow_decision_interval_seconds":
            int(SPEC["decision_interval_seconds"]),
        "flow_execution_model":
            "TRIGGER_CAUSALLY_THEN_WAIT_FOR_NEXT_EXECUTABLE_SAFE_QUOTE",
        "flow_windows_seconds": [30, 60],
        "large_trade_reference_seconds":
            int(SPEC["large_trade_reference_seconds"]),
        "large_trade_percentile":
            float(SPEC["large_trade_percentile"]),
        "baseline_exit":
            "LIVE_LIKE_MINUS_5000_PLUS_TRAILING_LOSS_RECOVERY_REVERSAL_HARD_EXIT",
        "diagnostics": diagnostics,
        "summaries": summaries,
        "results": results,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_order_calls": 0,
        "interpretation": (
            "EXPLORATORY_CAUSAL_FLOW_EXIT_RESEARCH; "
            "DO_NOT_CHANGE_LIVE_FROM_SMALL_SAMPLE RESULTS"
        ),
    }
