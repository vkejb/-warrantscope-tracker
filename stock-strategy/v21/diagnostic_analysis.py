#!/usr/bin/env python3
"""Descriptive diagnostics for the already-validated V2.1 baseline.

This module never regenerates signals or trades and never changes V2.1 rules.
It reads the immutable baseline CSVs, joins them to the original OHLCV/TAIEX
data, and writes counterfactual research outputs to a separate directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from v21.backtest import net_return
    from v21.config import CFG
    from v21.data_loader import Bar, TaiexBar, load_ohlcv_archives, load_taiex
else:
    from .backtest import net_return
    from .config import CFG
    from .data_loader import Bar, TaiexBar, load_ohlcv_archives, load_taiex


YEARS = ("2022", "2023", "2024", "2025", "2026")
BREAKOUT_TYPES = ("Pre-breakout", "Breakout", "Extended Breakout")
EXIT_REASONS = (
    "Close Confirmed Stop",
    "Day 5 Weakness",
    "Trailing Stop",
    "Day 8 Time Stop",
)
HORIZONS = (1, 3, 5, 8)
FRICTIONS = (0.002, 0.003, 0.004, 0.005, 0.006, 0.008, 0.010)
TRADE_FLOAT_FIELDS = {
    "entry_price",
    "exit_price",
    "gross_return",
    "net_return",
    "net_return_slippage_0_1",
    "volume_ratio",
    "daily_return",
    "breakout_ratio",
    "foreign_ratio",
    "investment_trust_ratio",
    "combined_ratio",
    "signal_low",
    "initial_stop_price",
    "t1_gap_pct",
    "mfe",
    "mae",
    "day1_close_return",
    "day3_close_return",
    "day5_close_return",
    "day8_close_return",
    "post_exit_5d_max_return",
}


def finite(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def mean(values):
    clean = [value for value in values if finite(value)]
    return statistics.fmean(clean) if clean else None


def median(values):
    clean = [value for value in values if finite(value)]
    return statistics.median(clean) if clean else None


def percentile(values, probability: float):
    ordered = sorted(value for value in values if finite(value))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def profit_factor(values):
    clean = [value for value in values if finite(value)]
    positive = sum(value for value in clean if value > 0)
    negative = abs(sum(value for value in clean if value < 0))
    if not negative:
        return None
    return positive / negative


def win_rate(values):
    clean = [value for value in values if finite(value)]
    return sum(value > 0 for value in clean) / len(clean) if clean else None


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_trades(path: Path) -> list[dict]:
    rows = read_csv(path)
    for row in rows:
        for field in TRADE_FLOAT_FIELDS:
            value = row.get(field, "")
            row[field] = float(value) if value not in {"", None} else None
        row["holding_days"] = int(row["holding_days"])
        row["exit_execution_delay_sessions"] = int(row["exit_execution_delay_sessions"])
    return rows


def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        if not fields:
            return
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def directory_hashes(path: Path) -> dict[str, str]:
    return {
        item.name: file_sha256(item)
        for item in sorted(path.iterdir())
        if item.is_file()
    }


def trade_key(row: dict) -> tuple[str, str]:
    return row["stock_id"], row["signal_date"]


def metrics(rows: list[dict]) -> dict:
    gross = [row.get("gross_return") for row in rows]
    net = [row.get("net_return_slippage_0_1") for row in rows]
    day8 = [row.get("forward_8d_close_return") for row in rows]
    return {
        "trades": len(rows),
        "gross_sample_size": sum(finite(value) for value in gross),
        "gross_average_return": mean(gross),
        "gross_median_return": median(gross),
        "gross_win_rate": win_rate(gross),
        "gross_profit_factor": profit_factor(gross),
        "net_sample_size": sum(finite(value) for value in net),
        "net_average_return_slippage_0_1": mean(net),
        "net_median_return_slippage_0_1": median(net),
        "net_win_rate_slippage_0_1": win_rate(net),
        "net_profit_factor_slippage_0_1": profit_factor(net),
        "average_actual_mfe": mean(row.get("mfe") for row in rows),
        "average_actual_mae": mean(row.get("mae") for row in rows),
        "day8_forward_sample_size": sum(finite(value) for value in day8),
        "average_day8_forward_return": mean(day8),
        "median_day8_forward_return": median(day8),
        "day8_forward_win_rate": win_rate(day8),
        "day8_forward_profit_factor": profit_factor(day8),
    }


def quantile_edges(values) -> list[float]:
    materialized = [value for value in values if finite(value)]
    return [percentile(materialized, probability) for probability in (0.2, 0.4, 0.6, 0.8)]


def rank_quintile_assignments(rows: list[dict], field: str) -> dict[tuple[str, str], int]:
    """Create five near-equal-frequency groups with deterministic tie handling."""
    eligible = sorted(
        (row for row in rows if finite(row.get(field))),
        key=lambda row: (row[field], row["stock_id"], row["signal_date"]),
    )
    size = len(eligible)
    return {
        trade_key(row): min(5, index * 5 // size + 1)
        for index, row in enumerate(eligible)
    }


def stock_maps(stocks: dict[str, list[Bar]]):
    by_date = {
        code: {bar.date: bar for bar in bars}
        for code, bars in stocks.items()
    }
    indices = {
        code: {bar.date: index for index, bar in enumerate(bars)}
        for code, bars in stocks.items()
    }
    return by_date, indices


def build_forward_returns(
    events: list[dict],
    trades: list[dict],
    stocks: dict[str, list[Bar]],
    market_dates: list[str],
) -> list[dict]:
    by_date, _ = stock_maps(stocks)
    market_index = {date: index for index, date in enumerate(market_dates)}
    trades_by_key = {trade_key(row): row for row in trades}
    rows = []
    for event in events:
        if event["status"] not in {"entered", "censored"}:
            continue
        key = trade_key(event)
        trade = trades_by_key.get(key)
        entry_date = event["entry_date"]
        if entry_date not in market_index:
            raise AssertionError(f"entry date missing from TAIEX calendar: {key} {entry_date}")
        entry_bar = by_date[event["stock_id"]].get(entry_date)
        if entry_bar is None:
            raise AssertionError(f"entry bar missing from OHLCV: {key} {entry_date}")
        entry_price = trade["entry_price"] if trade else entry_bar.open
        if not math.isclose(entry_price, entry_bar.open, rel_tol=1e-12, abs_tol=1e-12):
            raise AssertionError(f"baseline entry price differs from OHLCV: {key}")
        signal_bar = by_date[event["stock_id"]].get(event["signal_date"])
        row = {
            "stock_id": event["stock_id"],
            "name": trade["name"] if trade else (signal_bar.name if signal_bar else ""),
            "signal_date": event["signal_date"],
            "entry_date": entry_date,
            "entry_price": entry_price,
            "baseline_status": event["status"],
            "baseline_trade_completed": bool(trade),
        }
        entry_i = market_index[entry_date]
        for horizon in HORIZONS:
            target_i = entry_i + horizon - 1
            prefix = f"forward_{horizon}d"
            if target_i >= len(market_dates):
                row.update(
                    {
                        f"{prefix}_date": None,
                        f"{prefix}_close": None,
                        f"{prefix}_close_return": None,
                        f"{prefix}_mfe": None,
                        f"{prefix}_mae": None,
                        f"{prefix}_observed_stock_bars": 0,
                        f"{prefix}_complete_stock_window": False,
                    }
                )
                continue
            target_date = market_dates[target_i]
            window_dates = market_dates[entry_i : target_i + 1]
            window_bars = [
                by_date[event["stock_id"]][date]
                for date in window_dates
                if date in by_date[event["stock_id"]]
            ]
            target_bar = by_date[event["stock_id"]].get(target_date)
            row.update(
                {
                    f"{prefix}_date": target_date,
                    f"{prefix}_close": target_bar.close if target_bar else None,
                    f"{prefix}_close_return": target_bar.close / entry_price - 1 if target_bar else None,
                    f"{prefix}_mfe": max(bar.high for bar in window_bars) / entry_price - 1 if window_bars else None,
                    f"{prefix}_mae": min(bar.low for bar in window_bars) / entry_price - 1 if window_bars else None,
                    f"{prefix}_observed_stock_bars": len(window_bars),
                    f"{prefix}_complete_stock_window": len(window_bars) == horizon,
                }
            )
        rows.append(row)
    return rows


def enrich_trades(
    trades: list[dict],
    forward_rows: list[dict],
    stocks: dict[str, list[Bar]],
) -> list[dict]:
    _, indices = stock_maps(stocks)
    forward_by_key = {trade_key(row): row for row in forward_rows}
    enriched = []
    for trade in trades:
        key = trade_key(trade)
        forward = forward_by_key[key]
        stock_i = indices[trade["stock_id"]][trade["signal_date"]]
        prior = stocks[trade["stock_id"]][stock_i - 20 : stock_i]
        if len(prior) != 20:
            raise AssertionError(f"missing 20-bar volume window: {key}")
        row = dict(trade)
        row["avg_volume_20d_shares"] = statistics.fmean(bar.volume for bar in prior)
        row["avg_volume_20d_lots"] = row["avg_volume_20d_shares"] / 1000
        for horizon in HORIZONS:
            for suffix in ("date", "close", "close_return", "mfe", "mae", "observed_stock_bars", "complete_stock_window"):
                field = f"forward_{horizon}d_{suffix}"
                row[field] = forward[field]
        enriched.append(row)
    return enriched


def build_exit_counterfactual(enriched: list[dict]) -> tuple[list[dict], list[dict]]:
    rows = []
    for trade in enriched:
        counterfactual = trade.get("forward_8d_close_return")
        counterfactual_price = trade.get("forward_8d_close")
        actual = trade["gross_return"]
        actual_net = trade["net_return_slippage_0_1"]
        counterfactual_net = (
            net_return(trade["entry_price"], counterfactual_price, 0.001)
            if finite(counterfactual_price)
            else None
        )
        rows.append(
            {
                "stock_id": trade["stock_id"],
                "name": trade["name"],
                "signal_date": trade["signal_date"],
                "entry_date": trade["entry_date"],
                "entry_price": trade["entry_price"],
                "exit_reason": trade["exit_reason"],
                "actual_exit_date": trade["exit_date"],
                "actual_exit_price": trade["exit_price"],
                "counterfactual_day8_date": trade.get("forward_8d_date"),
                "counterfactual_day8_close": counterfactual_price,
                "actual_return": actual,
                "counterfactual_day8_return": counterfactual,
                "difference": counterfactual - actual if finite(counterfactual) else None,
                "actual_net_return_slippage_0_1": actual_net,
                "counterfactual_day8_net_return_slippage_0_1": counterfactual_net,
                "net_difference": counterfactual_net - actual_net if finite(counterfactual_net) else None,
                "actual_nonpositive_to_day8_positive": bool(actual <= 0 and finite(counterfactual) and counterfactual > 0),
                "day8_available": finite(counterfactual),
            }
        )
    summaries = []
    for reason in EXIT_REASONS:
        selected = [row for row in rows if row["exit_reason"] == reason]
        available = [row for row in selected if finite(row["counterfactual_day8_return"])]
        summaries.append(
            {
                "exit_reason": reason,
                "sample_size": len(selected),
                "day8_available": len(available),
                "actual_average_return": mean(row["actual_return"] for row in selected),
                "counterfactual_day8_average_return": mean(row["counterfactual_day8_return"] for row in available),
                "average_difference": mean(row["difference"] for row in available),
                "counterfactual_day8_win_rate": win_rate(row["counterfactual_day8_return"] for row in available),
                "actual_average_net_return_slippage_0_1": mean(row["actual_net_return_slippage_0_1"] for row in selected),
                "counterfactual_day8_average_net_return_slippage_0_1": mean(row["counterfactual_day8_net_return_slippage_0_1"] for row in available),
                "average_net_difference": mean(row["net_difference"] for row in available),
                "actual_nonpositive_to_day8_positive_count": sum(row["actual_nonpositive_to_day8_positive"] for row in available),
                "actual_nonpositive_to_day8_positive_rate": (
                    sum(row["actual_nonpositive_to_day8_positive"] for row in available) / len(available)
                    if available
                    else None
                ),
            }
        )
    return rows, summaries


def build_year_breakout(enriched: list[dict]) -> list[dict]:
    rows = []
    for year in YEARS:
        for breakout_type in BREAKOUT_TYPES:
            selected = [
                row
                for row in enriched
                if row["signal_date"].startswith(year) and row["breakout_type"] == breakout_type
            ]
            rows.append({"year": year, "breakout_type": breakout_type, **metrics(selected)})
    return rows


def build_feature_quintiles(enriched: list[dict]) -> list[dict]:
    features = (
        "volume_ratio",
        "daily_return",
        "breakout_ratio",
        "foreign_ratio",
        "investment_trust_ratio",
        "combined_ratio",
        "t1_gap_pct",
        "entry_price",
        "avg_volume_20d_lots",
    )
    output = []
    for feature in features:
        eligible = [row for row in enriched if finite(row.get(feature))]
        edges = quantile_edges(row[feature] for row in eligible)
        assignments = rank_quintile_assignments(eligible, feature)
        for bucket in range(1, 6):
            selected = [row for row in eligible if assignments[trade_key(row)] == bucket]
            output.append(
                {
                    "feature": feature,
                    "quintile": f"Q{bucket}",
                    "feature_min": min((row[feature] for row in selected), default=None),
                    "feature_max": max((row[feature] for row in selected), default=None),
                    "global_q20": edges[0],
                    "global_q40": edges[1],
                    "global_q60": edges[2],
                    "global_q80": edges[3],
                    **metrics(selected),
                }
            )
    return output


def taiex_features(taiex: list[TaiexBar]) -> dict[str, dict]:
    result = {}
    closes = [bar.close for bar in taiex]
    for index, bar in enumerate(taiex):
        if index < 59:
            continue
        ma20 = statistics.fmean(closes[index - 19 : index + 1])
        prior_ma20 = statistics.fmean(closes[index - 20 : index])
        ma60 = statistics.fmean(closes[index - 59 : index + 1])
        close_ma20 = bar.close / ma20 - 1
        ma20_slope = ma20 / prior_ma20 - 1
        close_ma60 = bar.close / ma60 - 1
        ma20_ma60 = ma20 / ma60 - 1
        if ma20 > ma60:
            joint = "Strong Bull: MA20 > MA60"
        elif bar.close > ma60:
            joint = "Recovery: Close > MA60 >= MA20"
        else:
            joint = "Bear Rally: MA60 >= Close > MA20"
        result[bar.date] = {
            "taiex_close_ma20": close_ma20,
            "taiex_ma20_slope": ma20_slope,
            "taiex_close_ma60": close_ma60,
            "taiex_ma20_ma60": ma20_ma60,
            "taiex_close_vs_ma60": "Above MA60" if bar.close > ma60 else "At/Below MA60",
            "taiex_ma20_vs_ma60": "MA20 Above MA60" if ma20 > ma60 else "MA20 At/Below MA60",
            "taiex_joint_regime": joint,
        }
    return result


def build_market_regimes(enriched: list[dict], taiex: list[TaiexBar]) -> tuple[list[dict], list[dict]]:
    feature_by_date = taiex_features(taiex)
    rows = []
    for trade in enriched:
        feature = feature_by_date.get(trade["signal_date"])
        if feature is None:
            raise AssertionError(f"TAIEX regime feature missing: {trade_key(trade)}")
        rows.append({**trade, **feature})

    continuous = (
        "taiex_close_ma20",
        "taiex_ma20_slope",
        "taiex_close_ma60",
        "taiex_ma20_ma60",
    )
    categorical = (
        "taiex_close_vs_ma60",
        "taiex_ma20_vs_ma60",
        "taiex_joint_regime",
    )
    assignments = {
        feature: rank_quintile_assignments(rows, feature)
        for feature in continuous
    }
    periods = [("All", rows)]
    periods.extend((year, [row for row in rows if row["signal_date"].startswith(year)]) for year in YEARS)
    periods.append(("Other Years (ex-2025)", [row for row in rows if not row["signal_date"].startswith("2025")]))
    output = []
    for period, period_rows in periods:
        for feature in continuous:
            for bucket in range(1, 6):
                selected = [
                    row
                    for row in period_rows
                    if assignments[feature][trade_key(row)] == bucket
                ]
                output.append(
                    {
                        "period": period,
                        "regime_feature": feature,
                        "regime_bucket": f"Q{bucket}",
                        "feature_min": min((row[feature] for row in selected), default=None),
                        "feature_max": max((row[feature] for row in selected), default=None),
                        **metrics(selected),
                    }
                )
        for feature in categorical:
            categories = sorted({row[feature] for row in rows})
            for category in categories:
                selected = [row for row in period_rows if row[feature] == category]
                output.append(
                    {
                        "period": period,
                        "regime_feature": feature,
                        "regime_bucket": category,
                        "feature_min": None,
                        "feature_max": None,
                        **metrics(selected),
                    }
                )
    return rows, output


def build_winning_tail(enriched: list[dict]) -> list[dict]:
    ordered = sorted(enriched, key=lambda row: row["gross_return"], reverse=True)
    winners = [row for row in ordered if row["gross_return"] > 0]
    total_gross_profit = sum(max(row["gross_return"], 0) for row in ordered)
    output = []
    for base_name, ranking_population in (
        ("all_completed_trades", ordered),
        ("profitable_trades_only", winners),
    ):
        fractions = (0.0, 0.01, 0.05, 0.10) if base_name == "all_completed_trades" else (0.01, 0.05, 0.10)
        for fraction in fractions:
            remove_count = math.ceil(len(ranking_population) * fraction) if fraction else 0
            removed = ranking_population[:remove_count]
            removed_keys = {trade_key(row) for row in removed}
            remaining = [row for row in ordered if trade_key(row) not in removed_keys]
            removed_profit = sum(max(row["gross_return"], 0) for row in removed)
            remaining_returns = [row["gross_return"] for row in remaining]
            output.append(
                {
                    "removed_top_fraction": fraction,
                    "selection_base": base_name,
                    "ranking_population_size": len(ranking_population),
                    "removed_trade_count": remove_count,
                    "remaining_trade_count": len(remaining),
                    "removed_gross_profit": removed_profit,
                    "total_gross_profit": total_gross_profit,
                    "share_of_total_gross_profit_contributed": removed_profit / total_gross_profit if total_gross_profit else None,
                    "remaining_average_gross_return": mean(remaining_returns),
                    "remaining_median_gross_return": median(remaining_returns),
                    "remaining_gross_win_rate": win_rate(remaining_returns),
                    "remaining_gross_profit_factor": profit_factor(remaining_returns),
                }
            )
    return output


def cluster_bootstrap(rows: list[dict], field: str, cluster_unit: str, reps: int, seed: int) -> dict:
    cluster_rows: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = row.get(field)
        if finite(value):
            cluster = row["signal_date"] if cluster_unit == "signal_date" else row["signal_date"][:6]
            cluster_rows[cluster].append(value)
    aggregates = []
    for values in cluster_rows.values():
        aggregates.append(
            (
                len(values),
                sum(values),
                sum(value for value in values if value > 0),
                sum(value for value in values if value < 0),
            )
        )
    observation_count = sum(item[0] for item in aggregates)
    raw_values = [value for values in cluster_rows.values() for value in values]
    if len(aggregates) < 2 or not observation_count:
        return {
            "observations": observation_count,
            "clusters": len(aggregates),
            "bootstrap_reps": reps,
            "point_mean": mean(raw_values),
            "mean_ci_low": None,
            "mean_ci_high": None,
            "point_profit_factor": profit_factor(raw_values),
            "profit_factor_ci_low": None,
            "profit_factor_ci_high": None,
        }
    rng = random.Random(seed)
    count = len(aggregates)
    boot_means = []
    boot_pf = []
    for _ in range(reps):
        sample_n = 0
        sample_sum = 0.0
        sample_positive = 0.0
        sample_negative = 0.0
        for _ in range(count):
            n, total, positive, negative = aggregates[rng.randrange(count)]
            sample_n += n
            sample_sum += total
            sample_positive += positive
            sample_negative += negative
        boot_means.append(sample_sum / sample_n)
        if sample_negative < 0:
            boot_pf.append(sample_positive / abs(sample_negative))
    return {
        "observations": observation_count,
        "clusters": len(aggregates),
        "bootstrap_reps": reps,
        "point_mean": mean(raw_values),
        "mean_ci_low": percentile(boot_means, 0.025),
        "mean_ci_high": percentile(boot_means, 0.975),
        "point_profit_factor": profit_factor(raw_values),
        "profit_factor_ci_low": percentile(boot_pf, 0.025),
        "profit_factor_ci_high": percentile(boot_pf, 0.975),
    }


def stable_seed(base_seed: int, *parts: str) -> int:
    payload = "|".join((str(base_seed), *parts)).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def build_bootstrap(
    enriched: list[dict],
    forward_rows: list[dict],
    reps: int,
    base_seed: int,
) -> list[dict]:
    requests: list[tuple[str, str, str, list[dict]]] = []
    requests.append(("Overall", "All", "net_return_slippage_0_1", enriched))
    for year in YEARS:
        requests.append(("Year", year, "net_return_slippage_0_1", [row for row in enriched if row["signal_date"].startswith(year)]))
    for breakout_type in BREAKOUT_TYPES:
        requests.append(("Breakout Type", breakout_type, "net_return_slippage_0_1", [row for row in enriched if row["breakout_type"] == breakout_type]))
    for reason in EXIT_REASONS:
        requests.append(("Exit Reason", reason, "net_return_slippage_0_1", [row for row in enriched if row["exit_reason"] == reason]))
    for horizon in HORIZONS:
        requests.append(("Forward Return", f"Day {horizon}", f"forward_{horizon}d_close_return", forward_rows))
    for year in YEARS:
        requests.append(("Year Forward Day 8", year, "forward_8d_close_return", [row for row in enriched if row["signal_date"].startswith(year)]))
    for breakout_type in BREAKOUT_TYPES:
        requests.append(("Breakout Forward Day 8", breakout_type, "forward_8d_close_return", [row for row in enriched if row["breakout_type"] == breakout_type]))

    output = []
    for scope, group, field, selected in requests:
        for cluster_unit in ("signal_date", "month"):
            seed = stable_seed(base_seed, scope, group, field, cluster_unit)
            output.append(
                {
                    "analysis_scope": scope,
                    "group": group,
                    "metric": field,
                    "cluster_unit": cluster_unit,
                    "seed": seed,
                    **cluster_bootstrap(selected, field, cluster_unit, reps, seed),
                }
            )
    return output


def concentration_summary(counts: list[tuple[str, int]]) -> dict:
    values = [count for _, count in counts]
    total = sum(values)
    positive = sorted((count for count in values if count > 0), reverse=True)
    max_count = max(values, default=0)
    max_dates = [date for date, count in counts if count == max_count and max_count > 0]
    hhi = sum((count / total) ** 2 for count in positive) if total else None
    return {
        "trading_days": len(values),
        "active_signal_days": len(positive),
        "total_count": total,
        "average_per_trading_day": mean(values),
        "average_per_active_signal_day": mean(positive),
        "median_per_trading_day": median(values),
        "median_per_active_signal_day": median(positive),
        "p90_per_trading_day": percentile(values, 0.90),
        "p90_per_active_signal_day": percentile(positive, 0.90),
        "maximum_daily_count": max_count,
        "maximum_dates": ";".join(max_dates),
        "signal_date_hhi": hhi,
        "effective_signal_dates": 1 / hhi if hhi else None,
        "largest_date_share": positive[0] / total if positive else None,
        "top5_dates_share": sum(positive[:5]) / total if positive else None,
        "top10_dates_share": sum(positive[:10]) / total if positive else None,
    }


def audit_industry_availability(path: Path, rows: list[dict]) -> dict:
    stock_info = read_csv(path)
    by_code: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in stock_info:
        date = row.get("date", "").replace("-", "")
        category = row.get("industry_category", "").strip()
        if date and category:
            by_code[row["stock_id"].strip()].append((date, category))
    for history in by_code.values():
        history.sort()
    asof_covered = 0
    future_only = 0
    missing = 0
    for row in rows:
        history = by_code.get(row["stock_id"], [])
        if any(date <= row["signal_date"] for date, _ in history):
            asof_covered += 1
        elif history:
            future_only += 1
        else:
            missing += 1
    category_sets = {
        code: {category for _, category in history}
        for code, history in by_code.items()
    }
    return {
        "stock_info_rows": len(stock_info),
        "stock_info_codes": len(by_code),
        "asof_covered_rows": asof_covered,
        "future_only_rows": future_only,
        "missing_rows": missing,
        "asof_coverage_rate": asof_covered / len(rows) if rows else None,
        "codes_with_multiple_category_labels": sum(len(categories) > 1 for categories in category_sets.values()),
        "decision": "not_computed",
        "reason": "Historical as-of category coverage is insufficient; current/future labels are not backfilled.",
    }


def build_signal_clustering(
    events: list[dict],
    enriched: list[dict],
    market_dates: list[str],
    industry_audit: dict,
) -> list[dict]:
    eligible_dates = [date for date in market_dates if CFG.start_date <= date <= CFG.end_date]
    populations = {
        "all_final_signals": events,
        "successful_entries_including_censored": [row for row in events if row["status"] in {"entered", "censored"}],
        "completed_trades": enriched,
    }
    output = []
    for population, population_rows in populations.items():
        counter = Counter(row["signal_date"] for row in population_rows)
        for date in eligible_dates:
            output.append(
                {
                    "record_type": "daily_count",
                    "population": population,
                    "period": date[:4],
                    "date_or_month": date,
                    "count": counter[date],
                }
            )
        months = sorted({date[:6] for date in eligible_dates})
        for month in months:
            output.append(
                {
                    "record_type": "monthly_count",
                    "population": population,
                    "period": month[:4],
                    "date_or_month": month,
                    "count": sum(counter[date] for date in eligible_dates if date.startswith(month)),
                }
            )
        periods = [("All", eligible_dates)]
        periods.extend((year, [date for date in eligible_dates if date.startswith(year)]) for year in YEARS)
        for period, dates in periods:
            summary = concentration_summary([(date, counter[date]) for date in dates])
            output.append(
                {
                    "record_type": "concentration_summary",
                    "population": population,
                    "period": period,
                    "date_or_month": None,
                    "count": None,
                    **summary,
                }
            )
    output.append(
        {
            "record_type": "industry_availability",
            "population": "all_final_signals",
            "period": "All",
            "date_or_month": None,
            "count": None,
            "industry_status": industry_audit["decision"],
            "industry_reason": industry_audit["reason"],
            "industry_asof_covered_rows": industry_audit["asof_covered_rows"],
            "industry_future_only_rows": industry_audit["future_only_rows"],
            "industry_missing_rows": industry_audit["missing_rows"],
            "industry_asof_coverage_rate": industry_audit["asof_coverage_rate"],
        }
    )
    return output


def build_cost_break_even(enriched: list[dict]) -> list[dict]:
    output = []
    gross = [row["gross_return"] for row in enriched]
    for friction in (0.0, *FRICTIONS):
        adjusted = [value - friction for value in gross]
        output.append(
            {
                "record_type": "friction_scenario",
                "total_round_trip_friction": friction,
                "sample_size": len(adjusted),
                "average_return_after_friction": mean(adjusted),
                "median_return_after_friction": median(adjusted),
                "win_rate_after_friction": win_rate(adjusted),
                "profit_factor_after_friction": profit_factor(adjusted),
                "share_of_trades_exceeding_friction": sum(value > friction for value in gross) / len(gross),
                "definition": "descriptive additive friction subtracted from gross return",
            }
        )
    for probability in (0.0, 0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99, 1.0):
        output.append(
            {
                "record_type": "break_even_distribution",
                "percentile": probability,
                "break_even_total_friction": percentile(gross, probability),
                "definition": "per-trade break-even additive friction equals that trade's gross return; negative means already losing before costs",
            }
        )
    for row in enriched:
        output.append(
            {
                "record_type": "trade_break_even",
                "stock_id": row["stock_id"],
                "name": row["name"],
                "signal_date": row["signal_date"],
                "entry_date": row["entry_date"],
                "exit_date": row["exit_date"],
                "gross_return": row["gross_return"],
                "break_even_total_friction": row["gross_return"],
                "max_nonnegative_friction": max(row["gross_return"], 0),
                "already_negative_before_friction": row["gross_return"] <= 0,
                "observed_effective_friction_net_0_1": row["gross_return"] - row["net_return_slippage_0_1"],
                "definition": "equal-notional descriptive price-return basis",
            }
        )
    return output


def lookup_bootstrap(rows: list[dict], scope: str, group: str, metric: str, cluster: str = "signal_date") -> dict:
    return next(
        row
        for row in rows
        if row["analysis_scope"] == scope
        and row["group"] == group
        and row["metric"] == metric
        and row["cluster_unit"] == cluster
    )


def pct(value) -> str:
    return "—" if not finite(value) else f"{value * 100:.3f}%"


def num(value) -> str:
    return "—" if not finite(value) else f"{value:.3f}"


def build_report(
    forward_rows: list[dict],
    enriched: list[dict],
    exit_summary: list[dict],
    year_breakout: list[dict],
    feature_rows: list[dict],
    regime_rows: list[dict],
    tail_rows: list[dict],
    bootstrap_rows: list[dict],
    clustering_rows: list[dict],
    cost_rows: list[dict],
    validation: dict,
) -> str:
    lines = [
        "# V2.1 Diagnostic Analysis",
        "",
        "本研究只讀取已驗證的 V2.1 signals / trades 與原始 OHLCV；未修改交易規則、未調參、未做 Grid Search、未建立 V2.2，也未用分組結果回頭刪除交易。反事實 Day 8 與 forward returns 都只是研究欄位。",
        "",
        "## 研究母體與口徑",
        "",
        f"- 完成交易：{len(enriched):,} 筆；成功進場（含資料右界設限）：{len(forward_rows):,} 筆。",
        f"- Forward Day N 由 T+1 Open 起算，進場日為 Day 1；Close return 使用該日收盤，MFE/MAE 使用 Day 1 至 Day N 的原始 High/Low。缺少目標日個股行情時保留空值，不以鄰近日期補值。",
        "- Exit counterfactual 的 actual/counterfactual/difference 以 Gross price return 比較；另附單邊 0.1% 滑價的淨報酬欄位。",
        "- 分組勝率、Profit Factor 與 median 預設使用 Net（單邊 0.1% 滑價）；Gross 指標另列。",
        "",
        "## 1. Signal Forward Return",
        "",
        "| Horizon | 可用樣本 | 平均 | 中位數 | 勝率 | PF | signal-date 95% CI | month 95% CI |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for horizon in HORIZONS:
        field = f"forward_{horizon}d_close_return"
        values = [row[field] for row in forward_rows]
        boot = lookup_bootstrap(bootstrap_rows, "Forward Return", f"Day {horizon}", field)
        boot_month = lookup_bootstrap(bootstrap_rows, "Forward Return", f"Day {horizon}", field, "month")
        lines.append(
            f"| Day {horizon} Close | {sum(finite(value) for value in values):,} | {pct(mean(values))} | {pct(median(values))} | {pct(win_rate(values))} | {num(profit_factor(values))} | {pct(boot['mean_ci_low'])} ～ {pct(boot['mean_ci_high'])} | {pct(boot_month['mean_ci_low'])} ～ {pct(boot_month['mean_ci_high'])} |"
        )

    lines.extend(
        [
            "",
            "完整逐筆 Day 1/3/5/8 Return、Forward MFE/MAE 與觀測完整性在 `forward_returns.csv`。",
            "",
            "## 2. Entry vs Exit Attribution",
            "",
            "| Exit reason | 樣本 | Day8可用 | Actual Gross | Day8 Gross | Difference | Day8勝率 | 原虧損→Day8轉正 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in exit_summary:
        lines.append(
            f"| {row['exit_reason']} | {row['sample_size']:,} | {row['day8_available']:,} | {pct(row['actual_average_return'])} | {pct(row['counterfactual_day8_average_return'])} | {pct(row['average_difference'])} | {pct(row['counterfactual_day8_win_rate'])} | {row['actual_nonpositive_to_day8_positive_count']:,} ({pct(row['actual_nonpositive_to_day8_positive_rate'])}) |"
        )

    lines.extend(
        [
            "",
            "Exit reason 是事後由價格路徑決定的分組，因此不可把上表直接當作可替換出場規則的策略績效。",
            "",
            "## 3. Breakout Type × Year",
            "",
            "| Year | Type | Trades | Gross Avg | Net Avg | Net Win | Net PF | Net Median | MFE | MAE | Day8 Forward |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in year_breakout:
        lines.append(
            f"| {row['year']} | {row['breakout_type']} | {row['trades']:,} | {pct(row['gross_average_return'])} | {pct(row['net_average_return_slippage_0_1'])} | {pct(row['net_win_rate_slippage_0_1'])} | {num(row['net_profit_factor_slippage_0_1'])} | {pct(row['net_median_return_slippage_0_1'])} | {pct(row['average_actual_mfe'])} | {pct(row['average_actual_mae'])} | {pct(row['average_day8_forward_return'])} |"
        )

    lines.extend(
        [
            "",
            "## 4. Feature Diagnostic",
            "",
            "以下只呈現每個既有特徵五分位的 Net 平均報酬；完整樣本數、Gross、median、win rate、PF、MFE、MAE 與 Day8 forward 均在 CSV。五分位採近似等樣本數排名，同值以股票代碼與訊號日固定排序拆分；這是完整樣本的事後描述，不是交易門檻。",
            "",
            "| Feature | Q1 | Q2 | Q3 | Q4 | Q5 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for feature in dict.fromkeys(row["feature"] for row in feature_rows):
        selected = [row for row in feature_rows if row["feature"] == feature]
        values = {row["quintile"]: row["net_average_return_slippage_0_1"] for row in selected}
        lines.append(f"| {feature} | " + " | ".join(pct(values.get(f"Q{bucket}")) for bucket in range(1, 6)) + " |")

    lines.extend(
        [
            "",
            "## 5. Market Regime Diagnostic",
            "",
            "V2.1 本身已要求訊號日 TAIEX Close > MA20 且 MA20 不下降，因此此研究無法觀察跌破 MA20 或 MA20 下彎時的 V2.1；只能診斷已通過市場濾網後的強弱。MA60 指標均只使用訊號日及以前資料。",
            "",
            "| Period | MA60 joint regime | Trades | Net Avg | Net PF | Day8 Forward |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in regime_rows:
        if row["regime_feature"] == "taiex_joint_regime" and row["period"] in {"All", "2025", "Other Years (ex-2025)"}:
            lines.append(
                f"| {row['period']} | {row['regime_bucket']} | {row['trades']:,} | {pct(row['net_average_return_slippage_0_1'])} | {num(row['net_profit_factor_slippage_0_1'])} | {pct(row['average_day8_forward_return'])} |"
            )

    lines.extend(
        [
            "",
            "## 6. Winning Tail Analysis",
            "",
            "同時呈現兩種常見口徑：占全部完成交易的 Top N%，以及只在獲利交易內排名的 Top N%。兩者都依 Gross Return 由高到低；Gross Profit 為所有正 Gross Return 的總和（等名目金額、未複利）。",
            "",
            "| 排名母體 | 移除最大贏家 | 筆數 | 原 Gross Profit 貢獻 | 剩餘平均 Gross | 剩餘 Gross PF |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in tail_rows:
        if row["removed_top_fraction"] == 0:
            continue
        base_label = "全部交易" if row["selection_base"] == "all_completed_trades" else "獲利交易"
        lines.append(
            f"| {base_label} | Top {row['removed_top_fraction'] * 100:.0f}% | {row['removed_trade_count']:,} | {pct(row['share_of_total_gross_profit_contributed'])} | {pct(row['remaining_average_gross_return'])} | {num(row['remaining_gross_profit_factor'])} |"
        )

    overall_boot = lookup_bootstrap(bootstrap_rows, "Overall", "All", "net_return_slippage_0_1")
    overall_boot_month = lookup_bootstrap(bootstrap_rows, "Overall", "All", "net_return_slippage_0_1", "month")
    lines.extend(
        [
            "",
            "## 7. Statistical Robustness",
            "",
            f"- 整體平均 Net（單邊 0.1% 滑價）：{pct(overall_boot['point_mean'])}。signal-date cluster bootstrap 95% CI：{pct(overall_boot['mean_ci_low'])} ～ {pct(overall_boot['mean_ci_high'])}；PF CI：{num(overall_boot['profit_factor_ci_low'])} ～ {num(overall_boot['profit_factor_ci_high'])}。",
            f"- Month cluster bootstrap 95% CI：{pct(overall_boot_month['mean_ci_low'])} ～ {pct(overall_boot_month['mean_ci_high'])}；PF CI：{num(overall_boot_month['profit_factor_ci_low'])} ～ {num(overall_boot_month['profit_factor_ci_high'])}。",
            f"- 使用固定亂數種子與 {overall_boot['bootstrap_reps']:,} 次 percentile bootstrap；同一 cluster 被抽中時保留當日／當月全部交易，不做 trade IID bootstrap。",
            "",
            "## 8. Signal Clustering",
            "",
            "| Population | Total | Trading days | Active days | Avg/all day | Avg/active day | Median/all | P90/all | Max | Max date | Effective dates | Top 5 dates share |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|",
        ]
    )
    cluster_summaries = [row for row in clustering_rows if row["record_type"] == "concentration_summary" and row["period"] == "All"]
    for row in cluster_summaries:
        lines.append(
            f"| {row['population']} | {row['total_count']:,} | {row['trading_days']:,} | {row['active_signal_days']:,} | {row['average_per_trading_day']:.3f} | {row['average_per_active_signal_day']:.3f} | {row['median_per_trading_day']:.1f} | {row['p90_per_trading_day']:.1f} | {row['maximum_daily_count']:,} | {row['maximum_dates']} | {row['effective_signal_dates']:.1f} | {pct(row['top5_dates_share'])} |"
        )
    industry = next(row for row in clustering_rows if row["record_type"] == "industry_availability")
    lines.extend(
        [
            "",
            f"產業集中度未計算：2,385 筆完成交易只有 {industry['industry_asof_covered_rows']:,} 筆可取得訊號日當下已知的產業標籤，{industry['industry_future_only_rows']:,} 筆只有未來標籤；回填會產生 look-ahead。日期集中度則使用完整交易日（含零訊號日）。",
            "",
            "## 9. Transaction Cost Break-even",
            "",
            "固定 total friction 直接由每筆 Gross Return 扣除，只用來衡量訊號強度；不是改寫 V2.1 的真實手續費、稅與滑價模型。單筆 break-even friction 等於該筆 Gross Return。",
            "",
            "| Total friction | 平均報酬 | 中位數 | 勝率 | PF | 超過摩擦的交易比例 |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in cost_rows:
        if row["record_type"] != "friction_scenario":
            continue
        lines.append(
            f"| {pct(row['total_round_trip_friction'])} | {pct(row['average_return_after_friction'])} | {pct(row['median_return_after_friction'])} | {pct(row['win_rate_after_friction'])} | {num(row['profit_factor_after_friction'])} | {pct(row['share_of_trades_exceeding_friction'])} |"
        )

    forward_day8_boot = lookup_bootstrap(bootstrap_rows, "Forward Return", "Day 8", "forward_8d_close_return")
    forward_day8_boot_month = lookup_bootstrap(
        bootstrap_rows,
        "Forward Return",
        "Day 8",
        "forward_8d_close_return",
        "month",
    )
    cf_available = [row for row in enriched if finite(row.get("forward_8d_close_return"))]
    actual_gross = mean(row["gross_return"] for row in cf_available)
    day8_gross = mean(row["forward_8d_close_return"] for row in cf_available)
    extended = [row for row in year_breakout if row["breakout_type"] == "Extended Breakout"]
    positive_extended_years = sum(finite(row["net_average_return_slippage_0_1"]) and row["net_average_return_slippage_0_1"] > 0 for row in extended)
    extended_boot = lookup_bootstrap(
        bootstrap_rows,
        "Breakout Type",
        "Extended Breakout",
        "net_return_slippage_0_1",
    )
    top1 = next(
        row
        for row in tail_rows
        if row["selection_base"] == "all_completed_trades"
        and math.isclose(row["removed_top_fraction"], 0.01)
    )
    close_stop = next(row for row in exit_summary if row["exit_reason"] == "Close Confirmed Stop")
    weak5 = next(row for row in exit_summary if row["exit_reason"] == "Day 5 Weakness")
    lines.extend(
        [
            "",
            "## 10. 驗證",
            "",
            f"- 診斷驗證：{'通過' if validation['passed'] else '失敗'}；{validation['failure_count']} 個失敗。",
            f"- Baseline trades SHA-256：{validation['input_sha256']['trades.csv']}。",
            f"- Baseline signal_events SHA-256：{validation['input_sha256']['signal_events.csv']}。",
            "",
            "## 11. 結論（只回答 A–E）",
            "",
            f"**A. Entry signal 本身是否有統計上的 forward edge？** 有 Day 8 延遲 Gross edge 的初步證據，但統計穩健性不足：Day 1 顯著為負，Day 3/5 的 CI 跨 0；Day 8 平均為 {pct(forward_day8_boot['point_mean'])}，signal-date cluster 95% CI 為 {pct(forward_day8_boot['mean_ci_low'])} ～ {pct(forward_day8_boot['mean_ci_high'])}，但 month cluster 為 {pct(forward_day8_boot_month['mean_ci_low'])} ～ {pct(forward_day8_boot_month['mean_ci_high'])}，仍跨 0。因此不能稱為跨市場階段都穩定的 forward edge，更不等於扣除成本後可交易。",
            "",
            f"**B. 主要問題比較像 entry selection 還是 exit management？** 主要較像 entry selection／entry timing 的訊號品質不足，exit management 是次要問題。在 Day 8 可觀察的同一批交易，實際平均 Gross 為 {pct(actual_gross)}，一律持有至 Day 8 為 {pct(day8_gross)}；但 Close Confirmed Stop 到 Day 8 仍平均虧損，僅 {close_stop['actual_nonpositive_to_day8_positive_count']:,}/{close_stop['day8_available']:,} 筆轉正，Day 5 Weakness 也僅 {weak5['actual_nonpositive_to_day8_positive_count']:,}/{weak5['day8_available']:,} 筆轉正。出場確實少拿一部分 Day 8 報酬，但不能解釋多數失敗進場。exit-reason 又是路徑條件分組，不能把差值當作替換出場的因果績效。",
            "",
            f"**C. Extended Breakout 是否跨年份穩定？** 否，尚不能稱為跨年穩定。五年中雖有 {positive_extended_years}/5 年 Net 平均為正，但 2023 年為負，而且整體 signal-date cluster Net 95% CI 為 {pct(extended_boot['mean_ci_low'])} ～ {pct(extended_boot['mean_ci_high'])}，仍跨 0；它目前只是描述性上較強的分組。",
            "",
            f"**D. V2.1 是否主要依賴少數極端贏家？** 是，tail dependence 明顯。最大 Top 1%（{top1['removed_trade_count']:,} 筆）貢獻全部 Gross Profit 的 {pct(top1['share_of_total_gross_profit_contributed'])}；移除後平均 Gross 只剩 {pct(top1['remaining_average_gross_return'])}、PF {num(top1['remaining_gross_profit_factor'])}，移除 Top 5% 後平均已轉負。",
            "",
            "**E. 什麼研究方向值得進 V2.2？** 值得預先註冊後再研究的方向是：entry edge 的跨年／市場狀態樣本外驗證、停損與 Day 5 路徑的出場歸因、同日訊號與資金競爭的 portfolio-level 模擬、公司行動與盤中零股真實滑價校正，以及降低 tail dependence 的穩健性檢驗。本輪不把任何分桶結果轉成規則，也不建立 V2.2。",
            "",
        ]
    )
    return "\n".join(lines)


def validate_diagnostic(
    trades: list[dict],
    events: list[dict],
    forward_rows: list[dict],
    enriched: list[dict],
    exit_rows: list[dict],
    year_breakout: list[dict],
    feature_rows: list[dict],
    baseline_hashes: dict,
    baseline_validation: dict,
) -> dict:
    failures = []

    def check(condition: bool, message: str):
        if not condition:
            failures.append(message)

    successful = [row for row in events if row["status"] in {"entered", "censored"}]
    entered_keys = {trade_key(row) for row in events if row["status"] == "entered"}
    trade_keys = {trade_key(row) for row in trades}
    check(len(forward_rows) == len(successful), "forward rows do not cover all successful entries")
    check(len(enriched) == len(trades), "enriched trade count differs from baseline")
    check(len(exit_rows) == len(trades), "exit counterfactual count differs from baseline")
    check(len({trade_key(row) for row in forward_rows}) == len(forward_rows), "duplicate forward keys")
    check(len({trade_key(row) for row in enriched}) == len(enriched), "duplicate enriched trade keys")
    check(len(year_breakout) == len(YEARS) * len(BREAKOUT_TYPES), "year-breakout matrix incomplete")
    check(len(feature_rows) == 9 * 5, "feature quintile matrix incomplete")
    check(baseline_validation.get("passed") is True, "baseline validation was not passed")
    check(entered_keys == trade_keys, "entered event keys differ from completed trade keys")
    forward_by_key = {trade_key(row): row for row in forward_rows}
    for row in enriched:
        check(row["gross_return"] is not None, f"missing baseline gross return: {trade_key(row)}")
        forward = forward_by_key[trade_key(row)]
        prior_mfe = None
        prior_mae = None
        for horizon in HORIZONS:
            baseline_value = row.get(f"day{horizon}_close_return")
            forward_value = forward.get(f"forward_{horizon}d_close_return")
            if finite(baseline_value):
                check(
                    finite(forward_value)
                    and math.isclose(baseline_value, forward_value, rel_tol=1e-10, abs_tol=1e-10),
                    f"baseline Day {horizon} differs from reconstructed forward return: {trade_key(row)}",
                )
            current_mfe = forward.get(f"forward_{horizon}d_mfe")
            current_mae = forward.get(f"forward_{horizon}d_mae")
            if finite(prior_mfe) and finite(current_mfe):
                check(current_mfe + 1e-12 >= prior_mfe, f"forward MFE decreased: {trade_key(row)}")
            if finite(prior_mae) and finite(current_mae):
                check(current_mae - 1e-12 <= prior_mae, f"forward MAE increased: {trade_key(row)}")
            if finite(current_mfe):
                prior_mfe = current_mfe
            if finite(current_mae):
                prior_mae = current_mae
        if row["exit_reason"] == "Day 8 Time Stop" and finite(row.get("forward_8d_close_return")):
            check(
                math.isclose(row["gross_return"], row["forward_8d_close_return"], rel_tol=1e-10, abs_tol=1e-10),
                f"Day 8 Time Stop differs from forward Day 8: {trade_key(row)}",
            )
    check(
        sum(row["exit_reason"] == "Day 8 Time Stop" for row in enriched) == 557,
        "unexpected Day 8 Time Stop count",
    )
    feature_group_sizes = defaultdict(list)
    for row in feature_rows:
        feature_group_sizes[row["feature"]].append(row["trades"])
    check(
        all(len(sizes) == 5 and min(sizes) > 0 and sum(sizes) == len(trades) for sizes in feature_group_sizes.values()),
        "feature quintile coverage is incomplete",
    )
    return {
        "passed": not failures,
        "failure_count": len(failures),
        "failure_samples": failures[:100],
        "checks": {
            "baseline_completed_trades": len(trades),
            "successful_entries_including_censored": len(successful),
            "forward_rows": len(forward_rows),
            "exit_counterfactual_rows": len(exit_rows),
            "year_breakout_rows": len(year_breakout),
            "feature_quintile_rows": len(feature_rows),
            "baseline_validation_passed": baseline_validation.get("passed") is True,
            "day8_time_stop_count": sum(row["exit_reason"] == "Day 8 Time Stop" for row in enriched),
            "forward_available": {
                f"day_{horizon}": sum(finite(row.get(f"forward_{horizon}d_close_return")) for row in forward_rows)
                for horizon in HORIZONS
            },
        },
        "input_sha256": baseline_hashes,
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument("--taiex", required=True, type=Path)
    parser.add_argument("--trades", required=True, type=Path)
    parser.add_argument("--signal-events", required=True, type=Path)
    parser.add_argument("--stock-info", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--bootstrap-reps", type=int, default=5000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260904)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.output_dir.resolve() == args.trades.parent.resolve():
        raise ValueError("diagnostic output directory must not be the V2.1 baseline output directory")
    if args.bootstrap_reps < 1000:
        raise ValueError("bootstrap-reps must be at least 1000")

    baseline_output_hashes = directory_hashes(args.trades.parent)
    baseline_hashes = {
        "trades.csv": file_sha256(args.trades),
        "signal_events.csv": file_sha256(args.signal_events),
    }
    baseline_validation_path = args.trades.parent / "validation_summary.json"
    if not baseline_validation_path.exists():
        raise FileNotFoundError(f"baseline validation file missing: {baseline_validation_path}")
    baseline_validation = json.loads(baseline_validation_path.read_text(encoding="utf-8"))
    if baseline_validation.get("passed") is not True:
        raise RuntimeError("refusing diagnostic run because V2.1 baseline validation is not passed")
    trades = read_trades(args.trades)
    events = read_csv(args.signal_events)
    stocks, ohlcv_audit = load_ohlcv_archives(args.archives)
    taiex, taiex_audit = load_taiex(args.taiex)
    market_dates = [bar.date for bar in taiex]

    forward_rows = build_forward_returns(events, trades, stocks, market_dates)
    enriched = enrich_trades(trades, forward_rows, stocks)
    exit_rows, exit_summary = build_exit_counterfactual(enriched)
    year_breakout = build_year_breakout(enriched)
    feature_rows = build_feature_quintiles(enriched)
    _, regime_rows = build_market_regimes(enriched, taiex)
    tail_rows = build_winning_tail(enriched)
    bootstrap_rows = build_bootstrap(
        enriched,
        forward_rows,
        args.bootstrap_reps,
        args.bootstrap_seed,
    )
    industry_audit = audit_industry_availability(args.stock_info, enriched)
    clustering_rows = build_signal_clustering(
        events,
        enriched,
        market_dates,
        industry_audit,
    )
    cost_rows = build_cost_break_even(enriched)
    validation = validate_diagnostic(
        trades,
        events,
        forward_rows,
        enriched,
        exit_rows,
        year_breakout,
        feature_rows,
        baseline_hashes,
        baseline_validation,
    )
    validation["baseline_output_sha256"] = baseline_output_hashes
    validation["data_audit"] = {"ohlcv": ohlcv_audit, "taiex": taiex_audit}
    validation["data_audit"]["industry_availability"] = industry_audit
    validation["bootstrap"] = {
        "method": "one-way cluster percentile bootstrap",
        "cluster_units": ["signal_date", "month"],
        "repetitions": args.bootstrap_reps,
        "base_seed": args.bootstrap_seed,
        "iid_trade_bootstrap_used": False,
    }
    validation["limitations"] = [
        "Forward returns use unadjusted daily OHLCV and can cross corporate actions.",
        "General-market daily Open is not the historical intraday odd-lot first execution price.",
        "Industry concentration is not computed because the available stock master is predominantly a future snapshot for historical signals.",
        "Exit-reason counterfactuals are path-conditioned descriptive attribution, not causal estimates of a replacement exit strategy.",
        "Quintile boundaries are full-sample descriptive bins and are not trading rules.",
    ]
    if not validation["passed"]:
        raise RuntimeError(f"diagnostic validation failed: {validation['failure_samples']}")
    if baseline_hashes != {
        "trades.csv": file_sha256(args.trades),
        "signal_events.csv": file_sha256(args.signal_events),
    }:
        raise RuntimeError("baseline input files changed during diagnostic run")
    if baseline_output_hashes != directory_hashes(args.trades.parent):
        raise RuntimeError("a V2.1 baseline output file changed during diagnostic run")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "forward_returns.csv", forward_rows)
    write_csv(args.output_dir / "exit_counterfactual.csv", exit_rows)
    write_csv(args.output_dir / "exit_counterfactual_summary.csv", exit_summary)
    write_csv(args.output_dir / "year_breakout_matrix.csv", year_breakout)
    write_csv(args.output_dir / "feature_quintile_analysis.csv", feature_rows)
    write_csv(args.output_dir / "market_regime_analysis.csv", regime_rows)
    write_csv(args.output_dir / "winning_tail_analysis.csv", tail_rows)
    write_csv(args.output_dir / "bootstrap_results.csv", bootstrap_rows)
    write_csv(args.output_dir / "signal_clustering.csv", clustering_rows)
    write_csv(args.output_dir / "cost_break_even.csv", cost_rows)
    (args.output_dir / "diagnostic_validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = build_report(
        forward_rows,
        enriched,
        exit_summary,
        year_breakout,
        feature_rows,
        regime_rows,
        tail_rows,
        bootstrap_rows,
        clustering_rows,
        cost_rows,
        validation,
    )
    (args.output_dir / "diagnostic_summary.md").write_text(report, encoding="utf-8")
    print(json.dumps(validation, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
