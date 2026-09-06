from __future__ import annotations

from surge_event_study_v01.models import PreparedBenchmark, PreparedStock

from .config import CFG, Config
from .features import (
    benchmark_return,
    close_location,
    finite_feature_dict,
    history_is_contiguous,
    mean,
    relative_strength,
)


def is_compact_retest(geometry: dict, cfg: Config = CFG) -> bool:
    """Frozen descriptive label; it never removes the parent N_RETEST row."""

    return bool(
        int(geometry.get("pivot_separation_sessions", 10**9))
        <= cfg.compact_max_separation_sessions
        and float(geometry.get("bottom_difference", -1.0)) > 0.0
    )


def _base_eligible(stock: PreparedStock, index: int, cfg: Config) -> bool:
    if not history_is_contiguous(stock, index, cfg.feature_lookback_sessions):
        return False
    current = stock.bars[index]
    if current.volume <= 0 or not (cfg.minimum_price <= current.close <= cfg.maximum_price):
        return False
    average_volume = mean(stock.prefix_volume, index - 20, index)
    average_turnover = mean(stock.prefix_turnover_proxy, index - 20, index)
    return bool(
        average_volume >= cfg.minimum_average_volume_20
        and average_turnover >= cfg.minimum_average_turnover_proxy_20
    )


def detect_trend_pullback(
    stock: PreparedStock,
    index: int,
    benchmark: PreparedBenchmark,
    cfg: Config = CFG,
) -> dict[str, float | int] | None:
    """Frozen, T-close-only Trend Pullback V0.1 baseline."""

    if not _base_eligible(stock, index, cfg):
        return None
    bars = stock.bars
    current = bars[index]
    sma20 = mean(stock.prefix_close, index - 19, index + 1)
    sma60 = mean(stock.prefix_close, index - 59, index + 1)
    sma20_t_minus_5 = mean(stock.prefix_close, index - 24, index - 4)
    if not (
        sma20 > sma60
        and sma20 > sma20_t_minus_5
        and current.close > sma60
        and current.close > bars[index - 1].high
    ):
        return None

    # A qualifying historical breakout must predate T by at least two sessions,
    # leaving room for a pullback before the T-day re-strengthening bar.
    breakout_candidates = []
    start = index - cfg.pullback_recent_high_window + 1
    for candidate in range(start, index - 1):
        prior20 = bars[candidate - 20 : candidate]
        if prior20 and bars[candidate].close > max(item.close for item in prior20):
            breakout_candidates.append(candidate)
    if not breakout_candidates:
        return None
    breakout_index = breakout_candidates[-1]

    peak_close = max(item.close for item in bars[breakout_index:index])
    peak_index = max(
        position
        for position in range(breakout_index, index)
        if bars[position].close == peak_close
    )
    if peak_index >= index - 1:
        return None
    trough_close = min(item.close for item in bars[peak_index + 1 : index])
    trough_index = max(
        position
        for position in range(peak_index + 1, index)
        if bars[position].close == trough_close
    )
    pullback_depth = 1.0 - trough_close / peak_close
    if not (
        cfg.pullback_minimum_depth
        <= pullback_depth
        <= cfg.pullback_maximum_depth
    ):
        return None

    impulse_start_index = min(
        range(max(0, peak_index - 20), peak_index + 1),
        key=lambda position: bars[position].close,
    )
    impulse_low = bars[impulse_start_index].close
    retracement = (
        (peak_close - trough_close) / (peak_close - impulse_low)
        if peak_close > impulse_low
        else 0.0
    )
    prior_volume_20 = mean(stock.prefix_volume, index - 20, index)
    rs5 = relative_strength(stock, index, benchmark, 5)
    values: dict[str, float | int] = {
        "pullback_depth": pullback_depth,
        "pullback_duration": index - peak_index,
        "distance_to_ma20": current.close / sma20 - 1.0,
        "distance_to_ma60": current.close / sma60 - 1.0,
        "retracement_of_prior_impulse": retracement,
        "signal_return": current.close / bars[index - 1].close - 1.0,
        "signal_volume_ratio": current.volume / prior_volume_20,
        "entry_gap": 0.0,  # Replaced only after the T+1 outcome is attached.
        "trend_breakout_index": breakout_index,
        "trend_peak_index": peak_index,
        "trend_trough_index": trough_index,
    }
    if rs5 is not None:
        values["relative_strength_vs_0050"] = rs5
    if not finite_feature_dict(values):
        return None
    return values


def _range_width(stock: PreparedStock, start: int, end: int, denominator: float) -> float:
    bars = stock.bars[start:end]
    return (max(item.high for item in bars) - min(item.low for item in bars)) / denominator


def detect_consolidation_breakout_v2(
    stock: PreparedStock,
    index: int,
    benchmark: PreparedBenchmark,
    cfg: Config = CFG,
) -> dict[str, float | int] | None:
    """Frozen V0.1 engineering baseline; all compression windows end at T-1."""

    if not _base_eligible(stock, index, cfg):
        return None
    bars = stock.bars
    current = bars[index]
    prior_close = bars[index - 1].close
    prior20 = bars[index - 20 : index]
    prior20_high = max(item.high for item in prior20)
    prior20_low = min(item.low for item in prior20)
    range_width_20 = (prior20_high - prior20_low) / prior_close
    ma5 = mean(stock.prefix_close, index - 5, index)
    ma10 = mean(stock.prefix_close, index - 10, index)
    ma20 = mean(stock.prefix_close, index - 20, index)
    ma_spread = (max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / prior_close
    atr5 = mean(stock.prefix_true_range_ratio, index - 5, index)
    atr20 = mean(stock.prefix_true_range_ratio, index - 20, index)
    if atr20 <= 0:
        return None
    atr_ratio = atr5 / atr20
    if not (
        range_width_20 <= cfg.consolidation_range_maximum
        and ma_spread <= cfg.consolidation_ma_spread_maximum
        and atr_ratio <= cfg.consolidation_atr_ratio_maximum
        and current.close > prior20_high
    ):
        return None

    final_low, final_high = prior20_low, prior20_high
    duration = 0
    for position in range(index - 1, max(-1, index - 41), -1):
        if final_low <= bars[position].close <= final_high:
            duration += 1
        else:
            break
    prior_volume_5 = mean(stock.prefix_volume, index - 5, index)
    prior_volume_20 = mean(stock.prefix_volume, index - 20, index)
    rs5 = relative_strength(stock, index, benchmark, 5)
    values: dict[str, float | int] = {
        "consolidation_duration_proxy": duration,
        "range_width_10": _range_width(stock, index - 10, index, prior_close),
        "range_width_20": range_width_20,
        "range_width_40": _range_width(stock, index - 40, index, prior_close),
        "ma_spread": ma_spread,
        "atr5_atr20": atr_ratio,
        "prior_volume_5_20": prior_volume_5 / prior_volume_20,
        "breakout_strength": current.close / prior20_high - 1.0,
        "breakout_day_return": current.close / prior_close - 1.0,
        "breakout_volume_ratio": current.volume / prior_volume_20,
        "close_location": close_location(stock, index),
        "entry_gap": 0.0,  # Replaced only after the T+1 outcome is attached.
    }
    if rs5 is not None:
        values["relative_strength_vs_0050"] = rs5
    if not finite_feature_dict(values):
        return None
    return values


def benchmark_context(
    benchmark: PreparedBenchmark, calendar_index: int
) -> dict[str, float | None]:
    """Descriptive market context only; never filters a V0.1 setup."""

    return {
        "benchmark_return_5": benchmark_return(benchmark, calendar_index, 5),
        "benchmark_return_20": benchmark_return(benchmark, calendar_index, 20),
    }
