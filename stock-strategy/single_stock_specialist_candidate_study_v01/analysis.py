from __future__ import annotations

import math
import statistics
from typing import Iterable

from surge_event_study_v01.models import PreparedStock

from .config import CFG, COMPONENT_WEIGHTS, PERIODS, Config


DISCOVERY = PERIODS[0][0]
LATER_PERIODS = tuple(item[0] for item in PERIODS[1:])


def _median(values: Iterable[float]) -> float | None:
    cleaned = [float(value) for value in values if math.isfinite(float(value))]
    return statistics.median(cleaned) if cleaned else None


def _quantile(values: Iterable[float], probability: float) -> float | None:
    cleaned = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not cleaned:
        return None
    position = (len(cleaned) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return cleaned[lower]
    return cleaned[lower] + (cleaned[upper] - cleaned[lower]) * (position - lower)


def _mean(values: Iterable[float]) -> float | None:
    cleaned = [float(value) for value in values if math.isfinite(float(value))]
    return statistics.fmean(cleaned) if cleaned else None


def _rate(values: Iterable[bool]) -> float | None:
    cleaned = list(values)
    return statistics.fmean(1.0 if value else 0.0 for value in cleaned) if cleaned else None


def _autocorrelation(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    left = [item[0] for item in pairs]
    right = [item[1] for item in pairs]
    left_mean, right_mean = statistics.fmean(left), statistics.fmean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in pairs)
    denominator = math.sqrt(
        sum((a - left_mean) ** 2 for a in left)
        * sum((b - right_mean) ** 2 for b in right)
    )
    return numerator / denominator if denominator else None


def _maximum_drawdown(stock: PreparedStock, positions: list[int]) -> float | None:
    if not positions:
        return None
    maximum_drawdown = 0.0
    peak = None
    previous_position = None
    previous_segment = None
    for position in positions:
        segment = stock.segment_ids[position]
        if previous_position is None or position != previous_position + 1 or segment != previous_segment:
            peak = stock.bars[position].close
        else:
            peak = max(float(peak), stock.bars[position].close)
        maximum_drawdown = min(maximum_drawdown, stock.bars[position].close / float(peak) - 1.0)
        previous_position, previous_segment = position, segment
    return maximum_drawdown * 100.0


def _forward_diagnostics(
    stock: PreparedStock, positions: list[int], cfg: Config
) -> dict[str, float | None]:
    mfes, maes, day10s, up_hits, down_hits = [], [], [], [], []
    bars = stock.bars
    for position in positions:
        end = position + cfg.forward_sessions
        if end >= len(bars):
            continue
        future = range(position + 1, end + 1)
        if any(
            stock.calendar_indices[index] != stock.calendar_indices[position] + index - position
            or stock.segment_ids[index] != stock.segment_ids[position]
            for index in future
        ):
            continue
        entry_open = bars[position + 1].open
        returns = [bars[index].close / entry_open - 1.0 for index in future]
        mfes.append(max(returns) * 100.0)
        maes.append(min(returns) * 100.0)
        day10s.append(returns[-1] * 100.0)
        up_hits.append(any(value >= 0.08 for value in returns))
        down_hits.append(any(value <= -0.05 for value in returns))
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
    market_calendar: list[str],
    start: str,
    end: str,
    label: str,
    cfg: Config = CFG,
) -> dict:
    positions = [
        index for index, bar in enumerate(stock.bars) if start <= bar.date <= end
    ]
    market_sessions = sum(start <= day <= end for day in market_calendar)
    returns, gaps, atrs, realized_vols, efficiencies = [], [], [], [], []
    autocorrelation_pairs: list[tuple[float, float]] = []
    position_set = set(positions)

    for position in positions:
        if position >= 1 and stock.segment_ids[position - 1] == stock.segment_ids[position]:
            returns.append(stock.daily_returns[position])
            gaps.append(abs(stock.bars[position].open / stock.bars[position - 1].close - 1.0))
        if (
            position >= cfg.atr_window
            and stock.segment_ids[position - cfg.atr_window] == stock.segment_ids[position]
        ):
            atrs.append(
                statistics.fmean(
                    stock.true_range_ratios[position - cfg.atr_window + 1 : position + 1]
                )
            )
        if (
            position >= cfg.realized_vol_window
            and stock.segment_ids[position - cfg.realized_vol_window]
            == stock.segment_ids[position]
        ):
            window = stock.daily_returns[
                position - cfg.realized_vol_window + 1 : position + 1
            ]
            realized_vols.append(
                statistics.pstdev(window) * math.sqrt(cfg.annualization_sessions)
            )
        if (
            position >= cfg.efficiency_window
            and stock.segment_ids[position - cfg.efficiency_window]
            == stock.segment_ids[position]
        ):
            path = stock.daily_returns[
                position - cfg.efficiency_window + 1 : position + 1
            ]
            denominator = sum(abs(value) for value in path)
            if denominator:
                efficiencies.append(
                    abs(
                        stock.bars[position].close
                        / stock.bars[position - cfg.efficiency_window].close
                        - 1.0
                    )
                    / denominator
                )
        if (
            position >= 2
            and position - 1 in position_set
            and stock.segment_ids[position - 2] == stock.segment_ids[position]
        ):
            autocorrelation_pairs.append(
                (stock.daily_returns[position - 1], stock.daily_returns[position])
            )

    close_values = [stock.bars[position].close for position in positions]
    turnover = [
        stock.bars[position].close * stock.bars[position].volume for position in positions
    ]
    absolute_returns = [abs(value) for value in returns]
    absolute_gaps = [abs(value) for value in gaps]
    result = {
        "period": label,
        "start_date": start,
        "end_date": end,
        "code": stock.code,
        "name": stock.name,
        "sessions": len(positions),
        "market_sessions": market_sessions,
        "coverage": len(positions) / market_sessions if market_sessions else None,
        "median_close": _median(close_values),
        "median_daily_turnover_proxy": _median(turnover),
        "p25_daily_turnover_proxy": _quantile(turnover, 0.25),
        "median_abs_return_pct": None if not absolute_returns else _median(absolute_returns) * 100.0,
        "p90_abs_return_pct": None if not absolute_returns else _quantile(absolute_returns, 0.90) * 100.0,
        "abs_return_ge_5pct_rate": _rate(value >= 0.05 for value in absolute_returns),
        "median_atr14_pct": None if not atrs else _median(atrs) * 100.0,
        "p75_atr14_pct": None if not atrs else _quantile(atrs, 0.75) * 100.0,
        "median_realized_vol20_annualized_pct": None if not realized_vols else _median(realized_vols) * 100.0,
        "median_efficiency20": _median(efficiencies),
        "lag1_daily_return_autocorrelation": _autocorrelation(autocorrelation_pairs),
        "median_abs_overnight_gap_pct": None if not absolute_gaps else _median(absolute_gaps) * 100.0,
        "p95_abs_overnight_gap_pct": None if not absolute_gaps else _quantile(absolute_gaps, 0.95) * 100.0,
        "maximum_close_drawdown_pct": _maximum_drawdown(stock, positions),
    }
    result.update(_forward_diagnostics(stock, positions, cfg))
    return result


def calculate_all_metrics(
    stocks: list[PreparedStock], market_calendar: list[str], cfg: Config = CFG
) -> tuple[list[dict], list[dict]]:
    period_rows = [
        calculate_metrics(stock, market_calendar, start, end, label, cfg)
        for label, start, end in PERIODS
        for stock in sorted(stocks, key=lambda item: cfg.candidates.index(item.code))
    ]
    annual_rows = [
        calculate_metrics(stock, market_calendar, f"{year}0101", f"{year}1231", str(year), cfg)
        for year in range(2020, 2026)
        for stock in sorted(stocks, key=lambda item: cfg.candidates.index(item.code))
    ]
    return period_rows, annual_rows


def _percentiles(values_by_code: dict[str, float], *, higher_is_better: bool) -> dict[str, float]:
    ordered_values = sorted(values_by_code.values())
    denominator = max(1, len(ordered_values) - 1)
    result = {}
    for code, value in values_by_code.items():
        positions = [index for index, item in enumerate(ordered_values) if item == value]
        percentile = statistics.fmean(positions) / denominator
        result[code] = percentile if higher_is_better else 1.0 - percentile
    return result


def _coefficient_of_variation(values: list[float]) -> float:
    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        return math.inf
    mean = statistics.fmean(values)
    return statistics.pstdev(values) / abs(mean) if mean else math.inf


def build_discovery_ranking(
    period_metrics: list[dict], annual_metrics: list[dict], cfg: Config = CFG
) -> list[dict]:
    discovery = {
        row["code"]: row for row in period_metrics if row["period"] == DISCOVERY
    }
    if set(discovery) != set(cfg.candidates):
        raise RuntimeError("discovery metrics do not contain the exact fixed candidate list")
    discovery_annual = [row for row in annual_metrics if row["period"] in {"2020", "2021", "2022"}]
    stability_raw = {}
    for code in cfg.candidates:
        rows = [row for row in discovery_annual if row["code"] == code]
        cvs = [
            _coefficient_of_variation([float(row[field]) for row in rows])
            for field in (
                "median_atr14_pct",
                "median_abs_return_pct",
                "median_daily_turnover_proxy",
            )
        ]
        stability_raw[code] = statistics.fmean(cvs)

    atr_pct = _percentiles({code: float(row["median_atr14_pct"]) for code, row in discovery.items()}, higher_is_better=True)
    return_pct = _percentiles({code: float(row["median_abs_return_pct"]) for code, row in discovery.items()}, higher_is_better=True)
    liquidity = _percentiles({code: float(row["median_daily_turnover_proxy"]) for code, row in discovery.items()}, higher_is_better=True)
    continuity = _percentiles({code: float(row["coverage"]) for code, row in discovery.items()}, higher_is_better=True)
    stability = _percentiles(stability_raw, higher_is_better=False)
    gap_safety = _percentiles({code: float(row["p95_abs_overnight_gap_pct"]) for code, row in discovery.items()}, higher_is_better=False)

    rows = []
    for code in cfg.candidates:
        components = {
            "volatility_opportunity_component": (atr_pct[code] + return_pct[code]) / 2.0,
            "liquidity_component": liquidity[code],
            "continuity_component": continuity[code],
            "behavior_stability_component": stability[code],
            "gap_safety_component": gap_safety[code],
        }
        score = sum(COMPONENT_WEIGHTS[key] * value for key, value in components.items())
        rows.append({
            "code": code,
            "name": discovery[code]["name"],
            "specialist_score": score,
            **components,
            "discovery_median_atr14_pct": discovery[code]["median_atr14_pct"],
            "discovery_median_abs_return_pct": discovery[code]["median_abs_return_pct"],
            "discovery_median_daily_turnover_proxy": discovery[code]["median_daily_turnover_proxy"],
            "discovery_coverage": discovery[code]["coverage"],
            "discovery_p95_abs_overnight_gap_pct": discovery[code]["p95_abs_overnight_gap_pct"],
            "behavior_stability_raw_mean_cv": stability_raw[code],
            "future_outcomes_used_in_candidate_score": False,
        })
    rows.sort(key=lambda row: (-row["specialist_score"], cfg.candidates.index(row["code"])))
    for rank, row in enumerate(rows, start=1):
        row["discovery_rank"] = rank
    return rows


def evaluate_stability(period_metrics: list[dict], cfg: Config = CFG) -> list[dict]:
    indexed = {(row["code"], row["period"]): row for row in period_metrics}
    results = []
    for code in cfg.candidates:
        discovery = indexed[(code, DISCOVERY)]
        for period in LATER_PERIODS:
            later = indexed[(code, period)]
            atr_retention = later["median_atr14_pct"] / discovery["median_atr14_pct"]
            return_retention = later["median_abs_return_pct"] / discovery["median_abs_return_pct"]
            gap_multiple = later["p95_abs_overnight_gap_pct"] / discovery["p95_abs_overnight_gap_pct"]
            checks = {
                "coverage_pass": later["coverage"] >= cfg.stability_minimum_coverage,
                "sessions_pass": later["sessions"] >= cfg.stability_minimum_sessions,
                "atr_retention_pass": cfg.retention_minimum <= atr_retention <= cfg.retention_maximum,
                "abs_return_retention_pass": cfg.retention_minimum <= return_retention <= cfg.retention_maximum,
                "gap_multiple_pass": gap_multiple <= cfg.maximum_gap_multiple,
            }
            passed = all(checks.values())
            results.append({
                "code": code,
                "name": later["name"],
                "period": period,
                "coverage": later["coverage"],
                "sessions": later["sessions"],
                "atr_retention_vs_discovery": atr_retention,
                "median_abs_return_retention_vs_discovery": return_retention,
                "p95_gap_multiple_vs_discovery": gap_multiple,
                **checks,
                "period_stability_pass": passed,
                "status": "PASS" if passed else "STRUCTURAL_STABILITY_FAILED",
            })
    for code in cfg.candidates:
        code_rows = [row for row in results if row["code"] == code]
        cross_pass = len(code_rows) == 2 and all(row["period_stability_pass"] for row in code_rows)
        for row in code_rows:
            row["cross_period_stability_pass"] = cross_pass
    return results


def select_candidate(ranking: list[dict], stability_rows: list[dict]) -> dict | None:
    passed = {
        row["code"]
        for row in stability_rows
        if row.get("cross_period_stability_pass") is True
    }
    for row in sorted(ranking, key=lambda item: item["discovery_rank"]):
        if row["code"] in passed:
            return row
    return None
