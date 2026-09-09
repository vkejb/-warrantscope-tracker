from __future__ import annotations

import numpy as np

from cross_sectional_alpha_ranking_v01.preprocessing import iter_date_slices


def conditional_ranks(
    scores: np.ndarray,
    stage_a_pool: np.ndarray,
    signal_dates: np.ndarray,
    stock_codes: np.ndarray,
) -> tuple[dict[str, np.ndarray], dict]:
    rank = np.zeros(len(scores), dtype=np.uint8)
    percentile = np.full(len(scores), np.nan, dtype=np.float64)
    decile = np.zeros(len(scores), dtype=np.uint8)
    for _day, region in iter_date_slices(signal_dates):
        local = np.flatnonzero(stage_a_pool[region]) + region.start
        order = np.lexsort((stock_codes[local], -scores[local]))
        ranked = local[order]
        local_rank = np.arange(1, len(ranked) + 1, dtype=np.uint8)
        rank[ranked] = local_rank
        percentile[ranked] = 1.0 if len(ranked) == 1 else 1.0 - (local_rank - 1) / (len(ranked) - 1)
        integer_rank = local_rank.astype(np.int64)
        decile[ranked] = (
            11 - np.ceil(integer_rank * 10 / len(ranked)).astype(np.uint8)
        )
    if np.any(rank[stage_a_pool] == 0) or np.any(rank[~stage_a_pool] != 0):
        raise RuntimeError("conditional ranking escaped frozen Stage A pool")
    return {
        "scores": np.asarray(scores, dtype=np.float64),
        "rank": rank,
        "rank_percentile": percentile,
        "probability_decile": decile,
    }, {
        "definition": "WITHIN_PUBLISHED_STAGE_A_TOP30_SCORE_DESC_STOCK_CODE_ASC",
        "pool_rows": int(np.count_nonzero(stage_a_pool)),
        "unique_dates": int(len(np.unique(signal_dates))),
        "minimum_pool_rank": int(np.min(rank[stage_a_pool])),
        "maximum_pool_rank": int(np.max(rank[stage_a_pool])),
    }


def cooldown_proxy(
    selected: np.ndarray,
    rank: np.ndarray,
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
        indices = indices[np.argsort(rank[indices], kind="stable")]
        for index in indices:
            code = int(stock_codes[index])
            previous = last_entry.get(code)
            if previous is None or session - previous >= cooldown_sessions:
                accepted[index] = True
                last_entry[code] = session
    return accepted, {
        "definition": "CONDITIONAL_TOP5_SAME_STOCK_SESSION_DISTANCE_GTE_10",
        "cooldown_sessions": cooldown_sessions,
        "raw": int(np.count_nonzero(selected)),
        "accepted": int(np.count_nonzero(accepted)),
    }
