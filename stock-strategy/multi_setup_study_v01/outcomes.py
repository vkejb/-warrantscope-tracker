from __future__ import annotations

from surge_event_study_v01.models import PreparedStock
from v21.backtest import net_return as v21_net_return
from v21.config import CFG as V21_CFG

from .config import CFG, Config


def _censored(reason: str, **values) -> dict:
    return {
        "outcome_status": "CENSORED",
        "outcome_reason": reason,
        "entry_date": None,
        "entry_open_proxy": None,
        "entry_gap": None,
        "primary_success": None,
        "path_result": None,
        "first_target_day": None,
        "first_stop_day": None,
        "plus10_before_minus5": None,
        "plus15_before_minus5": None,
        "day1_close_return": None,
        "day3_close_return": None,
        "day5_close_return": None,
        "day10_close_return": None,
        "mfe_5d": None,
        "mfe_10d": None,
        "mae_5d": None,
        "mae_10d": None,
        "mfe_abs_mae": None,
        "gross_return": None,
        "net_return": None,
        "gross_exit_price": None,
        "is_actual_order": False,
        "is_actual_fill": False,
        **values,
    }


def _first_day(returns: list[float], threshold: float, *, above: bool) -> int | None:
    for day, value in enumerate(returns, 1):
        if (above and value >= threshold - 1e-12) or (
            not above and value <= threshold + 1e-12
        ):
            return day
    return None


def _before_stop(
    returns: list[float], target: float, stop: float
) -> tuple[bool, int | None, int | None]:
    target_day = _first_day(returns, target, above=True)
    stop_day = _first_day(returns, stop, above=False)
    success = target_day is not None and (stop_day is None or target_day < stop_day)
    return success, target_day, stop_day


def cost_adjusted_return(
    entry: float, exit_price: float, cfg: Config = CFG
) -> float:
    """Resolve the existing V2.1 cost model and fail if its contract drifts."""

    resolved = {
        "per_trade_notional": V21_CFG.per_trade_notional,
        "commission_rate": V21_CFG.commission_rate * V21_CFG.commission_discount,
        "minimum_commission": V21_CFG.minimum_commission,
        "sell_tax_rate": V21_CFG.stock_transaction_tax,
    }
    expected = {
        "per_trade_notional": cfg.per_trade_notional,
        "commission_rate": cfg.commission_rate,
        "minimum_commission": cfg.minimum_commission,
        "sell_tax_rate": cfg.sell_tax_rate,
    }
    if resolved != expected:
        raise RuntimeError(
            f"shared V2.1 cost model drifted: resolved={resolved}, expected={expected}"
        )
    return v21_net_return(entry, exit_price, cfg.slippage_one_way)


def evaluate_unified_outcome(
    stock: PreparedStock, index: int, cfg: Config = CFG
) -> dict:
    """Apply one outcome definition to every setup, after signal formation."""

    if index + 1 >= len(stock.bars):
        return _censored("MISSING_T_PLUS_1_BAR")
    signal = stock.bars[index]
    entry = stock.bars[index + 1]
    if stock.calendar_indices[index + 1] != stock.calendar_indices[index] + 1:
        return _censored("MISSING_T_PLUS_1_BAR")
    entry_values = {
        "entry_date": entry.date,
        "entry_open_proxy": entry.open,
        "entry_gap": entry.open / signal.close - 1.0,
    }
    if entry.volume <= 0:
        return _censored("T_PLUS_1_ZERO_VOLUME", **entry_values)
    if stock.segment_ids[index + 1] != stock.segment_ids[index]:
        return _censored("T_PLUS_1_DISCONTINUITY", **entry_values)
    end = index + cfg.primary_horizon
    if end >= len(stock.bars):
        return _censored("INCOMPLETE_FORWARD_WINDOW", **entry_values)
    if stock.calendar_indices[end] != stock.calendar_indices[index] + cfg.primary_horizon:
        return _censored("MISSING_FORWARD_SESSION", **entry_values)
    if stock.segment_ids[end] != stock.segment_ids[index]:
        return _censored("FORWARD_DISCONTINUITY", **entry_values)

    window = stock.bars[index + 1 : end + 1]
    returns = [bar.close / entry.open - 1.0 for bar in window]
    primary_success, target_day, stop_day = _before_stop(
        returns, cfg.primary_target, cfg.primary_stop
    )
    plus10, _, _ = _before_stop(returns, 0.10, cfg.primary_stop)
    plus15, _, _ = _before_stop(returns, 0.15, cfg.primary_stop)
    if primary_success:
        path_result = "TARGET_BEFORE_STOP"
        exit_day = int(target_day or cfg.primary_horizon)
    elif stop_day is not None and (target_day is None or stop_day < target_day):
        path_result = "STOP_BEFORE_TARGET"
        exit_day = stop_day
    else:
        path_result = "DAY10_TIMEOUT"
        exit_day = cfg.primary_horizon
    gross_return = returns[exit_day - 1]
    exit_price = window[exit_day - 1].close
    net = cost_adjusted_return(entry.open, exit_price, cfg)
    mfe5 = max(returns[:5])
    mfe10 = max(returns)
    mae5 = min(returns[:5])
    mae10 = min(returns)
    ratio = mfe10 / abs(mae10) if abs(mae10) > 1e-15 else None
    return {
        "outcome_status": "EVALUABLE",
        "outcome_reason": "COMPLETE_FORWARD_WINDOW",
        **entry_values,
        "primary_success": primary_success,
        "path_result": path_result,
        "first_target_day": target_day,
        "first_stop_day": stop_day,
        "plus10_before_minus5": plus10,
        "plus15_before_minus5": plus15,
        "day1_close_return": returns[0],
        "day3_close_return": returns[2],
        "day5_close_return": returns[4],
        "day10_close_return": returns[9],
        "mfe_5d": mfe5,
        "mfe_10d": mfe10,
        "mae_5d": mae5,
        "mae_10d": mae10,
        "mfe_abs_mae": ratio,
        "gross_return": gross_return,
        "net_return": net,
        "gross_exit_price": exit_price,
        "is_actual_order": False,
        "is_actual_fill": False,
    }
