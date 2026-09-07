from __future__ import annotations

from collections import Counter
import hashlib
import json
import math

import numpy as np

from multi_setup_study_v01.config import CFG as MULTI_CFG
from multi_setup_study_v01.outcomes import evaluate_unified_outcome
from multi_setup_study_v01.setup_detectors import is_compact_retest
from reversal_event_study_v01.config import CFG as REVERSAL_CFG
from reversal_event_study_v01.study import build_pattern_observation
from surge_event_study_v01.analysis import percentile_ranks
from surge_event_study_v01.config import CFG as SURGE_CFG
from surge_event_study_v01.features import iter_signal_dates

from .config import CFG, COHORTS, FEATURE_NAMES, Config
from .features import (
    bucket_number,
    full_feature_vector,
    gap_bucket_number,
    momentum_scores,
)


OUTCOME_FIELDS: tuple[str, ...] = (
    "primary_success",
    "day1_close_return",
    "day3_close_return",
    "day5_close_return",
    "day10_close_return",
    "mfe_5d",
    "mfe_10d",
    "mae_5d",
    "mae_10d",
    "mfe_abs_mae",
    "gross_return",
    "net_return",
)

COHORT_BIT = {name: 1 << index for index, name in enumerate(COHORTS)}


class ObservationStore:
    """Compact append-only in-memory chunks for the full-market mother sample."""

    def __init__(self, chunk_size: int = 100_000):
        self.chunk_size = chunk_size
        self._chunks: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
        self._meta = self._new_meta()
        self._features = np.full((chunk_size, len(FEATURE_NAMES)), np.nan, dtype=np.float64)
        self._outcomes = np.full((chunk_size, len(OUTCOME_FIELDS)), np.nan, dtype=np.float64)
        self._position = 0
        self._total = 0

    def _new_meta(self) -> np.ndarray:
        return np.zeros(
            self.chunk_size,
            dtype=np.dtype(
                [
                    ("signal_date", "i4"),
                    ("stock_code", "i4"),
                    ("cohort_mask", "u1"),
                    ("momentum_strength_quintile", "u1"),
                    ("entry_gap_bucket", "u1"),
                    ("outcome_evaluable", "?"),
                ]
            ),
        )

    def _flush(self) -> None:
        if not self._position:
            return
        end = self._position
        self._chunks.append(
            (
                self._meta[:end].copy(),
                self._features[:end].copy(),
                self._outcomes[:end].copy(),
            )
        )
        self._meta = self._new_meta()
        self._features = np.full(
            (self.chunk_size, len(FEATURE_NAMES)), np.nan, dtype=np.float64
        )
        self._outcomes = np.full(
            (self.chunk_size, len(OUTCOME_FIELDS)), np.nan, dtype=np.float64
        )
        self._position = 0

    def append(
        self,
        *,
        signal_date: str,
        stock_code: str,
        cohort_mask: int,
        momentum_strength_quintile: int,
        entry_gap_bucket: int,
        feature_values: tuple[float, ...],
        outcome: dict,
    ) -> None:
        if self._position == self.chunk_size:
            self._flush()
        cursor = self._position
        self._meta[cursor] = (
            int(signal_date),
            int(stock_code),
            cohort_mask,
            momentum_strength_quintile,
            entry_gap_bucket,
            outcome["outcome_status"] == "EVALUABLE",
        )
        self._features[cursor] = feature_values
        if outcome["outcome_status"] == "EVALUABLE":
            self._outcomes[cursor] = tuple(
                float(bool(outcome[field]))
                if field == "primary_success"
                else (
                    math.nan
                    if outcome[field] is None
                    else float(outcome[field])
                )
                for field in OUTCOME_FIELDS
            )
        self._position += 1
        self._total += 1

    def finalize(self) -> dict[str, np.ndarray]:
        self._flush()
        if not self._chunks:
            raise RuntimeError("full-market observation store is empty")
        return {
            "meta": np.concatenate([chunk[0] for chunk in self._chunks]),
            "features": np.concatenate([chunk[1] for chunk in self._chunks]),
            "outcomes": np.concatenate([chunk[2] for chunk in self._chunks]),
        }


def validate_shared_contracts(cfg: Config = CFG) -> dict:
    resolved = {
        "surge_config_hash": SURGE_CFG.fingerprint(),
        "multi_setup_config_hash": MULTI_CFG.fingerprint(),
        "reversal_config_hash": REVERSAL_CFG.fingerprint(),
        "momentum_rule_hash": MULTI_CFG.source_surge_rule_hash,
    }
    expected = {
        "surge_config_hash": cfg.source_surge_config_hash,
        "multi_setup_config_hash": cfg.source_multi_setup_config_hash,
        "reversal_config_hash": cfg.source_reversal_config_hash,
        "momentum_rule_hash": cfg.source_surge_rule_hash,
    }
    if resolved != expected:
        raise RuntimeError(f"shared research contract drifted: {resolved} != {expected}")
    return resolved


def build_discovery_boundaries(
    prepared: list, benchmark, cfg: Config = CFG
) -> tuple[dict[str, tuple[float, ...]], dict[str, tuple[float, ...]], dict]:
    """Freeze boundaries on all finite 2020-2022 eligible observations."""

    chunks: list[np.ndarray] = []
    current = np.full((100_000, len(FEATURE_NAMES)), np.nan, dtype=np.float64)
    cursor = 0
    observations = 0
    bias20_parity_error = 0.0
    for _, _, contexts in iter_signal_dates(
        prepared, benchmark, cfg.discovery_start, cfg.discovery_end, cfg
    ):
        for observation, stock, local_index in contexts:
            if cursor == len(current):
                chunks.append(current)
                current = np.full_like(current, np.nan)
                cursor = 0
            values = full_feature_vector(stock, local_index)
            current[cursor] = values
            cursor += 1
            observations += 1
            bias20_parity_error = max(
                bias20_parity_error,
                abs(values[FEATURE_NAMES.index("bias_20")] - observation.features["close_vs_sma20"]),
            )
    if cursor:
        chunks.append(current[:cursor].copy())
    if not chunks:
        raise RuntimeError("no discovery mother-sample observations")
    matrix = np.concatenate(chunks)
    deciles: dict[str, tuple[float, ...]] = {}
    quintiles: dict[str, tuple[float, ...]] = {}
    finite_counts: dict[str, int] = {}
    for index, feature in enumerate(FEATURE_NAMES):
        values = matrix[:, index]
        values = values[np.isfinite(values)]
        if not len(values):
            raise RuntimeError(f"no finite discovery values for {feature}")
        deciles[feature] = tuple(
            float(value)
            for value in np.quantile(values, np.arange(1, 10) / 10.0, method="linear")
        )
        quintiles[feature] = tuple(
            float(value)
            for value in np.quantile(values, np.arange(1, 5) / 5.0, method="linear")
        )
        finite_counts[feature] = int(len(values))
        if not np.allclose(
            np.asarray(quintiles[feature]), np.asarray(deciles[feature])[1::2], rtol=0, atol=1e-14
        ):
            raise RuntimeError(f"quintile/decile boundary mismatch for {feature}")
    if bias20_parity_error > 1e-12:
        raise RuntimeError(f"bias_20 parity failed: maximum error={bias20_parity_error}")
    payload = json.dumps(
        {"deciles": deciles, "quintiles": quintiles},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    audit = {
        "discovery_observations": observations,
        "finite_counts": finite_counts,
        "entry_gap_boundary_denominator": "T_PLUS_1_ENTRY_OBSERVED_ONLY",
        "bias20_close_vs_sma20_max_abs_error": bias20_parity_error,
        "boundary_sha256": hashlib.sha256(payload).hexdigest(),
        "boundary_source_period": "HISTORICAL_DISCOVERY_2020_2022_ONLY",
    }
    return deciles, quintiles, audit


def _accepted(last_by_code: dict[str, int], code: str, calendar_index: int, cfg: Config) -> bool:
    previous = last_by_code.get(code)
    accepted = previous is None or calendar_index - previous >= cfg.causal_cooldown_sessions
    if accepted:
        last_by_code[code] = calendar_index
    return accepted


def _outcome_digest_values(outcome: dict) -> str:
    if outcome["outcome_status"] != "EVALUABLE":
        return f"{outcome['outcome_status']}:{outcome['outcome_reason']}"
    return ",".join(
        "NA" if outcome[field] is None else f"{float(outcome[field]):.17g}"
        for field in OUTCOME_FIELDS
    )


def scan_full_market(
    prepared: list,
    benchmark,
    decile_boundaries: dict[str, tuple[float, ...]],
    cfg: Config = CFG,
) -> tuple[dict[str, np.ndarray], dict]:
    """Continuous 2019 warmup scan; only 2020-2025 rows enter the mother sample."""

    store = ObservationStore()
    digest = hashlib.sha256()
    last_momentum: dict[str, int] = {}
    last_n: dict[str, int] = {}
    censored = Counter()
    year_counts = Counter()
    year_evaluable = Counter()
    cohort_counts = Counter()
    raw_candidates = Counter()
    accepted_candidates = Counter()
    bias20_parity_error = 0.0
    maximum_feature_date: str | None = None
    minimum_feature_date: str | None = None

    for day, calendar_index, contexts in iter_signal_dates(
        prepared, benchmark, cfg.warmup_start, cfg.stress_end, cfg
    ):
        _, strength_percentiles, top_indices = momentum_scores(contexts)
        accepted_momentum: set[int] = set()
        for index in top_indices:
            raw_candidates["MOMENTUM_DIRECTIONAL"] += 1
            code = contexts[index][0].code
            if _accepted(last_momentum, code, calendar_index, cfg):
                accepted_momentum.add(index)
                accepted_candidates["MOMENTUM_DIRECTIONAL"] += 1

        return20_percentiles = percentile_ranks(
            [float(context[0].features["return_20"]) for context in contexts]
        )
        accepted_n: dict[int, dict] = {}
        for index, (_, stock, local_index) in enumerate(contexts):
            legacy = build_pattern_observation(stock, local_index, benchmark, REVERSAL_CFG)
            if legacy is None or legacy.pattern != "N_RETEST":
                continue
            raw_candidates["N_RETEST"] += 1
            compact = is_compact_retest(legacy.geometry, MULTI_CFG)
            if compact:
                raw_candidates["N_COMPACT_RETEST_HYPOTHESIS"] += 1
            if _accepted(last_n, stock.code, calendar_index, cfg):
                accepted_n[index] = legacy.geometry
                accepted_candidates["N_RETEST"] += 1
                if compact:
                    accepted_candidates["N_COMPACT_RETEST_HYPOTHESIS"] += 1

        if day < cfg.discovery_start:
            continue
        if day > cfg.stress_end:
            raise RuntimeError(f"post-2025 feature date reached: {day}")

        for index, (observation, stock, local_index) in enumerate(contexts):
            values = full_feature_vector(stock, local_index)
            value_by_name = dict(zip(FEATURE_NAMES, values))
            bias20_parity_error = max(
                bias20_parity_error,
                abs(value_by_name["bias_20"] - observation.features["close_vs_sma20"]),
            )
            mask = COHORT_BIT["ALL_ELIGIBLE"]
            if return20_percentiles[index] >= 0.80:
                mask |= COHORT_BIT["RETURN20_TOP20PCT_SAME_DAY"]
            if observation.features["breakout_vs_prior20"] >= cfg.breakout_near_floor:
                mask |= COHORT_BIT["NEAR_OR_ABOVE_PRIOR20_CLOSE_HIGH_WITHIN_2PCT"]
            if index in accepted_momentum:
                mask |= COHORT_BIT["MOMENTUM_DIRECTIONAL_FROZEN_CANDIDATE"]
            if index in accepted_n:
                mask |= COHORT_BIT["N_RETEST"]
                if is_compact_retest(accepted_n[index], MULTI_CFG):
                    mask |= COHORT_BIT["N_COMPACT_RETEST_HYPOTHESIS"]

            outcome = evaluate_unified_outcome(stock, local_index, cfg)
            if outcome["outcome_status"] != "EVALUABLE":
                censored[outcome["outcome_reason"]] += 1
            strength_q = min(5, max(1, int(strength_percentiles[index] * 5) + 1))
            store.append(
                signal_date=day,
                stock_code=stock.code,
                cohort_mask=mask,
                momentum_strength_quintile=strength_q,
                entry_gap_bucket=gap_bucket_number(
                    value_by_name["entry_gap"]
                    if math.isfinite(value_by_name["entry_gap"])
                    else None
                ),
                feature_values=values,
                outcome=outcome,
            )
            year = day[:4]
            year_counts[year] += 1
            year_evaluable[year] += int(outcome["outcome_status"] == "EVALUABLE")
            for cohort, bit in COHORT_BIT.items():
                if mask & bit:
                    cohort_counts[cohort] += 1
            digest.update(
                (
                    f"{day}|{stock.code}|{mask}|{strength_q}|"
                    + ",".join(
                        "NA" if not math.isfinite(value) else f"{value:.17g}"
                        for value in values
                    )
                    + "|"
                    + _outcome_digest_values(outcome)
                    + "\n"
                ).encode("utf-8")
            )
            minimum_feature_date = min(minimum_feature_date or day, day)
            maximum_feature_date = max(maximum_feature_date or day, day)

    arrays = store.finalize()
    if bias20_parity_error > 1e-12:
        raise RuntimeError(f"bias_20 parity failed: maximum error={bias20_parity_error}")
    if len(arrays["meta"]) != sum(year_counts.values()):
        raise RuntimeError("observation-store count mismatch")
    # Ensure every stored finite value uses the single frozen discovery edge set.
    feature_bins = np.zeros(arrays["features"].shape, dtype=np.uint8)
    for column, feature in enumerate(FEATURE_NAMES):
        edges = np.asarray(decile_boundaries[feature], dtype=np.float64)
        values = arrays["features"][:, column]
        finite = np.isfinite(values)
        # searchsorted(side=left) preserves the preregistered equality-to-lower rule.
        feature_bins[finite, column] = np.searchsorted(
            edges, values[finite], side="left"
        ).astype(np.uint8) + 1
    arrays["feature_bins"] = feature_bins
    audit = {
        "mother_sample_rows": int(len(arrays["meta"])),
        "observation_sha256": digest.hexdigest(),
        "year_signal_counts": dict(sorted(year_counts.items())),
        "year_evaluable_counts": dict(sorted(year_evaluable.items())),
        "cohort_signal_counts": dict(sorted(cohort_counts.items())),
        "raw_candidates_including_2019_warmup": dict(sorted(raw_candidates.items())),
        "accepted_candidates_including_2019_warmup": dict(
            sorted(accepted_candidates.items())
        ),
        "censored_reasons": dict(sorted(censored.items())),
        "minimum_feature_date": minimum_feature_date,
        "maximum_feature_date": maximum_feature_date,
        "bias20_close_vs_sma20_max_abs_error": bias20_parity_error,
        "momentum_top_set_size_contract": cfg.momentum_daily_selection_count,
        "mother_sample_cooldown": "NONE",
        "candidate_cohort_cooldown_sessions": cfg.causal_cooldown_sessions,
        "features_read_after_t": False,
        "actual_orders": 0,
        "actual_fills": 0,
    }
    return arrays, audit


def save_observation_store(path, arrays: dict[str, np.ndarray], metadata: dict) -> None:
    np.savez_compressed(
        path,
        meta=arrays["meta"],
        features=arrays["features"],
        feature_bins=arrays["feature_bins"],
        outcomes=arrays["outcomes"],
        feature_names=np.asarray(FEATURE_NAMES),
        outcome_fields=np.asarray(OUTCOME_FIELDS),
        metadata_json=np.asarray(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, allow_nan=False)
        ),
    )
