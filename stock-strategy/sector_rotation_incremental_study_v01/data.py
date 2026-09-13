from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np

from conditional_path_quality_ranking_v01.data import protected_hashes
from surge_event_study_v01.data import load_ohlcv, prepare_stocks, sha256_file

from .config import CFG, FEATURES, Config


def load_frozen_arrays(
    stage_a_store: Path, conditional_store: Path, cfg: Config = CFG
) -> tuple[dict[str, np.ndarray], dict]:
    if sha256_file(stage_a_store) != cfg.expected_stage_a_store_sha256:
        raise RuntimeError("frozen Stage A store hash mismatch")
    if sha256_file(conditional_store) != cfg.expected_conditional_store_sha256:
        raise RuntimeError("frozen conditional store hash mismatch")
    with np.load(stage_a_store, allow_pickle=False) as payload:
        stage = {name: payload[name].copy() for name in payload.files}
    with np.load(conditional_store, allow_pickle=False) as payload:
        arrays = {name: payload[name].copy() for name in payload.files}
    if not np.array_equal(stage["meta"], arrays["meta"]):
        raise RuntimeError("frozen observation keys differ")
    if not np.array_equal(stage["stage_a_ranks"], arrays["stage_a_ranks"]):
        raise RuntimeError("frozen Stage A ranks drifted")
    expected_pool = arrays["stage_a_ranks"] <= 30
    if not np.array_equal(expected_pool, arrays["stage_a_pool"]):
        raise RuntimeError("Stage A Top30 definition drifted")
    dates = np.unique(arrays["meta"]["signal_date"])
    if int(np.count_nonzero(expected_pool)) != 30 * len(dates):
        raise RuntimeError("Stage A Top30 is not exactly 30 rows per date")
    return arrays, {
        "stage_a_store_sha256": sha256_file(stage_a_store),
        "conditional_store_sha256": sha256_file(conditional_store),
        "stage_a_rows": int(np.count_nonzero(expected_pool)),
        "stage_a_dates": int(len(dates)),
        "stage_a_refit_count": 0,
        "later_period_refit_count": 0,
    }


def build_panels(prepared, calendar: list[str]):
    codes = np.asarray(sorted(stock.code for stock in prepared), dtype="U8")
    code_pos = {code: i for i, code in enumerate(codes)}
    n_dates, n_codes = len(calendar), len(codes)
    close = np.full((n_dates, n_codes), np.nan, dtype=np.float64)
    volume = np.full_like(close, np.nan)
    returns = np.full_like(close, np.nan)
    segments = np.full((n_dates, n_codes), -1, dtype=np.int32)
    for stock in prepared:
        column = code_pos[stock.code]
        indices = np.asarray(stock.calendar_indices, dtype=np.int32)
        close[indices, column] = [bar.close for bar in stock.bars]
        volume[indices, column] = [bar.volume for bar in stock.bars]
        returns[indices, column] = stock.daily_returns
        segments[indices, column] = stock.segment_ids
    return codes, code_pos, close, volume, returns, segments


def _safe_ratio(numerator, denominator):
    if not np.isfinite(numerator) or not np.isfinite(denominator) or denominator == 0:
        return np.nan
    return float(numerator / denominator - 1.0)


def _row_features(
    day_index: int,
    peers: np.ndarray,
    stage_columns: set[int],
    close: np.ndarray,
    volume: np.ndarray,
    benchmark_close: np.ndarray,
    market_turnover: np.ndarray,
) -> tuple[np.ndarray, int]:
    current_close = close[day_index, peers]
    values: dict[str, float] = {}
    for horizon in (1, 3, 5, 20):
        peer_returns = current_close / close[day_index - horizon, peers] - 1.0
        values[f"peer_return_{horizon}d"] = float(np.nanmedian(peer_returns))
        if horizon in (3, 5, 20):
            benchmark_return = _safe_ratio(
                benchmark_close[day_index], benchmark_close[day_index - horizon]
            )
            values[f"peer_rs_{horizon}d_vs_0050"] = (
                values[f"peer_return_{horizon}d"] - benchmark_return
                if np.isfinite(benchmark_return) else np.nan
            )
    values["peer_breadth_up"] = float(np.mean(current_close > close[day_index - 1, peers]))
    ma20 = np.nanmean(close[day_index - 19 : day_index + 1, peers], axis=0)
    ma20_prior = np.nanmean(close[day_index - 24 : day_index - 4, peers], axis=0)
    values["peer_breadth_above_ma20"] = float(np.mean(current_close > ma20))
    values["peer_breadth_ma20_rising"] = float(np.mean(ma20 > ma20_prior))
    prior_high = np.nanmax(close[day_index - 20 : day_index, peers], axis=0)
    values["peer_breadth_20d_close_high"] = float(np.mean(current_close >= prior_high))

    turnover = close * volume
    peer_turnover = np.nansum(turnover[day_index - 20 : day_index + 1, peers], axis=1)
    shares = peer_turnover / market_turnover[day_index - 20 : day_index + 1]
    current_share = float(shares[-1])
    values["peer_turnover_market_share"] = current_share
    values["peer_turnover_share_vs_20d"] = _safe_ratio(current_share, np.nanmean(shares[:-1]))
    values["peer_turnover_share_change_5d"] = _safe_ratio(current_share, shares[-6])
    values["peer_turnover_share_change_20d"] = _safe_ratio(current_share, shares[0])
    current_volume = volume[day_index, peers]
    prior5 = np.nanmean(volume[day_index - 5 : day_index, peers], axis=0)
    prior20 = np.nanmean(volume[day_index - 20 : day_index, peers], axis=0)
    values["peer_median_volume_ratio5"] = float(np.nanmedian(current_volume / prior5))
    values["peer_median_volume_ratio20"] = float(np.nanmedian(current_volume / prior20))
    return np.asarray([values[name] for name in FEATURES], dtype=np.float64), len(
        stage_columns.intersection(int(x) for x in peers)
    )


def build_dynamic_peer_store(
    arrays: dict[str, np.ndarray], prepared, benchmark, cfg: Config = CFG
) -> tuple[dict[str, np.ndarray], dict]:
    """Compute exact trailing-return peers for frozen Stage A rows only."""
    meta = arrays["meta"]
    calendar = benchmark.calendar
    date_pos = {int(day): i for i, day in enumerate(calendar)}
    codes, code_pos, close, volume, daily_returns, segments = build_panels(prepared, calendar)
    benchmark_close = np.asarray(
        [np.nan if value is None else value for value in benchmark.normalized_closes], dtype=float
    )
    market_turnover = np.nansum(close * volume, axis=1)
    features = np.full((len(meta), len(FEATURES)), np.nan, dtype=np.float64)
    peer_codes = np.zeros((len(meta), cfg.peer_count), dtype=np.int32)
    peer_correlations = np.full((len(meta), cfg.peer_count), np.nan, dtype=np.float64)
    stage_a_peer_count = np.zeros(len(meta), dtype=np.uint8)
    available = np.zeros(len(meta), dtype=bool)
    diagnostics = Counter()

    unique_dates, starts = np.unique(meta["signal_date"], return_index=True)
    ends = np.r_[starts[1:], len(meta)]
    for day, start, end in zip(unique_dates, starts, ends):
        day = int(day)
        day_index = date_pos.get(day)
        if day_index is None or day_index < cfg.trailing_correlation_sessions:
            diagnostics["date_or_history_unavailable"] += 1
            continue
        block = slice(int(start), int(end))
        eligible_rows = np.arange(start, end)
        eligible_columns = np.asarray(
            [code_pos.get(str(code)) for code in meta["stock_code"][block]], dtype=object
        )
        valid_map = np.asarray([value is not None for value in eligible_columns])
        eligible_rows = eligible_rows[valid_map]
        candidate_columns = np.asarray(eligible_columns[valid_map], dtype=np.int32)
        stage_rows = eligible_rows[arrays["stage_a_pool"][eligible_rows]]
        stage_columns = {code_pos[str(meta["stock_code"][row])] for row in stage_rows}
        window = daily_returns[
            day_index - cfg.trailing_correlation_sessions + 1 : day_index + 1,
            candidate_columns,
        ]
        for row in stage_rows:
            target_code = str(meta["stock_code"][row])
            target_column = code_pos.get(target_code)
            if target_column is None:
                diagnostics["target_code_unavailable"] += 1
                continue
            target = daily_returns[
                day_index - cfg.trailing_correlation_sessions + 1 : day_index + 1,
                target_column,
            ]
            mask = np.isfinite(window) & np.isfinite(target[:, None])
            common = mask.sum(axis=0)
            x = np.where(mask, window, 0.0)
            y = np.where(mask, target[:, None], 0.0)
            x_mean = np.divide(x.sum(axis=0), common, out=np.zeros_like(common, dtype=float), where=common > 0)
            y_mean = np.divide(y.sum(axis=0), common, out=np.zeros_like(common, dtype=float), where=common > 0)
            xc = np.where(mask, window - x_mean, 0.0)
            yc = np.where(mask, target[:, None] - y_mean, 0.0)
            denominator = np.sqrt(np.sum(xc * xc, axis=0) * np.sum(yc * yc, axis=0))
            corr = np.divide(
                np.sum(xc * yc, axis=0), denominator,
                out=np.full(len(candidate_columns), np.nan), where=denominator > 0,
            )
            corr[common < cfg.minimum_common_sessions] = np.nan
            corr[candidate_columns == target_column] = np.nan
            valid = np.flatnonzero(np.isfinite(corr))
            if len(valid) < cfg.peer_count:
                diagnostics["insufficient_peers"] += 1
                continue
            tie_codes = codes[candidate_columns[valid]].astype("U8")
            order = valid[np.lexsort((tie_codes, -corr[valid]))[: cfg.peer_count]]
            peers = candidate_columns[order]
            if target_column in peers:
                raise RuntimeError("leave-one-out peer violation")
            vector, peer_count = _row_features(
                day_index, peers, stage_columns, close, volume, benchmark_close, market_turnover
            )
            if not np.all(np.isfinite(vector)):
                diagnostics["nonfinite_feature_vector"] += 1
                continue
            features[row] = vector
            peer_codes[row] = codes[peers].astype(np.int32)
            peer_correlations[row] = corr[order]
            stage_a_peer_count[row] = peer_count
            available[row] = True
    stage_rows = arrays["stage_a_pool"]
    return {
        "features": features,
        "peer_codes": peer_codes,
        "peer_correlations": peer_correlations,
        "stage_a_peer_count": stage_a_peer_count,
        "available": available,
    }, {
        "stage_a_rows": int(np.count_nonzero(stage_rows)),
        "available_rows": int(np.count_nonzero(available & stage_rows)),
        "coverage_pct": float(np.mean(available[stage_rows])),
        "unavailable_reasons": dict(sorted(diagnostics.items())),
        "peer_count": cfg.peer_count,
        "trailing_sessions": cfg.trailing_correlation_sessions,
        "minimum_common_sessions": cfg.minimum_common_sessions,
        "leave_one_out": True,
        "maximum_feature_timestamp": int(meta["signal_date"].max()),
        "future_data_used": False,
    }


def store_digest(store: dict[str, np.ndarray]) -> str:
    digest = hashlib.sha256()
    for name in sorted(store):
        digest.update(name.encode())
        digest.update(np.ascontiguousarray(store[name]).tobytes())
    return digest.hexdigest()


def save_store(path: Path, store: dict[str, np.ndarray], audit: dict) -> None:
    np.savez_compressed(
        path,
        **store,
        feature_names=np.asarray(FEATURES),
        metadata_json=np.asarray(json.dumps(audit, sort_keys=True, allow_nan=False)),
    )


def load_store(path: Path) -> tuple[dict[str, np.ndarray], dict]:
    with np.load(path, allow_pickle=False) as payload:
        required = {"features", "peer_codes", "peer_correlations", "stage_a_peer_count", "available", "feature_names", "metadata_json"}
        if not required.issubset(payload.files):
            raise RuntimeError("dynamic peer checkpoint is incomplete")
        if tuple(str(value) for value in payload["feature_names"]) != FEATURES:
            raise RuntimeError("dynamic peer checkpoint feature order drifted")
        store = {name: payload[name].copy() for name in required - {"feature_names", "metadata_json"}}
        audit = json.loads(str(payload["metadata_json"]))
    if audit.get("future_data_used") is not False or audit.get("leave_one_out") is not True:
        raise RuntimeError("dynamic peer checkpoint causal contract failed")
    return store, audit


__all__ = [
    "build_dynamic_peer_store", "load_frozen_arrays", "load_ohlcv", "prepare_stocks",
    "load_store", "protected_hashes", "save_store", "sha256_file", "store_digest",
]
