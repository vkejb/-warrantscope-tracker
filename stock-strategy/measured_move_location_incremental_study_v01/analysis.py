from __future__ import annotations

from collections import defaultdict
import hashlib
import math
import random
import statistics

from surge_event_study_v01.models import PreparedBenchmark, PreparedStock
from v21.backtest import net_return as v21_net_return
from v21.config import CFG as V21_CFG

from .config import CFG, Config


def quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def profit_factor(values: list[float]) -> float | None:
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
        raise RuntimeError(f"formal V2.1 cost contract drifted: {actual} != {expected}")
    return {**expected, "slippage_one_way": cfg.slippage_one_way}


def benchmark_context(benchmark: PreparedBenchmark) -> dict[str, str]:
    result = {}
    closes = benchmark.normalized_closes
    for index, day in enumerate(benchmark.calendar):
        if index < 59 or closes[index] is None:
            continue
        window = closes[index - 59 : index + 1]
        if any(value is None for value in window):
            continue
        if benchmark.segment_ids[index] != benchmark.segment_ids[index - 59]:
            continue
        ma60 = statistics.fmean(float(value) for value in window)
        result[day] = "0050_ABOVE_MA60" if float(closes[index]) > ma60 else "0050_AT_OR_BELOW_MA60"
    return result


def _true_range(stock: PreparedStock, position: int) -> float:
    bar = stock.bars[position]
    prior = stock.bars[position - 1].close
    return max(bar.high - bar.low, abs(bar.high - prior), abs(bar.low - prior))


def er20(stock: PreparedStock, position: int) -> float | None:
    if position < 20 or stock.segment_ids[position] != stock.segment_ids[position - 20]:
        return None
    if stock.calendar_indices[position] - stock.calendar_indices[position - 20] != 20:
        return None
    path = [abs(stock.bars[i].close / stock.bars[i - 1].close - 1.0) for i in range(position - 19, position + 1)]
    denominator = sum(path)
    return abs(stock.bars[position].close / stock.bars[position - 20].close - 1.0) / denominator if denominator > 0 else 0.0


def atr20_pct(stock: PreparedStock, position: int) -> float | None:
    if position < 20 or stock.segment_ids[position] != stock.segment_ids[position - 20]:
        return None
    if stock.calendar_indices[position] - stock.calendar_indices[position - 20] != 20:
        return None
    return statistics.fmean(_true_range(stock, i) / stock.bars[i - 1].close for i in range(position - 19, position + 1))


def stock_contexts(stock: PreparedStock) -> dict[int, dict]:
    ers, atrs = {}, {}
    for position in range(len(stock.bars)):
        if (value := er20(stock, position)) is not None:
            ers[position] = value
        if (value := atr20_pct(stock, position)) is not None:
            atrs[position] = value
    result = {}
    for year in range(2020, 2026):
        indices = [i for i in ers if stock.bars[i].date.startswith(str(year))]
        er_edges = [quantile([ers[i] for i in indices], q) for q in (0.25, 0.5, 0.75)]
        atr_edges = [quantile([atrs[i] for i in indices if i in atrs], q) for q in (0.25, 0.5, 0.75)]
        for i in indices:
            result[i] = {
                "stock_er20": ers[i],
                "stock_er20_quartile": _quartile(ers[i], er_edges),
                "stock_atr20_pct": atrs.get(i),
                "stock_atr20_percentile_bucket": _quartile(atrs[i], atr_edges) if i in atrs else "UNAVAILABLE",
            }
    return result


def _quartile(value: float, edges: list[float | None]) -> str:
    if any(edge is None for edge in edges):
        return "UNAVAILABLE"
    if value <= edges[0]:
        return "Q1"
    if value <= edges[1]:
        return "Q2"
    if value <= edges[2]:
        return "Q3"
    return "Q4"


def evaluate_outcome(stock: PreparedStock, position: int, period_end: str, cfg: Config = CFG) -> dict:
    end = position + cfg.secondary_horizon
    if end >= len(stock.bars) or stock.bars[end].date > period_end:
        return {"outcome_status": "UNAVAILABLE", "outcome_reason": "PERIOD_END_OR_MISSING_FUTURE"}
    if stock.segment_ids[end] != stock.segment_ids[position] or stock.calendar_indices[end] - stock.calendar_indices[position] != cfg.secondary_horizon:
        return {"outcome_status": "UNAVAILABLE", "outcome_reason": "DISCONTINUITY_OR_MISSING_SESSION"}
    entry = stock.bars[position + 1]
    if entry.volume <= 0:
        return {"outcome_status": "UNAVAILABLE", "outcome_reason": "NONTRADABLE_T_PLUS_1"}
    closes = [stock.bars[i].close / entry.open - 1.0 for i in range(position + 1, end + 1)]
    up8_days = [i for i, value in enumerate(closes, 1) if value >= 0.08]
    up10_days = [i for i, value in enumerate(closes, 1) if value >= 0.10]
    down5_days = [i for i, value in enumerate(closes, 1) if value <= -0.05]
    first_down = down5_days[0] if down5_days else None

    def before(target_days: list[int]) -> bool:
        return bool(target_days) and (first_down is None or target_days[0] < first_down)

    first_up = min([days[0] for days in (up8_days, up10_days) if days], default=None)
    downside_first = first_down is not None and (first_up is None or first_down < first_up)
    day5_close = stock.bars[position + cfg.primary_horizon].close
    day10_close = stock.bars[end].close
    return {
        "outcome_status": "AVAILABLE",
        "outcome_reason": "",
        "entry_date": entry.date,
        "entry_open": entry.open,
        "day5_exit_date": stock.bars[position + cfg.primary_horizon].date,
        "day10_exit_date": stock.bars[end].date,
        "day5_gross_return": closes[cfg.primary_horizon - 1],
        "day5_net_return": v21_net_return(entry.open, day5_close, cfg.slippage_one_way),
        "day10_gross_return": closes[-1],
        "day10_net_return": v21_net_return(entry.open, day10_close, cfg.slippage_one_way),
        "mfe5": max(closes[: cfg.primary_horizon]),
        "mae5": min(closes[: cfg.primary_horizon]),
        "mfe10": max(closes),
        "mae10": min(closes),
        "up5_within_5d": any(value >= 0.05 for value in closes[: cfg.primary_horizon]),
        "up8_within_10d": bool(up8_days),
        "up10_within_10d": bool(up10_days),
        "up8_before_down5": before(up8_days),
        "up10_before_down5": before(up10_days),
        "downside_first": downside_first,
        "calendar_month": stock.bars[position].date[:6],
    }


def summarize(rows: list[dict]) -> dict:
    usable = [row for row in rows if row.get("outcome_status") == "AVAILABLE"]
    result = {"n": len(usable)}
    for horizon in (5, 10):
        gross = [row[f"day{horizon}_gross_return"] for row in usable]
        net = [row[f"day{horizon}_net_return"] for row in usable]
        result.update({
            f"day{horizon}_gross_positive_rate": statistics.fmean(value > 0 for value in gross) if gross else None,
            f"day{horizon}_net_positive_rate": statistics.fmean(value > 0 for value in net) if net else None,
            f"day{horizon}_gross_mean": statistics.fmean(gross) if gross else None,
            f"day{horizon}_net_mean": statistics.fmean(net) if net else None,
            f"day{horizon}_gross_pf": profit_factor(gross),
            f"day{horizon}_net_pf": profit_factor(net),
        })
    for field in ("mfe5", "mae5", "mfe10", "mae10"):
        result[f"median_{field}"] = statistics.median(row[field] for row in usable) if usable else None
        result[f"mean_{field}"] = statistics.fmean(row[field] for row in usable) if usable else None
    for field in ("up5_within_5d", "up8_within_10d", "up10_within_10d", "up8_before_down5", "up10_before_down5", "downside_first"):
        result[f"{field}_rate"] = statistics.fmean(bool(row[field]) for row in usable) if usable else None
    return result


def average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda pair: pair[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        stop = index + 1
        while stop < len(ordered) and ordered[stop][1] == ordered[index][1]:
            stop += 1
        rank = (index + 1 + stop) / 2.0
        for original, _ in ordered[index:stop]:
            ranks[original] = rank
        index = stop
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    rx, ry = average_ranks(x), average_ranks(y)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    numerator = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    denominator = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return numerator / denominator if denominator > 0 else None


def continuous_rows(rows: list[dict], scope: str, period: str) -> list[dict]:
    usable = [row for row in rows if row.get("completion_ratio") is not None and row.get("outcome_status") == "AVAILABLE"]
    x = [row["completion_ratio"] for row in usable]
    targets = (
        ("MFE10", "mfe10"), ("MAE10", "mae10"),
        ("DAY5_NET", "day5_net_return"), ("DAY10_NET", "day10_net_return"),
        ("UP8_BEFORE_DOWN5", "up8_before_down5"),
    )
    return [{
        "scope": scope, "period": period, "relationship": f"R_VS_{label}",
        "n": len(usable), "spearman_rho": spearman(x, [float(row[field]) for row in usable]),
    } for label, field in targets]


def decile_rows(rows: list[dict], scope: str, period: str) -> list[dict]:
    usable = sorted(
        [row for row in rows if row.get("completion_ratio") is not None and row.get("outcome_status") == "AVAILABLE"],
        key=lambda row: (row["completion_ratio"], row["stock_id"], row["signal_date"], row["setup_id"]),
    )
    result = []
    total = len(usable)
    for decile in range(1, 11):
        selected = [row for index, row in enumerate(usable) if min(9, index * 10 // total) + 1 == decile] if total else []
        ratios = [row["completion_ratio"] for row in selected]
        result.append({
            "scope": scope, "period": period, "decile": decile,
            "ratio_min": min(ratios) if ratios else None,
            "ratio_max": max(ratios) if ratios else None,
            "ratio_median": statistics.median(ratios) if ratios else None,
            **summarize(selected),
        })
    return result


def delta_summary(left: list[dict], baseline: list[dict]) -> dict:
    a, b = summarize(left), summarize(baseline)
    metrics = (
        "mean_mfe10", "mean_mae10", "day5_net_mean", "day10_net_mean",
        "up8_before_down5_rate", "up10_before_down5_rate", "downside_first_rate",
        "day5_net_pf", "day10_net_pf",
    )
    result = {}
    for metric in metrics:
        result[f"{metric}_delta"] = a[metric] - b[metric] if a.get(metric) is not None and b.get(metric) is not None else None
    return result


BOOT_FIELDS = (
    "mfe10", "mae10", "day5_net_return", "day10_net_return",
    "up8_before_down5", "downside_first",
)


def cluster_bootstrap(rows: list[dict], period: str, cluster_field: str, cfg: Config = CFG) -> list[dict]:
    usable = [row for row in rows if row.get("primary_group") in {"A_R_LT_0_8", "B_R_0_8_TO_1_2", "C_R_GE_1_2"} and row.get("outcome_status") == "AVAILABLE"]
    by_cluster = defaultdict(list)
    for row in usable:
        key = row["signal_date"] if cluster_field == "signal_date" else row["calendar_month"]
        by_cluster[key].append(row)
    clusters = sorted(by_cluster)
    if not clusters:
        return []
    seed = int.from_bytes(hashlib.sha256(f"{cfg.bootstrap_seed}|{period}|{cluster_field}".encode()).digest()[:8], "big")
    generator = random.Random(seed)
    samples = defaultdict(list)
    for _ in range(cfg.bootstrap_iterations):
        picked = [generator.choice(clusters) for _ in clusters]
        groups = defaultdict(list)
        for key in picked:
            for row in by_cluster[key]:
                groups[row["primary_group"]].append(row)
        for comparison, group in (("A_MINUS_B", "A_R_LT_0_8"), ("C_MINUS_B", "C_R_GE_1_2")):
            left, base = groups[group], groups["B_R_0_8_TO_1_2"]
            for metric, field in (
                ("MFE10_MEAN", "mfe10"), ("MAE10_MEAN", "mae10"),
                ("DAY5_NET_MEAN", "day5_net_return"), ("DAY10_NET_MEAN", "day10_net_return"),
                ("UP8_BEFORE_DOWN5_RATE", "up8_before_down5"), ("DOWNSIDE_FIRST_RATE", "downside_first"),
            ):
                if left and base:
                    samples[(comparison, metric)].append(
                        statistics.fmean(float(row[field]) for row in left) - statistics.fmean(float(row[field]) for row in base)
                    )
            for metric, field in (("DAY5_NET_PF", "day5_net_return"), ("DAY10_NET_PF", "day10_net_return")):
                lp = profit_factor([row[field] for row in left])
                bp = profit_factor([row[field] for row in base])
                if lp is not None and bp is not None:
                    samples[(comparison, metric)].append(lp - bp)
    result = []
    for comparison, group in (("A_MINUS_B", "A_R_LT_0_8"), ("C_MINUS_B", "C_R_GE_1_2")):
        point = delta_summary(
            [row for row in usable if row["primary_group"] == group],
            [row for row in usable if row["primary_group"] == "B_R_0_8_TO_1_2"],
        )
        point_map = {
            "MFE10_MEAN": point["mean_mfe10_delta"], "MAE10_MEAN": point["mean_mae10_delta"],
            "DAY5_NET_MEAN": point["day5_net_mean_delta"], "DAY10_NET_MEAN": point["day10_net_mean_delta"],
            "UP8_BEFORE_DOWN5_RATE": point["up8_before_down5_rate_delta"],
            "DOWNSIDE_FIRST_RATE": point["downside_first_rate_delta"],
            "DAY5_NET_PF": point["day5_net_pf_delta"], "DAY10_NET_PF": point["day10_net_pf_delta"],
        }
        for metric, value in point_map.items():
            values = samples[(comparison, metric)]
            result.append({
                "scope": "PRIMARY_ACTIVE_SPECIALISTS", "period": period,
                "cluster_scheme": cluster_field.upper(), "cluster_count": len(clusters),
                "iterations": cfg.bootstrap_iterations, "comparison": comparison,
                "metric": metric, "point_delta": value,
                "ci_2_5": quantile(values, 0.025), "ci_97_5": quantile(values, 0.975),
                "valid_iterations": len(values),
            })
    return result


def classify(period_deltas: dict[str, dict], family_rows: list[dict], coverage: float, counts: dict[str, dict], cfg: Config = CFG) -> tuple[str, str]:
    if coverage < cfg.minimum_coverage or any(
        counts.get(period, {}).get(group, 0) < cfg.minimum_primary_group_observations_per_period
        for period in period_deltas for group in ("A_R_LT_0_8", "B_R_0_8_TO_1_2")
    ):
        return "MEASURED_MOVE_TOO_SPARSE", "INSUFFICIENT_PRIMARY_COVERAGE_OR_GROUP_N"

    def signs(delta: dict) -> tuple[bool, bool, bool]:
        return (
            (delta.get("mean_mfe10_delta") or 0) > 0,
            (delta.get("day5_net_mean_delta") or 0) > 0,
            (delta.get("downside_first_rate_delta") or 0) < 0,
        )

    labels = list(period_deltas)
    a_signs = {period: signs(period_deltas[period]["A_MINUS_B"]) for period in labels}
    c_signs = {period: signs(period_deltas[period]["C_MINUS_B"]) for period in labels}
    discovery = labels[0]
    later = labels[1:]
    if sum(a_signs[discovery]) >= 2 and sum(c_signs[discovery]) >= 2 and all(sum(c_signs[p]) >= 2 for p in later):
        return "POSSIBLE_U_SHAPED_LOCATION_EFFECT", "A_AND_C_OUTPERFORM_VICINITY_WITH_LATER_C_DIRECTION"
    if all(a_signs[discovery]) and all(sum(a_signs[p]) >= 2 for p in later):
        return "MEASURED_MOVE_LOCATION_EDGE_FOUND", "DISCOVERY_ALL_THREE_AND_LATER_AT_LEAST_TWO_OF_THREE"
    if all(a_signs[discovery]) and any(sum(a_signs[p]) >= 2 for p in later):
        return "MEASURED_MOVE_LOCATION_REGIME_DEPENDENT", "DISCOVERY_EDGE_ONLY_PARTLY_CONFIRMED"
    if all((period_deltas[p]["A_MINUS_B"].get("downside_first_rate_delta") or 0) < 0 for p in labels):
        return "MEASURED_MOVE_LOCATION_RISK_PATH_ONLY", "DOWNSIDE_FIRST_IMPROVES_WITHOUT_FULL_RETURN_EDGE"
    confirming_families = {
        row["setup_family"] for row in family_rows
        if row.get("comparison") == "A_MINUS_B" and row.get("effect_direction_count", 0) >= 2
    }
    if len(confirming_families) == 1:
        return "MEASURED_MOVE_LOCATION_FAMILY_SPECIFIC", "ONE_SETUP_FAMILY_SUPPORTS_DIRECTION"
    return "NO_MEASURED_MOVE_LOCATION_EDGE", "NO_CROSS_PERIOD_INCREMENTAL_EDGE"
