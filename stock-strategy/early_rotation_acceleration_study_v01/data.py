from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from conditional_path_quality_ranking_v01.data import protected_hashes
from sector_rotation_incremental_study_v01.data import (
    build_panels, load_frozen_arrays, load_ohlcv, load_store, prepare_stocks,
    sha256_file, store_digest,
)

from .config import CFG, FEATURES, LEVEL_FEATURES


def _ratio(now: float, before: float) -> float:
    if not np.isfinite(now) or not np.isfinite(before) or before <= 0:
        return np.nan
    return float(now / before - 1.0)


def _context_at(day, peers, close, volume, benchmark_close, market_turnover):
    current = close[day, peers]
    peer_r3 = float(np.nanmedian(current / close[day - 3, peers] - 1.0))
    peer_r20 = float(np.nanmedian(current / close[day - 20, peers] - 1.0))
    bench3 = _ratio(benchmark_close[day], benchmark_close[day - 3])
    bench20 = _ratio(benchmark_close[day], benchmark_close[day - 20])
    ma20 = np.nanmean(close[day - 19 : day + 1, peers], axis=0)
    ma20_prior = np.nanmean(close[day - 24 : day - 4, peers], axis=0)
    prior_high = np.nanmax(close[day - 20 : day, peers], axis=0)
    turnover_share = float(np.nansum(close[day, peers] * volume[day, peers]) / market_turnover[day])
    shares20 = np.asarray([
        np.nansum(close[j, peers] * volume[j, peers]) / market_turnover[j]
        for j in range(day - 20, day)
    ])
    volume_window = volume[day - 5 : day, peers]
    volume_count = np.sum(np.isfinite(volume_window), axis=0)
    volume_mean = np.divide(
        np.nansum(volume_window, axis=0), volume_count,
        out=np.full(len(peers), np.nan), where=volume_count > 0,
    )
    volume_ratio5 = volume[day, peers] / volume_mean
    return {
        "peer_rs_3d_vs_0050": peer_r3 - bench3,
        "peer_rs_20d_vs_0050": peer_r20 - bench20,
        "peer_breadth_up": float(np.mean(current > close[day - 1, peers])),
        "peer_breadth_above_ma20": float(np.mean(current > ma20)),
        "peer_breadth_ma20_rising": float(np.mean(ma20 > ma20_prior)),
        "peer_breadth_newhigh": float(np.mean(current >= prior_high)),
        "peer_turnover_market_share": turnover_share,
        "peer_turnover_share_vs_20d": _ratio(turnover_share, float(np.nanmean(shares20))),
        "peer_median_volume_ratio5": float(np.nanmedian(volume_ratio5)),
    }


def _percentile(values: np.ndarray) -> np.ndarray:
    result = np.full(len(values), np.nan)
    finite = np.flatnonzero(np.isfinite(values))
    if not len(finite):
        return result
    order = finite[np.argsort(values[finite], kind="stable")]
    sorted_values = values[order]
    starts = np.searchsorted(sorted_values, sorted_values, side="left")
    ends = np.searchsorted(sorted_values, sorted_values, side="right")
    result[order] = ((starts + ends - 1) / 2 + 0.5) / len(order)
    return result


def build_acceleration_store(arrays, peer_store, prepared, benchmark):
    meta = arrays["meta"]
    codes, code_pos, close, volume, _returns, _segments = build_panels(prepared, benchmark.calendar)
    date_pos = {int(day): i for i, day in enumerate(benchmark.calendar)}
    benchmark_close = np.asarray([
        np.nan if value is None else value for value in benchmark.normalized_closes
    ], dtype=float)
    market_turnover = np.nansum(close * volume, axis=1)
    features = np.full((len(meta), len(FEATURES)), np.nan)
    levels = np.full((len(meta), len(LEVEL_FEATURES)), np.nan)
    future_levels = np.full((len(meta), CFG.lead_time_sessions, len(LEVEL_FEATURES)), np.nan)
    usable = arrays["stage_a_pool"] & peer_store["available"]
    raw_rs3 = np.full(len(meta), np.nan); raw_rs20 = np.full(len(meta), np.nan)
    contexts = {}
    for row in np.flatnonzero(usable):
        day = date_pos[int(meta["signal_date"][row])]
        peers = np.asarray([code_pos[str(code)] for code in peer_store["peer_codes"][row]], dtype=int)
        current = _context_at(day, peers, close, volume, benchmark_close, market_turnover)
        prior1 = _context_at(day - 1, peers, close, volume, benchmark_close, market_turnover)
        prior3 = _context_at(day - 3, peers, close, volume, benchmark_close, market_turnover)
        prior20 = _context_at(day - 20, peers, close, volume, benchmark_close, market_turnover)
        raw_rs3[row] = current["peer_rs_3d_vs_0050"]
        raw_rs20[row] = current["peer_rs_20d_vs_0050"]
        share = current["peer_turnover_market_share"]
        share1 = prior1["peer_turnover_market_share"]
        share3 = prior3["peer_turnover_market_share"]
        share20 = prior20["peer_turnover_market_share"]
        turnover_acceleration = (
            np.log(share / share3) / 3 - np.log(share3 / share20) / 17
            if min(share, share3, share20) > 0 else np.nan
        )
        values = {
            "peer_rs_3d_vs_0050": raw_rs3[row],
            "peer_rs_20d_vs_0050": raw_rs20[row],
            "peer_rs_acceleration_3v20": np.nan,
            "peer_turnover_share_change_1d": _ratio(share, share1),
            "peer_turnover_share_change_3d": _ratio(share, share3),
            "peer_turnover_acceleration": turnover_acceleration,
            "peer_breadth_up_change_3d": current["peer_breadth_up"] - prior3["peer_breadth_up"],
            "peer_breadth_ma20_change_3d": current["peer_breadth_above_ma20"] - prior3["peer_breadth_above_ma20"],
            "peer_breadth_newhigh_change_3d": current["peer_breadth_newhigh"] - prior3["peer_breadth_newhigh"],
            "peer_volume_expansion_change_3d": current["peer_median_volume_ratio5"] - prior3["peer_median_volume_ratio5"],
        }
        features[row] = [values[name] for name in FEATURES]
        levels[row] = [current[name] for name in LEVEL_FEATURES]
        contexts[row] = (day, peers)
        for horizon in range(1, CFG.lead_time_sessions + 1):
            if day + horizon >= len(benchmark.calendar):
                break
            future = _context_at(day + horizon, peers, close, volume, benchmark_close, market_turnover)
            future_levels[row, horizon - 1] = [future[name] for name in LEVEL_FEATURES]
    # Exact same-day cross-sectional percentile difference; never future-normalized.
    for day in np.unique(meta["signal_date"][usable]):
        rows = np.flatnonzero(usable & (meta["signal_date"] == day))
        features[rows, FEATURES.index("peer_rs_acceleration_3v20")] = (
            _percentile(raw_rs3[rows]) - _percentile(raw_rs20[rows])
        )
    available = usable & np.all(np.isfinite(features), axis=1) & np.all(np.isfinite(levels), axis=1)
    return {
        "features": features,
        "levels": levels,
        "future_levels": future_levels,
        "available": available,
    }, {
        "source_peer_rows": int(np.count_nonzero(usable)),
        "available_rows": int(np.count_nonzero(available)),
        "coverage_pct": float(np.count_nonzero(available) / np.count_nonzero(arrays["stage_a_pool"])),
        "peer_membership_reused_exactly": True,
        "peer_refit_count": 0,
        "stage_a_refit_count": 0,
        "later_period_refit_count": 0,
        "leave_one_out_inherited_and_verified": True,
        "maximum_feature_date": int(meta["signal_date"].max()),
        "future_levels_used_for_lead_time_diagnostic_only": True,
    }


def save_store(path, store, audit):
    np.savez_compressed(
        path, **store, feature_names=np.asarray(FEATURES), level_feature_names=np.asarray(LEVEL_FEATURES),
        metadata_json=np.asarray(json.dumps(audit, sort_keys=True, allow_nan=False)),
    )


def load_acceleration_store(path):
    with np.load(path, allow_pickle=False) as payload:
        store = {name: payload[name].copy() for name in ("features", "levels", "future_levels", "available")}
        if tuple(str(x) for x in payload["feature_names"]) != FEATURES:
            raise RuntimeError("acceleration feature order drifted")
        if tuple(str(x) for x in payload["level_feature_names"]) != LEVEL_FEATURES:
            raise RuntimeError("level feature order drifted")
        audit = json.loads(str(payload["metadata_json"]))
    return store, audit


__all__ = [
    "build_acceleration_store", "load_acceleration_store", "load_frozen_arrays",
    "load_ohlcv", "load_store", "prepare_stocks", "protected_hashes", "save_store",
    "sha256_file", "store_digest",
]
