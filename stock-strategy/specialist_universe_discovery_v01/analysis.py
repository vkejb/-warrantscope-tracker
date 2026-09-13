from __future__ import annotations

from collections import defaultdict
import math
import statistics
from typing import Iterable

import numpy as np

from surge_event_study_v01.models import PreparedBenchmark, PreparedStock

from .config import CFG, CLUSTER_FEATURES, SCORE_WEIGHTS, Config


DISCOVERY_LABEL = "HISTORICAL_DISCOVERY"
LATER_LABELS = (
    "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS",
    "STRESS_PREVALENCE_SEEN_NOT_BLIND",
)


def _median(values: Iterable[float]) -> float | None:
    cleaned = [float(value) for value in values if math.isfinite(float(value))]
    return statistics.median(cleaned) if cleaned else None


def _quantile(values: Iterable[float], probability: float) -> float | None:
    cleaned = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not cleaned:
        return None
    position = (len(cleaned) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return cleaned[lower]
    return cleaned[lower] + (cleaned[upper] - cleaned[lower]) * (position - lower)


def _rate(values: Iterable[bool]) -> float | None:
    cleaned = list(values)
    return statistics.fmean(1.0 if value else 0.0 for value in cleaned) if cleaned else None


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean, right_mean = statistics.fmean(left), statistics.fmean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    denominator = math.sqrt(
        sum((a - left_mean) ** 2 for a in left)
        * sum((b - right_mean) ** 2 for b in right)
    )
    return numerator / denominator if denominator else None


def benchmark_returns(benchmark: PreparedBenchmark) -> dict[str, float]:
    result = {}
    for index in range(1, len(benchmark.calendar)):
        current, previous = benchmark.normalized_closes[index], benchmark.normalized_closes[index - 1]
        if (
            current is not None
            and previous is not None
            and benchmark.segment_ids[index] == benchmark.segment_ids[index - 1]
        ):
            result[benchmark.calendar[index]] = current / previous - 1.0
    return result


def stock_return_map(stock: PreparedStock, start: str, end: str) -> dict[str, float]:
    return {
        stock.bars[position].date: stock.daily_returns[position]
        for position in range(1, len(stock.bars))
        if start <= stock.bars[position].date <= end
        and stock.segment_ids[position] == stock.segment_ids[position - 1]
    }


def longest_missing_run(stock: PreparedStock, calendar: list[str], start: str, end: str) -> int:
    period_dates = [day for day in calendar if start <= day <= end]
    available = {bar.date for bar in stock.bars if start <= bar.date <= end}
    longest = current = 0
    for day in period_dates:
        if day in available:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def _maximum_drawdown(stock: PreparedStock, positions: list[int]) -> float | None:
    if not positions:
        return None
    maximum_drawdown, peak = 0.0, None
    previous_position = previous_segment = None
    for position in positions:
        segment = stock.segment_ids[position]
        if previous_position is None or position != previous_position + 1 or segment != previous_segment:
            peak = stock.bars[position].close
        else:
            peak = max(float(peak), stock.bars[position].close)
        maximum_drawdown = min(maximum_drawdown, stock.bars[position].close / float(peak) - 1.0)
        previous_position, previous_segment = position, segment
    return maximum_drawdown * 100.0


def _forward(stock: PreparedStock, positions: list[int], cfg: Config) -> dict:
    mfes, maes, day10s, up_hits, down_hits = [], [], [], [], []
    for position in positions:
        end = position + cfg.forward_sessions
        if end >= len(stock.bars):
            continue
        future = range(position + 1, end + 1)
        if any(
            stock.calendar_indices[index] != stock.calendar_indices[position] + index - position
            or stock.segment_ids[index] != stock.segment_ids[position]
            for index in future
        ):
            continue
        entry = stock.bars[position + 1].open
        path = [stock.bars[index].close / entry - 1.0 for index in future]
        mfes.append(max(path) * 100.0)
        maes.append(min(path) * 100.0)
        day10s.append(path[-1] * 100.0)
        up_hits.append(any(value >= 0.08 for value in path))
        down_hits.append(any(value <= -0.05 for value in path))
    return {
        "forward_evaluable_events": len(mfes),
        "median_mfe10_pct": _median(mfes),
        "median_mae10_pct": _median(maes),
        "median_day10_close_return_pct": _median(day10s),
        "plus_8pct_within_10d_close_hit_rate": _rate(up_hits),
        "minus_5pct_within_10d_close_hit_rate": _rate(down_hits),
    }


def calculate_metrics(
    stock: PreparedStock,
    calendar: list[str],
    benchmark_by_date: dict[str, float],
    start: str,
    end: str,
    label: str,
    *,
    include_forward: bool = False,
    cfg: Config = CFG,
) -> dict:
    positions = [index for index, bar in enumerate(stock.bars) if start <= bar.date <= end]
    position_set = set(positions)
    market_sessions = sum(start <= day <= end for day in calendar)
    returns, gaps, atrs, realized, efficiencies, autocorr_pairs = [], [], [], [], [], []
    discontinuities = 0
    for position in positions:
        same_segment = position >= 1 and stock.segment_ids[position] == stock.segment_ids[position - 1]
        if same_segment:
            returns.append(stock.daily_returns[position])
            gaps.append(abs(stock.bars[position].open / stock.bars[position - 1].close - 1.0))
        elif position >= 1 and stock.calendar_indices[position] == stock.calendar_indices[position - 1] + 1:
            discontinuities += 1
        if position >= cfg.atr_window and stock.segment_ids[position - cfg.atr_window] == stock.segment_ids[position]:
            atrs.append(statistics.fmean(stock.true_range_ratios[position - cfg.atr_window + 1 : position + 1]))
        if position >= cfg.realized_vol_window and stock.segment_ids[position - cfg.realized_vol_window] == stock.segment_ids[position]:
            window = stock.daily_returns[position - cfg.realized_vol_window + 1 : position + 1]
            realized.append(statistics.pstdev(window) * math.sqrt(cfg.annualization_sessions))
        if position >= cfg.efficiency_window and stock.segment_ids[position - cfg.efficiency_window] == stock.segment_ids[position]:
            path = stock.daily_returns[position - cfg.efficiency_window + 1 : position + 1]
            denominator = sum(abs(value) for value in path)
            if denominator:
                efficiencies.append(abs(stock.bars[position].close / stock.bars[position - cfg.efficiency_window].close - 1.0) / denominator)
        if position >= 2 and position - 1 in position_set and stock.segment_ids[position - 2] == stock.segment_ids[position]:
            autocorr_pairs.append((stock.daily_returns[position - 1], stock.daily_returns[position]))

    stock_returns = stock_return_map(stock, start, end)
    common_dates = sorted(set(stock_returns) & set(benchmark_by_date))
    stock_common = [stock_returns[day] for day in common_dates]
    benchmark_common = [benchmark_by_date[day] for day in common_dates]
    corr = _correlation(stock_common, benchmark_common)
    beta = None
    idiosyncratic = None
    if len(common_dates) >= 2:
        benchmark_mean, stock_mean = statistics.fmean(benchmark_common), statistics.fmean(stock_common)
        benchmark_ss = sum((value - benchmark_mean) ** 2 for value in benchmark_common)
        if benchmark_ss:
            beta = sum((b - benchmark_mean) * (s - stock_mean) for s, b in zip(stock_common, benchmark_common)) / benchmark_ss
            alpha = stock_mean - beta * benchmark_mean
            residuals = [s - alpha - beta * b for s, b in zip(stock_common, benchmark_common)]
            idiosyncratic = statistics.pstdev(residuals) * math.sqrt(cfg.annualization_sessions) * 100.0
    positive = [(s, b) for s, b in zip(stock_common, benchmark_common) if b > 0]
    negative = [(s, b) for s, b in zip(stock_common, benchmark_common) if b < 0]
    upside = statistics.fmean(item[0] for item in positive) / statistics.fmean(item[1] for item in positive) if positive else None
    downside = statistics.fmean(item[0] for item in negative) / statistics.fmean(item[1] for item in negative) if negative else None
    turnover = [stock.bars[position].close * stock.bars[position].volume for position in positions]
    absolute_returns = [abs(value) for value in returns]
    absolute_gaps = [abs(value) for value in gaps]
    row = {
        "period": label,
        "start_date": start,
        "end_date": end,
        "stock_id": stock.code,
        "stock_name": stock.name,
        "sessions": len(positions),
        "market_sessions": market_sessions,
        "coverage": len(positions) / market_sessions if market_sessions else None,
        "longest_missing_run_sessions": longest_missing_run(stock, calendar, start, end),
        "discontinuity_count": discontinuities,
        "median_close": _median(stock.bars[position].close for position in positions),
        "median_daily_turnover_proxy": _median(turnover),
        "p25_daily_turnover_proxy": _quantile(turnover, 0.25),
        "median_abs_return_pct": None if not absolute_returns else _median(absolute_returns) * 100.0,
        "p90_abs_return_pct": None if not absolute_returns else _quantile(absolute_returns, 0.90) * 100.0,
        "abs_return_ge_5pct_rate": _rate(value >= 0.05 for value in absolute_returns),
        "worst_single_day_return_pct": None if not returns else min(returns) * 100.0,
        "median_atr14_pct": None if not atrs else _median(atrs) * 100.0,
        "p75_atr14_pct": None if not atrs else _quantile(atrs, 0.75) * 100.0,
        "median_realized_vol20_annualized_pct": None if not realized else _median(realized) * 100.0,
        "median_efficiency20": _median(efficiencies),
        "lag1_daily_return_autocorrelation": _correlation([x for x, _ in autocorr_pairs], [y for _, y in autocorr_pairs]),
        "beta_0050": beta,
        "correlation_0050": corr,
        "upside_capture": upside,
        "downside_capture": downside,
        "idiosyncratic_volatility_annualized_pct": idiosyncratic,
        "median_abs_overnight_gap_pct": None if not absolute_gaps else _median(absolute_gaps) * 100.0,
        "p95_abs_overnight_gap_pct": None if not absolute_gaps else _quantile(absolute_gaps, 0.95) * 100.0,
        "maximum_close_drawdown_pct": _maximum_drawdown(stock, positions),
        "market_aligned_return_sessions": len(common_dates),
    }
    if include_forward:
        row.update(_forward(stock, positions, cfg))
    return row


def discovery_eligibility(
    all_stocks: dict[str, list],
    prepared: list[PreparedStock],
    calendar: list[str],
    benchmark_by_date: dict[str, float],
    cfg: Config = CFG,
) -> tuple[list[dict], list[dict], dict[str, PreparedStock]]:
    prepared_by_code = {stock.code: stock for stock in prepared}
    rows, eligible_metrics = [], []
    for code in sorted(all_stocks):
        stock = prepared_by_code.get(code)
        name = all_stocks[code][-1].name if all_stocks[code] else ""
        if stock is None:
            rows.append({"stock_id": code, "stock_name": name, "eligible": False, "failure_reasons": "INSUFFICIENT_TOTAL_HISTORY"})
            continue
        discovery = calculate_metrics(stock, calendar, benchmark_by_date, cfg.discovery_start, cfg.discovery_end, DISCOVERY_LABEL, cfg=cfg)
        annual = [
            calculate_metrics(stock, calendar, benchmark_by_date, f"{year}0101", f"{year}1231", str(year), cfg=cfg)
            for year in (2020, 2021, 2022)
        ]
        reasons = []
        for item in annual:
            if item["coverage"] < cfg.annual_minimum_coverage:
                reasons.append(f"{item['period']}_COVERAGE")
            if item["sessions"] < cfg.annual_minimum_sessions:
                reasons.append(f"{item['period']}_SESSIONS")
        gates = (
            (discovery["median_daily_turnover_proxy"] is not None and discovery["median_daily_turnover_proxy"] >= cfg.minimum_median_turnover_proxy, "TURNOVER"),
            (discovery["median_atr14_pct"] is not None and discovery["median_atr14_pct"] >= cfg.minimum_median_atr14_pct, "ATR14"),
            (discovery["median_close"] is not None and discovery["median_close"] >= cfg.minimum_median_close, "PRICE"),
            (discovery["p95_abs_overnight_gap_pct"] is not None and discovery["p95_abs_overnight_gap_pct"] <= cfg.maximum_p95_gap_pct, "P95_GAP"),
            (discovery["longest_missing_run_sessions"] <= cfg.maximum_discovery_missing_run_sessions, "LONG_MISSING_RUN"),
        )
        reasons.extend(label for passed, label in gates if not passed)
        row = {
            "stock_id": code,
            "stock_name": name,
            "eligible": not reasons,
            "failure_reasons": "|".join(reasons),
            "discovery_sessions": discovery["sessions"],
            "discovery_coverage": discovery["coverage"],
            "discovery_longest_missing_run_sessions": discovery["longest_missing_run_sessions"],
            "discovery_median_close": discovery["median_close"],
            "discovery_median_turnover_proxy": discovery["median_daily_turnover_proxy"],
            "discovery_median_atr14_pct": discovery["median_atr14_pct"],
            "discovery_p95_gap_pct": discovery["p95_abs_overnight_gap_pct"],
        }
        for item in annual:
            row[f"coverage_{item['period']}"] = item["coverage"]
            row[f"sessions_{item['period']}"] = item["sessions"]
        rows.append(row)
        if not reasons:
            eligible_metrics.append(discovery)
    return rows, eligible_metrics, prepared_by_code


def add_discovery_features(
    eligible_basic: list[dict], prepared_by_code: dict[str, PreparedStock], calendar: list[str], benchmark_by_date: dict[str, float], cfg: Config = CFG
) -> list[dict]:
    rows = []
    for basic in eligible_basic:
        stock = prepared_by_code[basic["stock_id"]]
        full = calculate_metrics(stock, calendar, benchmark_by_date, cfg.discovery_start, cfg.discovery_end, DISCOVERY_LABEL, include_forward=True, cfg=cfg)
        annual = [
            calculate_metrics(stock, calendar, benchmark_by_date, f"{year}0101", f"{year}1231", str(year), cfg=cfg)
            for year in (2020, 2021, 2022)
        ]
        cvs = []
        for field in ("median_atr14_pct", "median_abs_return_pct", "median_daily_turnover_proxy"):
            values = [float(item[field]) for item in annual]
            mean = statistics.fmean(values)
            cvs.append(statistics.pstdev(values) / abs(mean) if mean else math.inf)
        full["turnover_stability_cv_2020_2022"] = cvs[2]
        full["behavior_stability_raw_mean_cv"] = statistics.fmean(cvs)
        rows.append(full)
    return rows


def _percentiles(rows: list[dict], field: str, *, higher_better: bool = True) -> dict[str, float]:
    values = sorted(float(row[field]) for row in rows)
    denominator = max(1, len(values) - 1)
    result = {}
    for row in rows:
        value = float(row[field])
        positions = [index for index, item in enumerate(values) if item == value]
        percentile = statistics.fmean(positions) / denominator
        result[row["stock_id"]] = percentile if higher_better else 1.0 - percentile
    return result


def quality_scores(discovery_rows: list[dict], cfg: Config = CFG) -> list[dict]:
    percentiles = {
        field: _percentiles(discovery_rows, field, higher_better=higher)
        for field, higher in (
            ("median_daily_turnover_proxy", True),
            ("coverage", True),
            ("behavior_stability_raw_mean_cv", False),
            ("median_atr14_pct", True),
            ("median_abs_return_pct", True),
            ("p95_abs_overnight_gap_pct", False),
            ("median_efficiency20", True),
            ("lag1_daily_return_autocorrelation", True),
        )
    }
    absolute_autocorrelation = [
        {"stock_id": row["stock_id"], "absolute_autocorrelation": abs(float(row["lag1_daily_return_autocorrelation"]))}
        for row in discovery_rows
    ]
    autocorr_quality = _percentiles(absolute_autocorrelation, "absolute_autocorrelation", higher_better=False)
    scores = []
    for row in discovery_rows:
        code = row["stock_id"]
        atr_credit = min(percentiles["median_atr14_pct"][code] / cfg.tradable_volatility_full_credit_percentile, 1.0)
        return_credit = min(percentiles["median_abs_return_pct"][code] / cfg.tradable_volatility_full_credit_percentile, 1.0)
        efficiency_credit = min(percentiles["median_efficiency20"][code] / cfg.trend_efficiency_full_credit_percentile, 1.0)
        components = {
            "liquidity_component": percentiles["median_daily_turnover_proxy"][code],
            "continuity_component": percentiles["coverage"][code],
            "behavior_stability_component": percentiles["behavior_stability_raw_mean_cv"][code],
            "tradable_volatility_component": (atr_credit + return_credit) / 2.0,
            "gap_safety_component": percentiles["p95_abs_overnight_gap_pct"][code],
            "trend_structure_quality_component": (efficiency_credit + autocorr_quality[code]) / 2.0,
        }
        scores.append({
            "stock_id": code,
            "stock_name": row["stock_name"],
            "discovery_quality_score": sum(SCORE_WEIGHTS[key] * value for key, value in components.items()),
            **components,
            "future_outcomes_used_for_universe_selection": False,
            "industry_used_for_score_cluster_or_selection": False,
        })
    scores.sort(key=lambda item: (-item["discovery_quality_score"], item["stock_id"]))
    for rank, row in enumerate(scores, 1):
        row["discovery_quality_rank"] = rank
    return scores


def standardized_cluster_matrix(rows: list[dict], cfg: Config = CFG) -> tuple[np.ndarray, dict]:
    matrix = np.asarray([[float(row[field]) for field in CLUSTER_FEATURES] for row in rows], dtype=float)
    lower = np.quantile(matrix, cfg.winsor_lower_quantile, axis=0)
    upper = np.quantile(matrix, cfg.winsor_upper_quantile, axis=0)
    winsorized = np.clip(matrix, lower, upper)
    means = winsorized.mean(axis=0)
    standard_deviations = winsorized.std(axis=0)
    if np.any(standard_deviations == 0):
        raise RuntimeError("constant discovery clustering feature")
    standardized = (winsorized - means) / standard_deviations
    audit = {
        field: {"winsor_lower": float(lower[index]), "winsor_upper": float(upper[index]), "mean": float(means[index]), "std": float(standard_deviations[index])}
        for index, field in enumerate(CLUSTER_FEATURES)
    }
    return standardized, audit


def deterministic_kmeans(matrix: np.ndarray, k: int, max_iterations: int = 300, tolerance: float = 1e-10) -> tuple[np.ndarray, np.ndarray, int]:
    if matrix.ndim != 2 or len(matrix) < k:
        raise ValueError("kmeans requires at least k rows")
    chosen = [0]
    while len(chosen) < k:
        distances = np.min(np.sum((matrix[:, None, :] - matrix[np.asarray(chosen)][None, :, :]) ** 2, axis=2), axis=1)
        distances[np.asarray(chosen)] = -1.0
        chosen.append(int(np.argmax(distances)))
    centroids = matrix[np.asarray(chosen)].copy()
    labels = np.zeros(len(matrix), dtype=int)
    for iteration in range(1, max_iterations + 1):
        distances = np.sum((matrix[:, None, :] - centroids[None, :, :]) ** 2, axis=2)
        new_labels = np.argmin(distances, axis=1)
        new_centroids = centroids.copy()
        for cluster in range(k):
            members = matrix[new_labels == cluster]
            if len(members):
                new_centroids[cluster] = members.mean(axis=0)
            else:
                nearest = np.min(distances, axis=1)
                new_centroids[cluster] = matrix[int(np.argmax(nearest))]
        movement = float(np.max(np.abs(new_centroids - centroids)))
        labels, centroids = new_labels, new_centroids
        if movement <= tolerance:
            return labels, centroids, iteration
    return labels, centroids, max_iterations


def _cluster_label(centroid: np.ndarray) -> str:
    labels = {
        "beta_0050": "BETA",
        "correlation_0050": "MARKET_CORRELATION",
        "upside_capture": "UPSIDE_CAPTURE",
        "downside_capture": "DOWNSIDE_CAPTURE",
        "median_atr14_pct": "ATR",
        "median_realized_vol20_annualized_pct": "REALIZED_VOL",
        "median_efficiency20": "TREND_EFFICIENCY",
        "lag1_daily_return_autocorrelation": "AUTOCORRELATION",
        "idiosyncratic_volatility_annualized_pct": "IDIOSYNCRATIC_VOL",
        "p95_abs_overnight_gap_pct": "GAP_RISK",
    }
    order = sorted(range(len(centroid)), key=lambda index: (-abs(float(centroid[index])), index))[:2]
    parts = [("HIGH" if centroid[index] >= 0 else "LOW") + "_" + labels[CLUSTER_FEATURES[index]] for index in order]
    return "__".join(parts)


def cluster_discovery(rows: list[dict], cfg: Config = CFG) -> tuple[list[dict], list[dict], dict]:
    ordered = sorted(rows, key=lambda row: row["stock_id"])
    matrix, normalization = standardized_cluster_matrix(ordered, cfg)
    labels, centroids, iterations = deterministic_kmeans(matrix, cfg.cluster_count, cfg.kmeans_max_iterations, cfg.kmeans_tolerance)
    assignments = []
    for index, row in enumerate(ordered):
        item = {"stock_id": row["stock_id"], "stock_name": row["stock_name"], "discovery_cluster": int(labels[index])}
        item.update({f"z_{field}": float(matrix[index, feature_index]) for feature_index, field in enumerate(CLUSTER_FEATURES)})
        assignments.append(item)
    centroid_rows = []
    for cluster in range(cfg.cluster_count):
        members = [ordered[index] for index in range(len(ordered)) if labels[index] == cluster]
        centroid_row = {
            "discovery_cluster": cluster,
            "stock_count": len(members),
            "cluster_descriptive_label": _cluster_label(centroids[cluster]),
        }
        for feature_index, field in enumerate(CLUSTER_FEATURES):
            centroid_row[f"z_centroid_{field}"] = float(centroids[cluster, feature_index])
            centroid_row[f"median_{field}"] = _median(float(member[field]) for member in members)
        centroid_rows.append(centroid_row)
    return assignments, centroid_rows, {"iterations": iterations, "normalization": normalization}


def pair_correlation(left: PreparedStock, right: PreparedStock, start: str, end: str) -> float | None:
    left_map, right_map = stock_return_map(left, start, end), stock_return_map(right, start, end)
    common = sorted(set(left_map) & set(right_map))
    return _correlation([left_map[day] for day in common], [right_map[day] for day in common])


def select_representatives(
    scores: list[dict], assignments: list[dict], prepared_by_code: dict[str, PreparedStock], cfg: Config = CFG
) -> tuple[list[dict], list[dict]]:
    score_by_code = {row["stock_id"]: row for row in scores}
    cluster_by_code = {row["stock_id"]: row["discovery_cluster"] for row in assignments}
    by_cluster = defaultdict(list)
    for code, cluster in cluster_by_code.items():
        by_cluster[cluster].append(score_by_code[code])
    representatives = []
    primary_codes = []
    rank2_candidates = []
    for cluster in sorted(by_cluster):
        members = sorted(by_cluster[cluster], key=lambda row: (-row["discovery_quality_score"], row["stock_id"]))
        primary = members[0]
        primary_codes.append(primary["stock_id"])
        representatives.append({
            "discovery_cluster": cluster,
            "discovery_rank_within_cluster": 1,
            "stock_id": primary["stock_id"],
            "stock_name": primary["stock_name"],
            "discovery_quality_score": primary["discovery_quality_score"],
            "correlation_with_cluster_primary": 1.0,
            "rank2_correlation_gate_pass": True,
            "representative_eligible": True,
            "selection_priority": "CLUSTER_PRIMARY",
        })
        if len(members) > 1:
            secondary = members[1]
            corr = pair_correlation(prepared_by_code[primary["stock_id"]], prepared_by_code[secondary["stock_id"]], cfg.discovery_start, cfg.discovery_end)
            passed = corr is not None and corr < cfg.rank2_maximum_primary_correlation
            item = {
                "discovery_cluster": cluster,
                "discovery_rank_within_cluster": 2,
                "stock_id": secondary["stock_id"],
                "stock_name": secondary["stock_name"],
                "discovery_quality_score": secondary["discovery_quality_score"],
                "correlation_with_cluster_primary": corr,
                "rank2_correlation_gate_pass": passed,
                "representative_eligible": passed,
                "selection_priority": "CLUSTER_SECONDARY" if passed else "REJECTED_HIGH_PRIMARY_CORRELATION",
            }
            representatives.append(item)
            if passed:
                rank2_candidates.append(item)
    selected = list(primary_codes)
    while rank2_candidates and len(selected) < cfg.final_pool_maximum:
        evaluated = []
        for item in rank2_candidates:
            correlations = [pair_correlation(prepared_by_code[item["stock_id"]], prepared_by_code[code], cfg.discovery_start, cfg.discovery_end) for code in selected]
            average = statistics.fmean(value for value in correlations if value is not None)
            evaluated.append((item, average))
        evaluated.sort(key=lambda pair: (-pair[0]["discovery_quality_score"], pair[1], pair[0]["stock_id"]))
        chosen, average = evaluated[0]
        chosen["average_correlation_at_selection"] = average
        selected.append(chosen["stock_id"])
        rank2_candidates = [item for item in rank2_candidates if item["stock_id"] != chosen["stock_id"]]
    for item in representatives:
        item["discovery_selected"] = item["stock_id"] in selected
    return representatives, [score_by_code[code] for code in selected]


def correlation_matrix(selected_codes: list[str], prepared_by_code: dict[str, PreparedStock], cfg: Config = CFG) -> tuple[list[dict], dict]:
    matrix = {}
    pair_rows = []
    for left_index, left in enumerate(selected_codes):
        for right_index, right in enumerate(selected_codes):
            if left == right:
                value = 1.0
            elif (right, left) in matrix:
                value = matrix[(right, left)]
            else:
                value = pair_correlation(prepared_by_code[left], prepared_by_code[right], cfg.discovery_start, cfg.discovery_end)
            matrix[(left, right)] = value
            if left_index < right_index:
                pair_rows.append({"stock_id_1": left, "stock_id_2": right, "correlation": value, "redundancy_status": "PAIRWISE_HIGH_REDUNDANCY" if value is not None and value > cfg.pairwise_high_redundancy_threshold else "OK"})
    rows = []
    for code in selected_codes:
        others = [matrix[(code, other)] for other in selected_codes if other != code]
        rows.append({"stock_id": code, "average_correlation_to_pool": statistics.fmean(others), **{other: matrix[(code, other)] for other in selected_codes}})
    pair_values = [row["correlation"] for row in pair_rows]
    audit = {
        "pool_average_pairwise_correlation": statistics.fmean(pair_values) if pair_values else None,
        "maximum_pairwise_correlation": max(pair_values) if pair_values else None,
        "maximum_pair": None if not pair_rows else max(pair_rows, key=lambda row: row["correlation"]),
        "high_redundancy_pairs": [row for row in pair_rows if row["redundancy_status"] == "PAIRWISE_HIGH_REDUNDANCY"],
    }
    return rows, audit


def classify_stability(discovery: dict, later: dict, cfg: Config = CFG) -> dict:
    turnover_retention = later["median_daily_turnover_proxy"] / discovery["median_daily_turnover_proxy"]
    atr_retention = later["median_atr14_pct"] / discovery["median_atr14_pct"]
    return_retention = later["median_abs_return_pct"] / discovery["median_abs_return_pct"]
    gap_multiple = later["p95_abs_overnight_gap_pct"] / discovery["p95_abs_overnight_gap_pct"]
    beta_change = abs(later["beta_0050"] - discovery["beta_0050"])
    normal_checks = {
        "coverage_normal": later["coverage"] >= cfg.later_normal_minimum_coverage,
        "sessions_normal": later["sessions"] >= cfg.later_normal_minimum_sessions,
        "liquidity_normal": later["median_daily_turnover_proxy"] >= cfg.later_normal_minimum_turnover_proxy and turnover_retention >= cfg.later_normal_minimum_turnover_retention,
    }
    core_checks = {
        "atr_retention_core": cfg.core_retention_minimum <= atr_retention <= cfg.core_retention_maximum,
        "abs_return_retention_core": cfg.core_retention_minimum <= return_retention <= cfg.core_retention_maximum,
        "gap_multiple_core": gap_multiple <= cfg.core_maximum_gap_multiple,
        "beta_change_core": beta_change <= cfg.core_maximum_beta_absolute_change,
    }
    severe_checks = {
        "coverage_tradable": later["coverage"] >= cfg.severe_minimum_coverage,
        "sessions_tradable": later["sessions"] >= cfg.severe_minimum_sessions,
        "liquidity_tradable": later["median_daily_turnover_proxy"] >= cfg.severe_minimum_turnover_proxy and turnover_retention >= cfg.severe_minimum_turnover_retention,
        "missing_run_tradable": later["longest_missing_run_sessions"] <= cfg.severe_maximum_missing_run_sessions,
        "gap_tradable": later["p95_abs_overnight_gap_pct"] <= cfg.severe_maximum_p95_gap_pct and gap_multiple <= cfg.severe_maximum_gap_multiple,
    }
    severe_pass = all(severe_checks.values())
    core_pass = severe_pass and all(normal_checks.values()) and all(core_checks.values())
    return {
        "stock_id": later["stock_id"],
        "stock_name": later["stock_name"],
        "period": later["period"],
        "coverage": later["coverage"],
        "sessions": later["sessions"],
        "median_daily_turnover_proxy": later["median_daily_turnover_proxy"],
        "turnover_retention": turnover_retention,
        "atr_retention": atr_retention,
        "median_abs_return_retention": return_retention,
        "p95_gap_multiple": gap_multiple,
        "beta_absolute_change": beta_change,
        "correlation_0050": later["correlation_0050"],
        "median_efficiency20": later["median_efficiency20"],
        "downside_capture": later["downside_capture"],
        **normal_checks,
        **core_checks,
        **severe_checks,
        "severe_tradability_pass": severe_pass,
        "core_behavior_pass": core_pass,
        "period_stability": "CORE_STABLE" if core_pass else ("REGIME_SHIFT_TRADABLE" if severe_pass else "SEVERE_TRADABILITY_FAILURE"),
    }


def final_status(stability_rows: list[dict]) -> str:
    if any(not row["severe_tradability_pass"] for row in stability_rows):
        return "EXCLUDED_AFTER_STABILITY"
    if all(row["core_behavior_pass"] for row in stability_rows):
        return "CORE_SPECIALIST"
    return "REGIME_SPECIALIST"
