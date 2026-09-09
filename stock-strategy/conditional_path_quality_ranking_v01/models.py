from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

import numpy as np

from cross_sectional_alpha_ranking_v01.preprocessing import iter_date_slices, spearman

from .config import C_CANDIDATES, CONDITIONAL_FEATURE_NAMES


def sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(values, dtype=np.float64), -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-clipped))


@dataclass(frozen=True, slots=True)
class ConditionalModel:
    name: str
    fit_period: str
    feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float
    regularization_c: float | None
    training_observations: int
    score_mean: float
    score_scale: float
    iterations: int

    def decision_function(self, features: np.ndarray) -> np.ndarray:
        return np.asarray(features, dtype=np.float64) @ np.asarray(
            self.coefficients, dtype=np.float64
        ) + self.intercept

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        return sigmoid(self.decision_function(features))

    def payload(self) -> dict:
        return {
            "name": self.name,
            "fit_period": self.fit_period,
            "feature_names": list(self.feature_names),
            "coefficients": list(self.coefficients),
            "intercept": self.intercept,
            "regularization_c": self.regularization_c,
            "training_observations": self.training_observations,
            "score_mean": self.score_mean,
            "score_scale": self.score_scale,
            "iterations": self.iterations,
        }

    def fingerprint(self) -> str:
        raw = json.dumps(
            self.payload(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def stage_a_rank_percentile(
    ranks: np.ndarray, signal_dates: np.ndarray
) -> np.ndarray:
    output = np.zeros(len(ranks), dtype=np.float64)
    for _day, region in iter_date_slices(signal_dates):
        count = region.stop - region.start
        output[region] = 1.0 if count == 1 else 1.0 - (ranks[region] - 1) / (count - 1)
    return output


def build_conditional_features(
    transformed_features: np.ndarray,
    stage_a_scores: np.ndarray,
    stage_a_ranks: np.ndarray,
    signal_dates: np.ndarray,
    fit_mask: np.ndarray,
) -> tuple[np.ndarray, dict]:
    fit_scores = np.asarray(stage_a_scores[fit_mask], dtype=np.float64)
    if len(fit_scores) < 2:
        raise ValueError("insufficient rows to fit Stage A score scaling")
    score_mean = float(np.mean(fit_scores))
    score_scale = float(np.std(fit_scores))
    if score_scale <= 1e-15:
        raise ValueError("Stage A score scale is zero")
    rank_pct = stage_a_rank_percentile(stage_a_ranks, signal_dates)
    matrix = np.column_stack(
        (
            np.asarray(transformed_features, dtype=np.float64) - 0.5,
            (np.asarray(stage_a_scores, dtype=np.float64) - score_mean) / score_scale,
            rank_pct - 0.5,
        )
    )
    if matrix.shape[1] != len(CONDITIONAL_FEATURE_NAMES):
        raise RuntimeError("conditional feature width drifted")
    if not np.all(np.isfinite(matrix)):
        raise RuntimeError("conditional transformed features contain non-finite values")
    return matrix, {
        "base_transform": "PUBLISHED_SAME_DAY_PERCENTILE_MINUS_0_5",
        "stage_a_score_transform": "TRAINING_MASK_MEAN_STD_ZSCORE",
        "stage_a_score_mean": score_mean,
        "stage_a_score_scale": score_scale,
        "stage_a_rank_percentile": "PUBLISHED_FULL_UNIVERSE_SAME_DAY_RANK_PERCENTILE_MINUS_0_5",
        "fit_observations": int(np.count_nonzero(fit_mask)),
    }


def fit_logistic_ridge(
    features: np.ndarray,
    target: np.ndarray,
    mask: np.ndarray,
    regularization_c: float,
    fit_period: str,
    preprocessing: dict,
    *,
    max_iterations: int = 100,
    tolerance: float = 1e-10,
) -> ConditionalModel:
    if regularization_c not in C_CANDIDATES:
        raise ValueError("C outside preregistered candidate set")
    indices = np.flatnonzero(mask & np.isfinite(target))
    if len(indices) <= features.shape[1]:
        raise ValueError("insufficient conditional training observations")
    x = np.asarray(features[indices], dtype=np.float64)
    y = np.asarray(target[indices], dtype=np.float64)
    if not np.all(np.isin(y, (0.0, 1.0))):
        raise ValueError("conditional target must be binary")
    coefficients = np.zeros(x.shape[1], dtype=np.float64)
    event_rate = float(np.clip(np.mean(y), 1e-8, 1.0 - 1e-8))
    intercept = float(np.log(event_rate / (1.0 - event_rate)))
    penalty = 1.0 / regularization_c
    identity = np.eye(x.shape[1], dtype=np.float64)
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        probability = sigmoid(x @ coefficients + intercept)
        weight = np.maximum(probability * (1.0 - probability), 1e-8)
        residual = probability - y
        gradient = np.r_[
            (x.T @ residual) / len(x) + penalty * coefficients,
            float(np.mean(residual)),
        ]
        hessian = np.empty((x.shape[1] + 1, x.shape[1] + 1), dtype=np.float64)
        hessian[:-1, :-1] = (x.T @ (weight[:, None] * x)) / len(x) + penalty * identity
        cross = (x.T @ weight) / len(x)
        hessian[:-1, -1] = cross
        hessian[-1, :-1] = cross
        hessian[-1, -1] = float(np.mean(weight))
        step = np.linalg.solve(hessian + 1e-12 * np.eye(len(hessian)), gradient)
        coefficients -= step[:-1]
        intercept -= float(step[-1])
        if float(np.max(np.abs(step))) < tolerance:
            break
    return ConditionalModel(
        "LOGISTIC_RIDGE_PATH_SUCCESS", fit_period, CONDITIONAL_FEATURE_NAMES,
        tuple(float(value) for value in coefficients), intercept, regularization_c,
        len(indices), preprocessing["stage_a_score_mean"],
        preprocessing["stage_a_score_scale"], iterations,
    )


def fit_ic_weighted_path(
    features: np.ndarray,
    target: np.ndarray,
    signal_dates: np.ndarray,
    mask: np.ndarray,
    fit_period: str,
    preprocessing: dict,
) -> tuple[ConditionalModel, dict]:
    daily: list[list[float]] = []
    for _day, region in iter_date_slices(signal_dates):
        local = mask[region] & np.isfinite(target[region])
        if np.count_nonzero(local) < 3 or len(np.unique(target[region][local])) < 2:
            continue
        daily.append([
            np.nan if (value := spearman(features[region][local, column], target[region][local])) is None else value
            for column in range(features.shape[1])
        ])
    matrix = np.asarray(daily, dtype=np.float64)
    mean_ic = np.nanmean(matrix, axis=0)
    mean_ic = np.where(np.isfinite(mean_ic), mean_ic, 0.0)
    denominator = float(np.sum(np.abs(mean_ic)))
    if denominator <= 1e-15:
        raise RuntimeError("all discovery conditional IC weights are zero")
    weights = mean_ic / denominator
    model = ConditionalModel(
        "IC_WEIGHTED_PATH_SCORE", fit_period, CONDITIONAL_FEATURE_NAMES,
        tuple(float(value) for value in weights), 0.0, None,
        int(np.count_nonzero(mask & np.isfinite(target))),
        preprocessing["stage_a_score_mean"], preprocessing["stage_a_score_scale"], 0,
    )
    return model, {
        "daily_ic_observations": len(matrix),
        "feature_mean_daily_path_ic": dict(zip(CONDITIONAL_FEATURE_NAMES, map(float, mean_ic))),
        "weight_normalization": "L1_ABSOLUTE_SUM_EQUALS_ONE",
    }


def coefficient_cosine(left: ConditionalModel, right: ConditionalModel) -> float | None:
    a = np.asarray(left.coefficients, dtype=np.float64)
    b = np.asarray(right.coefficients, dtype=np.float64)
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b / denominator) if denominator > 1e-15 else None
