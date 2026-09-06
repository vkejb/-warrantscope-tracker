from __future__ import annotations

from bisect import bisect_left, bisect_right
import math

from .config import CFG, Config
from .models import Outcome, PreparedBenchmark, PreparedStock, SignalObservation


def _mean(prefix: list[float], start: int, end: int) -> float:
    return (prefix[end] - prefix[start]) / (end - start)


def _benchmark_return(
    benchmark: PreparedBenchmark, end_index: int, sessions: int
) -> float | None:
    start_index = end_index - sessions
    if start_index < 0:
        return None
    start = benchmark.normalized_closes[start_index]
    end = benchmark.normalized_closes[end_index]
    start_segment = benchmark.segment_ids[start_index]
    end_segment = benchmark.segment_ids[end_index]
    if (
        start is None
        or end is None
        or start_segment is None
        or end_segment is None
        or start_segment != end_segment
    ):
        return None
    return end / start - 1.0


def build_signal_observation(
    stock: PreparedStock,
    local_index: int,
    benchmark: PreparedBenchmark,
    cfg: Config = CFG,
) -> SignalObservation | None:
    """Build a T-close observation using T and earlier data only."""

    j = local_index
    if j < cfg.feature_lookback_sessions:
        return None
    calendar_index = stock.calendar_indices[j]
    if stock.calendar_indices[j - cfg.feature_lookback_sessions] != (
        calendar_index - cfg.feature_lookback_sessions
    ):
        return None
    if stock.segment_ids[j - cfg.feature_lookback_sessions] != stock.segment_ids[j]:
        return None
    current = stock.bars[j]
    if current.volume <= 0 or not (cfg.minimum_price <= current.close <= cfg.maximum_price):
        return None

    average_volume_20 = _mean(stock.prefix_volume, j - 20, j)
    average_turnover_20 = _mean(stock.prefix_turnover_proxy, j - 20, j)
    if (
        average_volume_20 < cfg.minimum_average_volume_20
        or average_turnover_20 < cfg.minimum_average_turnover_proxy_20
    ):
        return None

    closes = stock.bars
    close = current.close
    return_5 = close / closes[j - 5].close - 1.0
    return_20 = close / closes[j - 20].close - 1.0
    sma20 = _mean(stock.prefix_close, j - 19, j + 1)
    sma20_five_sessions_ago = _mean(stock.prefix_close, j - 24, j - 4)
    prior20_high = max(item.close for item in closes[j - 20 : j])
    high60 = max(item.close for item in closes[j - 59 : j + 1])
    average_volume_5 = _mean(stock.prefix_volume, j - 4, j + 1)
    prior_volume_5 = _mean(stock.prefix_volume, j - 5, j)
    earlier_volume_20 = _mean(stock.prefix_volume, j - 25, j - 5)
    volatility_mean = _mean(stock.prefix_return, j - 19, j + 1)
    volatility_second = _mean(stock.prefix_return_square, j - 19, j + 1)
    volatility_20 = math.sqrt(max(0.0, volatility_second - volatility_mean**2))
    prior10 = [item.close for item in closes[j - 10 : j]]
    range_compression_10 = max(prior10) / min(prior10) - 1.0
    atr_ratio_14 = _mean(stock.prefix_true_range_ratio, j - 13, j + 1)
    close_location = (
        (current.close - current.low) / (current.high - current.low)
        if current.high > current.low
        else 0.5
    )
    features = {
        "return_5": return_5,
        "return_20": return_20,
        "close_vs_sma20": close / sma20 - 1.0,
        "sma20_slope_5": sma20 / sma20_five_sessions_ago - 1.0,
        "breakout_vs_prior20": close / prior20_high - 1.0,
        "close_to_60d_high": close / high60,
        "volume_ratio_1_20": current.volume / average_volume_20,
        "volume_ratio_5_20": average_volume_5 / average_volume_20,
        "prior_volume_contraction_5_20": prior_volume_5 / earlier_volume_20,
        "turnover_proxy_ratio_1_20": (current.close * current.volume)
        / average_turnover_20,
        "volatility_20": volatility_20,
        "range_compression_10": range_compression_10,
        "atr_ratio_14": atr_ratio_14,
        "close_location": close_location,
    }
    benchmark_5 = _benchmark_return(benchmark, calendar_index, 5)
    benchmark_20 = _benchmark_return(benchmark, calendar_index, 20)
    if benchmark_5 is not None:
        features["rs_5_vs_0050"] = (1.0 + return_5) / (1.0 + benchmark_5) - 1.0
    if benchmark_20 is not None:
        features["rs_20_vs_0050"] = (1.0 + return_20) / (1.0 + benchmark_20) - 1.0
    if any(not math.isfinite(value) for value in features.values()):
        return None
    return SignalObservation(
        signal_date=current.date,
        calendar_index=calendar_index,
        code=stock.code,
        name=current.name,
        signal_close=current.close,
        average_volume_20=average_volume_20,
        average_turnover_proxy_20=average_turnover_20,
        features=features,
    )


def evaluate_outcome(
    stock: PreparedStock, local_index: int, cfg: Config = CFG
) -> Outcome:
    """Attach future research labels without affecting signal eligibility/rank."""

    j = local_index
    if j + 1 >= len(stock.bars) or stock.calendar_indices[j + 1] != stock.calendar_indices[j] + 1:
        return Outcome("NOT_EVALUABLE", "MISSING_T_PLUS_1_BAR")
    entry = stock.bars[j + 1]
    if entry.volume <= 0:
        return Outcome("NOT_EVALUABLE", "T_PLUS_1_ZERO_VOLUME")
    if stock.segment_ids[j + 1] != stock.segment_ids[j]:
        return Outcome("NOT_EVALUABLE", "T_PLUS_1_DISCONTINUITY")
    entry_open = entry.open
    max_close_returns: list[tuple[int, float]] = []
    max_high_returns: list[tuple[int, float]] = []
    valid_windows: dict[int, list] = {}
    for horizon in cfg.sensitivity_horizons:
        end = j + horizon
        if end >= len(stock.bars):
            continue
        if stock.calendar_indices[end] != stock.calendar_indices[j] + horizon:
            continue
        if stock.segment_ids[end] != stock.segment_ids[j]:
            continue
        window = stock.bars[j + 1 : end + 1]
        valid_windows[horizon] = window
        max_close_returns.append(
            (horizon, max(item.close for item in window) / entry_open - 1.0)
        )
        max_high_returns.append(
            (horizon, max(item.high for item in window) / entry_open - 1.0)
        )

    primary_window = valid_windows.get(cfg.primary_horizon)
    if primary_window is None:
        return Outcome(
            "NOT_EVALUABLE",
            "MISSING_OR_DISCONTINUOUS_PRIMARY_FORWARD_WINDOW",
            entry_date=entry.date,
            entry_open=entry_open,
            entry_gap=entry_open / stock.bars[j].close - 1.0,
            max_close_returns=tuple(max_close_returns),
            max_high_returns=tuple(max_high_returns),
        )
    close_returns = [item.close / entry_open - 1.0 for item in primary_window]
    high_returns = [item.high / entry_open - 1.0 for item in primary_window]
    hit_days = [
        day
        for day, value in enumerate(close_returns, start=1)
        if value >= cfg.primary_threshold - 1e-12
    ]
    first_hit = hit_days[0] if hit_days else None
    clean = bool(
        first_hit is not None
        and min(close_returns[:first_hit]) > -cfg.clean_close_drawdown
    )
    return Outcome(
        status="EVALUABLE",
        reason="COMPLETE_PRIMARY_FORWARD_WINDOW",
        entry_date=entry.date,
        entry_open=entry_open,
        entry_gap=entry_open / stock.bars[j].close - 1.0,
        primary_event=first_hit is not None,
        clean_primary_event=clean,
        first_primary_hit_day=first_hit,
        close_return_10=close_returns[-1],
        mfe_close_10=max(close_returns),
        mae_close_10=min(close_returns),
        high_touch_event_10=max(high_returns) >= cfg.primary_threshold - 1e-12,
        max_close_returns=tuple(max_close_returns),
        max_high_returns=tuple(max_high_returns),
    )


def iter_signal_dates(
    stocks: list[PreparedStock],
    benchmark: PreparedBenchmark,
    start_date: str,
    end_date: str,
    cfg: Config = CFG,
):
    """Yield date blocks without any outcome-dependent filtering."""

    start_index = bisect_left(benchmark.calendar, start_date)
    end_index = bisect_right(benchmark.calendar, end_date)
    positions = [bisect_left(stock.calendar_indices, start_index) for stock in stocks]
    for calendar_index in range(start_index, end_index):
        contexts = []
        for stock_index, stock in enumerate(stocks):
            local_index = positions[stock_index]
            while (
                local_index < len(stock.calendar_indices)
                and stock.calendar_indices[local_index] < calendar_index
            ):
                local_index += 1
            positions[stock_index] = local_index
            if (
                local_index >= len(stock.calendar_indices)
                or stock.calendar_indices[local_index] != calendar_index
            ):
                continue
            observation = build_signal_observation(stock, local_index, benchmark, cfg)
            if observation is not None:
                contexts.append((observation, stock, local_index))
        yield benchmark.calendar[calendar_index], calendar_index, contexts
