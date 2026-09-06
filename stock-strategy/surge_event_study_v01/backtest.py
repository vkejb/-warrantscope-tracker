from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
import math
import random
import statistics

from .config import CFG, Config
from .models import Bar, PreparedBenchmark, PreparedStock


def commission(gross: float, rate: float, minimum: int) -> float:
    return float(max(minimum, round(gross * rate))) if gross > 0 else 0.0


def _bar_at(stock: PreparedStock, calendar_index: int) -> Bar | None:
    position = bisect_left(stock.calendar_indices, calendar_index)
    if position < len(stock.calendar_indices) and stock.calendar_indices[position] == calendar_index:
        return stock.bars[position]
    return None


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _monthly_return_ci(equity_curve: list[dict], cfg: Config) -> dict:
    month_end: dict[str, float] = {}
    for row in equity_curve:
        month_end[row["date"][:6]] = row["equity"]
    ordered = sorted(month_end.items())
    monthly_returns = [
        ordered[index][1] / ordered[index - 1][1] - 1.0
        for index in range(1, len(ordered))
        if ordered[index - 1][1] > 0
    ]
    if not monthly_returns:
        return {"months": 0, "mean": None, "ci95": [None, None]}
    generator = random.Random(cfg.bootstrap_seed + 991)
    means = []
    for _ in range(cfg.bootstrap_iterations):
        sample = [generator.choice(monthly_returns) for _ in monthly_returns]
        means.append(statistics.fmean(sample))
    return {
        "months": len(monthly_returns),
        "mean": statistics.fmean(monthly_returns),
        "ci95": [_quantile(means, 0.025), _quantile(means, 0.975)],
        "method": "calendar-month return bootstrap",
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
        return {"name": "0050 price return, dividends excluded", "status": "INSUFFICIENT_DATA"}
    peak = 0.0
    drawdown = 0.0
    for _, value in values:
        peak = max(peak, float(value))
        drawdown = max(drawdown, (peak - float(value)) / peak if peak else 0.0)
    return {
        "name": "0050 normalized price return, dividends excluded",
        "start_date": values[0][0],
        "end_date": values[-1][0],
        "total_return": float(values[-1][1]) / float(values[0][1]) - 1.0,
        "maximum_drawdown": drawdown,
        "missing_benchmark_sessions": (end - start) - len(values),
    }


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
    """Daily-open proxy simulation; never represents actual odd-lot fills."""

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
    stock_by_code = {stock.code: stock for stock in stocks}
    signals_by_date: dict[str, list[dict]] = defaultdict(list)
    for row in signal_rows:
        if start_date <= row["signal_date"] <= end_date:
            signals_by_date[row["signal_date"]].append(row)
    for rows in signals_by_date.values():
        rows.sort(key=lambda row: (row["daily_rank"], row["code"]))

    start_index = bisect_left(benchmark.calendar, start_date)
    end_index = bisect_right(benchmark.calendar, end_date) - 1
    if start_index > end_index:
        raise ValueError("portfolio period has no market sessions")
    # Ensure all accepted signals can reach Day10 close and the next open inside
    # the stated period. This avoids leaking a later period into validation.
    last_signal_index = end_index - cfg.maximum_holding_sessions - 1

    cash = cfg.research_cash
    positions: dict[str, dict] = {}
    entry_orders: list[dict] = []
    trades: list[dict] = []
    equity_curve: list[dict] = []
    order_audit: dict[str, int] = defaultdict(int)

    for calendar_index in range(start_index, end_index + 1):
        day = benchmark.calendar[calendar_index]

        # Orders and quantities were fixed at T close using the 3% limit price.
        for order in entry_orders:
            if order["code"] in positions:
                order_audit["duplicate_position_rejected"] += 1
                continue
            stock = stock_by_code.get(order["code"])
            bar = _bar_at(stock, calendar_index) if stock else None
            if bar is None or bar.volume <= 0:
                order_audit["missing_or_zero_volume_t1"] += 1
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
                "signal_date": order["signal_date"],
                "entry_date": day,
                "entry_price_proxy": proxy_fill,
                "shares": order["quantity"],
                "entry_gross": gross,
                "buy_fee": fee,
                "holding_sessions": 0,
                "pending_exit": None,
                "last_mark": proxy_fill,
            }
            order_audit["proxy_entries"] += 1
        entry_orders = []

        # A prior close-confirmed exit executes at this day's next available open.
        exit_proceeds = 0.0
        for code, position in list(positions.items()):
            if position["pending_exit"] is None:
                continue
            stock = stock_by_code[code]
            bar = _bar_at(stock, calendar_index)
            if bar is None or bar.volume <= 0:
                order_audit["pending_exit_carried"] += 1
                continue
            proxy_fill = max(0.0, bar.open * (1.0 - slippage))
            gross = position["shares"] * proxy_fill
            sell_fee = commission(gross, commission_rate, cfg.minimum_commission)
            tax = round(gross * cfg.sell_tax_rate)
            proceeds = gross - sell_fee - tax
            pnl = proceeds - position["entry_gross"] - position["buy_fee"]
            exit_proceeds += proceeds
            trades.append(
                {
                    "scenario": scenario,
                    "code": code,
                    "name": position["name"],
                    "signal_date": position["signal_date"],
                    "entry_date": position["entry_date"],
                    "exit_date": day,
                    "entry_price_proxy": position["entry_price_proxy"],
                    "exit_price_proxy": proxy_fill,
                    "shares": position["shares"],
                    "holding_sessions": position["holding_sessions"],
                    "exit_reason": position["pending_exit"],
                    "pnl": pnl,
                    "net_return": pnl
                    / (position["entry_gross"] + position["buy_fee"]),
                    "costs": position["buy_fee"] + sell_fee + tax,
                    "is_actual_order": False,
                    "is_actual_fill": False,
                }
            )
            del positions[code]
        # Sale proceeds are deliberately unavailable to this morning's buys.
        cash += exit_proceeds

        for code, position in positions.items():
            position["holding_sessions"] += 1
            bar = _bar_at(stock_by_code[code], calendar_index)
            if bar is not None:
                position["last_mark"] = bar.close
            if position["pending_exit"] is not None:
                continue
            if bar is not None and bar.volume > 0:
                close_return = bar.close / position["entry_price_proxy"] - 1.0
                if close_return >= cfg.target_close_return:
                    position["pending_exit"] = "TARGET_CLOSE_CONFIRMED"
                elif close_return <= cfg.stop_close_return:
                    position["pending_exit"] = "STOP_CLOSE_CONFIRMED"
            if (
                position["pending_exit"] is None
                and position["holding_sessions"] >= cfg.maximum_holding_sessions
            ):
                position["pending_exit"] = "DAY10_TIME_EXIT"

        market_value = sum(
            position["shares"] * position["last_mark"] for position in positions.values()
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

        # Create causal orders after the close. Quantity uses the worst allowed
        # limit, not the next morning's potentially cheaper Open.
        if calendar_index <= last_signal_index:
            available_slots = cfg.maximum_positions - len(positions)
            reserved_cash = 0.0
            reserved_codes: set[str] = set()
            for signal in signals_by_date.get(day, []):
                if available_slots <= 0:
                    break
                code = signal["code"]
                if code in positions or code in reserved_codes:
                    continue
                allocation = max(0.0, cash - reserved_cash) / available_slots
                limit_price = signal["signal_close"] * (1.0 + cfg.entry_limit_above_signal)
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
                worst_cash = quantity * limit_price + commission(
                    quantity * limit_price, commission_rate, cfg.minimum_commission
                )
                entry_orders.append(
                    {
                        "code": code,
                        "name": signal["name"],
                        "signal_date": day,
                        "limit_price": limit_price,
                        "quantity": quantity,
                    }
                )
                reserved_codes.add(code)
                reserved_cash += worst_cash
                available_slots -= 1
                order_audit["orders_created"] += 1

    unresolved = []
    zero_value_equity = cash
    for position in positions.values():
        mark_value = position["shares"] * position["last_mark"]
        zero_value_equity += 0.0
        unresolved.append(
            {
                "code": position["code"],
                "name": position["name"],
                "entry_date": position["entry_date"],
                "shares": position["shares"],
                "last_mark": position["last_mark"],
                "mark_value": mark_value,
                "pending_exit": position["pending_exit"],
            }
        )
    final_equity = equity_curve[-1]["equity"] if equity_curve else cfg.research_cash
    wins = [trade["pnl"] for trade in trades if trade["pnl"] > 0]
    losses = [trade["pnl"] for trade in trades if trade["pnl"] < 0]
    peak = 0.0
    maximum_drawdown = 0.0
    for row in equity_curve:
        peak = max(peak, row["equity"])
        maximum_drawdown = max(
            maximum_drawdown,
            (peak - row["equity"]) / peak if peak else 0.0,
        )
    monthly = _monthly_return_ci(equity_curve, cfg)
    summary = {
        "scenario": scenario,
        "initial_cash": cfg.research_cash,
        "final_equity_mark_to_market": final_equity,
        "total_return_mark_to_market": final_equity / cfg.research_cash - 1.0,
        "zero_value_unresolved_sensitivity_equity": zero_value_equity,
        "zero_value_unresolved_sensitivity_return": zero_value_equity
        / cfg.research_cash
        - 1.0,
        "maximum_drawdown": maximum_drawdown,
        "completed_trades": len(trades),
        "unresolved_positions": len(unresolved),
        "trade_win_rate": len(wins) / len(trades) if trades else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "average_net_trade_return": statistics.fmean(
            trade["net_return"] for trade in trades
        )
        if trades
        else None,
        "median_net_trade_return": statistics.median(
            trade["net_return"] for trade in trades
        )
        if trades
        else None,
        "total_costs": sum(trade["costs"] for trade in trades),
        "monthly_net_return": monthly,
        "proxy_only": True,
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


def execution_proxy_decision(baseline: dict, stress: dict) -> dict:
    summary = baseline["summary"]
    stress_summary = stress["summary"]
    monthly_lower = summary["monthly_net_return"]["ci95"][0]
    gates = {
        "baseline_total_return_positive": summary["total_return_mark_to_market"] > 0,
        "baseline_profit_factor_above_one": summary["profit_factor"] is not None
        and summary["profit_factor"] > 1.0,
        "at_least_100_completed_trades": summary["completed_trades"] >= 100,
        "monthly_return_ci_lower_positive": monthly_lower is not None
        and monthly_lower > 0.0,
        "stress_total_return_nonnegative": stress_summary[
            "total_return_mark_to_market"
        ]
        >= 0.0,
        "no_unresolved_positions": summary["unresolved_positions"] == 0
        and stress_summary["unresolved_positions"] == 0,
    }
    return {
        "status": "EXECUTION_PROXY_VIABLE" if all(gates.values()) else "EXECUTION_PROXY_FAIL",
        "all_gates_passed": all(gates.values()),
        "gates": gates,
        "warning": "Daily regular-session Open is not a verified historical odd-lot fill.",
    }
