from __future__ import annotations

from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path
import zipfile

import numpy as np

from extension_entry_study_v01.pipeline import OUTCOME_FIELDS
from prospective_shadow_v01.detector import assert_frozen_contract
from surge_event_study_v01.data import prepare_benchmark, sha256_file
from surge_event_study_v01.models import Bar
from winner_coverage_taxonomy_v01.config import FAMILY_BIT

from .config import CFG, FEATURE_NAMES, Config


OUTCOME_INDEX = {name: index for index, name in enumerate(OUTCOME_FIELDS)}


def _read_npz(path: Path, expected_sha: str) -> dict[str, np.ndarray]:
    actual = sha256_file(path)
    if actual != expected_sha:
        raise RuntimeError(f"source store hash drifted: {path}: {actual}")
    with np.load(path, allow_pickle=False) as payload:
        return {name: payload[name].copy() for name in payload.files}


def load_reused_mother(
    winner_store: Path,
    extension_store: Path,
    cfg: Config = CFG,
) -> tuple[dict[str, np.ndarray], dict]:
    """Join the two already-published stores without creating another mother sample."""

    assert_frozen_contract()
    winner = _read_npz(winner_store, cfg.expected_winner_store_sha256)
    extension = _read_npz(extension_store, cfg.expected_extension_store_sha256)
    if len(winner["meta"]) != cfg.expected_mother_rows:
        raise RuntimeError("winner mother row count drifted")
    if not np.array_equal(winner["meta"], extension["meta"]):
        raise RuntimeError("winner and extension mother keys/metadata differ")
    winner_feature_names = tuple(str(value) for value in winner["feature_names"])
    extension_feature_names = tuple(str(value) for value in extension["feature_names"])
    outcome_fields = tuple(str(value) for value in winner["outcome_fields"])
    if outcome_fields != OUTCOME_FIELDS or tuple(str(x) for x in extension["outcome_fields"]) != OUTCOME_FIELDS:
        raise RuntimeError("shared outcome field order drifted")
    if not np.allclose(
        winner["outcomes"], extension["outcomes"], equal_nan=True, rtol=0.0, atol=0.0
    ):
        raise RuntimeError("shared outcome values differ across published stores")
    missing = [name for name in FEATURE_NAMES if name not in winner_feature_names]
    if missing:
        raise RuntimeError(f"required causal feature(s) missing: {missing}")
    feature_columns = [winner_feature_names.index(name) for name in FEATURE_NAMES]
    if "entry_gap" not in extension_feature_names:
        raise RuntimeError("published extension store has no entry_gap")
    entry_gap = extension["features"][:, extension_feature_names.index("entry_gap")]
    family_masks = winner["family_masks"]
    compact = (
        family_masks & FAMILY_BIT["N_COMPACT_RETEST_HYPOTHESIS"]
    ) != 0
    n_retest = (family_masks & FAMILY_BIT["N_RETEST"]) != 0
    if not np.all(~compact | n_retest):
        raise RuntimeError("N Compact is not a subset of N Retest")
    by_year = Counter(str(value)[:4] for value in winner["meta"]["signal_date"])
    if dict(sorted(by_year.items())) != dict(cfg.expected_year_counts):
        raise RuntimeError(f"mother year counts drifted: {dict(by_year)}")
    arrays = {
        "meta": winner["meta"],
        "raw_features": winner["taxonomy_features"][:, feature_columns],
        "outcomes": winner["outcomes"],
        "entry_gap": entry_gap,
        "n_compact": compact,
    }
    return arrays, {
        "mother_rows": len(winner["meta"]),
        "year_counts": dict(sorted(by_year.items())),
        "winner_store_sha256": sha256_file(winner_store),
        "extension_store_sha256": sha256_file(extension_store),
        "outcome_fields": list(OUTCOME_FIELDS),
        "feature_names": list(FEATURE_NAMES),
        "n_compact_signals": int(np.count_nonzero(compact)),
        "prospective_observations": int(
            np.count_nonzero(winner["meta"]["signal_date"] >= 20260907)
        ),
    }


def ledger_hashes(stock_strategy_dir: Path) -> dict[str, str]:
    data_dir = stock_strategy_dir / "prospective_shadow_v01" / "data"
    return {
        name: sha256_file(data_dir / name)
        for name in (
            "prospective_signals.csv",
            "prospective_outcomes.csv",
            "prospective_scan_log.csv",
            "shadow_status.json",
        )
    }


def _resolve_source(stock_strategy_dir: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else stock_strategy_dir / path


def _benchmark_rows(path: Path) -> list[Bar]:
    rows: list[Bar] = []

    def consume(handle) -> None:
        for row in csv.DictReader(handle):
            if str(row.get("code", "")).strip() != "0050":
                continue
            rows.append(
                Bar(
                    str(row["date"]).strip(),
                    "0050",
                    str(row.get("name", "0050")).strip(),
                    int(float(str(row["volume"]).replace(",", ""))),
                    float(str(row["open"]).replace(",", "")),
                    float(str(row["high"]).replace(",", "")),
                    float(str(row["low"]).replace(",", "")),
                    float(str(row["close"]).replace(",", "")),
                )
            )

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = sorted(name for name in archive.namelist() if name.lower().endswith(".csv"))
            for member in members:
                with archive.open(member) as raw:
                    consume(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
    elif path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            consume(handle)
    return rows


def load_market_regime(
    stock_strategy_dir: Path,
    signal_dates: np.ndarray,
    winner_manifest: Path,
    cfg: Config = CFG,
) -> tuple[dict[str, np.ndarray], dict]:
    """Load only 0050 from the immutable input list; TAIEX is unavailable."""

    manifest = json.loads(winner_manifest.read_text(encoding="utf-8"))
    sources = []
    bars_by_date: dict[str, Bar] = {}
    for item in manifest["input_hashes"]:
        path = _resolve_source(stock_strategy_dir, item["path"])
        actual = sha256_file(path)
        if actual != item["sha256"]:
            raise RuntimeError(f"market source hash drifted: {path}")
        for bar in _benchmark_rows(path):
            bars_by_date[bar.date] = bar
        sources.append({"path": str(path), "sha256": actual})
    calendar = [str(value) for value in np.unique(signal_dates)]
    benchmark = prepare_benchmark(calendar, sorted(bars_by_date.values(), key=lambda bar: bar.date), cfg)
    count = len(calendar)
    close_vs_ma20 = np.full(count, np.nan)
    ma20_slope5 = np.full(count, np.nan)
    volatility20 = np.full(count, np.nan)
    closes = np.asarray(
        [np.nan if value is None else value for value in benchmark.normalized_closes],
        dtype=float,
    )
    segments = np.asarray(
        [-1 if value is None else value for value in benchmark.segment_ids], dtype=int
    )
    for index in range(count):
        if index < 24 or segments[index] < 0 or segments[index - 24] != segments[index]:
            continue
        now = closes[index - 19 : index + 1]
        prior = closes[index - 24 : index - 4]
        if not np.all(np.isfinite(now)) or not np.all(np.isfinite(prior)):
            continue
        ma20 = float(np.mean(now))
        prior_ma20 = float(np.mean(prior))
        close_vs_ma20[index] = closes[index] / ma20 - 1.0
        ma20_slope5[index] = ma20 / prior_ma20 - 1.0
        returns = now[1:] / now[:-1] - 1.0
        volatility20[index] = float(np.std(returns, ddof=0))
    date_index = {int(day): index for index, day in enumerate(calendar)}
    aligned = np.asarray([date_index[int(day)] for day in signal_dates], dtype=int)
    discovery = np.asarray(
        [cfg.discovery_start <= day <= cfg.discovery_end for day in calendar]
    )
    volatility_boundary = float(np.nanmedian(volatility20[discovery]))
    result = {
        "close_vs_ma20": close_vs_ma20[aligned],
        "ma20_slope5": ma20_slope5[aligned],
        "volatility20": volatility20[aligned],
        "volatility_discovery_median": np.full(len(aligned), volatility_boundary),
    }
    return result, {
        "market_proxy": "0050",
        "taiex_status": "NOT_AVAILABLE_IN_REUSED_ARCHIVES",
        "regime_features_are_t_or_earlier": True,
        "calendar_dates": len(calendar),
        "benchmark_rows": len(bars_by_date),
        "unavailable_regime_dates": int(np.count_nonzero(~np.isfinite(close_vs_ma20))),
        "volatility_boundary_fit_period": "HISTORICAL_DISCOVERY_2020_2022_ONLY",
        "volatility_discovery_median": volatility_boundary,
        "sources": sources,
    }


def sha256_array_bundle(arrays: dict[str, np.ndarray]) -> str:
    digest = hashlib.sha256()
    for name in sorted(arrays):
        digest.update(name.encode("utf-8"))
        digest.update(np.ascontiguousarray(arrays[name]).tobytes())
    return digest.hexdigest()
