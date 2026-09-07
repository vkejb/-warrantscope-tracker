from __future__ import annotations

from bisect import bisect_left
import math

from multi_setup_study_v01.config import CFG as MULTI_CFG
from multi_setup_study_v01.main import _momentum_selected
from surge_event_study_v01.analysis import percentile_ranks
from surge_event_study_v01.models import PreparedStock
from v21.diagnostic_analysis import percentile

from .config import CFG, FEATURE_NAMES, GAP_BUCKETS, Config


MOMENTUM_SCORE_FEATURES = MULTI_CFG.momentum_score_features


def _mean(prefix: list[float], start: int, end: int) -> float:
    if end <= start:
        raise ValueError("mean window must be non-empty")
    return (prefix[end] - prefix[start]) / (end - start)


def _price_true_range(stock: PreparedStock, index: int) -> float:
    current = stock.bars[index]
    if index == 0 or stock.segment_ids[index] != stock.segment_ids[index - 1]:
        return current.high - current.low
    previous_close = stock.bars[index - 1].close
    return max(
        current.high - current.low,
        abs(current.high - previous_close),
        abs(current.low - previous_close),
    )


def atr20_price(stock: PreparedStock, index: int) -> float:
    """Arithmetic mean of 20 price true-ranges, inclusive of T."""

    if index < 19:
        raise ValueError("ATR20 needs 20 observations")
    return sum(_price_true_range(stock, cursor) for cursor in range(index - 19, index + 1)) / 20.0


def _bias(stock: PreparedStock, index: int, sessions: int) -> float:
    ma = _mean(stock.prefix_close, index - sessions + 1, index + 1)
    return stock.bars[index].close / ma - 1.0


def build_extension_features(stock: PreparedStock, index: int) -> dict[str, float]:
    """Build only T-close features. This function never reads T+1 or an outcome."""

    if index < 60:
        raise ValueError("extension features require at least 60 prior sessions")
    close = stock.bars[index].close
    ma10 = _mean(stock.prefix_close, index - 9, index + 1)
    ma20 = _mean(stock.prefix_close, index - 19, index + 1)
    atr = atr20_price(stock, index)
    if atr <= 0:
        raise ValueError("ATR20 price must be positive")
    bias10 = close / ma10 - 1.0
    bias20 = close / ma20 - 1.0
    bias10_t3 = _bias(stock, index - 3, 10)
    bias20_t3 = _bias(stock, index - 3, 20)
    values = {
        "bias_5": _bias(stock, index, 5),
        "bias_10": bias10,
        "bias_20": bias20,
        "bias_60": _bias(stock, index, 60),
        "atr_adjusted_bias20": (close - ma20) / atr,
        "atr_adjusted_bias10": (close - ma10) / atr,
        "return_3": close / stock.bars[index - 3].close - 1.0,
        "return_5": close / stock.bars[index - 5].close - 1.0,
        "return_10": close / stock.bars[index - 10].close - 1.0,
        "return_20": close / stock.bars[index - 20].close - 1.0,
        "bias20_change_3d": bias20 - bias20_t3,
        "bias10_change_3d": bias10 - bias10_t3,
        "distance_to_20d_high": close
        / max(item.close for item in stock.bars[index - 19 : index + 1])
        - 1.0,
        "distance_to_60d_high": close
        / max(item.close for item in stock.bars[index - 59 : index + 1])
        - 1.0,
    }
    if any(not math.isfinite(value) for value in values.values()):
        raise ValueError("non-finite extension feature")
    return values


def observed_entry_gap(stock: PreparedStock, index: int) -> float | None:
    """T+1 diagnostic only; deliberately separate from T-close feature formation."""

    if index + 1 >= len(stock.bars):
        return None
    entry = stock.bars[index + 1]
    if stock.calendar_indices[index + 1] != stock.calendar_indices[index] + 1:
        return None
    if entry.volume <= 0 or stock.segment_ids[index + 1] != stock.segment_ids[index]:
        return None
    value = entry.open / stock.bars[index].close - 1.0
    return value if math.isfinite(value) else None


def full_feature_vector(stock: PreparedStock, index: int) -> tuple[float, ...]:
    values = build_extension_features(stock, index)
    gap = observed_entry_gap(stock, index)
    values["entry_gap"] = math.nan if gap is None else gap
    return tuple(values[name] for name in FEATURE_NAMES)


def frozen_quantile_boundaries(
    feature_values: dict[str, list[float]], buckets: int
) -> dict[str, tuple[float, ...]]:
    if buckets not in {5, 10}:
        raise ValueError("only preregistered quintiles and deciles are supported")
    output: dict[str, tuple[float, ...]] = {}
    for feature in FEATURE_NAMES:
        values = [value for value in feature_values[feature] if math.isfinite(value)]
        if not values:
            raise ValueError(f"no discovery values for {feature}")
        output[feature] = tuple(
            float(percentile(values, step / buckets))
            for step in range(1, buckets)
        )
    return output


def bucket_number(value: float, boundaries: tuple[float, ...]) -> int:
    """One-based bucket; equality remains in the lower bucket."""

    if not math.isfinite(value):
        return 0
    return bisect_left(boundaries, value) + 1


def gap_bucket_number(value: float | None) -> int:
    if value is None or not math.isfinite(value):
        return 0
    if value < -0.01:
        return 1
    if value < 0.0:
        return 2
    if value < 0.01:
        return 3
    if value < 0.02:
        return 4
    if value < 0.03:
        return 5
    return 6


def gap_bucket_label(number: int) -> str | None:
    return GAP_BUCKETS[number - 1] if 1 <= number <= len(GAP_BUCKETS) else None


def momentum_scores(contexts: list[tuple]) -> tuple[list[float], list[float], list[int]]:
    """Frozen Surge composite for every eligible name plus strength ranks/top30."""

    if not contexts:
        return [], [], []
    per_feature: list[list[float]] = []
    for feature in MOMENTUM_SCORE_FEATURES:
        values = [float(context[0].features[feature]) for context in contexts]
        per_feature.append(percentile_ranks(values))
    scores = [
        sum(values[index] for values in per_feature) / len(per_feature)
        for index in range(len(contexts))
    ]
    strength_percentiles = percentile_ranks(scores)
    # Candidate membership is sourced from the existing frozen implementation,
    # rather than merely reimplementing its Top30/tie-break contract here.
    context_index = {
        (context[0].signal_date, context[0].code): index
        for index, context in enumerate(contexts)
    }
    selected = _momentum_selected(contexts)
    top_indices = [
        context_index[(item["observation"].signal_date, item["observation"].code)]
        for item in selected
    ]
    return scores, strength_percentiles, top_indices


def assert_momentum_selection_parity(contexts: list[tuple], top_indices: list[int]) -> None:
    existing = _momentum_selected(contexts)
    existing_keys = [item["observation"].code for item in existing]
    mirror_keys = [contexts[index][0].code for index in top_indices]
    if existing_keys != mirror_keys:
        raise RuntimeError(
            f"frozen momentum selection drifted: existing={existing_keys}, mirror={mirror_keys}"
        )


def percentile_quintile(value: float) -> int:
    return min(5, max(1, int(value * 5) + 1))
