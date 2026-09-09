from __future__ import annotations

from collections import defaultdict
import math

import numpy as np

from extension_entry_study_v01.pipeline import OUTCOME_FIELDS

from .config import CFG, Config, period_label
from .preprocessing import iter_date_slices, ordinal_score_deciles, spearman, topk_indices


OUTCOME_INDEX = {name: index for index, name in enumerate(OUTCOME_FIELDS)}


def build_daily_ranks(
    scores: np.ndarray,
    signal_dates: np.ndarray,
    stock_codes: np.ndarray,
    cfg: Config = CFG,
) -> tuple[dict[str, np.ndarray], dict]:
    count = len(scores)
    rank = np.zeros(count, dtype=np.uint16)
    percentile = np.zeros(count, dtype=np.float32)
    decile = np.zeros(count, dtype=np.uint8)
    for _day, region in iter_date_slices(signal_dates):
        local_scores = scores[region]
        local_codes = stock_codes[region]
        order = np.lexsort((local_codes, -local_scores))
        local_rank = np.empty(len(order), dtype=np.uint16)
        local_rank[order] = np.arange(1, len(order) + 1, dtype=np.uint16)
        rank[region] = local_rank
        if len(order) == 1:
            percentile[region] = 1.0
        else:
            percentile[region] = 1.0 - (local_rank - 1) / (len(order) - 1)
        decile[region] = ordinal_score_deciles(
            local_scores, local_codes, cfg.score_deciles
        )
    if np.any(rank == 0):
        raise RuntimeError("unranked mother observations remain")
    return {
        "scores": np.asarray(scores, dtype=np.float64),
        "rank": rank,
        "rank_percentile": percentile,
        "score_decile": decile,
    }, {
        "ranking_definition": "SCORE_DESC_STOCK_CODE_ASC_TIEBREAK",
        "ranked_observations": count,
        "unique_signal_dates": int(len(np.unique(signal_dates))),
        "primary_top_k": cfg.primary_top_k,
    }


def selection_mask(rank: np.ndarray, k: int) -> np.ndarray:
    return rank <= k


def cooldown_trade_proxy(
    rank: np.ndarray,
    signal_dates: np.ndarray,
    stock_codes: np.ndarray,
    cooldown_sessions: int = CFG.cooldown_sessions,
) -> tuple[np.ndarray, dict]:
    """Top10 entries with the existing >=10 market-session cooldown convention."""

    selected = rank <= CFG.primary_top_k
    accepted = np.zeros(len(rank), dtype=bool)
    last_entry: dict[int, int] = {}
    date_number = -1
    for _day, region in iter_date_slices(signal_dates):
        date_number += 1
        local = np.flatnonzero(selected[region]) + region.start
        local = local[np.argsort(rank[local], kind="stable")]
        for index in local:
            code = int(stock_codes[index])
            previous = last_entry.get(code)
            if previous is None or date_number - previous >= cooldown_sessions:
                accepted[index] = True
                last_entry[code] = date_number
    return accepted, {
        "definition": "TOP10_THEN_SAME_STOCK_MARKET_SESSION_DISTANCE_GTE_10",
        "cooldown_sessions": cooldown_sessions,
        "raw_top10": int(np.count_nonzero(selected)),
        "accepted_trade_proxies": int(np.count_nonzero(accepted)),
    }


def _mean(values: np.ndarray) -> float | None:
    finite = values[np.isfinite(values)]
    return float(np.mean(finite)) if len(finite) else None


def _median(values: np.ndarray) -> float | None:
    finite = values[np.isfinite(values)]
    return float(np.median(finite)) if len(finite) else None


def _global_indices(region: slice, local: np.ndarray) -> np.ndarray:
    return local + int(region.start or 0)


def daily_ranking_rows(
    model_name: str,
    ranking: dict[str, np.ndarray],
    arrays: dict[str, np.ndarray],
) -> list[dict]:
    meta = arrays["meta"]
    outcomes = arrays["outcomes"]
    evaluable = meta["outcome_evaluable"]
    net = outcomes[:, OUTCOME_INDEX["net_return"]]
    gross = outcomes[:, OUTCOME_INDEX["gross_return"]]
    winner = outcomes[:, OUTCOME_INDEX["primary_success"]]
    entry_gap = arrays["entry_gap"]
    rows: list[dict] = []
    for day, region in iter_date_slices(meta["signal_date"]):
        scores = ranking["scores"][region]
        codes = meta["stock_code"][region]
        local_evaluable = evaluable[region]
        top10 = topk_indices(scores, codes, 10, largest=True)
        bottom10 = topk_indices(scores, codes, 10, largest=False)
        deciles = ranking["score_decile"][region]
        top_decile = np.flatnonzero(deciles == 10)
        bottom_decile = np.flatnonzero(deciles == 1)
        all_local = np.flatnonzero(local_evaluable)
        top10_eval = top10[local_evaluable[top10]]
        bottom10_eval = bottom10[local_evaluable[bottom10]]
        top_decile_eval = top_decile[local_evaluable[top_decile]]
        bottom_decile_eval = bottom_decile[local_evaluable[bottom_decile]]
        global_all = _global_indices(region, all_local)
        global_top = _global_indices(region, top10_eval)
        global_bottom = _global_indices(region, bottom10_eval)
        global_td = _global_indices(region, top_decile_eval)
        global_bd = _global_indices(region, bottom_decile_eval)
        daily_ic = spearman(scores[local_evaluable], net[region][local_evaluable])
        score_gap_ic = spearman(
            scores[np.isfinite(entry_gap[region])],
            entry_gap[region][np.isfinite(entry_gap[region])],
        )
        top_gap_net_ic = spearman(entry_gap[global_top], net[global_top])
        universe_net = _mean(net[global_all])
        top_net = _mean(net[global_top])
        bottom_net = _mean(net[global_bottom])
        td_net = _mean(net[global_td])
        bd_net = _mean(net[global_bd])
        signal_close_proxy = (
            (1.0 + entry_gap[global_top]) * (1.0 + gross[global_top]) - 1.0
            if len(global_top)
            else np.asarray([], dtype=float)
        )
        row = {
            "signal_date": day,
            "year": day // 10000,
            "month": day // 100,
            "period": period_label(day),
            "model": model_name,
            "eligible_stocks": len(scores),
            "evaluable_stocks": len(global_all),
            "daily_spearman_ic_net_return": daily_ic,
            "top10_candidates": len(top10),
            "top10_evaluable": len(global_top),
            "top10_winner_rate": _mean(winner[global_top]),
            "universe_winner_rate": _mean(winner[global_all]),
            "top10_gross_mean": _mean(gross[global_top]),
            "top10_net_mean": top_net,
            "top10_median_net": _median(net[global_top]),
            "universe_net_mean": universe_net,
            "universe_net_median": _median(net[global_all]),
            "bottom10_net_mean": bottom_net,
            "top_decile_net_mean": td_net,
            "bottom_decile_net_mean": bd_net,
            "top10_minus_universe_net_spread": (
                top_net - universe_net if top_net is not None and universe_net is not None else None
            ),
            "top10_minus_bottom10_net_spread": (
                top_net - bottom_net if top_net is not None and bottom_net is not None else None
            ),
            "top_decile_minus_bottom_decile_net_spread": (
                td_net - bd_net if td_net is not None and bd_net is not None else None
            ),
            "top1_score": float(np.max(scores)),
            "top10_mean_score": float(np.mean(scores[top10])),
            "universe_mean_score": float(np.mean(scores)),
            "universe_median_score": float(np.median(scores)),
            "top10_vs_universe_score_spread": float(np.mean(scores[top10]) - np.mean(scores)),
            "top10_vs_median_score_spread": float(np.mean(scores[top10]) - np.median(scores)),
            "score_vs_entry_gap_spearman": score_gap_ic,
            "top10_entry_gap_mean": _mean(entry_gap[global_top]),
            "universe_entry_gap_mean": _mean(entry_gap[global_all]),
            "top10_entry_gap_vs_net_spearman": top_gap_net_ic,
            "top10_signal_close_to_exit_proxy_mean": _mean(signal_close_proxy),
            "top10_gap_payoff_erosion": (
                _mean(signal_close_proxy) - _mean(gross[global_top])
                if len(global_top)
                else None
            ),
        }
        rows.append(row)
    monthly: dict[int, list[float]] = defaultdict(list)
    yearly: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        value = row["daily_spearman_ic_net_return"]
        if value is not None and math.isfinite(value):
            monthly[row["month"]].append(value)
            yearly[row["year"]].append(value)
    for row in rows:
        row["monthly_mean_daily_ic"] = _mean(np.asarray(monthly[row["month"]]))
        row["yearly_mean_daily_ic"] = _mean(np.asarray(yearly[row["year"]]))
    return rows
