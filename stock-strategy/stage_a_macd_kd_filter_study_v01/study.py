from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math

import numpy as np

from multi_setup_study_v01.config import CFG as COST_CFG


MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
KD_WINDOW = 9
KD_SMOOTH = 3
WARMUP = 60


@dataclass(frozen=True, slots=True)
class Indicator:
    dif: float
    dea: float
    histogram: float
    k: float
    d: float

    @property
    def macd_pass(self) -> bool:
        return self.dif > self.dea and self.dif > 0.0

    @property
    def kd_pass(self) -> bool:
        return self.k > self.d and self.k < 80.0


def indicators_for_stock(stock, wanted_dates: set[int]) -> dict[tuple[int, int], Indicator]:
    """Compute causal 12/26/9 MACD and 9/3/3 KD, resetting at discontinuities."""

    result: dict[tuple[int, int], Indicator] = {}
    ema_fast = ema_slow = dea = math.nan
    k = d = 50.0
    segment = None
    segment_start = 0
    alpha_fast = 2.0 / (MACD_FAST + 1.0)
    alpha_slow = 2.0 / (MACD_SLOW + 1.0)
    alpha_signal = 2.0 / (MACD_SIGNAL + 1.0)
    kd_alpha = 1.0 / KD_SMOOTH

    for index, bar in enumerate(stock.bars):
        if segment != stock.segment_ids[index]:
            segment = stock.segment_ids[index]
            segment_start = index
            ema_fast = ema_slow = bar.close
            dea = 0.0
            k = d = 50.0
        else:
            ema_fast += alpha_fast * (bar.close - ema_fast)
            ema_slow += alpha_slow * (bar.close - ema_slow)
        dif = ema_fast - ema_slow
        dea += alpha_signal * (dif - dea)

        if index - segment_start + 1 >= KD_WINDOW:
            window = stock.bars[index - KD_WINDOW + 1 : index + 1]
            low = min(item.low for item in window)
            high = max(item.high for item in window)
            rsv = 50.0 if high <= low else (bar.close - low) / (high - low) * 100.0
            k += kd_alpha * (rsv - k)
            d += kd_alpha * (k - d)

        signal_date = int(bar.date)
        if signal_date in wanted_dates and index - segment_start + 1 >= WARMUP:
            result[(signal_date, int(stock.code))] = Indicator(dif, dea, dif - dea, k, d)
    return result


def build_indicator_store(prepared: list, wanted_keys: set[tuple[int, int]]) -> dict[tuple[int, int], Indicator]:
    dates_by_code: dict[int, set[int]] = {}
    for signal_date, stock_code in wanted_keys:
        dates_by_code.setdefault(stock_code, set()).add(signal_date)
    output: dict[tuple[int, int], Indicator] = {}
    for stock in prepared:
        code = int(stock.code)
        if code not in dates_by_code:
            continue
        output.update(indicators_for_stock(stock, dates_by_code[code]))
    return output


def commission(notional: float) -> float:
    return float(max(COST_CFG.minimum_commission, round(notional * COST_CFG.commission_rate)))


def buy_cost(raw_open: float, shares: int) -> tuple[float, float]:
    execution = raw_open * (1.0 + COST_CFG.slippage_one_way)
    notional = execution * shares
    return execution, notional + commission(notional)


def sell_proceeds(raw_open: float, shares: int) -> tuple[float, float]:
    execution = raw_open * (1.0 - COST_CFG.slippage_one_way)
    notional = execution * shares
    proceeds = notional - commission(notional) - round(notional * COST_CFG.sell_tax_rate)
    return execution, proceeds


def maximum_drawdown(values: list[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def cagr(start_value: float, end_value: float, start_day: str, end_day: str) -> float:
    years = (date.fromisoformat(f"{end_day[:4]}-{end_day[4:6]}-{end_day[6:]}") - date.fromisoformat(
        f"{start_day[:4]}-{start_day[4:6]}-{start_day[6:]}"
    )).days / 365.2425
    return (end_value / start_value) ** (1.0 / years) - 1.0


def simulate_portfolio(
    name: str,
    calendar: list[str],
    stage_by_date: dict[str, list[int]],
    eligible_by_date: dict[str, set[int]],
    bars_by_code_date: dict[tuple[int, str], object],
    *,
    start_capital: float = 30_000.0,
) -> tuple[dict, list[dict], list[dict]]:
    """Execute T-close selections at T+1 open; filter applies only to new entries."""

    signal_days = sorted(stage_by_date)
    usable_signal_days = signal_days[:-1]
    signal_set = set(usable_signal_days)
    index_by_day = {day: index for index, day in enumerate(calendar)}
    execution_to_signal = {
        calendar[index_by_day[day] + 1]: day
        for day in usable_signal_days
        if index_by_day[day] + 1 < len(calendar)
    }
    liquidation_day = calendar[index_by_day[usable_signal_days[-1]] + 2]
    final_calendar = [day for day in calendar if execution_to_signal.get(day) or day <= liquidation_day]
    first_execution = min(execution_to_signal)
    final_calendar = [day for day in final_calendar if day >= first_execution]

    cash = start_capital
    positions: dict[int, dict] = {}
    trades: list[dict] = []
    equity: list[dict] = []

    for day in final_calendar:
        if day == liquidation_day:
            target: set[int] = set()
            signal_day = None
            target_order: list[int] = []
        else:
            signal_day = execution_to_signal.get(day)
            if signal_day is None:
                target = set(positions)
                target_order = list(positions)
            else:
                target_order = stage_by_date[signal_day]
                target = set(target_order)

        for code in sorted(set(positions) - target):
            bar = bars_by_code_date.get((code, day))
            if bar is None or bar.volume <= 0:
                continue
            position = positions.pop(code)
            sell_price, proceeds = sell_proceeds(bar.open, position["shares"])
            cash += proceeds
            pnl = proceeds - position["total_cost"]
            trades.append({
                "strategy": name, "stock_code": code,
                "entry_signal_date": position["signal_date"], "entry_date": position["entry_date"],
                "exit_date": day, "shares": position["shares"],
                "entry_price": position["entry_price"], "exit_price": sell_price,
                "total_cost": position["total_cost"], "proceeds": proceeds,
                "net_pnl": pnl, "net_return": pnl / position["total_cost"],
                "holding_sessions": position["holding_sessions"],
            })

        if signal_day is not None:
            nav_open = cash
            for code, position in positions.items():
                bar = bars_by_code_date.get((code, day))
                nav_open += position["shares"] * (bar.open if bar is not None else position["last_close"])
            budget = min(nav_open / 30.0, 2_000.0)
            allowed = eligible_by_date.get(signal_day, set())
            for code in target_order:
                if code in positions or code not in allowed:
                    continue
                bar = bars_by_code_date.get((code, day))
                if bar is None or bar.volume <= 0:
                    continue
                execution = bar.open * (1.0 + COST_CFG.slippage_one_way)
                shares = int((min(budget, cash) - COST_CFG.minimum_commission) // execution)
                while shares > 0:
                    entry_price, total_cost = buy_cost(bar.open, shares)
                    if total_cost <= budget + 1e-9 and total_cost <= cash + 1e-9:
                        break
                    shares -= 1
                if shares <= 0:
                    continue
                cash -= total_cost
                positions[code] = {
                    "shares": shares, "signal_date": signal_day, "entry_date": day,
                    "entry_price": entry_price, "total_cost": total_cost,
                    "last_close": bar.close, "holding_sessions": 0,
                }

        nav_close = cash
        for code, position in positions.items():
            bar = bars_by_code_date.get((code, day))
            if bar is not None and bar.volume > 0:
                position["last_close"] = bar.close
                position["holding_sessions"] += 1
            nav_close += position["shares"] * position["last_close"]
        equity.append({"strategy": name, "date": day, "equity": nav_close, "cash": cash, "positions": len(positions)})
        if day == liquidation_day:
            break

    if positions:
        raise RuntimeError(f"{name}: final liquidation incomplete")
    returns = np.asarray([row["net_return"] for row in trades], dtype=float)
    profits = np.asarray([row["net_pnl"] for row in trades], dtype=float)
    positive = float(np.sum(profits[profits > 0]))
    negative = float(-np.sum(profits[profits < 0]))
    ending = float(equity[-1]["equity"])
    summary = {
        "strategy": name,
        "starting_capital": start_capital,
        "ending_equity": ending,
        "total_return": ending / start_capital - 1.0,
        "cagr": cagr(start_capital, ending, equity[0]["date"], equity[-1]["date"]),
        "max_drawdown": maximum_drawdown([row["equity"] for row in equity]),
        "closed_trades": len(trades),
        "win_rate": float(np.mean(returns > 0)) if len(returns) else math.nan,
        "average_net_trade_return": float(np.mean(returns)) if len(returns) else math.nan,
        "median_net_trade_return": float(np.median(returns)) if len(returns) else math.nan,
        "net_profit_factor": positive / negative if negative else math.inf,
        "average_holding_sessions": float(np.mean([row["holding_sessions"] for row in trades])) if trades else math.nan,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
    }
    return summary, trades, equity


def path_summary(name: str, mask: np.ndarray, outcomes: np.ndarray, evaluable: np.ndarray) -> dict:
    local = mask & evaluable
    success = outcomes[local, 0]
    gross = outcomes[local, 10]
    net = outcomes[local, 11]
    return {
        "strategy": name,
        "evaluable_stage_a_signal_rows": int(np.count_nonzero(local)),
        "plus8_before_minus5_rate": float(np.mean(success)) if len(success) else math.nan,
        "common_outcome_gross_mean": float(np.mean(gross)) if len(gross) else math.nan,
        "common_outcome_net_mean": float(np.mean(net)) if len(net) else math.nan,
    }

