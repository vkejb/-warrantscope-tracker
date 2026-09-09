from __future__ import annotations

import numpy as np

from cross_sectional_alpha_ranking_v01.preprocessing import iter_date_slices


def two_stage_ranks(
    stage_a_rank: np.ndarray,
    stage_b_score: np.ndarray,
    signal_dates: np.ndarray,
    stock_codes: np.ndarray,
    pool_size: int = 30,
) -> tuple[np.ndarray, dict]:
    output = np.zeros(len(stage_a_rank), dtype=np.uint8)
    pool_rows = 0
    for _day, region in iter_date_slices(signal_dates):
        local = np.flatnonzero(stage_a_rank[region] <= pool_size) + region.start
        order = np.lexsort((stock_codes[local], -stage_b_score[local]))
        ranked = local[order]
        output[ranked] = np.arange(1, len(ranked) + 1, dtype=np.uint8)
        pool_rows += len(ranked)
    return output, {
        "definition": "STAGE_A_TOP30_THEN_FROZEN_STAGE_B_SCORE_DESC_CODE_ASC",
        "pool_size": pool_size,
        "pool_rows": pool_rows,
        "stage_b_refit_count": 0,
    }

def same_day_quadrants(
    upside_scores: np.ndarray,
    risk_quality_scores: np.ndarray,
    signal_dates: np.ndarray,
) -> np.ndarray:
    """Fixed same-day median split: high is greater than or equal to median."""

    labels = np.zeros(len(signal_dates), dtype=np.uint8)
    for _day, region in iter_date_slices(signal_dates):
        upside_high = upside_scores[region] >= np.median(upside_scores[region])
        quality_high = risk_quality_scores[region] >= np.median(risk_quality_scores[region])
        labels[region] = np.select(
            (
                upside_high & quality_high,
                upside_high & ~quality_high,
                ~upside_high & quality_high,
            ),
            (1, 2, 3),
            default=4,
        )
    return labels


def cooldown_proxy(
    selected: np.ndarray,
    final_rank: np.ndarray,
    signal_dates: np.ndarray,
    stock_codes: np.ndarray,
    cooldown_sessions: int = 10,
) -> tuple[np.ndarray, dict]:
    accepted = np.zeros(len(selected), dtype=bool)
    last_entry: dict[int, int] = {}
    session = -1
    for _day, region in iter_date_slices(signal_dates):
        session += 1
        indices = np.flatnonzero(selected[region]) + region.start
        indices = indices[np.argsort(final_rank[indices], kind="stable")]
        for index in indices:
            code = int(stock_codes[index])
            previous = last_entry.get(code)
            if previous is None or session - previous >= cooldown_sessions:
                accepted[index] = True
                last_entry[code] = session
    return accepted, {
        "definition": "TWO_STAGE_TOP5_SAME_STOCK_SESSION_DISTANCE_GTE_10",
        "cooldown_sessions": cooldown_sessions,
        "raw": int(np.count_nonzero(selected)),
        "accepted": int(np.count_nonzero(accepted)),
    }
