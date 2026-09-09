from __future__ import annotations

import csv
from pathlib import Path
import zipfile

import numpy as np

from cross_sectional_alpha_ranking_v01.preprocessing import iter_date_slices, rank_percentile

from .config import CHIP_FEATURES, CFG


def prior_session_map(signal_dates: np.ndarray, lag_sessions: int) -> dict[int, int | None]:
    unique = np.unique(signal_dates).astype(np.int64)
    result = {}
    for index, date in enumerate(unique):
        source = index - lag_sessions
        result[int(date)] = int(unique[source]) if source >= 0 else None
    return result


def needed_codes(
    signal_dates: np.ndarray,
    stock_codes: np.ndarray,
    stage_a_pool: np.ndarray,
    lag_sessions: int = CFG.publication_lag_sessions,
    lookback: int = 6,
) -> dict[int, set[int]]:
    unique = np.unique(signal_dates).astype(np.int64)
    position = {int(date): index for index, date in enumerate(unique)}
    result: dict[int, set[int]] = {}
    for index in np.flatnonzero(stage_a_pool):
        signal_date = int(signal_dates[index])
        code = int(stock_codes[index])
        end = position[signal_date] - lag_sessions
        for source_position in range(end - lookback + 1, end + 1):
            if source_position >= 0:
                result.setdefault(int(unique[source_position]), set()).add(code)
    return result


def _read_csv_rows(path: Path):
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(names) != 1:
                raise RuntimeError(f"expected one CSV in {path}")
            with archive.open(names[0]) as raw:
                yield from csv.DictReader(line.decode("utf-8-sig") for line in raw)
    else:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            yield from csv.DictReader(handle)


def load_needed_volumes(input_dir: Path, needed: dict[int, set[int]]) -> tuple[dict[tuple[int, int], float], dict]:
    sources = sorted(input_dir.glob("yearly_20*.zip"))
    supplement = input_dir / "twse_price_supplement.csv"
    if supplement.exists():
        sources.append(supplement)
    if not sources:
        raise FileNotFoundError(f"no OHLCV archives under {input_dir}")
    volumes: dict[tuple[int, int], float] = {}
    duplicates = 0
    invalid = 0
    for source in sources:
        for row in _read_csv_rows(source):
            try:
                date = int(str(row["date"]).replace("-", ""))
                code_text = str(row["code"]).strip()
                if not code_text.isdigit() or int(code_text) not in needed.get(date, set()):
                    continue
                code = int(code_text)
                volume = float(str(row["volume"]).replace(",", ""))
                if volume <= 0:
                    invalid += 1
                    continue
                key = (date, code)
                if key in volumes and not np.isclose(volumes[key], volume):
                    duplicates += 1
                    continue
                volumes[key] = volume
            except (KeyError, TypeError, ValueError):
                invalid += 1
    return volumes, {
        "source_files": [str(path) for path in sources],
        "needed_code_dates": sum(len(value) for value in needed.values()),
        "loaded_code_dates": len(volumes),
        "missing_code_dates": sum(len(value) for value in needed.values()) - len(volumes),
        "conflicting_duplicates": duplicates,
        "invalid_needed_volume_rows": invalid,
    }


def _consecutive_positive(values: np.ndarray) -> float:
    count = 0
    for value in values[::-1]:
        if value > 0:
            count += 1
        else:
            break
    return float(count)


def _ratio(values: np.ndarray, volumes: np.ndarray, sessions: int) -> float:
    denominator = float(np.sum(volumes[-sessions:]))
    return float(np.sum(values[-sessions:]) / denominator) if denominator > 0 else np.nan


def build_chip_features(
    meta: np.ndarray,
    stage_a_pool: np.ndarray,
    chip_daily: np.ndarray,
    volumes: dict[tuple[int, int], float],
    lag_sessions: int = CFG.publication_lag_sessions,
) -> tuple[dict[str, np.ndarray], dict]:
    dates = meta["signal_date"]
    codes = meta["stock_code"]
    unique = np.unique(dates).astype(np.int64)
    position = {int(date): index for index, date in enumerate(unique)}
    lookup = {(int(row["source_date"]), int(row["stock_code"])): row for row in chip_daily}
    raw = np.full((len(meta), len(CHIP_FEATURES)), np.nan, dtype=np.float64)
    source_date = np.zeros(len(meta), dtype=np.int32)
    institutional_complete = np.zeros(len(meta), dtype=bool)
    margin_observed = np.zeros(len(meta), dtype=bool)
    for index in np.flatnonzero(stage_a_pool):
        date = int(dates[index])
        code = int(codes[index])
        end = position[date] - lag_sessions
        if end < 5:
            continue
        history_dates = [int(value) for value in unique[end - 5:end + 1]]
        records = [lookup.get((day, code)) for day in history_dates]
        daily_volumes = np.asarray([volumes.get((day, code), np.nan) for day in history_dates])
        if any(record is None for record in records) or not np.all(np.isfinite(daily_volumes)):
            continue
        institutional = np.asarray([
            [float(record[name]) for name in ("foreign", "investment_trust", "dealer")]
            for record in records
        ])
        if not np.all(np.isfinite(institutional)):
            continue
        cursor = 0
        for column in range(3):
            values = institutional[-5:, column]
            raw[index, cursor:cursor + 5] = (
                _ratio(values, daily_volumes[-5:], 1),
                _ratio(values, daily_volumes[-5:], 3),
                _ratio(values, daily_volumes[-5:], 5),
                _consecutive_positive(values),
                float(np.mean(np.sign(values))),
            )
            cursor += 5
        institutional_complete[index] = True
        margin = np.asarray([[float(record["margin_balance"]), float(record["short_balance"])] for record in records])
        if np.all(np.isfinite(margin)):
            margin_shares = margin * 1000.0
            delta_margin = np.diff(margin_shares[:, 0])
            delta_short = np.diff(margin_shares[:, 1])
            raw[index, cursor:cursor + 8] = (
                _ratio(delta_margin[-1:], daily_volumes[-1:], 1),
                _ratio(delta_margin[-3:], daily_volumes[-3:], 3),
                _ratio(delta_margin, daily_volumes[-5:], 5),
                _ratio(delta_short[-1:], daily_volumes[-1:], 1),
                _ratio(delta_short[-3:], daily_volumes[-3:], 3),
                _ratio(delta_short, daily_volumes[-5:], 5),
                float(margin[-1, 1] / margin[-1, 0]) if margin[-1, 0] > 0 else 0.0,
                1.0,
            )
            margin_observed[index] = True
        else:
            raw[index, cursor:cursor + 7] = 0.0
            raw[index, cursor + 7] = 0.0
        source_date[index] = history_dates[-1]
    valid = stage_a_pool & institutional_complete & np.all(np.isfinite(raw), axis=1)
    transformed = np.zeros_like(raw)
    for _date, region in iter_date_slices(dates):
        local = valid[region]
        if not np.any(local):
            continue
        for column in range(raw.shape[1]):
            values = raw[region, column]
            finite = local & np.isfinite(values)
            if np.any(finite):
                temp = np.zeros(region.stop - region.start, dtype=np.float64)
                temp[finite] = rank_percentile(values[finite]) - 0.5
                transformed[region, column] = temp
    if np.any(source_date[valid] >= dates[valid]):
        raise RuntimeError("PIT violation: chip source date is not before signal date")
    return {
        "raw_chip_features": raw,
        "transformed_chip_features": transformed,
        "chip_valid": valid,
        "chip_source_date": source_date,
        "institutional_complete": institutional_complete,
        "margin_observed": margin_observed,
    }, {
        "stage_a_pool_rows": int(np.count_nonzero(stage_a_pool)),
        "chip_valid_rows": int(np.count_nonzero(valid)),
        "chip_valid_coverage_pct": float(np.mean(valid[stage_a_pool])),
        "institutional_complete_rows": int(np.count_nonzero(stage_a_pool & institutional_complete)),
        "institutional_coverage_pct": float(np.mean(institutional_complete[stage_a_pool])),
        "margin_observed_rows": int(np.count_nonzero(stage_a_pool & margin_observed)),
        "margin_coverage_pct": float(np.mean(margin_observed[stage_a_pool])),
        "lag_sessions": lag_sessions,
        "future_or_same_date_source_rows": int(np.count_nonzero(valid & (source_date >= dates))),
    }


__all__ = ["needed_codes", "load_needed_volumes", "build_chip_features", "prior_session_map"]
