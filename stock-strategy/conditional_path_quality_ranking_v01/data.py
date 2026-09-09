from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from cross_sectional_alpha_ranking_v01.data import ledger_hashes
from cross_sectional_alpha_ranking_v01.ranking import build_daily_ranks
from prospective_shadow_v01.detector import assert_frozen_contract
from surge_event_study_v01.data import sha256_file
from upside_opportunity_ranking_v01.models import UpsideModel

from .config import CFG, Config


def _npz(path: Path, expected: str) -> dict[str, np.ndarray]:
    actual = sha256_file(path)
    if actual != expected:
        raise RuntimeError(f"immutable store hash drifted: {path}: {actual}")
    with np.load(path, allow_pickle=False) as payload:
        return {name: payload[name].copy() for name in payload.files}


def path_classes(
    outcomes: np.ndarray,
    outcome_fields: tuple[str, ...],
    evaluable: np.ndarray,
    cfg: Config = CFG,
) -> np.ndarray:
    """1=upside first, 2=downside first, 3=timeout, 0=censored."""

    index = {name: position for position, name in enumerate(outcome_fields)}
    success = outcomes[:, index["primary_success"]]
    mfe10 = outcomes[:, index["mfe_10d"]]
    mae10 = outcomes[:, index["mae_10d"]]
    classes = np.zeros(len(outcomes), dtype=np.uint8)
    classes[evaluable & (success >= 0.5)] = 1
    failure = evaluable & (success < 0.5)
    classes[failure & (mae10 <= cfg.primary_stop + 1e-12)] = 2
    classes[failure & (mae10 > cfg.primary_stop + 1e-12)] = 3
    if np.any(evaluable & (classes == 0)):
        raise RuntimeError("evaluable path could not be classified")
    timeout = classes == 3
    if np.any(timeout & (mfe10 >= cfg.primary_target - 1e-12)):
        raise RuntimeError("timeout row reached target")
    return classes


def load_reused_inputs(
    winner_store: Path,
    cross_store: Path,
    upside_store: Path,
    stage_a_model_spec: Path,
    stage_a_manifest: Path,
    cfg: Config = CFG,
) -> tuple[dict[str, np.ndarray], UpsideModel, dict]:
    assert_frozen_contract()
    winner = _npz(winner_store, cfg.expected_winner_store_sha256)
    cross = _npz(cross_store, cfg.expected_cross_store_sha256)
    upside = _npz(upside_store, cfg.expected_upside_store_sha256)
    if len(winner["meta"]) != cfg.expected_mother_rows:
        raise RuntimeError("mother row count drifted")
    if not np.array_equal(winner["meta"], cross["meta"]):
        raise RuntimeError("winner and cross-sectional keys differ")
    if not np.array_equal(winner["meta"], upside["meta"]):
        raise RuntimeError("published Stage A keys differ from mother sample")
    if sha256_file(stage_a_model_spec) != cfg.expected_stage_a_model_spec_sha256:
        raise RuntimeError("published Stage A model spec hash drifted")
    if sha256_file(stage_a_manifest) != cfg.expected_stage_a_manifest_sha256:
        raise RuntimeError("published Stage A manifest hash drifted")
    model_spec = json.loads(stage_a_model_spec.read_text(encoding="utf-8"))
    manifest = json.loads(stage_a_manifest.read_text(encoding="utf-8"))
    payload = model_spec["stage_a_primary_model"]
    model = UpsideModel(
        payload["name"], payload["fit_period"], tuple(payload["feature_names"]),
        tuple(payload["coefficients"]), payload["intercept"],
        payload["regularization_alpha"], payload["training_observations"],
    )
    if model.fingerprint() != cfg.expected_stage_a_fingerprint:
        raise RuntimeError("published Stage A fingerprint drifted")
    if manifest["stage_a_model_fingerprints"][model.name] != model.fingerprint():
        raise RuntimeError("published Stage A manifest fingerprint disagrees")
    reproduced_scores = model.predict(cross["transformed_features"])
    if not np.allclose(reproduced_scores, upside["stage_a_scores"], rtol=0.0, atol=1e-15):
        raise RuntimeError("published Stage A scores do not reproduce exactly")
    reproduced_ranking, _ = build_daily_ranks(
        reproduced_scores, winner["meta"]["signal_date"], winner["meta"]["stock_code"]
    )
    if not np.array_equal(reproduced_ranking["rank"], upside["stage_a_ranks"]):
        raise RuntimeError("published Stage A ranks do not reproduce exactly")
    if int(np.count_nonzero(winner["meta"]["signal_date"] >= 20260907)):
        raise RuntimeError("prospective observations entered historical study")
    outcome_fields = tuple(str(value) for value in winner["outcome_fields"])
    paths = path_classes(
        winner["outcomes"], outcome_fields, winner["meta"]["outcome_evaluable"], cfg
    )
    arrays = {
        "meta": winner["meta"],
        "outcomes": winner["outcomes"],
        "descriptive_outcomes": winner["descriptive_outcomes"],
        "outcome_fields": winner["outcome_fields"],
        "raw_features": cross["raw_features"],
        "transformed_features": cross["transformed_features"],
        "feature_names": cross["feature_names"],
        "entry_gap": upside["entry_gap"],
        "n_compact": upside["n_compact"],
        "stage_a_scores": upside["stage_a_scores"],
        "stage_a_ranks": upside["stage_a_ranks"],
        "previous_two_stage_ranks": upside["two_stage_ranks"],
        "path_class": paths,
        "path_success": (paths == 1).astype(np.float64),
    }
    pool = arrays["stage_a_ranks"] <= cfg.stage_a_pool_size
    expected_pool_rows = cfg.stage_a_pool_size * len(np.unique(arrays["meta"]["signal_date"]))
    if int(np.count_nonzero(pool)) != expected_pool_rows:
        raise RuntimeError("published Stage A Top30 pool is not exactly 30 rows per date")
    return arrays, model, {
        "mother_rows": len(winner["meta"]),
        "stage_a_pool_rows": int(np.count_nonzero(pool)),
        "stage_a_pool_dates": int(len(np.unique(winner["meta"]["signal_date"]))),
        "winner_store_sha256": sha256_file(winner_store),
        "cross_store_sha256": sha256_file(cross_store),
        "upside_store_sha256": sha256_file(upside_store),
        "stage_a_model_spec_sha256": sha256_file(stage_a_model_spec),
        "stage_a_manifest_sha256": sha256_file(stage_a_manifest),
        "stage_a_model_fingerprint": model.fingerprint(),
        "stage_a_refit_count": 0,
        "prospective_observations": 0,
        "n_compact_signals": int(np.count_nonzero(arrays["n_compact"])),
        "path_class_counts": {
            "UPSIDE_FIRST": int(np.count_nonzero(paths == 1)),
            "DOWNSIDE_FIRST": int(np.count_nonzero(paths == 2)),
            "TIMEOUT": int(np.count_nonzero(paths == 3)),
            "CENSORED": int(np.count_nonzero(paths == 0)),
        },
    }


def protected_hashes(stock_strategy: Path) -> dict[str, str]:
    result = ledger_hashes(stock_strategy)
    for module in (
        "upside_opportunity_ranking_v01",
        "cross_sectional_alpha_ranking_v01",
        "winner_coverage_taxonomy_v01",
        "multi_setup_study_v01",
        "prospective_shadow_v01",
        "shadow_daily_runner",
    ):
        root = stock_strategy / module
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if (
                path.is_file()
                and path.suffix.lower() in {".csv", ".json", ".py", ".toml", ".plist"}
                and "__pycache__" not in path.parts
            ):
                result[str(path.relative_to(stock_strategy))] = sha256_file(path)
    return result


def array_digest(arrays: dict[str, np.ndarray]) -> str:
    digest = hashlib.sha256()
    for name in sorted(arrays):
        value = arrays[name]
        if isinstance(value, np.ndarray):
            digest.update(name.encode("utf-8"))
            digest.update(np.ascontiguousarray(value).tobytes())
    return digest.hexdigest()


__all__ = [
    "array_digest", "load_reused_inputs", "path_classes", "protected_hashes",
]
