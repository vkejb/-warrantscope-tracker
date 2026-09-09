from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from cross_sectional_alpha_ranking_v01.data import ledger_hashes
from cross_sectional_alpha_ranking_v01.models import LinearModel
from prospective_shadow_v01.detector import assert_frozen_contract
from surge_event_study_v01.data import sha256_file

from .config import CFG, Config


def _npz(path: Path, expected: str) -> dict[str, np.ndarray]:
    actual = sha256_file(path)
    if actual != expected:
        raise RuntimeError(f"immutable store hash drifted: {path}: {actual}")
    with np.load(path, allow_pickle=False) as payload:
        return {name: payload[name].copy() for name in payload.files}


def load_reused_inputs(
    winner_store: Path,
    cross_store: Path,
    stage_b_model_spec: Path,
    stage_b_manifest: Path,
    cfg: Config = CFG,
) -> tuple[dict[str, np.ndarray], LinearModel, dict]:
    assert_frozen_contract()
    winner = _npz(winner_store, cfg.expected_winner_store_sha256)
    cross = _npz(cross_store, cfg.expected_cross_store_sha256)
    if len(winner["meta"]) != cfg.expected_mother_rows:
        raise RuntimeError("mother row count drifted")
    if not np.array_equal(winner["meta"], cross["meta"]):
        raise RuntimeError("winner and published cross-sectional keys differ")
    if sha256_file(stage_b_model_spec) != cfg.expected_stage_b_model_spec_sha256:
        raise RuntimeError("published Stage B model spec hash drifted")
    model_spec = json.loads(stage_b_model_spec.read_text(encoding="utf-8"))
    manifest = json.loads(stage_b_manifest.read_text(encoding="utf-8"))
    payload = model_spec["primary_model"]
    stage_b = LinearModel(
        payload["name"], payload["fit_period"], tuple(payload["coefficients"]),
        payload["intercept"], payload["regularization_alpha"],
        payload["training_observations"],
    )
    if stage_b.fingerprint() != cfg.expected_stage_b_fingerprint:
        raise RuntimeError("published Stage B model fingerprint drifted")
    if manifest["model_fingerprints"][stage_b.name] != stage_b.fingerprint():
        raise RuntimeError("Stage B manifest fingerprint disagrees")
    predicted = stage_b.predict(cross["transformed_features"])
    if not np.allclose(predicted, cross["ridge_scores"], rtol=0.0, atol=1e-15):
        raise RuntimeError("published Stage B scores do not reproduce exactly")
    if int(np.count_nonzero(winner["meta"]["signal_date"] >= 20260907)):
        raise RuntimeError("prospective observations entered historical study")
    feature_names = tuple(str(value) for value in cross["feature_names"])
    if tuple(payload["feature_names"]) != feature_names:
        raise RuntimeError("Stage B feature order drifted")
    arrays = {
        "meta": winner["meta"],
        "outcomes": winner["outcomes"],
        "descriptive_outcomes": winner["descriptive_outcomes"],
        "raw_features": cross["raw_features"],
        "transformed_features": cross["transformed_features"],
        "entry_gap": cross["entry_gap"],
        "n_compact": cross["n_compact"],
        "stage_b_scores": cross["ridge_scores"],
        "stage_b_ranks": cross["ridge_ranks"],
        "feature_names": np.asarray(feature_names),
        "outcome_fields": winner["outcome_fields"],
    }
    return arrays, stage_b, {
        "mother_rows": len(winner["meta"]),
        "winner_store_sha256": sha256_file(winner_store),
        "cross_store_sha256": sha256_file(cross_store),
        "stage_b_model_spec_sha256": sha256_file(stage_b_model_spec),
        "stage_b_manifest_sha256": sha256_file(stage_b_manifest),
        "stage_b_model_fingerprint": stage_b.fingerprint(),
        "stage_b_refit_count": 0,
        "prospective_observations": 0,
        "n_compact_signals": int(np.count_nonzero(cross["n_compact"])),
    }


def protected_hashes(stock_strategy: Path) -> dict[str, str]:
    result = ledger_hashes(stock_strategy)
    for module in (
        "cross_sectional_alpha_ranking_v01",
        "winner_coverage_taxonomy_v01",
        "multi_setup_study_v01",
    ):
        for path in sorted((stock_strategy / module).iterdir()):
            if path.is_file() and path.suffix.lower() in {".csv", ".json", ".py"}:
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


__all__ = ["array_digest", "ledger_hashes", "load_reused_inputs", "protected_hashes"]
