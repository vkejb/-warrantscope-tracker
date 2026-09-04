from __future__ import annotations

from collections import defaultdict
from datetime import time, timedelta
import math
import random
import statistics

from .config import CFG, Config
from .models import MinuteBar, Signal


def forward_returns(
    signals: list[Signal],
    minutes: list[MinuteBar],
    *,
    cfg: Config = CFG,
) -> list[dict]:
    """Measure signal edge from the known confirmation close, not a claimed fill."""

    by_key: dict[tuple[str, str, object], list[MinuteBar]] = defaultdict(list)
    for bar in minutes:
        by_key[(bar.market, bar.symbol, bar.date)].append(bar)
    for rows in by_key.values():
        rows.sort(key=lambda x: x.bar_end)

    output: list[dict] = []
    for signal in signals:
        rows = by_key.get((signal.market, signal.symbol, signal.trade_date), [])
        future = [bar for bar in rows if bar.bar_end > signal.signal_time]
        base = {
            "strategy_id": signal.strategy_id,
            "config_hash": signal.config_hash,
            "trade_date": signal.trade_date.isoformat(),
            "symbol": signal.symbol,
            "market": signal.market,
            "signal_time": signal.signal_time.isoformat(sep=" "),
            "reference_price": signal.signal_price,
            "reference_basis": "SIGNAL_CONFIRMATION_CLOSE_NOT_EXECUTABLE_FILL",
        }
        for horizon in cfg.forward_minutes:
            expected = [
                signal.signal_time + timedelta(minutes=offset)
                for offset in range(1, horizon + 1)
            ]
            window = future[:horizon]
            complete = len(window) == horizon and [bar.bar_end for bar in window] == expected
            row = {
                **base,
                "horizon": f"{horizon}m",
                "complete": complete,
                "forward_return": None,
                "mfe": None,
                "mae": None,
                "target_time": expected[-1].isoformat(sep=" "),
            }
            if complete:
                row.update(
                    forward_return=window[-1].close / signal.signal_price - 1.0,
                    mfe=max(bar.high for bar in window) / signal.signal_price - 1.0,
                    mae=min(bar.low for bar in window) / signal.signal_price - 1.0,
                )
            output.append(row)

        official_close = next(
            (bar for bar in reversed(rows) if bar.bar_end.time() == time(13, 30)),
            None,
        )
        expected_close_times = []
        cursor = signal.signal_time + timedelta(minutes=1)
        while cursor <= signal.signal_time.replace(hour=13, minute=30):
            expected_close_times.append(cursor)
            cursor += timedelta(minutes=1)
        expected_close_set = set(expected_close_times)
        close_window = [bar for bar in rows if bar.bar_end in expected_close_set]
        close_complete = (
            official_close is not None
            and [bar.bar_end for bar in close_window] == expected_close_times
        )
        close_row = {
            **base,
            "horizon": "close",
            "complete": close_complete,
            "forward_return": None,
            "mfe": None,
            "mae": None,
            "target_time": (
                official_close.bar_end.isoformat(sep=" ") if official_close else ""
            ),
        }
        if close_complete and official_close is not None:
            close_row.update(
                forward_return=official_close.close / signal.signal_price - 1.0,
                mfe=max(bar.high for bar in close_window) / signal.signal_price - 1.0,
                mae=min(bar.low for bar in close_window) / signal.signal_price - 1.0,
            )
        output.append(close_row)
    return output


def summarize_forward(
    rows: list[dict],
    *,
    cfg: Config = CFG,
    bootstrap_iterations: int = 2_000,
    bootstrap_seed: int = 20260904,
) -> tuple[list[dict], list[dict]]:
    summaries: list[dict] = []
    bootstraps: list[dict] = []
    horizons = [f"{value}m" for value in cfg.forward_minutes] + ["close"]
    for horizon in horizons:
        complete_rows = [
            row
            for row in rows
            if row["horizon"] == horizon
            and row["complete"]
            and row["forward_return"] is not None
            and math.isfinite(row["forward_return"])
        ]
        gross_lower, gross_upper = cluster_bootstrap_mean(
            complete_rows,
            friction=0.0,
            iterations=bootstrap_iterations,
            seed=bootstrap_seed,
        )
        for friction in cfg.total_friction_scenarios:
            values = [row["forward_return"] - friction for row in complete_rows]
            wins = [value for value in values if value > 0]
            losses = [value for value in values if value < 0]
            positive = sum(wins)
            negative = abs(sum(losses))
            summaries.append(
                {
                    "horizon": horizon,
                    "total_friction": friction,
                    "sample_size": len(values),
                    "average_return": statistics.fmean(values) if values else None,
                    "median_return": statistics.median(values) if values else None,
                    "win_rate": len(wins) / len(values) if values else None,
                    "profit_factor": positive / negative if negative else None,
                    "average_mfe": _mean_field(complete_rows, "mfe"),
                    "average_mae": _mean_field(complete_rows, "mae"),
                    "interpretation": "signal forward return; not executable trade P&L",
                }
            )
            bootstraps.append(
                {
                    "horizon": horizon,
                    "total_friction": friction,
                    "cluster": "trade_date",
                    "iterations": bootstrap_iterations,
                    "mean_return_ci_2_5": (
                        gross_lower - friction if gross_lower is not None else None
                    ),
                    "mean_return_ci_97_5": (
                        gross_upper - friction if gross_upper is not None else None
                    ),
                }
            )
    return summaries, bootstraps


def cluster_bootstrap_mean(
    rows: list[dict],
    *,
    friction: float,
    iterations: int,
    seed: int,
) -> tuple[float | None, float | None]:
    clusters: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        clusters[row["trade_date"]].append(row["forward_return"] - friction)
    keys = sorted(clusters)
    if not keys or iterations <= 0:
        return None, None
    rng = random.Random(seed)
    estimates = []
    for _ in range(iterations):
        total = 0.0
        count = 0
        for _ in keys:
            values = clusters[rng.choice(keys)]
            total += sum(values)
            count += len(values)
        estimates.append(total / count)
    estimates.sort()
    return _percentile(estimates, 0.025), _percentile(estimates, 0.975)


def _mean_field(rows: list[dict], field: str) -> float | None:
    values = [row[field] for row in rows if row[field] is not None]
    return statistics.fmean(values) if values else None


def _percentile(values: list[float], probability: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight
