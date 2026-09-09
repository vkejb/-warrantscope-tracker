from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import math

import numpy as np

from extension_entry_study_v01.pipeline import OUTCOME_FIELDS

from .config import (
    CFG,
    ALL_FAMILIES,
    FAMILY_BIT,
    FEATURE_NAMES,
    MAJOR_SETUPS,
    PERIODS,
    Config,
)
from .taxonomy import FrozenTaxonomy, centroid_characteristics


OUTCOME_INDEX = {name: index for index, name in enumerate(OUTCOME_FIELDS)}
FEATURE_INDEX = {name: index for index, name in enumerate(FEATURE_NAMES)}


def _date_mask(meta: np.ndarray, start: str, end: str) -> np.ndarray:
    return (meta["signal_date"] >= int(start)) & (meta["signal_date"] <= int(end))


def _family_mask(arrays: dict[str, np.ndarray], family: str) -> np.ndarray:
    return (arrays["family_masks"] & FAMILY_BIT[family]) != 0


def _winner_mask(arrays: dict[str, np.ndarray]) -> np.ndarray:
    evaluable = arrays["meta"]["outcome_evaluable"]
    success = arrays["outcomes"][:, OUTCOME_INDEX["primary_success"]] == 1.0
    return evaluable & success


def _major_union(arrays: dict[str, np.ndarray]) -> np.ndarray:
    result = np.zeros(len(arrays["meta"]), dtype=bool)
    for setup in MAJOR_SETUPS:
        result |= _family_mask(arrays, setup)
    return result


def _profit_factor(values: np.ndarray) -> float | None:
    if not len(values):
        return None
    positive = float(values[values > 0].sum())
    negative = float(-values[values < 0].sum())
    return positive / negative if negative > 0 else None


def _finite_mean(values: np.ndarray) -> float | None:
    values = values[np.isfinite(values)]
    return float(np.mean(values)) if len(values) else None


def _remove_top_positive(gross: np.ndarray, fraction: float) -> tuple[np.ndarray, int]:
    keep = np.ones(len(gross), dtype=bool)
    winners = np.flatnonzero(gross > 0)
    remove_count = int(math.ceil(len(winners) * fraction)) if len(winners) else 0
    if remove_count:
        ranked = winners[np.argsort(-gross[winners], kind="stable")]
        keep[ranked[:remove_count]] = False
    return keep, remove_count


def metric_summary(arrays: dict[str, np.ndarray], signal_mask: np.ndarray) -> dict:
    valid = signal_mask & arrays["meta"]["outcome_evaluable"]
    outcome = arrays["outcomes"][valid]
    signals = int(np.count_nonzero(signal_mask))
    evaluable = len(outcome)
    if not evaluable:
        return {
            "total_signals": signals,
            "evaluable_signals": 0,
            "winner_signals": 0,
            "precision_all_signals": 0.0 if signals else None,
            "precision_evaluable_signals": None,
            "gross_mean": None,
            "net_mean": None,
            "median": None,
            "profit_factor": None,
            "day1_return": None,
            "day3_return": None,
            "day5_return": None,
            "day10_return": None,
            "mfe5": None,
            "mae5": None,
            "mfe10": None,
            "mae10": None,
            "mfe_abs_mae": None,
            "plus10_before_minus5_rate": None,
            "plus15_before_minus5_rate": None,
            "top1_removed_gross": None,
            "top1_removed_pf": None,
            "top5_removed_gross": None,
            "top5_removed_pf": None,
            "tail_dependent": None,
        }
    gross = outcome[:, OUTCOME_INDEX["gross_return"]]
    net = outcome[:, OUTCOME_INDEX["net_return"]]
    successes = int(np.sum(outcome[:, OUTCOME_INDEX["primary_success"]] == 1.0))
    mfe = outcome[:, OUTCOME_INDEX["mfe_10d"]]
    mae = outcome[:, OUTCOME_INDEX["mae_10d"]]
    result = {
        "total_signals": signals,
        "evaluable_signals": int(evaluable),
        "winner_signals": successes,
        "precision_all_signals": successes / signals if signals else None,
        "precision_evaluable_signals": successes / evaluable,
        "gross_mean": float(np.mean(gross)),
        "net_mean": float(np.mean(net)),
        "median": float(np.median(gross)),
        "profit_factor": _profit_factor(gross),
        "day1_return": float(np.mean(outcome[:, OUTCOME_INDEX["day1_close_return"]])),
        "day3_return": float(np.mean(outcome[:, OUTCOME_INDEX["day3_close_return"]])),
        "day5_return": float(np.mean(outcome[:, OUTCOME_INDEX["day5_close_return"]])),
        "day10_return": float(np.mean(outcome[:, OUTCOME_INDEX["day10_close_return"]])),
        "mfe5": float(np.mean(outcome[:, OUTCOME_INDEX["mfe_5d"]])),
        "mae5": float(np.mean(outcome[:, OUTCOME_INDEX["mae_5d"]])),
        "mfe10": float(np.mean(mfe)),
        "mae10": float(np.mean(mae)),
        "mfe_abs_mae": (
            float(np.mean(mfe)) / abs(float(np.mean(mae)))
            if abs(float(np.mean(mae))) > 1e-15
            else None
        ),
        "plus10_before_minus5_rate": float(
            np.nanmean(arrays["descriptive_outcomes"][valid, 0])
        ),
        "plus15_before_minus5_rate": float(
            np.nanmean(arrays["descriptive_outcomes"][valid, 1])
        ),
    }
    for label, fraction in (("top1", 0.01), ("top5", 0.05)):
        keep, removed = _remove_top_positive(gross, fraction)
        result[f"{label}_removed_count"] = removed
        result[f"{label}_removed_gross"] = _finite_mean(gross[keep])
        result[f"{label}_removed_pf"] = _profit_factor(gross[keep])
    result["tail_dependent"] = bool(
        result["gross_mean"] > 0
        and (
            result["top1_removed_gross"] is None
            or result["top1_removed_gross"] <= 0
            or result["top1_removed_pf"] is None
            or result["top1_removed_pf"] <= 1.0
        )
    )
    return result


def time_slices(include_overall: bool = True) -> list[tuple[str, str, str, str]]:
    rows = [("PERIOD", label, start, end) for label, start, end in PERIODS]
    rows.extend(
        ("YEAR", str(year), f"{year}0101", f"{year}1231")
        for year in range(2020, 2026)
    )
    if include_overall:
        rows.append(("OVERALL", "2020_2025", "20200101", "20251231"))
    return rows


def mother_sample_rows(arrays: dict[str, np.ndarray]) -> list[dict]:
    winner = _winner_mask(arrays)
    rows = []
    for year in range(2020, 2026):
        mask = _date_mask(arrays["meta"], f"{year}0101", f"{year}1231")
        evaluable = mask & arrays["meta"]["outcome_evaluable"]
        winners = evaluable & winner
        rows.append(
            {
                "year": year,
                "period": next(label for label, start, end in PERIODS if start <= f"{year}0101" <= end),
                "eligible_observations": int(np.count_nonzero(mask)),
                "evaluable_observations": int(np.count_nonzero(evaluable)),
                "winner_count": int(np.count_nonzero(winners)),
                "winner_base_rate": (
                    np.count_nonzero(winners) / np.count_nonzero(evaluable)
                    if np.count_nonzero(evaluable)
                    else None
                ),
            }
        )
    return rows


def winner_base_rate_rows(arrays: dict[str, np.ndarray]) -> list[dict]:
    winner = _winner_mask(arrays)
    rows = []
    for kind, label, start, end in time_slices():
        mask = _date_mask(arrays["meta"], start, end)
        evaluable = mask & arrays["meta"]["outcome_evaluable"]
        count = int(np.count_nonzero(evaluable))
        winners = int(np.count_nonzero(mask & winner))
        rows.append(
            {
                "time_slice_type": kind,
                "time_slice": label,
                "evaluable_observations": count,
                "winner_count": winners,
                "winner_base_rate": winners / count if count else None,
            }
        )
    return rows


def setup_coverage_rows(arrays: dict[str, np.ndarray]) -> list[dict]:
    winner = _winner_mask(arrays)
    rows = []
    for kind, label, start, end in time_slices():
        period = _date_mask(arrays["meta"], start, end)
        all_winners = int(np.count_nonzero(period & winner))
        for family in ALL_FAMILIES:
            selected = period & _family_mask(arrays, family)
            summary = metric_summary(arrays, selected)
            family_winners = int(np.count_nonzero(selected & winner))
            rows.append(
                {
                    "time_slice_type": kind,
                    "time_slice": label,
                    "family": family,
                    "family_type": "FROZEN_SETUP" if family in MAJOR_SETUPS else "DESCRIPTIVE_COHORT",
                    "all_winners_denominator": all_winners,
                    "winner_coverage_recall": family_winners / all_winners if all_winners else None,
                    **summary,
                }
            )
    return rows


def overlap_rows(arrays: dict[str, np.ndarray]) -> list[dict]:
    winner = _winner_mask(arrays)
    rows = []
    for kind, label, start, end in time_slices():
        period_winner = _date_mask(arrays["meta"], start, end) & winner
        denominator = int(np.count_nonzero(period_winner))
        for left in MAJOR_SETUPS:
            left_mask = period_winner & _family_mask(arrays, left)
            for right in MAJOR_SETUPS:
                right_mask = period_winner & _family_mask(arrays, right)
                intersection = int(np.count_nonzero(left_mask & right_mask))
                union = int(np.count_nonzero(left_mask | right_mask))
                rows.append(
                    {
                        "time_slice_type": kind,
                        "time_slice": label,
                        "left_setup": left,
                        "right_setup": right,
                        "left_winners": int(np.count_nonzero(left_mask)),
                        "right_winners": int(np.count_nonzero(right_mask)),
                        "intersection_winners": intersection,
                        "intersection_share_all_winners": intersection / denominator if denominator else None,
                        "winner_jaccard": intersection / union if union else None,
                    }
                )
    return rows


def unique_marginal_rows(arrays: dict[str, np.ndarray]) -> list[dict]:
    winner = _winner_mask(arrays)
    rows = []
    for kind, label, start, end in time_slices():
        period_winner = _date_mask(arrays["meta"], start, end) & winner
        denominator = int(np.count_nonzero(period_winner))
        compact = _family_mask(arrays, "N_COMPACT_RETEST_HYPOTHESIS")
        for family in MAJOR_SETUPS:
            selected = _family_mask(arrays, family)
            others = np.zeros(len(selected), dtype=bool)
            for other in MAJOR_SETUPS:
                if other != family:
                    others |= _family_mask(arrays, other)
            total = int(np.count_nonzero(period_winner & selected))
            unique = int(np.count_nonzero(period_winner & selected & ~others))
            incremental = int(np.count_nonzero(period_winner & selected & ~compact))
            rows.append(
                {
                    "time_slice_type": kind,
                    "time_slice": label,
                    "setup": family,
                    "all_winners_denominator": denominator,
                    "total_winner_coverage_count": total,
                    "total_winner_coverage": total / denominator if denominator else None,
                    "unique_winner_coverage_count": unique,
                    "unique_winner_coverage": unique / denominator if denominator else None,
                    "incremental_winners_vs_n_compact": incremental,
                    "incremental_coverage_vs_n_compact": incremental / denominator if denominator else None,
                    "overlap_winners_with_n_compact": int(
                        np.count_nonzero(period_winner & selected & compact)
                    ),
                    "overlap_winners_with_momentum": int(
                        np.count_nonzero(
                            period_winner
                            & selected
                            & _family_mask(arrays, "MOMENTUM_DIRECTIONAL")
                        )
                    ),
                }
            )
    return rows


def unexplained_winner_rows(arrays: dict[str, np.ndarray]) -> list[dict]:
    winner = _winner_mask(arrays)
    explained = _major_union(arrays)
    rows = []
    for kind, label, start, end in time_slices():
        period_winner = _date_mask(arrays["meta"], start, end) & winner
        unexplained = period_winner & ~explained
        denominator = int(np.count_nonzero(period_winner))
        rows.append(
            {
                "time_slice_type": kind,
                "time_slice": label,
                "winner_count": denominator,
                "explained_winner_count": int(np.count_nonzero(period_winner & explained)),
                "unexplained_winner_count": int(np.count_nonzero(unexplained)),
                "unexplained_winner_share": (
                    np.count_nonzero(unexplained) / denominator if denominator else None
                ),
            }
        )
    return rows


def _frozen_bucket_ids(values: np.ndarray, boundaries: np.ndarray) -> np.ndarray:
    result = np.zeros(len(values), dtype=np.uint8)
    finite = np.isfinite(values)
    result[finite] = np.searchsorted(boundaries, values[finite], side="left") + 1
    return result


def build_same_date_controls(
    arrays: dict[str, np.ndarray],
    target_winner_mask: np.ndarray,
    cfg: Config = CFG,
) -> tuple[np.ndarray, dict]:
    """One deterministic non-Winner control per target, same date/price/volume bucket."""

    meta = arrays["meta"]
    winner = _winner_mask(arrays)
    discovery = _date_mask(meta, cfg.discovery_start, cfg.discovery_end)
    valid = discovery & meta["outcome_evaluable"]
    price = arrays["taxonomy_features"][:, FEATURE_INDEX["signal_close"]]
    volume = np.log1p(arrays["taxonomy_features"][:, FEATURE_INDEX["average_volume_20"]])
    price_edges = np.quantile(
        price[valid], np.arange(1, cfg.control_price_buckets) / cfg.control_price_buckets
    )
    volume_edges = np.quantile(
        volume[valid], np.arange(1, cfg.control_volume_buckets) / cfg.control_volume_buckets
    )
    price_bucket = _frozen_bucket_ids(price, price_edges)
    volume_bucket = _frozen_bucket_ids(volume, volume_edges)
    pools: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for index in np.flatnonzero(valid & ~winner):
        pools[(int(meta["signal_date"][index]), int(price_bucket[index]), int(volume_bucket[index]))].append(int(index))
    selected = []
    used_controls: set[int] = set()
    unmatched = 0
    for index in np.flatnonzero(target_winner_mask & valid):
        key = (
            int(meta["signal_date"][index]),
            int(price_bucket[index]),
            int(volume_bucket[index]),
        )
        pool = pools.get(key, [])
        if not pool:
            unmatched += 1
            continue
        digest = hashlib.sha256(
            f"{meta['signal_date'][index]}|{meta['stock_code'][index]}|CONTROL".encode()
        ).digest()
        start = int.from_bytes(digest[:8], "big") % len(pool)
        available = next(
            (pool[(start + offset) % len(pool)] for offset in range(len(pool))
             if pool[(start + offset) % len(pool)] not in used_controls),
            None,
        )
        if available is None:
            unmatched += 1
            continue
        selected.append(available)
        used_controls.add(available)
    mask = np.zeros(len(meta), dtype=bool)
    mask[np.asarray(selected, dtype=int)] = True
    return mask, {
        "target_winners": int(np.count_nonzero(target_winner_mask & valid)),
        "matched_controls": len(selected),
        "unique_controls": int(np.count_nonzero(mask)),
        "unmatched_targets": unmatched,
        "matching": "SAME_DATE_AND_DISCOVERY_FROZEN_PRICE_VOLUME_QUINTILES",
        "price_boundaries": [float(value) for value in price_edges],
        "log_volume_boundaries": [float(value) for value in volume_edges],
    }


def feature_control_rows(
    arrays: dict[str, np.ndarray],
    taxonomy_labels: np.ndarray,
    model: FrozenTaxonomy,
    cfg: Config = CFG,
) -> tuple[list[dict], dict]:
    winner = _winner_mask(arrays)
    discovery = _date_mask(arrays["meta"], cfg.discovery_start, cfg.discovery_end)
    unexplained = winner & ~_major_union(arrays)
    states: list[tuple[str, np.ndarray]] = [
        ("ALL_WINNERS", discovery & winner),
        ("UNEXPLAINED_WINNERS", discovery & unexplained),
    ]
    for number, name in enumerate(model.cluster_names, 1):
        states.append((name, discovery & unexplained & (taxonomy_labels == number)))
    rows = []
    audits = {}
    for state, target in states:
        controls, audit = build_same_date_controls(arrays, target, cfg)
        audits[state] = audit
        target_indices = np.flatnonzero(target & discovery)
        control_indices = np.flatnonzero(controls)
        for feature_number, feature in enumerate(FEATURE_NAMES):
            left = arrays["taxonomy_features"][target_indices, feature_number]
            right = arrays["taxonomy_features"][control_indices, feature_number]
            left = left[np.isfinite(left)]
            right = right[np.isfinite(right)]
            if not len(left) or not len(right):
                continue
            left_mean = float(np.mean(left))
            right_mean = float(np.mean(right))
            pooled = math.sqrt((float(np.var(left)) + float(np.var(right))) / 2.0)
            superiority = float(
                np.mean(np.searchsorted(np.sort(right), left, side="right") / len(right))
            )
            rows.append(
                {
                    "fit_period": "HISTORICAL_DISCOVERY_2020_2022_ONLY",
                    "winner_state": state,
                    "feature": feature,
                    "winner_n": len(left),
                    "control_n": len(right),
                    "winner_mean": left_mean,
                    "control_mean": right_mean,
                    "mean_difference": left_mean - right_mean,
                    "winner_median": float(np.median(left)),
                    "control_median": float(np.median(right)),
                    "standardized_mean_difference": (
                        (left_mean - right_mean) / pooled if pooled > 1e-15 else None
                    ),
                    "probability_superiority_minus_half": superiority - 0.5,
                }
            )
    return rows, audits


def taxonomy_summary_rows(
    arrays: dict[str, np.ndarray], labels: np.ndarray, model: FrozenTaxonomy, cfg: Config = CFG
) -> list[dict]:
    discovery = _date_mask(arrays["meta"], cfg.discovery_start, cfg.discovery_end)
    unexplained = _winner_mask(arrays) & ~_major_union(arrays)
    characteristics = {row["cluster_number"]: row for row in centroid_characteristics(model)}
    denominator = int(np.count_nonzero(discovery & unexplained))
    rows = []
    for number, name in enumerate(model.cluster_names, 1):
        members = discovery & unexplained & (labels == number)
        row = {
            "fit_period": model.fit_period,
            "candidate_family": name,
            "cluster_number": number,
            "chosen_k": model.chosen_k,
            "discovery_unexplained_winner_members": int(np.count_nonzero(members)),
            "discovery_unexplained_winner_share": (
                np.count_nonzero(members) / denominator if denominator else None
            ),
            "minimum_centroid_separation": model.minimum_centroid_separation,
            "minimum_cluster_share": model.minimum_cluster_share,
            "stable_taxonomy": model.stable_taxonomy,
            **characteristics[number],
        }
        rows.append(row)
    return rows


def _frequency(meta: np.ndarray, selected: np.ndarray, start: str, end: str) -> dict:
    mask = selected & _date_mask(meta, start, end)
    dates = meta["signal_date"][mask]
    counts = Counter(int(value) for value in dates)
    month_count = len({int(value) // 100 for value in meta["signal_date"] if int(start) <= value <= int(end)})
    values = np.asarray(list(counts.values()), dtype=float)
    top5 = sum(sorted(counts.values(), reverse=True)[:5])
    return {
        "signals_per_year": len(dates) / max(1.0, (int(end[:4]) - int(start[:4]) + 1)),
        "signals_per_month": len(dates) / month_count if month_count else None,
        "active_signal_dates": len(counts),
        "median_signals_active_day": float(np.median(values)) if len(values) else None,
        "p90_signals_active_day": float(np.quantile(values, 0.90)) if len(values) else None,
        "maximum_signals_day": int(max(values)) if len(values) else 0,
        "maximum_signal_date": min(day for day, count in counts.items() if count == max(counts.values())) if counts else None,
        "top5_dates_share": top5 / len(dates) if len(dates) else None,
        "maximum_signal_month": (
            min(
                month
                for month, count in Counter(int(value) // 100 for value in dates).items()
                if count == max(Counter(int(value) // 100 for value in dates).values())
            )
            if len(dates)
            else None
        ),
        "maximum_signal_month_share": (
            max(Counter(int(value) // 100 for value in dates).values()) / len(dates)
            if len(dates)
            else None
        ),
    }


def candidate_family_rows(
    arrays: dict[str, np.ndarray], labels: np.ndarray, model: FrozenTaxonomy
) -> list[dict]:
    winner = _winner_mask(arrays)
    explained = _major_union(arrays)
    compact = _family_mask(arrays, "N_COMPACT_RETEST_HYPOTHESIS")
    rows = []
    for number, name in enumerate(model.cluster_names, 1):
        candidate = labels == number
        for kind, label, start, end in time_slices():
            period = _date_mask(arrays["meta"], start, end)
            selected = period & candidate
            all_winners = int(np.count_nonzero(period & winner))
            unique = int(np.count_nonzero(selected & winner & ~explained))
            row = {
                "time_slice_type": kind,
                "time_slice": label,
                "candidate_family": name,
                "cluster_number": number,
                "taxonomy_model_sha256": model.fingerprint(),
                "winner_recall": (
                    np.count_nonzero(selected & winner) / all_winners if all_winners else None
                ),
                "unique_winner_coverage_count": unique,
                "unique_winner_coverage": unique / all_winners if all_winners else None,
                "overlap_winners_with_n_compact": int(np.count_nonzero(selected & winner & compact)),
                **metric_summary(arrays, selected),
                **_frequency(arrays["meta"], candidate, start, end),
            }
            combined = candidate | compact
            row["n_compact_signals_per_month"] = _frequency(
                arrays["meta"], compact, start, end
            )["signals_per_month"]
            row["candidate_plus_n_compact_signals_per_month"] = _frequency(
                arrays["meta"], combined, start, end
            )["signals_per_month"]
            row["candidate_plus_n_compact_unique_signal_dates"] = _frequency(
                arrays["meta"], combined, start, end
            )["active_signal_dates"]
            rows.append(row)
    for family in model.cluster_names:
        yearly = [
            row
            for row in rows
            if row["candidate_family"] == family and row["time_slice_type"] == "YEAR"
        ]
        positive = sum(
            row["gross_mean"] is not None and row["gross_mean"] > 0 for row in yearly
        )
        pf_above_one = sum(
            row["profit_factor"] is not None and row["profit_factor"] > 1 for row in yearly
        )
        signs = {
            1 if row["gross_mean"] > 0 else -1
            for row in yearly
            if row["gross_mean"] is not None and abs(row["gross_mean"]) > 1e-15
        }
        for row in rows:
            if row["candidate_family"] == family:
                row["positive_gross_years_2020_2025"] = positive
                row["pf_above_one_years_2020_2025"] = pf_above_one
                row["gross_direction_consistent_all_years"] = len(yearly) == 6 and len(signs) == 1
    return rows


def choose_discovery_candidate(candidate_rows: list[dict]) -> str | None:
    discovery = [
        row
        for row in candidate_rows
        if row["time_slice_type"] == "PERIOD" and row["time_slice"] == "HISTORICAL_DISCOVERY"
        and row["gross_mean"] is not None
        and row["gross_mean"] > 0
        and row["profit_factor"] is not None
        and row["profit_factor"] > 1.0
        and row["top1_removed_pf"] is not None
        and row["top1_removed_pf"] > 1.0
        and not row["tail_dependent"]
    ]
    if not discovery:
        return None
    # A next-detector hypothesis must first show non-tail-dependent directional
    # edge in discovery. Selection then uses discovery only; later performance
    # can evaluate but never change which family was selected.
    best = max(
        discovery,
        key=lambda row: (
            row["unique_winner_coverage"] or -1.0,
            row["precision_evaluable_signals"] or -1.0,
            row["candidate_family"],
        ),
    )
    return str(best["candidate_family"])


def build_validation_summary(
    arrays: dict[str, np.ndarray],
    coverage: list[dict],
    marginal: list[dict],
    unexplained: list[dict],
    taxonomy_rows: list[dict],
    candidate_rows: list[dict],
    model: FrozenTaxonomy,
) -> dict:
    overall_coverage = {
        row["family"]: row
        for row in coverage
        if row["time_slice_type"] == "OVERALL"
    }
    overall_marginal = [row for row in marginal if row["time_slice_type"] == "OVERALL"]
    largest_coverage = max(
        (row for row in overall_coverage.values() if row["family"] in MAJOR_SETUPS),
        key=lambda row: row["winner_coverage_recall"] or -1,
    )
    incremental = max(
        (row for row in overall_marginal if row["setup"] != "N_COMPACT_RETEST_HYPOTHESIS"),
        key=lambda row: row["incremental_coverage_vs_n_compact"] or -1,
    )
    chosen = choose_discovery_candidate(candidate_rows)
    chosen_periods = {
        row["time_slice"]: row
        for row in candidate_rows
        if row["candidate_family"] == chosen and row["time_slice_type"] == "PERIOD"
    }
    candidate_survives = bool(
        chosen
        and all(
            chosen_periods[label]["gross_mean"] is not None
            and chosen_periods[label]["gross_mean"] > 0
            and chosen_periods[label]["profit_factor"] is not None
            and chosen_periods[label]["profit_factor"] > 1
            and not chosen_periods[label]["tail_dependent"]
            for label, _, _ in PERIODS
        )
    )
    if not model.stable_taxonomy:
        final_status = "NO_STABLE_NEW_FAMILY_FOUND"
    elif candidate_survives:
        final_status = "PROMISING_FOR_NEXT_HYPOTHESIS"
    else:
        final_status = "DESCRIPTIVE_ONLY"
    unexplained_overall = next(row for row in unexplained if row["time_slice_type"] == "OVERALL")
    def state_evidence(token: str) -> dict:
        matches = [
            row for row in taxonomy_rows if token in row["candidate_family"]
        ]
        evidence = []
        for taxonomy_row in matches:
            family = taxonomy_row["candidate_family"]
            periods = {
                row["time_slice"]: row
                for row in candidate_rows
                if row["candidate_family"] == family
                and row["time_slice_type"] == "PERIOD"
            }
            consistent = len(periods) == 3 and all(
                row["gross_mean"] is not None
                and row["gross_mean"] > 0
                and row["profit_factor"] is not None
                and row["profit_factor"] > 1
                for row in periods.values()
            )
            evidence.append(
                {
                    "candidate_family": family,
                    "discovery_members": taxonomy_row["discovery_unexplained_winner_members"],
                    "directionally_consistent_all_periods": consistent,
                }
            )
        return {
            "state_identified": bool(matches),
            "stable_unsupervised_taxonomy": bool(matches) and model.stable_taxonomy,
            "directionally_consistent_candidate": any(
                row["directionally_consistent_all_periods"] for row in evidence
            ),
            "evidence": evidence,
        }
    return {
        "study_id": CFG.study_id,
        "status": "COMPLETE",
        "result_status": CFG.result_status,
        "final_classification": final_status,
        "period_discipline": {
            "2020_2022": "HISTORICAL_DISCOVERY",
            "2023_2024": "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS",
            "2025": "STRESS_PREVALENCE_SEEN_NOT_BLIND",
            "prospective_from_2026_09": "EXCLUDED_FROM_DISCOVERY_AND_TAXONOMY",
        },
        "answers": {
            "1_full_market_winner_base_rate": next(
                row["winner_base_rate"]
                for row in winner_base_rate_rows(arrays)
                if row["time_slice_type"] == "OVERALL"
            ),
            "2_n_compact_winner_coverage": overall_coverage["N_COMPACT_RETEST_HYPOTHESIS"]["winner_coverage_recall"],
            "3_n_compact_precision": overall_coverage["N_COMPACT_RETEST_HYPOTHESIS"]["precision_all_signals"],
            "4_largest_existing_setup_coverage": {
                "setup": largest_coverage["family"],
                "coverage": largest_coverage["winner_coverage_recall"],
            },
            "5_largest_incremental_vs_n_compact": {
                "setup": incremental["setup"],
                "incremental_coverage": incremental["incremental_coverage_vs_n_compact"],
            },
            "6_unexplained_winner_share": unexplained_overall["unexplained_winner_share"],
            "7_unexplained_t_day_states": [
                {
                    "family": row["candidate_family"],
                    "share": row["discovery_unexplained_winner_share"],
                    "characteristics": row["top_standardized_characteristics"],
                }
                for row in taxonomy_rows
            ],
            "8_direct_continuation_family_found": state_evidence("DIRECT_CONTINUATION"),
            "9_breakout_squeeze_family_found": state_evidence("BREAKOUT_SQUEEZE"),
            "10_next_detector_hypothesis": chosen,
            "11_candidate_monthly_signals": (
                chosen_periods.get("HISTORICAL_DISCOVERY", {}).get("signals_per_month")
                if chosen else None
            ),
            "12_n_compact_plus_candidate_monthly": {
                "n_compact": chosen_periods.get("HISTORICAL_DISCOVERY", {}).get("n_compact_signals_per_month") if chosen else None,
                "combined": chosen_periods.get("HISTORICAL_DISCOVERY", {}).get("candidate_plus_n_compact_signals_per_month") if chosen else None,
            },
            "13_candidate_trade_quality": chosen_periods,
            "14_cross_period_direction": {
                "positive_gross_all_periods": bool(
                    chosen
                    and all(row["gross_mean"] is not None and row["gross_mean"] > 0 for row in chosen_periods.values())
                ),
                "pf_above_one_all_periods": bool(
                    chosen
                    and all(row["profit_factor"] is not None and row["profit_factor"] > 1 for row in chosen_periods.values())
                ),
            },
            "15_regime_month_tail_warning": {
                "tail_dependent_any_period": bool(
                    chosen and any(bool(row["tail_dependent"]) for row in chosen_periods.values())
                ),
                "top5_dates_share_by_period": {
                    label: row["top5_dates_share"] for label, row in chosen_periods.items()
                } if chosen else {},
                "warning": "Period and date concentration are descriptive; market-regime causality is not established.",
            },
        },
        "taxonomy": {
            **model.payload(),
            "taxonomy_model_sha256": model.fingerprint(),
            "fit_count": 1,
            "later_period_refits": 0,
        },
        "execution": {
            "parameter_search": "NONE",
            "supervised_ml": "NONE",
            "broker_connection": "ABSENT",
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
        },
    }
