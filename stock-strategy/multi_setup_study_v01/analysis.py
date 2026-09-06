from __future__ import annotations

from collections import Counter
import math
import statistics

from v21.diagnostic_analysis import (
    cluster_bootstrap as _shared_cluster_bootstrap,
    concentration_summary,
    mean,
    median,
    percentile,
    profit_factor,
    stable_seed,
    win_rate,
)

from .config import CFG, Config


def evaluable(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row.get("outcome_status") == "EVALUABLE"]


def metric_summary(rows: list[dict]) -> dict:
    valid = evaluable(rows)
    gross = [row.get("gross_return") for row in valid]
    net = [row.get("net_return") for row in valid]
    return {
        "signals": len(rows),
        "evaluable_signals": len(valid),
        "outcome_observation_rate": len(valid) / len(rows) if rows else None,
        "active_signal_dates": len({row["signal_date"] for row in rows}),
        "primary_success_rate": (
            sum(bool(row.get("primary_success")) for row in valid) / len(valid)
            if valid
            else None
        ),
        "gross_average_return": mean(gross),
        "net_average_return": mean(net),
        "gross_median_return": median(gross),
        "net_median_return": median(net),
        "gross_win_rate": win_rate(gross),
        "net_win_rate": win_rate(net),
        "gross_profit_factor": profit_factor(gross),
        "net_profit_factor": profit_factor(net),
        "average_mfe_5d": mean(row.get("mfe_5d") for row in valid),
        "average_mfe_10d": mean(row.get("mfe_10d") for row in valid),
        "average_mae_5d": mean(row.get("mae_5d") for row in valid),
        "average_mae_10d": mean(row.get("mae_10d") for row in valid),
        "average_mfe_abs_mae": mean(row.get("mfe_abs_mae") for row in valid),
        "plus10_before_minus5_rate": (
            sum(bool(row.get("plus10_before_minus5")) for row in valid) / len(valid)
            if valid
            else None
        ),
        "plus15_before_minus5_rate": (
            sum(bool(row.get("plus15_before_minus5")) for row in valid) / len(valid)
            if valid
            else None
        ),
    }


def entry_gap_bucket(value: float) -> str:
    if value < -0.01:
        return "< -1%"
    if value < 0.0:
        return "-1% to 0%"
    if value < 0.01:
        return "0% to 1%"
    if value < 0.02:
        return "1% to 2%"
    if value < 0.03:
        return "2% to 3%"
    return ">= 3%"


GAP_BUCKETS = (
    "< -1%",
    "-1% to 0%",
    "0% to 1%",
    "1% to 2%",
    "2% to 3%",
    ">= 3%",
)


def entry_gap_rows(rows: list[dict], setup: str, period: str) -> list[dict]:
    valid = evaluable(rows)
    output = []
    for bucket in GAP_BUCKETS:
        selected = [
            row for row in valid if entry_gap_bucket(float(row["entry_gap"])) == bucket
        ]
        summary = metric_summary(selected)
        output.append(
            {
                "period": period,
                "setup": setup,
                "entry_gap_bucket": bucket,
                "sample_size": len(selected),
                "success_rate": summary["primary_success_rate"],
                "gross_average_return": summary["gross_average_return"],
                "gross_profit_factor": summary["gross_profit_factor"],
                "average_mfe_10d": summary["average_mfe_10d"],
                "average_mae_10d": summary["average_mae_10d"],
            }
        )
    return output


def _remove_top_winners(rows: list[dict], fraction: float) -> tuple[list[dict], int]:
    valid = evaluable(rows)
    winners = sorted(
        (row for row in valid if float(row["gross_return"]) > 0),
        key=lambda row: (
            -float(row["gross_return"]),
            str(row.get("signal_date", "")),
            str(row.get("code", "")),
        ),
    )
    remove_count = math.ceil(len(winners) * fraction) if fraction and winners else 0
    removed_ids = {id(row) for row in winners[:remove_count]}
    return [row for row in valid if id(row) not in removed_ids], remove_count


def winner_dependence_rows(rows: list[dict], setup: str, period: str) -> list[dict]:
    baseline = metric_summary(rows)
    output = []
    for label, fraction in (("ORIGINAL", 0.0), ("REMOVE_TOP_1PCT_WINNERS", 0.01), ("REMOVE_TOP_5PCT_WINNERS", 0.05)):
        remaining, removed = _remove_top_winners(rows, fraction)
        summary = metric_summary(remaining)
        tail_dependent = bool(
            fraction == 0.01
            and (
                (
                    baseline["gross_average_return"] is not None
                    and baseline["gross_average_return"] > 0
                    and (
                        summary["gross_average_return"] is None
                        or summary["gross_average_return"] <= 0
                    )
                )
                or (
                    baseline["gross_profit_factor"] is not None
                    and baseline["gross_profit_factor"] > 1
                    and (
                        summary["gross_profit_factor"] is None
                        or summary["gross_profit_factor"] <= 1
                    )
                )
            )
        )
        net_tail_dependent = bool(
            fraction == 0.01
            and baseline["net_average_return"] is not None
            and baseline["net_average_return"] > 0
            and (
                summary["net_average_return"] is None
                or summary["net_average_return"] <= 0
            )
        )
        output.append(
            {
                "period": period,
                "setup": setup,
                "tail_case": label,
                "removed_fraction_of_winners": fraction,
                "removed_winners": removed,
                "remaining_evaluable": summary["evaluable_signals"],
                "average_gross_return": summary["gross_average_return"],
                "median_gross_return": summary["gross_median_return"],
                "gross_profit_factor": summary["gross_profit_factor"],
                "average_net_return": summary["net_average_return"],
                "median_net_return": summary["net_median_return"],
                "net_profit_factor": summary["net_profit_factor"],
                "tail_dependent": tail_dependent,
                "TAIL_DEPENDENT": tail_dependent,
                "net_tail_dependent": net_tail_dependent,
            }
        )
    return output


def signal_clustering_rows(
    rows: list[dict], setup: str, period: str, calendar_dates: list[str]
) -> list[dict]:
    counts = Counter(row["signal_date"] for row in rows)
    calendar_counts = [(day, counts.get(day, 0)) for day in calendar_dates]
    concentration = concentration_summary(calendar_counts)
    maximum_date = (
        min(
            (day for day, count in calendar_counts if count == concentration["maximum_daily_count"]),
            default=None,
        )
        if concentration["maximum_daily_count"]
        else None
    )
    remaining = [row for row in rows if row["signal_date"] != maximum_date]
    removed_summary = metric_summary(remaining)
    return [
        {
            "period": period,
            "setup": setup,
            "total_signals": len(rows),
            **concentration,
            "max_date": maximum_date,
            "remove_max_signal_date_average_gross_return": removed_summary[
                "gross_average_return"
            ],
            "remove_max_signal_date_gross_profit_factor": removed_summary[
                "gross_profit_factor"
            ],
            "remove_max_signal_date_evaluable": removed_summary["evaluable_signals"],
        }
    ]


def cluster_bootstrap(
    rows: list[dict],
    field: str,
    cluster_unit: str,
    reps: int = CFG.bootstrap_iterations,
    seed: int | None = None,
) -> dict:
    if field not in {"gross_return", "net_return"}:
        raise ValueError("bootstrap field must be gross_return or net_return")
    if cluster_unit not in {"signal_date", "month"}:
        raise ValueError("cluster_unit must be signal_date or month")
    actual_seed = seed if seed is not None else stable_seed(
        CFG.bootstrap_seed, field, cluster_unit
    )
    result = _shared_cluster_bootstrap(
        evaluable(rows), field, cluster_unit, reps, actual_seed
    )
    return {
        "metric": field,
        "cluster_unit": cluster_unit,
        "seed": actual_seed,
        **result,
    }


def bootstrap_rows(
    rows: list[dict], setup: str, period: str, cfg: Config = CFG
) -> list[dict]:
    output = []
    for field in ("gross_return", "net_return"):
        for cluster_unit in ("signal_date", "month"):
            seed = stable_seed(cfg.bootstrap_seed, setup, period, field, cluster_unit)
            output.append(
                {
                    "period": period,
                    "setup": setup,
                    **cluster_bootstrap(
                        rows,
                        field,
                        cluster_unit,
                        reps=cfg.bootstrap_iterations,
                        seed=seed,
                    ),
                }
            )
    return output
