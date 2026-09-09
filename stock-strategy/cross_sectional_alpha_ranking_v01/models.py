from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

import numpy as np

from .config import FEATURE_NAMES
from .preprocessing import iter_date_slices, spearman


@dataclass(frozen=True, slots=True)
class LinearModel:
    name: str
    fit_period: str
    coefficients: tuple[float, ...]
    intercept: float
    regularization_alpha: float | None
    training_observations: int

    def predict(self, transformed_features: np.ndarray) -> np.ndarray:
        matrix = np.asarray(transformed_features, dtype=np.float64) - 0.5
        return matrix @ np.asarray(self.coefficients) + self.intercept

    def payload(self) -> dict:
        return {
            "name": self.name,
            "fit_period": self.fit_period,
            "feature_names": list(FEATURE_NAMES),
            "coefficients": list(self.coefficients),
            "intercept": self.intercept,
            "regularization_alpha": self.regularization_alpha,
            "training_observations": self.training_observations,
        }

    def fingerprint(self) -> str:
        raw = json.dumps(
            self.payload(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def fit_ridge(
    transformed_features: np.ndarray,
    target: np.ndarray,
    mask: np.ndarray,
    alpha: float,
    fit_period: str,
) -> LinearModel:
    if alpha not in {0.1, 1.0, 10.0, 100.0}:
        raise ValueError("alpha is outside the preregistered candidate set")
    indices = np.flatnonzero(mask & np.isfinite(target))
    if len(indices) <= transformed_features.shape[1]:
        raise ValueError("insufficient Ridge training observations")
    features = np.asarray(transformed_features[indices], dtype=np.float64) - 0.5
    response = np.asarray(target[indices], dtype=np.float64)
    x_mean = np.mean(features, axis=0)
    y_mean = float(np.mean(response))
    centered = features - x_mean
    centered_y = response - y_mean
    gram = centered.T @ centered
    rhs = centered.T @ centered_y
    coefficients = np.linalg.solve(
        gram + alpha * np.eye(gram.shape[0], dtype=float), rhs
    )
    intercept = y_mean - float(x_mean @ coefficients)
    return LinearModel(
        name="LINEAR_RIDGE_NET_RETURN",
        fit_period=fit_period,
        coefficients=tuple(float(value) for value in coefficients),
        intercept=intercept,
        regularization_alpha=alpha,
        training_observations=len(indices),
    )


def mean_daily_feature_ic(
    transformed_features: np.ndarray,
    target: np.ndarray,
    signal_dates: np.ndarray,
    mask: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    daily: list[list[float]] = []
    for _day, region in iter_date_slices(signal_dates):
        local_mask = mask[region] & np.isfinite(target[region])
        if np.count_nonzero(local_mask) < 3:
            continue
        block = transformed_features[region][local_mask]
        response = target[region][local_mask]
        row = []
        for column in range(block.shape[1]):
            value = spearman(block[:, column], response)
            row.append(np.nan if value is None else value)
        daily.append(row)
    matrix = np.asarray(daily, dtype=float)
    return np.nanmean(matrix, axis=0), matrix


def fit_ic_weighted(
    transformed_features: np.ndarray,
    target: np.ndarray,
    signal_dates: np.ndarray,
    mask: np.ndarray,
    fit_period: str,
) -> tuple[LinearModel, dict]:
    mean_ic, daily = mean_daily_feature_ic(
        transformed_features, target, signal_dates, mask
    )
    mean_ic = np.where(np.isfinite(mean_ic), mean_ic, 0.0)
    denominator = float(np.sum(np.abs(mean_ic)))
    if denominator <= 1e-15:
        raise RuntimeError("all discovery feature IC weights are zero")
    weights = mean_ic / denominator
    model = LinearModel(
        name="IC_WEIGHTED_LINEAR_RANK",
        fit_period=fit_period,
        coefficients=tuple(float(value) for value in weights),
        intercept=0.0,
        regularization_alpha=None,
        training_observations=int(np.count_nonzero(mask & np.isfinite(target))),
    )
    return model, {
        "daily_ic_observations": len(daily),
        "feature_mean_daily_ic": {
            feature: float(value) for feature, value in zip(FEATURE_NAMES, mean_ic)
        },
        "weight_normalization": "L1_ABSOLUTE_SUM_EQUALS_ONE",
    }


def coefficient_comparison(left: LinearModel, right: LinearModel) -> dict:
    first = np.asarray(left.coefficients)
    second = np.asarray(right.coefficients)
    nonzero = (np.abs(first) > 1e-15) & (np.abs(second) > 1e-15)
    denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
    return {
        "left_model": left.name,
        "left_fit_period": left.fit_period,
        "right_fit_period": right.fit_period,
        "sign_agreement": (
            float(np.mean(np.sign(first[nonzero]) == np.sign(second[nonzero])))
            if np.any(nonzero)
            else None
        ),
        "cosine_similarity": (
            float(first @ second / denominator) if denominator > 1e-15 else None
        ),
    }
