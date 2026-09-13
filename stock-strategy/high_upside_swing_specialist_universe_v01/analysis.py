from __future__ import annotations

from collections import defaultdict
import math
import statistics
from typing import Iterable

from surge_event_study_v01.models import PreparedStock

from .config import CFG, SCORE_WEIGHTS, THRESHOLDS, Config


def median(values: Iterable[float]) -> float | None:
    cleaned = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return statistics.median(cleaned) if cleaned else None


def quantile(values: Iterable[float], probability: float) -> float | None:
    cleaned = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not cleaned:
        return None
    position = (len(cleaned) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return cleaned[lower]
    return cleaned[lower] + (cleaned[upper] - cleaned[lower]) * (position - lower)


def rate(values: Iterable[bool]) -> float | None:
    cleaned = list(values)
    return statistics.fmean(1.0 if value else 0.0 for value in cleaned) if cleaned else None


def correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean, right_mean = statistics.fmean(left), statistics.fmean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    denominator = math.sqrt(
        sum((value - left_mean) ** 2 for value in left)
        * sum((value - right_mean) ** 2 for value in right)
    )
    return numerator / denominator if denominator else None


def _continuous(stock: PreparedStock, position: int, lookback: int) -> bool:
    return (
        position >= lookback
        and stock.segment_ids[position - lookback] == stock.segment_ids[position]
        and stock.calendar_indices[position] - stock.calendar_indices[position - lookback] == lookback
    )


def longest_missing_run(stock: PreparedStock, calendar: list[str], start: str, end: str) -> int:
    available = {bar.date for bar in stock.bars if start <= bar.date <= end}
    longest = current = 0
    for day in calendar:
        if not start <= day <= end:
            continue
        if day in available:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def daily_metrics(
    stock: PreparedStock, calendar: list[str], start: str, end: str, label: str, cfg: Config = CFG
) -> dict:
    positions = [position for position, bar in enumerate(stock.bars) if start <= bar.date <= end]
    market_sessions = sum(start <= day <= end for day in calendar)
    returns, absolute_returns, ranges, gaps, atr14, er10, er20 = [], [], [], [], [], [], []
    discontinuities = 0
    for position in positions:
        if position >= 1 and stock.segment_ids[position] == stock.segment_ids[position - 1]:
            daily_return = stock.daily_returns[position]
            returns.append(daily_return)
            absolute_returns.append(abs(daily_return))
            ranges.append((stock.bars[position].high - stock.bars[position].low) / stock.bars[position - 1].close)
            gaps.append(abs(stock.bars[position].open / stock.bars[position - 1].close - 1.0))
        elif position >= 1 and stock.calendar_indices[position] == stock.calendar_indices[position - 1] + 1:
            discontinuities += 1
        if _continuous(stock, position, cfg.atr_window):
            atr14.append(statistics.fmean(stock.true_range_ratios[position - cfg.atr_window + 1 : position + 1]))
        for window, target in ((cfg.efficiency_short_window, er10), (cfg.efficiency_long_window, er20)):
            if _continuous(stock, position, window):
                path = stock.daily_returns[position - window + 1 : position + 1]
                denominator = sum(abs(value) for value in path)
                if denominator > 0:
                    target.append(abs(stock.bars[position].close / stock.bars[position - window].close - 1.0) / denominator)
    turnover = [stock.bars[position].close * stock.bars[position].volume for position in positions]
    return {
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
        "median_close": median(stock.bars[position].close for position in positions),
        "median_daily_turnover_proxy": median(turnover),
        "median_atr14_pct": None if not atr14 else median(atr14) * 100.0,
        "p75_atr14_pct": None if not atr14 else quantile(atr14, 0.75) * 100.0,
        "median_daily_range_pct": None if not ranges else median(ranges) * 100.0,
        "p75_daily_range_pct": None if not ranges else quantile(ranges, 0.75) * 100.0,
        "median_abs_close_return_pct": None if not absolute_returns else median(absolute_returns) * 100.0,
        "p90_abs_close_return_pct": None if not absolute_returns else quantile(absolute_returns, 0.90) * 100.0,
        "abs_return_ge_5pct_rate": rate(value >= 0.05 for value in absolute_returns),
        "median_abs_overnight_gap_pct": None if not gaps else median(gaps) * 100.0,
        "p95_abs_overnight_gap_pct": None if not gaps else quantile(gaps, 0.95) * 100.0,
        "median_er10": median(er10),
        "median_er20": median(er20),
        "p75_er20": quantile(er20, 0.75),
    }


def forward_outcomes(stock: PreparedStock, start: str, end: str, cfg: Config = CFG) -> list[dict]:
    outcomes = []
    for position, bar in enumerate(stock.bars):
        if not start <= bar.date <= end:
            continue
        terminal = position + cfg.forward_sessions
        if terminal >= len(stock.bars) or stock.bars[terminal].date > end:
            continue
        if not _continuous(stock, terminal, cfg.forward_sessions):
            continue
        entry = stock.bars[position + 1]
        if entry.open <= 0 or entry.volume <= 0:
            continue
        path = [stock.bars[index].close / entry.open - 1.0 for index in range(position + 1, terminal + 1)]
        first_down5 = next((day for day, value in enumerate(path, 1) if value <= -0.05), None)
        first_up8 = next((day for day, value in enumerate(path, 1) if value >= 0.08), None)
        first_up10 = next((day for day, value in enumerate(path, 1) if value >= 0.10), None)
        outcomes.append({
            "position": position,
            "calendar_index": stock.calendar_indices[position],
            "signal_date": bar.date,
            "entry_date": entry.date,
            "mfe5": max(path[:5]),
            "mae5": min(path[:5]),
            "mfe10": max(path),
            "mae10": min(path),
            "day5_close_return": path[4],
            "day10_close_return": path[9],
            "abs_day5_close_displacement": abs(path[4]),
            "abs_day10_close_displacement": abs(path[9]),
            "up5_5d": max(path[:5]) >= 0.05,
            "up8_10d": first_up8 is not None,
            "up10_10d": first_up10 is not None,
            "up15_10d": max(path) >= 0.15,
            "up8_before_down5": first_up8 is not None and (first_down5 is None or first_up8 < first_down5),
            "up10_before_down5": first_up10 is not None and (first_down5 is None or first_up10 < first_down5),
            "down5_before_up8": first_down5 is not None and (first_up8 is None or first_down5 < first_up8),
            "down5_before_up10": first_down5 is not None and (first_up10 is None or first_down5 < first_up10),
            "first_up5_day": next((day for day, value in enumerate(path[:5], 1) if value >= 0.05), None),
            "first_up8_day": first_up8,
            "first_up10_day": first_up10,
            "first_up15_day": next((day for day, value in enumerate(path, 1) if value >= 0.15), None),
        })
    return outcomes


def deoverlap_episodes(
    outcomes: list[dict], stock_id: str, stock_name: str, period: str, period_sessions: int,
    calendar_years: int, cfg: Config = CFG,
) -> tuple[list[dict], dict]:
    episode_rows, metrics = [], {}
    outcome_count = len(outcomes)
    for threshold_id, threshold, horizon in THRESHOLDS:
        field = threshold_id.lower()
        hits = [row for row in outcomes if row[field]]
        accepted, blocked_through = [], -1
        for row in hits:
            if row["calendar_index"] <= blocked_through:
                continue
            accepted.append(row)
            blocked_through = row["calendar_index"] + cfg.episode_cooldown_sessions
        prefix = {"UP5_5D": "up5", "UP8_10D": "up8", "UP10_10D": "up10", "UP15_10D": "up15"}[threshold_id]
        metrics.update({
            f"{prefix}_raw_hit_count": len(hits),
            f"{prefix}_raw_hit_rate": len(hits) / outcome_count if outcome_count else None,
            f"{prefix}_deoverlapped_episode_count": len(accepted),
            f"{prefix}_episodes_per_year": len(accepted) / calendar_years if calendar_years else None,
            f"{prefix}_episodes_per_252_sessions": len(accepted) / period_sessions * 252.0 if period_sessions else None,
        })
        hit_day_field = f"first_{prefix}_day"
        for ordinal, row in enumerate(accepted, 1):
            episode_rows.append({
                "stock_id": stock_id,
                "stock_name": stock_name,
                "period": period,
                "threshold_id": threshold_id,
                "threshold_return": threshold,
                "threshold_horizon_sessions": horizon,
                "episode_ordinal": ordinal,
                "signal_date": row["signal_date"],
                "entry_date": row["entry_date"],
                "first_hit_day": row[hit_day_field],
                "cooldown_sessions": cfg.episode_cooldown_sessions,
                "blocked_through_calendar_index": row["calendar_index"] + cfg.episode_cooldown_sessions,
                "mfe10": row["mfe10"],
                "mae10": row["mae10"],
            })
    return episode_rows, metrics


def swing_metrics(
    stock: PreparedStock, daily: dict, start: str, end: str, label: str, calendar_years: int,
    cfg: Config = CFG,
) -> tuple[dict, list[dict], list[dict]]:
    outcomes = forward_outcomes(stock, start, end, cfg)
    episodes, episode_metrics = deoverlap_episodes(
        outcomes, stock.code, stock.name, label, daily["sessions"], calendar_years, cfg
    )
    mfe5 = [row["mfe5"] for row in outcomes]
    mfe10 = [row["mfe10"] for row in outcomes]
    median_abs10 = median(row["abs_day10_close_displacement"] for row in outcomes)
    daily_range = daily["median_daily_range_pct"] / 100.0 if daily["median_daily_range_pct"] is not None else None
    persistence = (
        median_abs10 / (math.sqrt(cfg.forward_sessions) * daily_range)
        if median_abs10 is not None and daily_range and daily_range > 0 else None
    )
    result = {
        "period": label,
        "stock_id": stock.code,
        "stock_name": stock.name,
        "forward_evaluable_observations": len(outcomes),
        "median_mfe5": median(mfe5),
        "median_mae5": median(row["mae5"] for row in outcomes),
        "median_mfe10": median(mfe10),
        "p75_mfe10": quantile(mfe10, 0.75),
        "median_mae10": median(row["mae10"] for row in outcomes),
        "median_day5_close_return": median(row["day5_close_return"] for row in outcomes),
        "median_day10_close_return": median(row["day10_close_return"] for row in outcomes),
        "median_abs_day5_close_displacement": median(row["abs_day5_close_displacement"] for row in outcomes),
        "median_abs_day10_close_displacement": median_abs10,
        "up8_before_down5_rate": rate(row["up8_before_down5"] for row in outcomes),
        "up10_before_down5_rate": rate(row["up10_before_down5"] for row in outcomes),
        "down5_before_up8_rate": rate(row["down5_before_up8"] for row in outcomes),
        "down5_before_up10_rate": rate(row["down5_before_up10"] for row in outcomes),
        "swing_persistence": persistence,
        **episode_metrics,
    }
    return result, episodes, outcomes


def tail_diagnostics(outcomes: list[dict], removal_fraction: float) -> dict:
    removal_count = math.ceil(len(outcomes) * removal_fraction) if removal_fraction else 0
    kept = sorted(outcomes, key=lambda row: (-row["mfe10"], row["signal_date"]))[removal_count:]
    return {
        "removal_fraction": removal_fraction,
        "removed_observations": removal_count,
        "remaining_observations": len(kept),
        "median_mfe10": median(row["mfe10"] for row in kept),
        "p75_mfe10": quantile((row["mfe10"] for row in kept), 0.75),
        "up8_raw_hit_rate": rate(row["up8_10d"] for row in kept),
        "up10_raw_hit_rate": rate(row["up10_10d"] for row in kept),
    }


def tail_dependent(original: dict, top5: dict, cfg: Config = CFG) -> bool:
    def retention(key: str) -> float | None:
        base = original[key]
        return top5[key] / base if base is not None and base > 0 and top5[key] is not None else None
    p75_retention = retention("p75_mfe10")
    up10_retention = retention("up10_raw_hit_rate")
    return bool(
        (p75_retention is not None and p75_retention < cfg.tail_p75_mfe10_minimum_retention)
        or (up10_retention is not None and up10_retention < cfg.tail_up10_rate_minimum_retention)
    )


def eligibility(
    all_stocks: dict[str, list], prepared: list[PreparedStock], calendar: list[str], cfg: Config = CFG
) -> tuple[list[dict], dict[str, dict], dict[str, PreparedStock]]:
    prepared_by_code = {stock.code: stock for stock in prepared}
    discovery_daily, rows = {}, []
    for code in sorted(all_stocks):
        stock = prepared_by_code.get(code)
        name = all_stocks[code][-1].name if all_stocks[code] else ""
        if stock is None:
            rows.append({"stock_id": code, "stock_name": name, "eligible": False, "failure_reasons": "INSUFFICIENT_TOTAL_HISTORY", "data_quality_pass": False})
            continue
        daily = daily_metrics(stock, calendar, "20200101", "20221231", "HISTORICAL_DISCOVERY", cfg)
        discovery_daily[code] = daily
        annual = [daily_metrics(stock, calendar, f"{year}0101", f"{year}1231", str(year), cfg) for year in (2020, 2021, 2022)]
        reasons = []
        for item in annual:
            if item["coverage"] is None or item["coverage"] < cfg.annual_minimum_coverage:
                reasons.append(f"{item['period']}_COVERAGE")
            if item["sessions"] < cfg.annual_minimum_sessions:
                reasons.append(f"{item['period']}_SESSIONS")
        gates = (
            (daily["median_daily_turnover_proxy"] is not None and daily["median_daily_turnover_proxy"] >= cfg.minimum_median_turnover_proxy, "TURNOVER"),
            (daily["median_close"] is not None and daily["median_close"] >= cfg.minimum_median_close, "PRICE"),
            (daily["p95_abs_overnight_gap_pct"] is not None and daily["p95_abs_overnight_gap_pct"] <= cfg.maximum_p95_gap_pct, "P95_GAP"),
            (daily["longest_missing_run_sessions"] <= cfg.maximum_discovery_missing_run_sessions, "LONG_MISSING_RUN"),
            (
                (daily["median_atr14_pct"] is not None and daily["median_atr14_pct"] >= cfg.minimum_median_atr14_pct)
                or (daily["median_daily_range_pct"] is not None and daily["median_daily_range_pct"] >= cfg.minimum_median_daily_range_pct),
                "DAILY_MOVEMENT",
            ),
        )
        reasons.extend(label for passed, label in gates if not passed)
        row = {
            "stock_id": code,
            "stock_name": name,
            "eligible": not reasons,
            "failure_reasons": "|".join(reasons),
            "data_quality_pass": not any(reason.endswith(("COVERAGE", "SESSIONS")) or reason in {"P95_GAP", "LONG_MISSING_RUN"} for reason in reasons),
            **{key: value for key, value in daily.items() if key not in {"period", "stock_id", "stock_name", "start_date", "end_date"}},
        }
        for item in annual:
            row[f"coverage_{item['period']}"] = item["coverage"]
            row[f"sessions_{item['period']}"] = item["sessions"]
        rows.append(row)
    return rows, discovery_daily, prepared_by_code


def repeatability(annual_rows: list[dict], cfg: Config = CFG) -> dict[str, dict]:
    grouped = defaultdict(list)
    for row in annual_rows:
        grouped[row["stock_id"]].append(row)
    result = {}
    for code, rows in grouped.items():
        qualifying = sum(
            row["up8_deoverlapped_episode_count"] >= cfg.repeatability_minimum_up8_episodes
            and row["up10_deoverlapped_episode_count"] >= cfg.repeatability_minimum_up10_episodes
            for row in rows
        )
        result[code] = {
            "repeatability_qualifying_years": qualifying,
            "swing_repeatability_pass": qualifying >= cfg.repeatability_minimum_years,
            "repeatability_status": "PASS" if qualifying >= cfg.repeatability_minimum_years else "ONE_OFF_SWING_STOCK",
        }
    return result


def percentile_credits(rows: list[dict], field: str) -> dict[str, float]:
    ordered = sorted(rows, key=lambda row: (row[field], row["stock_id"]))
    denominator = len(ordered) - 1
    result, position = {}, 0
    while position < len(ordered):
        end = position + 1
        while end < len(ordered) and ordered[end][field] == ordered[position][field]:
            end += 1
        average_rank = (position + end - 1) / 2.0
        credit = average_rank / denominator if denominator > 0 else 1.0
        for index in range(position, end):
            result[ordered[index]["stock_id"]] = credit
        position = end
    return result


def score_discovery(rows: list[dict], cfg: Config = CFG) -> list[dict]:
    required = (
        "up8_episodes_per_252_sessions", "up10_episodes_per_252_sessions",
        "up15_episodes_per_252_sessions", "p75_mfe10", "median_mfe10",
        "up8_before_down5_rate", "swing_persistence", "median_atr14_pct",
        "median_daily_range_pct",
    )
    if any(any(row[field] is None for field in required) for row in rows):
        raise RuntimeError("eligible discovery scoring metrics contain unavailable values")
    credits = {field: percentile_credits(rows, field) for field in required}
    scored = []
    for row in rows:
        code = row["stock_id"]
        components = {
            "up8_episode_component": credits["up8_episodes_per_252_sessions"][code],
            "up10_episode_component": credits["up10_episodes_per_252_sessions"][code],
            "up15_episode_component": credits["up15_episodes_per_252_sessions"][code],
            "p75_mfe10_component": credits["p75_mfe10"][code],
            "median_mfe10_component": credits["median_mfe10"][code],
            "up8_before_down5_component": credits["up8_before_down5_rate"][code],
            "swing_persistence_component": credits["swing_persistence"][code],
            "daily_movement_component": (
                credits["median_atr14_pct"][code] + credits["median_daily_range_pct"][code]
            ) / 2.0,
        }
        scored.append({
            **row,
            **components,
            "swing_score": sum(components[key] * weight for key, weight in SCORE_WEIGHTS.items()),
            "score_period": "2020-2022_ONLY",
            "later_period_data_used_in_score": False,
        })
    return sorted(scored, key=lambda row: (-row["swing_score"], row["stock_id"]))


def return_map(stock: PreparedStock, start: str = "20200101", end: str = "20221231") -> dict[str, float]:
    return {
        stock.bars[position].date: stock.daily_returns[position]
        for position in range(1, len(stock.bars))
        if start <= stock.bars[position].date <= end
        and stock.segment_ids[position] == stock.segment_ids[position - 1]
    }


def pair_correlation(left: PreparedStock, right: PreparedStock) -> float | None:
    left_map, right_map = return_map(left), return_map(right)
    dates = sorted(set(left_map) & set(right_map))
    return correlation([left_map[day] for day in dates], [right_map[day] for day in dates])


def greedy_select(
    rankings: list[dict], prepared_by_code: dict[str, PreparedStock], cfg: Config = CFG
) -> tuple[list[dict], list[dict], list[dict]]:
    selected, high_corr = [], []
    for candidate in rankings:
        if len(selected) >= cfg.maximum_primary_pool_size:
            break
        correlations = [
            (row["stock_id"], pair_correlation(prepared_by_code[candidate["stock_id"]], prepared_by_code[row["stock_id"]]))
            for row in selected
        ]
        if any(value is None for _, value in correlations):
            raise RuntimeError(f"discovery correlation unavailable for {candidate['stock_id']}")
        blockers = [(code, value) for code, value in correlations if value > cfg.maximum_pairwise_correlation]
        if blockers:
            blocking_code, maximum = max(blockers, key=lambda item: (item[1], item[0]))
            high_corr.append({
                "stock_id": candidate["stock_id"], "stock_name": candidate["stock_name"],
                "discovery_rank": candidate["discovery_rank"], "swing_score": candidate["swing_score"],
                "reserve_reason": "PAIRWISE_CORRELATION_GT_0_80", "blocking_selected_stock_id": blocking_code,
                "maximum_correlation_to_selected": maximum,
            })
            continue
        selected.append({**candidate, "primary_selection_order": len(selected) + 1})
    selected_codes = {row["stock_id"] for row in selected}
    high_corr_by_code = {row["stock_id"]: row for row in high_corr}
    reserve = []
    for row in rankings:
        if row["stock_id"] in selected_codes:
            continue
        reserve.append({
            "reserve_order": len(reserve) + 1,
            "stock_id": row["stock_id"], "stock_name": row["stock_name"],
            "discovery_rank": row["discovery_rank"], "swing_score": row["swing_score"],
            "reserve_reason": high_corr_by_code.get(row["stock_id"], {}).get("reserve_reason", "PRIMARY_POOL_CAPACITY"),
            "also_high_correlation_reserve": row["stock_id"] in high_corr_by_code,
        })
        if len(reserve) >= cfg.maximum_reserve_size:
            break
    return selected, high_corr, reserve


def correlation_matrix(codes: list[str], prepared_by_code: dict[str, PreparedStock]) -> tuple[list[dict], dict]:
    cache, pairs, rows = {}, [], []
    for left in codes:
        row = {"stock_id": left}
        for right in codes:
            key = tuple(sorted((left, right)))
            if left == right:
                value = 1.0
            elif key not in cache:
                cache[key] = pair_correlation(prepared_by_code[left], prepared_by_code[right])
                value = cache[key]
                pairs.append(value)
            else:
                value = cache[key]
            row[right] = value
        cache[(left, left)] = 1.0
        rows.append(row)
    average = statistics.fmean(pairs) if pairs else 0.0
    maximum = max(pairs) if pairs else 0.0
    return rows, {"average_pairwise_correlation": average, "maximum_pairwise_correlation": maximum}


def per_stock_pool_correlations(matrix_rows: list[dict], codes: list[str]) -> dict[str, dict]:
    result = {}
    for row in matrix_rows:
        values = [row[code] for code in codes if code != row["stock_id"]]
        result[row["stock_id"]] = {
            "average_pool_correlation": statistics.fmean(values) if values else 0.0,
            "maximum_pool_correlation": max(values) if values else 0.0,
        }
    return result


def later_assessment(discovery: dict, later_daily: dict, later_swing: dict, cfg: Config = CFG) -> dict:
    def retention(later_key: str, discovery_key: str) -> float | None:
        base = discovery[discovery_key]
        return later_swing[later_key] / base if base is not None and base > 0 and later_swing[later_key] is not None else None
    up8 = retention("up8_episodes_per_252_sessions", "up8_episodes_per_252_sessions")
    up10 = retention("up10_episodes_per_252_sessions", "up10_episodes_per_252_sessions")
    mfe = retention("median_mfe10", "median_mfe10")
    normal_liquidity = (
        later_daily["coverage"] is not None and later_daily["coverage"] >= cfg.later_normal_minimum_coverage
        and later_daily["sessions"] >= cfg.later_normal_minimum_sessions
        and later_daily["median_daily_turnover_proxy"] is not None
        and later_daily["median_daily_turnover_proxy"] >= cfg.later_normal_minimum_turnover_proxy
    )
    discovery_gap = discovery["p95_abs_overnight_gap_pct"]
    severe_tradability = (
        later_daily["coverage"] is not None and later_daily["coverage"] >= cfg.severe_minimum_coverage
        and later_daily["sessions"] >= cfg.severe_minimum_sessions
        and later_daily["median_daily_turnover_proxy"] is not None
        and later_daily["median_daily_turnover_proxy"] >= cfg.severe_minimum_turnover_proxy
        and later_daily["longest_missing_run_sessions"] <= cfg.severe_maximum_missing_run_sessions
        and later_daily["p95_abs_overnight_gap_pct"] is not None
        and later_daily["p95_abs_overnight_gap_pct"] <= cfg.severe_maximum_p95_gap_pct
        and (discovery_gap is None or discovery_gap <= 0 or later_daily["p95_abs_overnight_gap_pct"] <= discovery_gap * cfg.severe_maximum_gap_multiple)
    )
    persistence_pass = bool(
        normal_liquidity and up8 is not None and up8 >= cfg.persistent_up8_minimum_retention
        and up10 is not None and up10 >= cfg.persistent_up10_minimum_retention
        and mfe is not None and mfe >= cfg.persistent_mfe10_minimum_retention
    )
    return {
        **later_daily,
        **{key: value for key, value in later_swing.items() if key not in {"period", "stock_id", "stock_name"}},
        "up8_episode_retention": up8,
        "up10_episode_retention": up10,
        "median_mfe10_retention": mfe,
        "normal_liquidity_gate_pass": normal_liquidity,
        "severe_tradability_gate_pass": severe_tradability,
        "persistent_character_gate_pass": persistence_pass,
        "period_character_status": "LOST_TRADABILITY" if not severe_tradability else ("MAINTAINED_HIGH_UPSIDE" if persistence_pass else "REGIME_SHIFT"),
    }


def final_classification(later_rows: list[dict], cfg: Config = CFG) -> str:
    if any(not row["severe_tradability_gate_pass"] for row in later_rows):
        return "LOST_TRADABILITY"
    passes = [row["persistent_character_gate_pass"] for row in later_rows]
    if all(passes):
        return "PERSISTENT_HIGH_UPSIDE_SPECIALIST"
    if any(passes):
        return "REGIME_HIGH_UPSIDE_SPECIALIST"
    expansion = any(
        any(row[key] is not None and row[key] >= cfg.regime_expansion_retention for key in (
            "up8_episode_retention", "up10_episode_retention", "median_mfe10_retention"
        ))
        for row in later_rows
    )
    return "REGIME_HIGH_UPSIDE_SPECIALIST" if expansion else "DISCOVERY_ONLY_HIGH_UPSIDE"
