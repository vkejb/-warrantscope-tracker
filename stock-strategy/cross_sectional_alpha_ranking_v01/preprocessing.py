from __future__ import annotations

import numpy as np

from .config import CFG, Config


def iter_date_slices(signal_dates: np.ndarray):
    if len(signal_dates) == 0:
        return
    starts = np.flatnonzero(
        np.r_[True, signal_dates[1:] != signal_dates[:-1]]
    )
    ends = np.r_[starts[1:], len(signal_dates)]
    for start, end in zip(starts, ends):
        yield int(signal_dates[start]), slice(int(start), int(end))


def rank_percentile(
    values: np.ndarray,
    *,
    missing_value: float = 0.5,
) -> np.ndarray:
    """Average-tie percentile in [0,1]; missing values have fixed neutral rank."""

    values = np.asarray(values, dtype=float)
    output = np.full(len(values), missing_value, dtype=np.float64)
    finite_indices = np.flatnonzero(np.isfinite(values))
    if not len(finite_indices):
        return output
    if len(finite_indices) == 1:
        output[finite_indices[0]] = 0.5
        return output
    finite_values = values[finite_indices]
    order = np.argsort(finite_values, kind="stable")
    sorted_values = finite_values[order]
    ranks = np.empty(len(order), dtype=float)
    position = 0
    while position < len(order):
        end = position + 1
        while end < len(order) and sorted_values[end] == sorted_values[position]:
            end += 1
        ranks[position:end] = (position + end - 1) / 2.0
        position = end
    percentiles = ranks / (len(order) - 1)
    restored = np.empty(len(order), dtype=float)
    restored[order] = percentiles
    output[finite_indices] = restored
    return output


def same_day_cross_sectional_percentiles(
    raw_features: np.ndarray,
    signal_dates: np.ndarray,
    cfg: Config = CFG,
) -> tuple[np.ndarray, dict]:
    """Transform each date independently; no historical or future fit is used."""

    if len(raw_features) != len(signal_dates):
        raise ValueError("feature/date length mismatch")
    transformed = np.empty(raw_features.shape, dtype=np.float32)
    dates = 0
    for _day, region in iter_date_slices(signal_dates):
        dates += 1
        block = raw_features[region]
        for column in range(block.shape[1]):
            transformed[region, column] = rank_percentile(
                block[:, column], missing_value=cfg.cross_sectional_missing_value
            )
    return transformed, {
        "definition": "SAME_DAY_AVERAGE_TIE_PERCENTILE_0_TO_1",
        "missing_value": cfg.cross_sectional_missing_value,
        "signal_dates": dates,
        "fit_required": False,
        "minimum": float(np.min(transformed)),
        "maximum": float(np.max(transformed)),
        "raw_missing_cells": int(np.count_nonzero(~np.isfinite(raw_features))),
        "transformed_missing_cells": int(np.count_nonzero(~np.isfinite(transformed))),
    }


def purged_training_mask(
    signal_dates: np.ndarray,
    evaluable: np.ndarray,
    start: str,
    end: str,
    purge_sessions: int,
) -> tuple[np.ndarray, dict]:
    period = (signal_dates >= int(start)) & (signal_dates <= int(end))
    dates = np.unique(signal_dates[period])
    if len(dates) <= purge_sessions:
        raise ValueError("training period too short for label purge")
    cutoff = int(dates[-purge_sessions - 1])
    mask = evaluable & period & (signal_dates <= cutoff)
    return mask, {
        "requested_start": start,
        "requested_end": end,
        "purge_sessions": purge_sessions,
        "last_included_signal_date": cutoff,
        "purged_signal_dates": [int(value) for value in dates[-purge_sessions:]],
        "training_observations": int(np.count_nonzero(mask)),
    }


def topk_indices(
    scores: np.ndarray,
    stock_codes: np.ndarray,
    k: int,
    *,
    largest: bool,
) -> np.ndarray:
    """Deterministic score ordering; stock code is the tie-breaker."""

    scores = np.asarray(scores, dtype=float)
    stock_codes = np.asarray(stock_codes)
    if len(scores) != len(stock_codes):
        raise ValueError("score/code length mismatch")
    order = np.lexsort((stock_codes, -scores if largest else scores))
    return order[: min(k, len(order))]


def ordinal_score_deciles(
    scores: np.ndarray,
    stock_codes: np.ndarray,
    deciles: int = 10,
) -> np.ndarray:
    """Exact same-day ordinal buckets; higher score maps to higher decile."""

    order = np.lexsort((stock_codes, scores))
    result = np.empty(len(scores), dtype=np.uint8)
    for position, index in enumerate(order):
        result[index] = min(deciles, int(position * deciles / len(order)) + 1)
    return result


def spearman(left: np.ndarray, right: np.ndarray) -> float | None:
    finite = np.isfinite(left) & np.isfinite(right)
    if np.count_nonzero(finite) < 3:
        return None
    lrank = rank_percentile(np.asarray(left)[finite])
    rrank = rank_percentile(np.asarray(right)[finite])
    if float(np.std(lrank)) <= 1e-15 or float(np.std(rrank)) <= 1e-15:
        return None
    return float(np.corrcoef(lrank, rrank)[0, 1])
