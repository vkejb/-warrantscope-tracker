from __future__ import annotations

from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from extension_entry_study_v01.pipeline import COHORT_BIT, OUTCOME_FIELDS
from multi_setup_study_v01.config import CFG as MULTI_CFG
from multi_setup_study_v01.outcomes import evaluate_unified_outcome
from prospective_shadow_v01.detector import assert_frozen_contract
from surge_event_study_v01.data import sha256_file
from surge_event_study_v01.features import iter_signal_dates

from .config import CFG, FAMILY_BIT, FEATURE_NAMES, MAJOR_SETUPS, Config
from .features import build_taxonomy_features


def load_extension_mother(path: Path, cfg: Config = CFG) -> tuple[dict[str, np.ndarray], dict]:
    if sha256_file(path) != cfg.expected_extension_store_sha256:
        raise RuntimeError("published extension observation store hash drifted")
    with np.load(path, allow_pickle=False) as payload:
        arrays = {name: payload[name].copy() for name in ("meta", "features", "outcomes")}
        outcome_fields = tuple(str(value) for value in payload["outcome_fields"])
        metadata = json.loads(str(payload["metadata_json"]))
    if outcome_fields != OUTCOME_FIELDS:
        raise RuntimeError("shared outcome field order drifted")
    if metadata["observation_sha256"] != cfg.expected_extension_observation_sha256:
        raise RuntimeError("published mother-sample observation hash drifted")
    if int(metadata["row_count"]) != len(arrays["meta"]):
        raise RuntimeError("published mother-sample count mismatch")
    return arrays, metadata


def _set(mask: int, family: str) -> int:
    return mask | FAMILY_BIT[family]


def load_frozen_setup_memberships(
    signal_csv: Path,
) -> tuple[dict[tuple[int, int], int], dict[str, int]]:
    """Load exact published Setup membership; never reinterpret old detector output."""

    counts: Counter[str] = Counter()
    memberships: dict[tuple[int, int], int] = {}
    seen: set[tuple[int, int, str]] = set()
    with signal_csv.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            setup = row["setup"]
            if setup not in MAJOR_SETUPS or not (
                "20200101" <= row["signal_date"] <= "20251231"
            ):
                continue
            key = (int(row["signal_date"]), int(row["code"]))
            unique = (*key, setup)
            if unique in seen:
                raise RuntimeError(f"duplicate frozen Setup membership: {unique}")
            seen.add(unique)
            memberships[key] = _set(memberships.get(key, 0), setup)
            counts[setup] += 1
    missing = [setup for setup in MAJOR_SETUPS if counts[setup] == 0]
    if missing:
        raise RuntimeError(f"published signal source missing Setup(s): {missing}")
    compact = FAMILY_BIT["N_COMPACT_RETEST_HYPOTHESIS"]
    parent = FAMILY_BIT["N_RETEST"]
    if any((mask & compact) and not (mask & parent) for mask in memberships.values()):
        raise RuntimeError("published N Compact membership is not a subset of N Retest")
    return memberships, dict(sorted(counts.items()))


def scan_taxonomy_inputs(
    prepared: list,
    benchmark,
    mother: dict[str, np.ndarray],
    frozen_memberships: dict[tuple[int, int], int],
    cfg: Config = CFG,
) -> tuple[dict[str, np.ndarray], dict]:
    """Join exact frozen Setup membership and compute T-or-earlier features."""

    assert_frozen_contract()
    meta = mother["meta"]
    features = np.full((len(meta), len(FEATURE_NAMES)), np.nan, dtype=np.float64)
    family_masks = np.zeros(len(meta), dtype=np.uint16)
    descriptive_outcomes = np.full((len(meta), 2), np.nan, dtype=np.float64)
    counts: Counter[str] = Counter()
    cursor = 0
    used_memberships: set[tuple[int, int]] = set()
    feature_missing: Counter[str] = Counter()
    maximum_t_read: str | None = None

    for day, _calendar_index, contexts in iter_signal_dates(
        prepared, benchmark, cfg.warmup_start, cfg.stress_end, cfg
    ):
        if day < cfg.discovery_start:
            continue
        if day > cfg.stress_end:
            raise RuntimeError(f"post-2025 observation reached: {day}")

        for _observation, stock, local_index in contexts:
            if cursor >= len(meta):
                raise RuntimeError("replay produced more rows than frozen mother sample")
            expected = meta[cursor]
            if int(expected["signal_date"]) != int(day) or int(expected["stock_code"]) != int(stock.code):
                raise RuntimeError(
                    "mother/replay key mismatch at row "
                    f"{cursor}: expected={(expected['signal_date'], expected['stock_code'])}, "
                    f"actual={(day, stock.code)}"
                )
            key = (int(day), int(stock.code))
            mask = frozen_memberships.get(key, 0)
            if key in frozen_memberships:
                used_memberships.add(key)
            extension_mask = int(expected["cohort_mask"])
            if extension_mask & COHORT_BIT["NEAR_OR_ABOVE_PRIOR20_CLOSE_HIGH_WITHIN_2PCT"]:
                mask = _set(mask, "NEAR_OR_ABOVE_PRIOR20_CLOSE_HIGH_WITHIN_2PCT")
            if extension_mask & COHORT_BIT["RETURN20_TOP20PCT_SAME_DAY"]:
                mask = _set(mask, "RETURN20_TOP20PCT_SAME_DAY")
            vector = build_taxonomy_features(stock, local_index, benchmark)
            features[cursor] = vector
            for feature, value in zip(FEATURE_NAMES, vector):
                if not math.isfinite(value):
                    feature_missing[feature] += 1
            family_masks[cursor] = mask
            shared_outcome = evaluate_unified_outcome(stock, local_index, MULTI_CFG)
            expected_evaluable = bool(expected["outcome_evaluable"])
            actual_evaluable = shared_outcome["outcome_status"] == "EVALUABLE"
            if expected_evaluable != actual_evaluable:
                raise RuntimeError(
                    f"shared outcome evaluability drifted for {day}/{stock.code}"
                )
            if actual_evaluable:
                expected_success = mother["outcomes"][
                    cursor, OUTCOME_FIELDS.index("primary_success")
                ]
                if float(bool(shared_outcome["primary_success"])) != expected_success:
                    raise RuntimeError(
                        f"shared Winner label drifted for {day}/{stock.code}"
                    )
                descriptive_outcomes[cursor] = (
                    float(bool(shared_outcome["plus10_before_minus5"])),
                    float(bool(shared_outcome["plus15_before_minus5"])),
                )
            for family, bit in FAMILY_BIT.items():
                if mask & bit:
                    counts[family] += 1
            cursor += 1
            maximum_t_read = max(maximum_t_read or day, day)

    if cursor != len(meta):
        raise RuntimeError(f"mother/replay row mismatch: {cursor} != {len(meta)}")
    unused = sorted(set(frozen_memberships) - used_memberships)
    if unused:
        raise RuntimeError(
            f"published Setup membership falls outside mother sample: {unused[:5]}"
        )
    by_year = Counter(str(value)[:4] for value in meta["signal_date"])
    expected_years = dict(cfg.expected_mother_year_counts)
    if dict(sorted(by_year.items())) != expected_years:
        raise RuntimeError(f"mother year counts drifted: {dict(by_year)}")
    return {
        **mother,
        "taxonomy_features": features,
        "family_masks": family_masks,
        "descriptive_outcomes": descriptive_outcomes,
    }, {
        "mother_sample_rows": len(meta),
        "year_observation_counts": expected_years,
        "family_signal_counts": dict(sorted(counts.items())),
        "frozen_membership_source": "multi_setup_study_v01/signal_observations.csv",
        "unused_frozen_membership_keys": 0,
        "feature_missing_counts": dict(sorted(feature_missing.items())),
        "maximum_t_feature_date_read": maximum_t_read,
        "features_read_after_t": False,
        "setup_membership_read_after_t": False,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }


def save_local_store(path: Path, arrays: dict[str, np.ndarray], metadata: dict) -> None:
    np.savez_compressed(
        path,
        meta=arrays["meta"],
        outcomes=arrays["outcomes"],
        taxonomy_features=arrays["taxonomy_features"],
        family_masks=arrays["family_masks"],
        descriptive_outcomes=arrays["descriptive_outcomes"],
        feature_names=np.asarray(FEATURE_NAMES),
        outcome_fields=np.asarray(OUTCOME_FIELDS),
        metadata_json=np.asarray(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, allow_nan=False)
        ),
    )


def store_digest(arrays: dict[str, np.ndarray]) -> str:
    digest = hashlib.sha256()
    for name in (
        "meta",
        "outcomes",
        "taxonomy_features",
        "family_masks",
        "descriptive_outcomes",
    ):
        digest.update(name.encode("ascii"))
        digest.update(np.ascontiguousarray(arrays[name]).tobytes())
    return digest.hexdigest()
