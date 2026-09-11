from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

import numpy as np

from conditional_path_quality_ranking_v01.models import sigmoid, stage_a_rank_percentile
from conditional_path_quality_ranking_v01.ranking import conditional_ranks
from conditional_path_quality_ranking_v01.config import CONDITIONAL_FEATURE_NAMES

from .config import C_CANDIDATES, CHIP_FEATURES


@dataclass(frozen=True, slots=True)
class ChipModel:
    name: str
    fit_period: str
    feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float
    regularization_c: float
    training_observations: int
    iterations: int

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        return sigmoid(np.asarray(features) @ np.asarray(self.coefficients) + self.intercept)

    def payload(self) -> dict:
        return {
            "name": self.name, "fit_period": self.fit_period,
            "feature_names": list(self.feature_names),
            "coefficients": list(self.coefficients), "intercept": self.intercept,
            "regularization_c": self.regularization_c,
            "training_observations": self.training_observations,
            "iterations": self.iterations,
        }

    def fingerprint(self) -> str:
        raw = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return hashlib.sha256(raw).hexdigest()


def frozen_ohlcv_feature_matrix(arrays: dict[str, np.ndarray], frozen_model) -> np.ndarray:
    rank_pct = stage_a_rank_percentile(arrays["stage_a_ranks"], arrays["meta"]["signal_date"])
    matrix = np.column_stack((
        arrays["transformed_features"] - 0.5,
        (arrays["stage_a_scores"] - frozen_model.score_mean) / frozen_model.score_scale,
        rank_pct - 0.5,
    ))
    if matrix.shape[1] != len(CONDITIONAL_FEATURE_NAMES):
        raise RuntimeError("frozen conditional feature width drifted")
    reproduced = frozen_model.predict_proba(matrix)
    published = arrays["ohlcv_conditional_probabilities"]
    # The immutable observation store serializes inputs separately from the
    # published probability vector.  Require sub-2e-9 numeric agreement and,
    # more importantly for this ranking study, exact Stage A Top30 ordering.
    if not np.allclose(reproduced, published, rtol=0.0, atol=2e-9):
        raise RuntimeError("frozen OHLCV conditional probabilities do not reproduce")
    pool = arrays["stage_a_ranks"] <= 30
    reproduced_rank = conditional_ranks(
        reproduced, pool, arrays["meta"]["signal_date"], arrays["meta"]["stock_code"]
    )[0]["rank"]
    published_rank = conditional_ranks(
        published, pool, arrays["meta"]["signal_date"], arrays["meta"]["stock_code"]
    )[0]["rank"]
    if not np.array_equal(reproduced_rank[pool], published_rank[pool]):
        raise RuntimeError("frozen OHLCV conditional ranking does not reproduce")
    return matrix


def fit_chip_logistic(
    features: np.ndarray,
    target: np.ndarray,
    mask: np.ndarray,
    regularization_c: float,
    fit_period: str,
    feature_names: tuple[str, ...],
    *,
    max_iterations: int = 100,
    tolerance: float = 1e-10,
) -> ChipModel:
    if regularization_c not in C_CANDIDATES:
        raise ValueError("C outside preregistered set")
    indices = np.flatnonzero(mask & np.isfinite(target))
    x = np.asarray(features[indices], dtype=np.float64)
    y = np.asarray(target[indices], dtype=np.float64)
    if len(indices) <= x.shape[1] or not np.all(np.isin(y, (0.0, 1.0))):
        raise ValueError("invalid chip training sample")
    coefficients = np.zeros(x.shape[1], dtype=np.float64)
    rate = float(np.clip(np.mean(y), 1e-8, 1 - 1e-8))
    intercept = float(np.log(rate / (1 - rate)))
    identity = np.eye(x.shape[1])
    penalty = 1.0 / regularization_c
    for iteration in range(1, max_iterations + 1):
        probability = sigmoid(x @ coefficients + intercept)
        weight = np.maximum(probability * (1 - probability), 1e-8)
        residual = probability - y
        gradient = np.r_[(x.T @ residual) / len(x) + penalty * coefficients, np.mean(residual)]
        hessian = np.empty((x.shape[1] + 1, x.shape[1] + 1))
        hessian[:-1, :-1] = (x.T @ (weight[:, None] * x)) / len(x) + penalty * identity
        cross = (x.T @ weight) / len(x)
        hessian[:-1, -1] = cross
        hessian[-1, :-1] = cross
        hessian[-1, -1] = np.mean(weight)
        step = np.linalg.solve(hessian + 1e-12 * np.eye(len(hessian)), gradient)
        coefficients -= step[:-1]
        intercept -= float(step[-1])
        if float(np.max(np.abs(step))) < tolerance:
            break
    return ChipModel(
        "LOGISTIC_RIDGE_PATH_SUCCESS_CHIP", fit_period, feature_names,
        tuple(float(value) for value in coefficients), intercept,
        regularization_c, len(indices), iteration,
    )


ALL_MODEL_FEATURES = tuple(CONDITIONAL_FEATURE_NAMES) + tuple(CHIP_FEATURES)

__all__ = ["ChipModel", "fit_chip_logistic", "frozen_ohlcv_feature_matrix", "ALL_MODEL_FEATURES"]
