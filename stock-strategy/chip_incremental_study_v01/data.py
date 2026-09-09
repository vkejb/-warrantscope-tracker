from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from conditional_path_quality_ranking_v01.data import load_reused_inputs, protected_hashes as upstream_protected_hashes
from conditional_path_quality_ranking_v01.models import ConditionalModel
from surge_event_study_v01.data import sha256_file

from .config import CFG


def load_frozen_research(stock_strategy: Path) -> tuple[dict[str, np.ndarray], ConditionalModel, dict]:
    arrays, _stage_a_model, stage_a_audit = load_reused_inputs(
        stock_strategy / "winner_coverage_taxonomy_v01/runtime/observation_store.npz",
        stock_strategy / "cross_sectional_alpha_ranking_v01/runtime/ranking_store.npz",
        stock_strategy / "upside_opportunity_ranking_v01/runtime/ranking_store.npz",
        stock_strategy / "upside_opportunity_ranking_v01/stage_a_model_spec.json",
        stock_strategy / "upside_opportunity_ranking_v01/run_manifest.json",
    )
    model_spec_path = stock_strategy / "conditional_path_quality_ranking_v01/conditional_model_spec.json"
    manifest_path = stock_strategy / "conditional_path_quality_ranking_v01/run_manifest.json"
    store_path = stock_strategy / "conditional_path_quality_ranking_v01/runtime/conditional_store.npz"
    expected = (
        (model_spec_path, CFG.expected_conditional_model_spec_sha256),
        (manifest_path, CFG.expected_conditional_manifest_sha256),
        (store_path, CFG.expected_conditional_store_sha256),
    )
    for path, digest in expected:
        if sha256_file(path) != digest:
            raise RuntimeError(f"frozen conditional artifact drifted: {path}")
    spec = json.loads(model_spec_path.read_text(encoding="utf-8"))
    payload = spec["primary_model"]
    model = ConditionalModel(
        payload["name"], payload["fit_period"], tuple(payload["feature_names"]),
        tuple(payload["coefficients"]), payload["intercept"],
        payload["regularization_c"], payload["training_observations"],
        payload["score_mean"], payload["score_scale"], payload["iterations"],
    )
    with np.load(store_path, allow_pickle=False) as frozen:
        if not np.array_equal(frozen["meta"], arrays["meta"]):
            raise RuntimeError("frozen OHLCV conditional keys differ")
        arrays["ohlcv_conditional_probabilities"] = frozen["conditional_probabilities"].copy()
        arrays["ohlcv_conditional_ranks"] = frozen["conditional_ranks"].copy()
    return arrays, model, {
        **stage_a_audit,
        "conditional_model_spec_sha256": sha256_file(model_spec_path),
        "conditional_manifest_sha256": sha256_file(manifest_path),
        "conditional_store_sha256": sha256_file(store_path),
        "frozen_ohlcv_model_fingerprint": model.fingerprint(),
        "frozen_ohlcv_refit_count": 0,
    }


def protected_hashes(stock_strategy: Path) -> dict[str, str]:
    result = upstream_protected_hashes(stock_strategy)
    root = stock_strategy / "conditional_path_quality_ranking_v01"
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix.lower() in {".py", ".csv", ".json"}:
            result[str(path.relative_to(stock_strategy))] = sha256_file(path)
    return result


def array_digest(arrays: dict[str, np.ndarray]) -> str:
    digest = hashlib.sha256()
    for name in sorted(arrays):
        value = arrays[name]
        if isinstance(value, np.ndarray):
            digest.update(name.encode())
            digest.update(np.ascontiguousarray(value).tobytes())
    return digest.hexdigest()


__all__ = ["load_frozen_research", "protected_hashes", "array_digest"]
