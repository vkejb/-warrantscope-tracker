from __future__ import annotations

from collections import defaultdict
import hashlib
import math
import random
import statistics

from surge_event_study_v01.models import PreparedBenchmark, PreparedStock
from v21.backtest import net_return as v21_net_return
from v21.config import CFG as V21_CFG

from .config import CFG, SETUP_DEFINITIONS, Config
from .setups import FAMILY_BY_SETUP, setup_flags


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return gains / losses if losses > 0 else None


def validate_cost_contract(cfg: Config = CFG) -> dict:
    actual = {
        "per_trade_notional": V21_CFG.per_trade_notional,
        "commission_rate": V21_CFG.commission_rate * V21_CFG.commission_discount,
        "minimum_commission": V21_CFG.minimum_commission,
        "sell_tax_rate": V21_CFG.stock_transaction_tax,
    }
    expected = {
        "per_trade_notional": cfg.per_trade_notional,
        "commission_rate": cfg.commission_rate,
        "minimum_commission": cfg.minimum_commission,
        "sell_tax_rate": cfg.sell_tax_rate,
    }
    if actual != expected:
        raise RuntimeError(f"formal V2.1 cost contract drifted: actual={actual}, expected={expected}")
    return {**expected, "slippage_one_way": cfg.slippage_one_way}


def benchmark_context(benchmark: PreparedBenchmark) -> dict[str, dict[str, str]]:
    result = {}
    closes = benchmark.normalized_closes
    for index, day in enumerate(benchmark.calendar):
        if index < 60 or closes[index] is None or closes[index - 20] is None:
            continue
        if benchmark.segment_ids[index] != benchmark.segment_ids[index - 60]:
            continue
        window = closes[index - 59 : index + 1]
        if any(value is None for value in window):
            continue
        ma60 = statistics.fmean(float(value) for value in window)
        return20 = float(closes[index]) / float(closes[index - 20]) - 1.0
        result[day] = {
            "market_ma60_context": "ABOVE_MA60" if float(closes[index]) > ma60 else "AT_OR_BELOW_MA60",
            "market_return20_context": "RETURN20_NONNEGATIVE" if return20 >= 0 else "RETURN20_NEGATIVE",
        }
    return result


def raw_signals(
    stock: PreparedStock, start: str, end: str, cfg: Config = CFG
) -> dict[str, list[int]]:
    result = {setup_id: [] for setup_id, _, _ in SETUP_DEFINITIONS}
    for position, bar in enumerate(stock.bars):
        if not start <= bar.date <= end:
            continue
        for setup_id, active in setup_flags(stock, position, cfg).items():
            if active:
                result[setup_id].append(position)
    return result


def deoverlap_positions(raw_positions: list[int], holding_horizon: int = 5) -> list[int]:
    """Allow a new T-close signal when the earlier Day5 close has ended."""

    accepted = []
    next_allowed = -1
    for position in sorted(raw_positions):
        if position < next_allowed:
            continue
        accepted.append(position)
        next_allowed = position + holding_horizon
    return accepted


def evaluate_trade(
    stock: PreparedStock,
    position: int,
    period_end: str,
    context_by_date: dict[str, dict[str, str]],
    cfg: Config = CFG,
) -> dict | None:
    """Evaluate T+1 Open through Day10 Close without crossing period/segment boundaries."""

    end = position + cfg.secondary_horizon_sessions
    if end >= len(stock.bars) or stock.bars[end].date > period_end:
        return None
    if (
        stock.calendar_indices[end] - stock.calendar_indices[position]
        != cfg.secondary_horizon_sessions
        or stock.segment_ids[end] != stock.segment_ids[position]
    ):
        return None
    entry = stock.bars[position + 1]
    if entry.volume <= 0:
        return None
    closes = [stock.bars[index].close / entry.open - 1.0 for index in range(position + 1, end + 1)]
    target_days = [day for day, value in enumerate(closes, 1) if value >= 0.08]
    downside_days = [day for day, value in enumerate(closes, 1) if value <= -0.05]
    target_day = target_days[0] if target_days else None
    downside_day = downside_days[0] if downside_days else None
    signal_date = stock.bars[position].date
    context = context_by_date.get(signal_date, {"market_ma60_context": "UNAVAILABLE", "market_return20_context": "UNAVAILABLE"})
    return {
        "stock_id": stock.code,
        "stock_name": stock.name,
        "signal_date": signal_date,
        "entry_date": entry.date,
        "entry_open": entry.open,
        "day5_exit_date": stock.bars[position + cfg.primary_horizon_sessions].date,
        "day10_exit_date": stock.bars[end].date,
        "day5_gross_return": closes[cfg.primary_horizon_sessions - 1],
        "day5_net_return": v21_net_return(entry.open, stock.bars[position + cfg.primary_horizon_sessions].close, cfg.slippage_one_way),
        "day10_gross_return": closes[-1],
        "day10_net_return": v21_net_return(entry.open, stock.bars[end].close, cfg.slippage_one_way),
        "mfe5": max(closes[: cfg.primary_horizon_sessions]),
        "mae5": min(closes[: cfg.primary_horizon_sessions]),
        "mfe10": max(closes),
        "mae10": min(closes),
        "plus8_before_minus5": target_day is not None and (downside_day is None or target_day < downside_day),
        "downside_first": downside_day is not None and (target_day is None or downside_day < target_day),
        "calendar_year": signal_date[:4],
        "calendar_quarter": f"{signal_date[:4]}Q{(int(signal_date[4:6]) - 1) // 3 + 1}",
        "calendar_month": signal_date[:6],
        **context,
    }


def summarize_trades(
    trades: list[dict], *, raw_signal_count: int = 0, deoverlapped_signal_count: int = 0
) -> dict:
    result = {
        "raw_signal_count": raw_signal_count,
        "deoverlapped_signal_count": deoverlapped_signal_count,
        "deoverlapped_trade_count": len(trades),
    }
    for horizon in (5, 10):
        gross = [row[f"day{horizon}_gross_return"] for row in trades]
        net = [row[f"day{horizon}_net_return"] for row in trades]
        result.update({
            f"day{horizon}_gross_positive_rate": statistics.fmean(value > 0 for value in gross) if gross else None,
            f"day{horizon}_net_positive_rate": statistics.fmean(value > 0 for value in net) if net else None,
            f"day{horizon}_gross_mean": statistics.fmean(gross) if gross else None,
            f"day{horizon}_net_mean": statistics.fmean(net) if net else None,
            f"day{horizon}_gross_pf": _profit_factor(gross),
            f"day{horizon}_net_pf": _profit_factor(net),
            f"day{horizon}_gross_median": statistics.median(gross) if gross else None,
            f"day{horizon}_net_median": statistics.median(net) if net else None,
        })
    result.update({
        "median_mfe5": statistics.median(row["mfe5"] for row in trades) if trades else None,
        "median_mae5": statistics.median(row["mae5"] for row in trades) if trades else None,
        "median_mfe10": statistics.median(row["mfe10"] for row in trades) if trades else None,
        "median_mae10": statistics.median(row["mae10"] for row in trades) if trades else None,
        "plus8_before_minus5_rate": statistics.fmean(row["plus8_before_minus5"] for row in trades) if trades else None,
        "downside_first_rate": statistics.fmean(row["downside_first"] for row in trades) if trades else None,
    })
    return result


def evaluate_period(
    stock: PreparedStock,
    start: str,
    end: str,
    label: str,
    context_by_date: dict[str, dict[str, str]],
    cfg: Config = CFG,
) -> tuple[list[dict], dict[str, list[dict]]]:
    signals = raw_signals(stock, start, end, cfg)
    summaries, trades_by_setup = [], {}
    for setup_id, family, _ in SETUP_DEFINITIONS:
        accepted = deoverlap_positions(signals[setup_id], cfg.primary_horizon_sessions)
        trades = [
            outcome
            for position in accepted
            if (outcome := evaluate_trade(stock, position, end, context_by_date, cfg)) is not None
        ]
        for trade in trades:
            trade.update(setup_id=setup_id, setup_family=family, period=label)
        trades_by_setup[setup_id] = trades
        summaries.append({
            "stock_id": stock.code,
            "stock_name": stock.name,
            "period": label,
            "setup_id": setup_id,
            "setup_family": family,
            **summarize_trades(trades, raw_signal_count=len(signals[setup_id]), deoverlapped_signal_count=len(accepted)),
        })
    return summaries, trades_by_setup


def tail_robustness(trades: list[dict], removal_fraction: float) -> dict:
    remove_count = math.ceil(len(trades) * removal_fraction) if removal_fraction > 0 else 0
    ordered = sorted(trades, key=lambda row: (-row["day5_net_return"], row["signal_date"]))
    kept = ordered[remove_count:]
    values = [row["day5_net_return"] for row in kept]
    return {
        "removal_fraction": removal_fraction,
        "removed_trade_count": remove_count,
        "remaining_trade_count": len(kept),
        "day5_net_mean": statistics.fmean(values) if values else None,
        "day5_net_pf": _profit_factor(values),
        "day5_net_positive_rate": statistics.fmean(value > 0 for value in values) if values else None,
    }


def annual_results(stock_id: str, stock_name: str, setup_id: str, trades: list[dict]) -> list[dict]:
    rows = []
    for year in ("2020", "2021", "2022"):
        selected = [row for row in trades if row["calendar_year"] == year]
        rows.append({
            "stock_id": stock_id,
            "stock_name": stock_name,
            "setup_id": setup_id,
            "setup_family": FAMILY_BY_SETUP[setup_id],
            "calendar_year": year,
            **summarize_trades(selected),
        })
    return rows


def discovery_gate(
    summary: dict, trades: list[dict], annual: list[dict], cfg: Config = CFG
) -> dict:
    top5 = tail_robustness(trades, 0.05)
    annual_means = [row["day5_net_mean"] for row in annual]
    positive_years = sum(value is not None and value > 0 for value in annual_means)
    quarter_sums = defaultdict(float)
    for trade in trades:
        quarter_sums[trade["calendar_quarter"]] += trade["day5_net_return"]
    positive_quarters = [max(0.0, value) for value in quarter_sums.values()]
    positive_total = sum(positive_quarters)
    maximum_share = max(positive_quarters) / positive_total if positive_total > 0 else None
    checks = {
        "sample_gate_pass": summary["deoverlapped_trade_count"] >= cfg.minimum_discovery_trades,
        "day5_net_mean_pass": summary["day5_net_mean"] is not None and summary["day5_net_mean"] > 0,
        "day5_net_pf_pass": summary["day5_net_pf"] is not None and summary["day5_net_pf"] > cfg.minimum_discovery_day5_net_pf,
        "annual_consistency_pass": positive_years >= cfg.minimum_positive_discovery_years,
        "top5_tail_pass": top5["day5_net_pf"] is not None and top5["day5_net_pf"] >= cfg.top5_removed_minimum_net_pf,
        "quarter_concentration_pass": maximum_share is not None and maximum_share <= cfg.maximum_positive_quarter_share,
    }
    return {
        **checks,
        "positive_discovery_years": positive_years,
        "minimum_annual_day5_net_mean": min(annual_means) if all(value is not None for value in annual_means) else None,
        "top5_removed_day5_net_pf": top5["day5_net_pf"],
        "maximum_positive_quarter_share": maximum_share,
        "discovery_candidate_pass": all(checks.values()),
        "discovery_gate_status": "PASS" if all(checks.values()) else ("TOO_SPARSE" if not checks["sample_gate_pass"] else "FAILED_ROBUSTNESS_GATE"),
    }


def select_primary_setup(discovery_rows: list[dict]) -> dict | None:
    """Select from discovery fields only; later outcomes and specialist type are absent."""

    candidates = [row for row in discovery_rows if row["discovery_candidate_pass"]]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda row: (
            -(row["minimum_annual_day5_net_mean"] if row["minimum_annual_day5_net_mean"] is not None else -1e100),
            -row["day5_net_pf"],
            -row["day5_net_mean"],
            -row["deoverlapped_trade_count"],
            row["setup_id"],
        ),
    )[0]


def month_cluster_bootstrap(
    trades: list[dict], stock_id: str, setup_id: str, period: str, cfg: Config = CFG
) -> dict:
    by_month = defaultdict(list)
    for trade in trades:
        by_month[trade["calendar_month"]].append(trade["day5_net_return"])
    months = sorted(by_month)
    if not months:
        return {"month_clusters": 0, "iterations": cfg.bootstrap_iterations, "day5_net_mean_ci_low": None, "day5_net_mean_ci_high": None, "day5_positive_rate_ci_low": None, "day5_positive_rate_ci_high": None}
    seed_text = f"{cfg.bootstrap_seed}|{stock_id}|{setup_id}|{period}".encode()
    seed = int.from_bytes(hashlib.sha256(seed_text).digest()[:8], "big")
    generator = random.Random(seed)
    means, positive_rates = [], []
    for _ in range(cfg.bootstrap_iterations):
        sampled = [generator.choice(months) for _ in months]
        values = [value for month in sampled for value in by_month[month]]
        means.append(statistics.fmean(values))
        positive_rates.append(statistics.fmean(value > 0 for value in values))
    return {
        "month_clusters": len(months),
        "iterations": cfg.bootstrap_iterations,
        "day5_net_mean_ci_low": _quantile(means, 0.025),
        "day5_net_mean_ci_high": _quantile(means, 0.975),
        "day5_positive_rate_ci_low": _quantile(positive_rates, 0.025),
        "day5_positive_rate_ci_high": _quantile(positive_rates, 0.975),
    }


def market_context_rows(
    stock_id: str, stock_name: str, setup_id: str, period: str, trades: list[dict]
) -> list[dict]:
    rows = []
    dimensions = (
        ("0050_CLOSE_VS_MA60", "market_ma60_context"),
        ("0050_RETURN20_SIGN", "market_return20_context"),
    )
    for dimension, field in dimensions:
        for bucket in sorted({trade[field] for trade in trades}):
            selected = [trade for trade in trades if trade[field] == bucket]
            rows.append({
                "stock_id": stock_id,
                "stock_name": stock_name,
                "setup_id": setup_id,
                "setup_family": FAMILY_BY_SETUP[setup_id],
                "period": period,
                "context_dimension": dimension,
                "context_bucket": bucket,
                **summarize_trades(selected),
                "context_used_as_entry_gate": False,
            })
    return rows


def classify_final(discovery_selected: bool, discovery_too_sparse: bool, confirmation: dict | None, stress: dict | None) -> str:
    if not discovery_selected:
        return "TOO_SPARSE" if discovery_too_sparse else "NO_STABLE_STOCK_EDGE"
    confirmation_pass = bool(confirmation and confirmation["day5_net_mean"] is not None and confirmation["day5_net_mean"] > 0 and confirmation["day5_net_pf"] is not None and confirmation["day5_net_pf"] > 1.0)
    stress_pass = bool(stress and stress["day5_net_mean"] is not None and stress["day5_net_mean"] > 0 and stress["day5_net_pf"] is not None and stress["day5_net_pf"] > 1.0)
    if confirmation_pass and stress_pass:
        return "STABLE_STOCK_SPECIFIC_EDGE"
    if confirmation_pass != stress_pass:
        return "REGIME_DEPENDENT_STOCK_EDGE"
    return "DISCOVERY_ONLY_EDGE"
