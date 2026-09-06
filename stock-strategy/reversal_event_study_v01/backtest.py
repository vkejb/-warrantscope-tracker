from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
import math
import random
import statistics
from typing import Iterable

from .config import CFG, Config
from surge_event_study_v01.models import Bar, PreparedBenchmark, PreparedStock


def commission(gross: float, rate: float, minimum: int) -> float:
    """Return the explicit commission proxy, including the odd-lot minimum."""

    return float(max(minimum, round(gross * rate))) if gross > 0 else 0.0


def _bar_position_at(
    stock: PreparedStock, calendar_index: int
) -> tuple[int, Bar] | None:
    position = bisect_left(stock.calendar_indices, calendar_index)
    if (
        position < len(stock.calendar_indices)
        and stock.calendar_indices[position] == calendar_index
    ):
        return position, stock.bars[position]
    return None


def _bar_at(stock: PreparedStock, calendar_index: int) -> Bar | None:
    found = _bar_position_at(stock, calendar_index)
    return found[1] if found is not None else None


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _profit_factor_from_pnls(pnls: Iterable[float]) -> float | None:
    values = list(pnls)
    gross_profit = sum(value for value in values if value > 0.0)
    gross_loss = abs(sum(value for value in values if value < 0.0))
    # None is deliberately used instead of JSON-invalid Infinity. A no-loss
    # sample is too small/degenerate to pass the execution gate automatically.
    return gross_profit / gross_loss if gross_loss > 0.0 else None


def _monthly_cluster_bootstrap(trades: list[dict], cfg: Config) -> dict:
    """Bootstrap complete signal-month clusters, not IID individual trades."""

    clusters: dict[str, dict[str, float | int]] = {}
    for trade in trades:
        month = str(trade["signal_date"])[:6]
        cluster = clusters.setdefault(
            month,
            {
                "count": 0,
                "net_return_sum": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
            },
        )
        cluster["count"] = int(cluster["count"]) + 1
        cluster["net_return_sum"] = float(cluster["net_return_sum"]) + float(
            trade["net_return"]
        )
        pnl = float(trade["pnl"])
        if pnl > 0.0:
            cluster["gross_profit"] = float(cluster["gross_profit"]) + pnl
        elif pnl < 0.0:
            cluster["gross_loss"] = float(cluster["gross_loss"]) + abs(pnl)

    ordered = [clusters[key] for key in sorted(clusters)]
    if not ordered:
        return {
            "method": "signal-month cluster bootstrap",
            "clusters": 0,
            "iterations": cfg.bootstrap_iterations,
            "average_net_trade_return": None,
            "average_net_trade_return_ci95": [None, None],
            "profit_factor": None,
            "profit_factor_ci95": [None, None],
        }

    alpha = getattr(cfg, "validation_ci_alpha", 0.025)
    generator = random.Random(cfg.bootstrap_seed + 1_903)
    boot_means: list[float] = []
    boot_profit_factors: list[float] = []
    cluster_count = len(ordered)
    for _ in range(cfg.bootstrap_iterations):
        sampled = [generator.choice(ordered) for _ in range(cluster_count)]
        count = sum(int(cluster["count"]) for cluster in sampled)
        net_return_sum = sum(
            float(cluster["net_return_sum"]) for cluster in sampled
        )
        gross_profit = sum(float(cluster["gross_profit"]) for cluster in sampled)
        gross_loss = sum(float(cluster["gross_loss"]) for cluster in sampled)
        if count:
            boot_means.append(net_return_sum / count)
        if gross_loss > 0.0:
            boot_profit_factors.append(gross_profit / gross_loss)

    return {
        "method": (
            "signal-month cluster bootstrap; every draw resamples complete months "
            "with replacement"
        ),
        "clusters": cluster_count,
        "iterations": cfg.bootstrap_iterations,
        "seed": cfg.bootstrap_seed + 1_903,
        "average_net_trade_return": statistics.fmean(
            float(trade["net_return"]) for trade in trades
        ),
        "average_net_trade_return_ci95": [
            _quantile(boot_means, alpha),
            _quantile(boot_means, 1.0 - alpha),
        ],
        "profit_factor": _profit_factor_from_pnls(
            float(trade["pnl"]) for trade in trades
        ),
        "profit_factor_ci95": [
            _quantile(boot_profit_factors, alpha),
            _quantile(boot_profit_factors, 1.0 - alpha),
        ],
        "finite_profit_factor_draws": len(boot_profit_factors),
    }


def _yearly_returns(equity_curve: list[dict], initial_cash: float) -> dict[str, float]:
    year_end: dict[str, float] = {}
    for row in equity_curve:
        year_end[str(row["date"])[:4]] = float(row["equity"])
    result: dict[str, float] = {}
    previous = initial_cash
    for year in sorted(year_end):
        result[year] = year_end[year] / previous - 1.0 if previous > 0.0 else -1.0
        previous = year_end[year]
    return result


def _top_one_percent_winner_sensitivity(trades: list[dict]) -> dict:
    winners = [
        (index, trade)
        for index, trade in enumerate(trades)
        if float(trade["pnl"]) > 0.0
    ]
    remove_count = math.ceil(len(winners) * 0.01) if winners else 0
    # "Largest winner" is defined by return rather than share count/notional.
    removed_indices = {
        index
        for index, _ in sorted(
            winners,
            key=lambda item: (
                float(item[1]["net_return"]),
                float(item[1]["pnl"]),
                str(item[1]["code"]),
            ),
            reverse=True,
        )[:remove_count]
    }
    remaining = [
        trade for index, trade in enumerate(trades) if index not in removed_indices
    ]
    return {
        "definition": "remove the top 1% of profitable trades by net return (ceil)",
        "winning_trades": len(winners),
        "removed_trades": remove_count,
        "remaining_trades": len(remaining),
        "remaining_average_net_trade_return": statistics.fmean(
            float(trade["net_return"]) for trade in remaining
        )
        if remaining
        else None,
        "remaining_profit_factor": _profit_factor_from_pnls(
            float(trade["pnl"]) for trade in remaining
        ),
    }


def benchmark_summary(
    benchmark: PreparedBenchmark, start_date: str, end_date: str
) -> dict:
    start = bisect_left(benchmark.calendar, start_date)
    end = bisect_right(benchmark.calendar, end_date)
    values = [
        (benchmark.calendar[index], benchmark.normalized_closes[index])
        for index in range(start, end)
        if benchmark.normalized_closes[index] is not None
    ]
    if len(values) < 2:
        return {
            "name": "0050 normalized price return, dividends excluded",
            "status": "INSUFFICIENT_DATA",
        }
    peak = 0.0
    maximum_drawdown = 0.0
    for _, value in values:
        numeric = float(value)
        peak = max(peak, numeric)
        maximum_drawdown = max(
            maximum_drawdown, (peak - numeric) / peak if peak else 0.0
        )
    return {
        "name": "0050 normalized price return, dividends excluded",
        "start_date": values[0][0],
        "end_date": values[-1][0],
        "total_return": float(values[-1][1]) / float(values[0][1]) - 1.0,
        "maximum_drawdown": maximum_drawdown,
        "missing_benchmark_sessions": (end - start) - len(values),
    }


def _signal_sort_key(row: dict) -> tuple:
    rank = row.get(
        "daily_rank", row.get("pattern_rank", row.get("rank", 1_000_000_000))
    )
    return (int(rank), str(row["code"]))


def simulate_portfolio(
    signal_rows: list[dict],
    stocks: list[PreparedStock],
    benchmark: PreparedBenchmark,
    start_date: str,
    end_date: str,
    *,
    scenario: str = "baseline",
    cfg: Config = CFG,
) -> dict:
    """Run a research-only daily Open proxy; no order is ever submitted.

    A signal is known at T close. Both its quantity and its +3% limit are frozen
    then, before the T+1 regular-session Open is observed. Close-confirmed exits
    execute at the next available positive-volume Open. Missing stock bars still
    consume market-calendar holding sessions.
    """

    if scenario not in {"baseline", "stress"}:
        raise ValueError(f"unsupported scenario: {scenario}")
    slippage = (
        cfg.baseline_slippage_one_way
        if scenario == "baseline"
        else cfg.stress_slippage_one_way
    )
    commission_rate = (
        cfg.commission_rate if scenario == "baseline" else cfg.stress_commission_rate
    )
    target_return = cfg.primary_target
    stop_return = cfg.primary_stop

    stock_by_code = {stock.code: stock for stock in stocks}
    signals_by_date: dict[str, list[dict]] = defaultdict(list)
    for row in signal_rows:
        signal_date = str(row["signal_date"])
        if start_date <= signal_date <= end_date:
            signals_by_date[signal_date].append(row)
    for rows in signals_by_date.values():
        rows.sort(key=_signal_sort_key)

    start_index = bisect_left(benchmark.calendar, start_date)
    end_index = bisect_right(benchmark.calendar, end_date) - 1
    if start_index > end_index:
        raise ValueError("portfolio period has no market sessions")
    # A normal entry needs T+1, ten close observations by market-session count,
    # and one following Open. Censored positions may nevertheless remain open if
    # a suspension extends beyond end_date; that risk is reported explicitly.
    last_signal_index = end_index - cfg.maximum_holding_sessions - 1

    cash = float(cfg.research_cash)
    positions: dict[str, dict] = {}
    entry_orders: list[dict] = []
    trades: list[dict] = []
    equity_curve: list[dict] = []
    order_audit: dict[str, int] = defaultdict(int)
    market_dates = set(benchmark.calendar[start_index : end_index + 1])
    order_audit["signals_outside_market_calendar"] = sum(
        len(rows) for day, rows in signals_by_date.items() if day not in market_dates
    )

    for calendar_index in range(start_index, end_index + 1):
        day = benchmark.calendar[calendar_index]

        # Detect an unadjusted-price segment break at its first observable Open.
        # It overrides an earlier exit label because the raw-price return has
        # become corporate-action/data-gap censored. That same Open is the first
        # available causal liquidation proxy.
        for code, position in positions.items():
            found = _bar_position_at(stock_by_code[code], calendar_index)
            if found is None:
                continue
            local_index, _ = found
            current_segment = stock_by_code[code].segment_ids[local_index]
            if current_segment != position["entry_segment_id"]:
                if not position["corporate_action_censored"]:
                    position["pre_censor_pending_exit"] = position["pending_exit"]
                    order_audit["corporate_action_segment_breaks"] += 1
                position["corporate_action_censored"] = True
                position["pending_exit"] = "CORPORATE_ACTION_CENSORED"

        # Fill only the orders created at yesterday's close. Today's sale
        # proceeds are added later and therefore cannot finance these buys.
        for order in entry_orders:
            if order["code"] in positions:
                order_audit["duplicate_position_rejected"] += 1
                continue
            stock = stock_by_code.get(order["code"])
            found = _bar_position_at(stock, calendar_index) if stock else None
            if found is None or found[1].volume <= 0:
                order_audit["missing_or_zero_volume_t1"] += 1
                continue
            local_index, bar = found
            expected_segment = order.get("signal_segment_id")
            if (
                expected_segment is not None
                and stock.segment_ids[local_index] != expected_segment
            ):
                order_audit["t_plus_1_discontinuity_rejected"] += 1
                continue
            proxy_fill = bar.open * (1.0 + slippage)
            if proxy_fill > order["limit_price"] + 1e-12:
                order_audit["limit_not_filled"] += 1
                continue
            gross = order["quantity"] * proxy_fill
            fee = commission(gross, commission_rate, cfg.minimum_commission)
            if gross + fee > cash + 1e-9:
                order_audit["cash_rejected"] += 1
                continue
            cash -= gross + fee
            positions[order["code"]] = {
                "code": order["code"],
                "name": order["name"],
                "pattern": order.get("pattern"),
                "signal_date": order["signal_date"],
                "entry_date": day,
                "entry_calendar_index": calendar_index,
                "entry_raw_open": bar.open,
                "entry_price_proxy": proxy_fill,
                "entry_segment_id": stock.segment_ids[local_index],
                "shares": order["quantity"],
                "entry_gross": gross,
                "buy_fee": fee,
                "holding_sessions": 0,
                "pending_exit": None,
                "pre_censor_pending_exit": None,
                "corporate_action_censored": False,
                "last_mark": proxy_fill,
            }
            order_audit["proxy_entries"] += 1
        entry_orders = []

        # All close-confirmed/time/censor exits use the next positive-volume
        # regular-session Open proxy. Censor transitions detected above can exit
        # at this same first observable Open.
        exit_proceeds = 0.0
        for code, position in list(positions.items()):
            if position["pending_exit"] is None:
                continue
            stock = stock_by_code[code]
            found = _bar_position_at(stock, calendar_index)
            if found is None or found[1].volume <= 0:
                order_audit["pending_exit_carried"] += 1
                continue
            exit_local_index, bar = found
            proxy_fill = max(0.0, bar.open * (1.0 - slippage))
            gross = position["shares"] * proxy_fill
            sell_fee = commission(gross, commission_rate, cfg.minimum_commission)
            tax = float(round(gross * cfg.sell_tax_rate))
            proceeds = gross - sell_fee - tax
            pnl = proceeds - position["entry_gross"] - position["buy_fee"]
            exit_proceeds += proceeds
            trades.append(
                {
                    "scenario": scenario,
                    "code": code,
                    "name": position["name"],
                    "pattern": position["pattern"],
                    "signal_date": position["signal_date"],
                    "entry_date": position["entry_date"],
                    "exit_date": day,
                    "entry_raw_open": position["entry_raw_open"],
                    "exit_raw_open": bar.open,
                    "entry_price_proxy": position["entry_price_proxy"],
                    "exit_price_proxy": proxy_fill,
                    "entry_segment_id": position["entry_segment_id"],
                    "exit_segment_id": stock.segment_ids[exit_local_index],
                    "shares": position["shares"],
                    "holding_sessions": position["holding_sessions"],
                    "exit_reason": position["pending_exit"],
                    "pre_censor_pending_exit": position["pre_censor_pending_exit"],
                    "corporate_action_censored": position[
                        "corporate_action_censored"
                    ],
                    "entry_gross": position["entry_gross"],
                    "exit_gross": gross,
                    "buy_fee": position["buy_fee"],
                    "sell_fee": sell_fee,
                    "sell_tax": tax,
                    "pnl": pnl,
                    "net_return": pnl
                    / (position["entry_gross"] + position["buy_fee"]),
                    "explicit_costs": position["buy_fee"] + sell_fee + tax,
                    "costs": position["buy_fee"] + sell_fee + tax,
                    "is_actual_order": False,
                    "is_actual_fill": False,
                    "execution_proxy": (
                        "regular-session Open with slippage; historical odd-lot "
                        "fill not verified"
                    ),
                }
            )
            del positions[code]
        cash += exit_proceeds

        # Day 1 is the entry day's Close. Holding age advances on every market
        # session, including a session for which this stock has no bar.
        for code, position in positions.items():
            position["holding_sessions"] += 1
            found = _bar_position_at(stock_by_code[code], calendar_index)
            bar = found[1] if found is not None else None
            if bar is not None:
                position["last_mark"] = bar.close
            if position["pending_exit"] is not None:
                continue
            if bar is not None and bar.volume > 0:
                close_return = bar.close / position["entry_price_proxy"] - 1.0
                if close_return >= target_return:
                    position["pending_exit"] = "TARGET_CLOSE_CONFIRMED"
                elif close_return <= stop_return:
                    position["pending_exit"] = "STOP_CLOSE_CONFIRMED"
            if (
                position["pending_exit"] is None
                and position["holding_sessions"] >= cfg.maximum_holding_sessions
            ):
                position["pending_exit"] = "DAY10_TIME_EXIT"

        market_value = sum(
            position["shares"] * position["last_mark"]
            for position in positions.values()
        )
        equity_curve.append(
            {
                "scenario": scenario,
                "date": day,
                "cash": cash,
                "market_value": market_value,
                "equity": cash + market_value,
                "open_positions": len(positions),
            }
        )

        # Freeze next-session orders after this Close. Quantities use the worst
        # permissible price and do not expand if T+1 Open is cheaper.
        if calendar_index <= last_signal_index:
            available_slots = cfg.maximum_positions - len(positions)
            reserved_cash = 0.0
            reserved_codes: set[str] = set()
            for signal in signals_by_date.get(day, []):
                if available_slots <= 0:
                    order_audit["signals_rejected_no_slot"] += 1
                    continue
                code = str(signal["code"])
                if code in positions or code in reserved_codes:
                    order_audit["duplicate_signal_or_position_rejected"] += 1
                    continue
                signal_close = float(signal["signal_close"])
                if not math.isfinite(signal_close) or signal_close <= 0.0:
                    order_audit["invalid_signal_close"] += 1
                    continue
                allocation = max(0.0, cash - reserved_cash) / available_slots
                limit_price = signal_close * (1.0 + cfg.entry_limit_above_signal)
                quantity = min(
                    cfg.maximum_odd_lot_shares,
                    math.floor(
                        max(0.0, allocation - cfg.minimum_commission) / limit_price
                    ),
                )
                while quantity > 0:
                    worst_gross = quantity * limit_price
                    worst_fee = commission(
                        worst_gross, commission_rate, cfg.minimum_commission
                    )
                    if worst_gross + worst_fee <= allocation + 1e-9:
                        break
                    quantity -= 1
                if quantity <= 0:
                    order_audit["zero_quantity"] += 1
                    continue
                worst_gross = quantity * limit_price
                worst_fee = commission(
                    worst_gross, commission_rate, cfg.minimum_commission
                )
                entry_orders.append(
                    {
                        "code": code,
                        "name": str(signal.get("name", "")),
                        "pattern": signal.get("pattern"),
                        "signal_date": day,
                        "signal_segment_id": signal.get("signal_segment_id"),
                        "limit_price": limit_price,
                        "quantity": quantity,
                        "worst_case_reserved_cash": worst_gross + worst_fee,
                        "is_actual_order": False,
                    }
                )
                reserved_codes.add(code)
                reserved_cash += worst_gross + worst_fee
                available_slots -= 1
                order_audit["orders_created"] += 1
        elif signals_by_date.get(day):
            order_audit["signals_rejected_insufficient_forward_calendar"] += len(
                signals_by_date[day]
            )

    unresolved: list[dict] = []
    for position in positions.values():
        unresolved.append(
            {
                "code": position["code"],
                "name": position["name"],
                "pattern": position["pattern"],
                "signal_date": position["signal_date"],
                "entry_date": position["entry_date"],
                "shares": position["shares"],
                "holding_sessions": position["holding_sessions"],
                "last_mark": position["last_mark"],
                "mark_value": position["shares"] * position["last_mark"],
                "pending_exit": position["pending_exit"],
                "corporate_action_censored": position[
                    "corporate_action_censored"
                ],
                "zero_value_sensitivity_contribution": 0.0,
                "is_actual_order": False,
                "is_actual_fill": False,
            }
        )

    final_equity = equity_curve[-1]["equity"] if equity_curve else cfg.research_cash
    zero_value_equity = cash
    pnls = [float(trade["pnl"]) for trade in trades]
    net_returns = [float(trade["net_return"]) for trade in trades]
    wins = [value for value in pnls if value > 0.0]
    peak = float(cfg.research_cash)
    maximum_drawdown = 0.0
    for row in equity_curve:
        equity = float(row["equity"])
        peak = max(peak, equity)
        maximum_drawdown = max(
            maximum_drawdown, (peak - equity) / peak if peak else 0.0
        )

    yearly_returns = _yearly_returns(equity_curve, float(cfg.research_cash))
    monthly_bootstrap = _monthly_cluster_bootstrap(trades, cfg)
    top_one_percent = _top_one_percent_winner_sensitivity(trades)
    corporate_action_censored_trades = sum(
        bool(trade["corporate_action_censored"]) for trade in trades
    )
    unresolved_censored_positions = sum(
        bool(position["corporate_action_censored"]) for position in unresolved
    )

    summary = {
        "strategy_id": cfg.strategy_id,
        "result_status": cfg.result_status,
        "scenario": scenario,
        "initial_cash": cfg.research_cash,
        "final_equity_mark_to_market": final_equity,
        "total_return_mark_to_market": final_equity / cfg.research_cash - 1.0,
        "zero_value_unresolved_sensitivity_equity": zero_value_equity,
        "zero_value_unresolved_sensitivity_return": zero_value_equity
        / cfg.research_cash
        - 1.0,
        "maximum_drawdown": maximum_drawdown,
        "yearly_returns": yearly_returns,
        "completed_trades": len(trades),
        "unresolved_positions": len(unresolved),
        "trade_win_rate": len(wins) / len(trades) if trades else None,
        "profit_factor": _profit_factor_from_pnls(pnls),
        "average_net_trade_return": statistics.fmean(net_returns)
        if net_returns
        else None,
        "median_net_trade_return": statistics.median(net_returns)
        if net_returns
        else None,
        "top1_percent_winners_removed": top_one_percent,
        "top1_winner_removed_average_net_trade_return": top_one_percent[
            "remaining_average_net_trade_return"
        ],
        "top1_winner_removed_profit_factor": top_one_percent[
            "remaining_profit_factor"
        ],
        "top1_percent_removed_average_net_trade_return": top_one_percent[
            "remaining_average_net_trade_return"
        ],
        "top1_percent_removed_profit_factor": top_one_percent[
            "remaining_profit_factor"
        ],
        "monthly_cluster_bootstrap": monthly_bootstrap,
        "total_explicit_costs": sum(float(trade["costs"]) for trade in trades),
        "corporate_action_censored_trades": corporate_action_censored_trades,
        "unresolved_corporate_action_censored_positions": (
            unresolved_censored_positions
        ),
        "corporate_action_disclosure": (
            "Unadjusted OHLCV segment changes are exited at the first observable "
            "positive-volume Open and labelled CORPORATE_ACTION_CENSORED; the "
            "detector cannot identify every ex-right/ex-dividend event."
        ),
        "proxy_only": True,
        "is_actual_order": False,
        "is_actual_fill": False,
    }
    return {
        "summary": summary,
        "trades": trades,
        "equity_curve": equity_curve,
        "unresolved": unresolved,
        "order_audit": dict(sorted(order_audit.items())),
        "benchmark": benchmark_summary(benchmark, start_date, end_date),
    }


def execution_proxy_decision(
    baseline: dict, stress: dict, cfg: Config = CFG
) -> dict:
    """Apply only the immutable execution-proxy gates pre-registered in Config."""

    summary = baseline["summary"]
    stress_summary = stress["summary"]
    yearly_returns = summary.get("yearly_returns", {})
    profit_factor = summary.get("profit_factor")
    gates = {
        "baseline_total_return_positive": summary["total_return_mark_to_market"]
        > 0.0,
        "baseline_profit_factor_at_least_configured_minimum": (
            profit_factor is not None
            and profit_factor >= cfg.validation_minimum_profit_factor
        ),
        "every_observed_calendar_year_positive": bool(yearly_returns)
        and all(value > 0.0 for value in yearly_returns.values()),
        "maximum_drawdown_within_configured_limit": summary["maximum_drawdown"]
        <= cfg.validation_maximum_drawdown,
        "stress_total_return_nonnegative": stress_summary[
            "total_return_mark_to_market"
        ]
        >= 0.0,
        "no_unresolved_positions": summary["unresolved_positions"] == 0
        and stress_summary["unresolved_positions"] == 0,
    }
    return {
        "status": (
            "EXECUTION_PROXY_VIABLE" if all(gates.values()) else "EXECUTION_PROXY_FAIL"
        ),
        "all_gates_passed": all(gates.values()),
        "gates": gates,
        "configured_profit_factor_minimum": cfg.validation_minimum_profit_factor,
        "configured_maximum_drawdown": cfg.validation_maximum_drawdown,
        "warning": (
            "Daily regular-session Open is a proxy, not a verified historical "
            "odd-lot fill; no broker connection or order submission exists."
        ),
    }
