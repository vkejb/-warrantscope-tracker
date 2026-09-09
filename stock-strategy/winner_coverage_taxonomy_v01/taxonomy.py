from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

import numpy as np

from .config import CFG, CLUSTER_FEATURES, FEATURE_NAMES, Config


FEATURE_INDEX = {name: index for index, name in enumerate(FEATURE_NAMES)}
CLUSTER_COLUMNS = np.asarray([FEATURE_INDEX[name] for name in CLUSTER_FEATURES])


@dataclass(frozen=True, slots=True)
class FrozenTaxonomy:
    fit_period: str
    chosen_k: int
    feature_names: tuple[str, ...]
    lower: tuple[float, ...]
    upper: tuple[float, ...]
    center: tuple[float, ...]
    scale: tuple[float, ...]
    centroids: tuple[tuple[float, ...], ...]
    radii: tuple[float, ...]
    cluster_names: tuple[str, ...]
    k_selection: tuple[dict, ...]
    minimum_centroid_separation: float
    minimum_cluster_share: float
    stable_taxonomy: bool

    def payload(self) -> dict:
        return {
            "fit_period": self.fit_period,
            "chosen_k": self.chosen_k,
            "feature_names": list(self.feature_names),
            "winsor_lower": list(self.lower),
            "winsor_upper": list(self.upper),
            "robust_center": list(self.center),
            "robust_scale": list(self.scale),
            "centroids": [list(row) for row in self.centroids],
            "assignment_radii": list(self.radii),
            "cluster_names": list(self.cluster_names),
            "k_selection": list(self.k_selection),
            "minimum_centroid_separation": self.minimum_centroid_separation,
            "minimum_cluster_share": self.minimum_cluster_share,
            "stable_taxonomy": self.stable_taxonomy,
        }

    def fingerprint(self) -> str:
        raw = json.dumps(
            self.payload(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def _transform(
    matrix: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    center: np.ndarray,
    scale: np.ndarray,
) -> np.ndarray:
    return (np.clip(matrix, lower, upper) - center) / scale


def _init_centroids(matrix: np.ndarray, k: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed + k * 1009)
    chosen = [int(rng.integers(0, len(matrix)))]
    distances = np.sum((matrix - matrix[chosen[0]]) ** 2, axis=1)
    while len(chosen) < k:
        total = float(distances.sum())
        if total <= 0:
            candidate = next(index for index in range(len(matrix)) if index not in chosen)
        else:
            candidate = int(rng.choice(len(matrix), p=distances / total))
        if candidate in chosen:
            candidate = int(np.argmax(distances))
        chosen.append(candidate)
        distances = np.minimum(
            distances,
            np.sum((matrix - matrix[candidate]) ** 2, axis=1),
        )
    return matrix[np.asarray(chosen)].copy()


def _kmeans(matrix: np.ndarray, k: int, cfg: Config) -> tuple[np.ndarray, np.ndarray, float]:
    centroids = _init_centroids(matrix, k, cfg.random_seed)
    labels = np.zeros(len(matrix), dtype=np.int16)
    for _ in range(cfg.cluster_max_iterations):
        distances = np.sum((matrix[:, None, :] - centroids[None, :, :]) ** 2, axis=2)
        labels = np.argmin(distances, axis=1).astype(np.int16)
        updated = centroids.copy()
        nearest = distances[np.arange(len(matrix)), labels]
        for cluster in range(k):
            members = matrix[labels == cluster]
            if len(members):
                updated[cluster] = members.mean(axis=0)
            else:
                updated[cluster] = matrix[int(np.argmax(nearest))]
        shift = float(np.max(np.linalg.norm(updated - centroids, axis=1)))
        centroids = updated
        if shift <= cfg.cluster_tolerance:
            break
    distances = np.sum((matrix[:, None, :] - centroids[None, :, :]) ** 2, axis=2)
    labels = np.argmin(distances, axis=1).astype(np.int16)
    within = float(
        sum(np.sum((matrix[labels == cluster] - centroids[cluster]) ** 2) for cluster in range(k))
    )
    overall = matrix.mean(axis=0)
    between = float(
        sum(
            np.count_nonzero(labels == cluster)
            * np.sum((centroids[cluster] - overall) ** 2)
            for cluster in range(k)
        )
    )
    ch = (
        between / (k - 1) / (within / (len(matrix) - k))
        if k > 1 and len(matrix) > k and within > 0
        else 0.0
    )
    return labels, centroids, ch


def _cluster_base_name(centroid: np.ndarray) -> str:
    values = dict(zip(CLUSTER_FEATURES, centroid))
    if (
        values["return_20"] > 0.35
        and values["distance_to_prior20_close_high"] > -0.20
    ):
        return "DIRECT_CONTINUATION_STATE"
    if (
        values["recent_breakout_flag"] > 0.20
        and values["range_compression20"] < 0.0
    ):
        return "BREAKOUT_SQUEEZE_STATE"
    if values["return_20"] < -0.35 and values["close_vs_ma20"] > -0.10:
        return "REVERSAL_RECOVERY_STATE"
    if values["ma20_slope"] > 0.15 and values["recent_retest_flag"] > 0.10:
        return "TREND_RETEST_STATE"
    if values["volatility20"] > 0.50:
        return "HIGH_VOLATILITY_REPRICING_STATE"
    return "MIXED_MARKET_STATE"


def fit_discovery_taxonomy(
    feature_matrix: np.ndarray,
    signal_dates: np.ndarray,
    discovery_unexplained_winner_mask: np.ndarray,
    cfg: Config = CFG,
) -> FrozenTaxonomy:
    """Fit once on discovery unexplained Winners; later dates are rejected."""

    selected_dates = signal_dates[discovery_unexplained_winner_mask]
    if not len(selected_dates):
        raise RuntimeError("no discovery unexplained Winners")
    if int(selected_dates.max()) > int(cfg.discovery_end):
        raise RuntimeError("taxonomy fitting received post-discovery observations")
    raw = feature_matrix[discovery_unexplained_winner_mask][:, CLUSTER_COLUMNS]
    finite = np.all(np.isfinite(raw), axis=1)
    raw = raw[finite]
    if len(raw) < max(cfg.cluster_k_candidates) * 20:
        raise RuntimeError("too few complete discovery unexplained Winners for taxonomy")
    lower = np.quantile(raw, cfg.winsor_lower_quantile, axis=0, method="linear")
    upper = np.quantile(raw, cfg.winsor_upper_quantile, axis=0, method="linear")
    clipped = np.clip(raw, lower, upper)
    center = np.median(clipped, axis=0)
    q25, q75 = np.quantile(clipped, (0.25, 0.75), axis=0, method="linear")
    scale = q75 - q25
    scale[scale <= 1e-12] = 1.0
    matrix = _transform(raw, lower, upper, center, scale)
    trials = []
    fits: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for k in cfg.cluster_k_candidates:
        labels, centroids, score = _kmeans(matrix, k, cfg)
        shares = [float(np.mean(labels == cluster)) for cluster in range(k)]
        trials.append(
            {
                "k": k,
                "calinski_harabasz": score,
                "minimum_cluster_share": min(shares),
                "criterion": "MAX_CALINSKI_HARABASZ_UNSUPERVISED_DISCOVERY_ONLY",
            }
        )
        fits[k] = (labels, centroids)
    chosen_k = max(trials, key=lambda row: (row["calinski_harabasz"], -row["k"]))["k"]
    labels, centroids = fits[chosen_k]
    # Give stable IDs using increasing discovery return20 state, then return5.
    return20 = CLUSTER_FEATURES.index("return_20")
    return5 = CLUSTER_FEATURES.index("return_5")
    order = sorted(range(chosen_k), key=lambda i: (centroids[i, return20], centroids[i, return5]))
    centroids = centroids[order]
    remap = {old: new for new, old in enumerate(order)}
    labels = np.asarray([remap[int(value)] for value in labels], dtype=np.int16)
    radii = []
    shares = []
    for cluster in range(chosen_k):
        distances = np.linalg.norm(matrix[labels == cluster] - centroids[cluster], axis=1)
        radii.append(float(np.quantile(distances, cfg.cluster_radius_quantile, method="linear")))
        shares.append(float(np.mean(labels == cluster)))
    pairwise = [
        float(np.linalg.norm(centroids[left] - centroids[right]))
        for left in range(chosen_k)
        for right in range(left + 1, chosen_k)
    ]
    minimum_separation = min(pairwise) if pairwise else 0.0
    base_names = [_cluster_base_name(row) for row in centroids]
    duplicates: dict[str, int] = {}
    names = []
    for cluster, base in enumerate(base_names, 1):
        duplicates[base] = duplicates.get(base, 0) + 1
        suffix = f"_{duplicates[base]}" if base_names.count(base) > 1 else ""
        names.append(f"CANDIDATE_{cluster}_{base}{suffix}")
    stable = bool(
        min(shares) >= cfg.minimum_cluster_share
        and minimum_separation >= cfg.minimum_centroid_separation
    )
    return FrozenTaxonomy(
        fit_period="HISTORICAL_DISCOVERY_2020_2022_UNEXPLAINED_WINNERS_ONLY",
        chosen_k=chosen_k,
        feature_names=CLUSTER_FEATURES,
        lower=tuple(float(value) for value in lower),
        upper=tuple(float(value) for value in upper),
        center=tuple(float(value) for value in center),
        scale=tuple(float(value) for value in scale),
        centroids=tuple(tuple(float(value) for value in row) for row in centroids),
        radii=tuple(radii),
        cluster_names=tuple(names),
        k_selection=tuple(trials),
        minimum_centroid_separation=minimum_separation,
        minimum_cluster_share=min(shares),
        stable_taxonomy=stable,
    )


def assign_frozen_taxonomy(
    feature_matrix: np.ndarray,
    model: FrozenTaxonomy,
) -> np.ndarray:
    """Assign with frozen discovery preprocessing, centroids and radii only."""

    raw = feature_matrix[:, CLUSTER_COLUMNS]
    result = np.zeros(len(raw), dtype=np.uint8)
    finite = np.all(np.isfinite(raw), axis=1)
    if not np.any(finite):
        return result
    matrix = _transform(
        raw[finite],
        np.asarray(model.lower),
        np.asarray(model.upper),
        np.asarray(model.center),
        np.asarray(model.scale),
    )
    centroids = np.asarray(model.centroids)
    distances = np.linalg.norm(matrix[:, None, :] - centroids[None, :, :], axis=2)
    nearest = np.argmin(distances, axis=1)
    nearest_distance = distances[np.arange(len(matrix)), nearest]
    accepted = nearest_distance <= np.asarray(model.radii)[nearest]
    assigned = np.where(accepted, nearest + 1, 0).astype(np.uint8)
    result[np.flatnonzero(finite)] = assigned
    return result


def centroid_characteristics(model: FrozenTaxonomy, top_n: int = 4) -> list[dict]:
    rows = []
    for cluster, (name, centroid) in enumerate(
        zip(model.cluster_names, model.centroids), 1
    ):
        ranked = sorted(
            zip(model.feature_names, centroid),
            key=lambda item: (-abs(item[1]), item[0]),
        )[:top_n]
        rows.append(
            {
                "candidate_family": name,
                "cluster_number": cluster,
                "top_standardized_characteristics": ";".join(
                    f"{feature}={value:+.3f}" for feature, value in ranked
                ),
                "assignment_radius_discovery_p90": model.radii[cluster - 1],
            }
        )
    return rows
