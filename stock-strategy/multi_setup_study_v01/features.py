from __future__ import annotations

import math

from surge_event_study_v01.models import PreparedBenchmark, PreparedStock


def mean(prefix: list[float], start: int, end: int) -> float:
    if end <= start:
        raise ValueError("mean window must be non-empty")
    return (prefix[end] - prefix[start]) / (end - start)


def close_location(stock: PreparedStock, index: int) -> float:
    bar = stock.bars[index]
    if bar.high <= bar.low:
        return 0.5
    return (bar.close - bar.low) / (bar.high - bar.low)


def benchmark_return(
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


def relative_strength(
    stock: PreparedStock,
    index: int,
    benchmark: PreparedBenchmark,
    sessions: int,
) -> float | None:
    if index < sessions:
        return None
    market_return = benchmark_return(
        benchmark, stock.calendar_indices[index], sessions
    )
    if market_return is None:
        return None
    stock_return = stock.bars[index].close / stock.bars[index - sessions].close - 1.0
    return (1.0 + stock_return) / (1.0 + market_return) - 1.0


def history_is_contiguous(stock: PreparedStock, index: int, sessions: int) -> bool:
    if index < sessions:
        return False
    return bool(
        stock.calendar_indices[index - sessions]
        == stock.calendar_indices[index] - sessions
        and stock.segment_ids[index - sessions] == stock.segment_ids[index]
    )


def finite_feature_dict(values: dict[str, float | int]) -> bool:
    return all(
        isinstance(value, int) or math.isfinite(float(value))
        for value in values.values()
    )
