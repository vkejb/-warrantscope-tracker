from __future__ import annotations

import math

from multi_setup_study_v01.features import benchmark_return
from surge_event_study_v01.models import PreparedBenchmark, PreparedStock

from .config import FEATURE_NAMES


def _mean(prefix: list[float], start: int, end: int) -> float:
    if start < 0 or end <= start:
        raise ValueError("invalid causal mean window")
    return (prefix[end] - prefix[start]) / (end - start)


def _ma(stock: PreparedStock, index: int, sessions: int) -> float:
    return _mean(stock.prefix_close, index - sessions + 1, index + 1)


def _atr(stock: PreparedStock, index: int, sessions: int) -> float:
    return _mean(stock.prefix_true_range_ratio, index - sessions + 1, index + 1)


def _slope(stock: PreparedStock, index: int, sessions: int) -> float:
    if index - 5 < sessions - 1:
        return math.nan
    current = _ma(stock, index, sessions)
    previous = _ma(stock, index - 5, sessions)
    return current / previous - 1.0


def _relative_strength(
    stock: PreparedStock,
    index: int,
    benchmark: PreparedBenchmark,
    sessions: int,
) -> float:
    market = benchmark_return(benchmark, stock.calendar_indices[index], sessions)
    if market is None:
        return math.nan
    stock_return = stock.bars[index].close / stock.bars[index - sessions].close - 1.0
    return (1.0 + stock_return) / (1.0 + market) - 1.0


def _latest_max_index(values: list[float]) -> int:
    maximum = max(values)
    return max(index for index, value in enumerate(values) if value == maximum)


def build_taxonomy_features(
    stock: PreparedStock,
    index: int,
    benchmark: PreparedBenchmark,
) -> tuple[float, ...]:
    """Build all taxonomy inputs from T and earlier bars only."""

    if index < 60:
        raise ValueError("taxonomy features require 60 causal sessions")
    bars = stock.bars
    current = bars[index]
    if stock.segment_ids[index - 60] != stock.segment_ids[index]:
        raise ValueError("taxonomy feature window crosses a discontinuity")
    close = current.close
    prior20_close = [bar.close for bar in bars[index - 20 : index]]
    prior60_close = [bar.close for bar in bars[index - 60 : index]]
    high20 = max(bar.high for bar in bars[index - 19 : index + 1])
    high60 = max(bar.high for bar in bars[index - 59 : index + 1])
    close20 = [bar.close for bar in bars[index - 19 : index + 1]]
    close60 = [bar.close for bar in bars[index - 59 : index + 1]]
    ma5 = _ma(stock, index, 5)
    ma10 = _ma(stock, index, 10)
    ma20 = _ma(stock, index, 20)
    ma60 = _ma(stock, index, 60)
    atr5 = _atr(stock, index, 5)
    atr20 = _atr(stock, index, 20)
    atr60 = _atr(stock, index, 60)
    if atr20 <= 0:
        raise ValueError("ATR20 must be positive")
    average_volume_20 = _mean(stock.prefix_volume, index - 20, index)
    prior_volume_5 = _mean(stock.prefix_volume, index - 5, index)
    earlier_volume_20 = _mean(stock.prefix_volume, index - 25, index - 5)
    if min(average_volume_20, prior_volume_5, earlier_volume_20) <= 0:
        raise ValueError("volume denominators must be positive")
    daily = stock.daily_returns[index - 19 : index + 1]
    daily_mean = sum(daily) / len(daily)
    volatility20 = math.sqrt(
        max(0.0, sum((value - daily_mean) ** 2 for value in daily) / len(daily))
    )
    recent_low = min(bar.low for bar in bars[index - 19 : index + 1])
    recent_high = max(bar.high for bar in bars[index - 19 : index + 1])
    days_since20 = 19 - _latest_max_index(close20)
    days_since60 = 59 - _latest_max_index(close60)
    recent_retest = bool(
        close >= ma20
        and min(bar.low for bar in bars[index - 4 : index + 1]) <= ma20
    )
    values = {
        "signal_close": close,
        "average_volume_20": average_volume_20,
        "return_3": close / bars[index - 3].close - 1.0,
        "return_5": close / bars[index - 5].close - 1.0,
        "return_10": close / bars[index - 10].close - 1.0,
        "return_20": close / bars[index - 20].close - 1.0,
        "return_60": close / bars[index - 60].close - 1.0,
        "distance_to_prior20_close_high": close / max(prior20_close) - 1.0,
        "distance_to_prior60_close_high": close / max(prior60_close) - 1.0,
        "drawdown_from_20d_high": close / high20 - 1.0,
        "drawdown_from_60d_high": close / high60 - 1.0,
        "close_vs_ma5": close / ma5 - 1.0,
        "close_vs_ma10": close / ma10 - 1.0,
        "close_vs_ma20": close / ma20 - 1.0,
        "close_vs_ma60": close / ma60 - 1.0,
        "ma5_slope": _slope(stock, index, 5),
        "ma10_slope": _slope(stock, index, 10),
        "ma20_slope": _slope(stock, index, 20),
        "ma60_slope": _slope(stock, index, 60),
        "atr5": atr5,
        "atr20": atr20,
        "atr60": atr60,
        "atr5_atr20": atr5 / atr20,
        "range_compression10": max(close20[-10:]) / min(close20[-10:]) - 1.0,
        "range_compression20": max(close20) / min(close20) - 1.0,
        "volatility20": volatility20,
        "volume_ratio5": current.volume / prior_volume_5,
        "volume_ratio20": current.volume / average_volume_20,
        "prior_volume_contraction_5_20": prior_volume_5 / earlier_volume_20,
        "rs5_vs_0050": _relative_strength(stock, index, benchmark, 5),
        "rs20_vs_0050": _relative_strength(stock, index, benchmark, 20),
        "rs60_vs_0050": _relative_strength(stock, index, benchmark, 60),
        "recent_local_low_distance": close / recent_low - 1.0,
        "recent_local_high_distance": close / recent_high - 1.0,
        "days_since_20d_high": float(days_since20),
        "days_since_60d_high": float(days_since60),
        "recent_breakout_flag": float(close >= max(prior20_close)),
        "recent_retest_flag": float(recent_retest),
        "bias5": close / ma5 - 1.0,
        "bias10": close / ma10 - 1.0,
        "bias20": close / ma20 - 1.0,
    }
    vector = tuple(float(values[name]) for name in FEATURE_NAMES)
    if any(math.isinf(value) for value in vector):
        raise ValueError("taxonomy feature vector contains infinity")
    return vector
