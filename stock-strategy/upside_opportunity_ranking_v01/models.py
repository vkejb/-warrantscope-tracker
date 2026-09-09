from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

import numpy as np

from cross_sectional_alpha_ranking_v01.preprocessing import iter_date_slices, spearman

from .config import ALPHA_CANDIDATES


@dataclass(frozen=True, slots=True)
class UpsideModel:
    name: str
    fit_period: str
    feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float
    regularization_alpha: float | None
    training_observations: int

    def predict(self, transformed: np.ndarray) -> np.ndarray:
        return (np.asarray(transformed, dtype=np.float64) - 0.5) @ np.asarray(
            self.coefficients
        ) + self.intercept

    def payload(self) -> dict:
        return {
            "name": self.name,
            "fit_period": self.fit_period,
            "feature_names": list(self.feature_names),
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


def fit_ridge_mfe(
    transformed: np.ndarray,
    target: np.ndarray,
    mask: np.ndarray,
    alpha: float,
    feature_names: tuple[str, ...],
    fit_period: str,
) -> UpsideModel:
    if alpha not in ALPHA_CANDIDATES:
        raise ValueError("alpha outside preregistered candidate set")
    indices = np.flatnonzero(mask & np.isfinite(target))
    if len(indices) <= transformed.shape[1]:
        raise ValueError("insufficient Stage A training observations")
    x = np.asarray(transformed[indices], dtype=np.float64) - 0.5
    y = np.asarray(target[indices], dtype=np.float64)
    x_mean = np.mean(x, axis=0)
    y_mean = float(np.mean(y))
    centered = x - x_mean
    coefficients = np.linalg.solve(
        centered.T @ centered + alpha * np.eye(x.shape[1]),
        centered.T @ (y - y_mean),
    )
    intercept = y_mean - float(x_mean @ coefficients)
    return UpsideModel(
        "LINEAR_RIDGE_MFE10",
        fit_period,
        feature_names,
        tuple(float(value) for value in coefficients),
        intercept,
        alpha,
        len(indices),
    )


def fit_ic_weighted_mfe(
    transformed: np.ndarray,
    target: np.ndarray,
    signal_dates: np.ndarray,
    mask: np.ndarray,
    feature_names: tuple[str, ...],
    fit_period: str,
) -> tuple[UpsideModel, dict]:
    daily = []
    for _day, region in iter_date_slices(signal_dates):
        local = mask[region] & np.isfinite(target[region])
        if np.count_nonzero(local) < 3:
            continue
        row = []
        for column in range(transformed.shape[1]):
            value = spearman(transformed[region][local, column], target[region][local])
            row.append(np.nan if value is None else value)
        daily.append(row)
    matrix = np.asarray(daily, dtype=float)
    mean_ic = np.nanmean(matrix, axis=0)
    mean_ic = np.where(np.isfinite(mean_ic), mean_ic, 0.0)
    denominator = float(np.sum(np.abs(mean_ic)))
    if denominator <= 1e-15:
        raise RuntimeError("all discovery MFE IC weights are zero")
    weights = mean_ic / denominator
    model = UpsideModel(
        "IC_WEIGHTED_MFE_RANK",
        fit_period,
        feature_names,
        tuple(float(value) for value in weights),
        0.0,
        None,
        int(np.count_nonzero(mask & np.isfinite(target))),
    )
    return model, {
        "daily_ic_observations": len(matrix),
        "feature_mean_daily_ic": dict(zip(feature_names, map(float, mean_ic))),
        "weight_normalization": "L1_ABSOLUTE_SUM_EQUALS_ONE",
    }
