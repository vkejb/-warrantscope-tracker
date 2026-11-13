"""All-signal outcome labels for entry-quality research.

This deliberately ignores the one-trade account slot while labelling signal
quality.  Diagnostic P&L across overlapping signals must never be presented as
account performance.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable

from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import _projected_net_pnl

from .early_entry_backtest import DEFAULT_RESULTS_DIR as EARLY_RESULTS_DIR
from .scanner import affordable_quantity
from .signal_outcomes import _track, _verified_selected_books
from .strict_replay import DEFAULT_RUNS_DIR, _write_csv


MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = MODULE_DIR / "signal_selection_quality_results"
POLICY = {
    "analysis_id": "ALL_EARLY_READY_SIGNAL_SELECTION_QUALITY_V1",
    "frozen_at_taipei": "2026-11-13T09:30:00+08:00",
    "mode": "KNOWN_ARCHIVE_POST_OUTCOME_EXPLORATORY_NO_BROKER",
    "parent_signals": "every first-per-symbol EARLY_ENTRY_READY_OBSERVED signal",
    "entry": "frozen causal observer entry reference and whole-lot affordability",
    "horizons_minutes": [1, 3, 5, 15, 30],
    "quality_window_minutes": 30,
    "strong_rise_net_mfe_twd": 3500,
    "cost_covering_net_mfe_twd": 0,
    "existing_rs_challenger": 0.01,
    "interpretation": "independent signal labels, not shared-capital account PnL",
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
POLICY_HASH = hashlib.sha256(canonical_bytes(POLICY)).hexdigest()


def quality_label(mfe: float | None) -> str:
    if mfe is None:
        return "DATA_INSUFFICIENT"
    if mfe >= float(POLICY["strong_rise_net_mfe_twd"]):
        return "STRONG_RISE_1R"
    if mfe > float(POLICY["cost_covering_net_mfe_twd"]):
        return "COST_COVERING_RISE_BELOW_1R"
    return "NO_COST_COVERING_RISE"


def _path(books: list[dict[str, Any]], entry_time: datetime, entry_price: float,
          quantity: int) -> dict[str, Any]:
    end = entry_time + timedelta(minutes=int(POLICY["quality_window_minutes"]))
    points = []
    for row in books:
        if entry_time <= row["time"] <= end:
            net = float(_projected_net_pnl(
                "LONG", entry_price, float(row["best_bid"]), quantity,
            )[3])
            points.append((net, row["time"], float(row["best_bid"])))
    if not points:
        return {
            "mfe_30m_net_pnl_twd": None, "mfe_30m_at": None,
            "mfe_30m_bid": None, "mae_30m_net_pnl_twd": None,
            "mae_30m_at": None, "time_to_cost_cover_seconds": None,
            "time_to_1r_seconds": None,
        }
    best = max(points, key=lambda item: item[0])
    worst = min(points, key=lambda item: item[0])

    def first_time(threshold: float) -> float | None:
        match = next((item for item in points if item[0] >= threshold), None)
        return ((match[1] - entry_time).total_seconds() if match else None)

    return {
        "mfe_30m_net_pnl_twd": best[0], "mfe_30m_at": best[1],
        "mfe_30m_bid": best[2], "mae_30m_net_pnl_twd": worst[0],
        "mae_30m_at": worst[1],
        "time_to_cost_cover_seconds": first_time(0.0),
        "time_to_1r_seconds": first_time(float(POLICY["strong_rise_net_mfe_twd"])),
    }


def outcome_row(signal: dict[str, str], books: list[dict[str, Any]]) -> dict[str, Any]:
    entry_time = datetime.fromisoformat(signal["signal_time"])
    entry_price = float(signal["entry_price"])
    quantity, _fee, _cash = affordable_quantity(entry_price)
    base = {
        "session_date": signal["session_date"], "stock_id": signal["stock_id"],
        "stock_name": signal["stock_name"], "signal_time": entry_time,
        "entry_price": entry_price, "quantity": quantity,
        "relative_strength_5m": float(signal["relative_strength_5m"]),
        "market_regime": signal["market_regime"],
        "daily_ready_rank": int(signal["daily_ready_rank"]),
        "observation_source": signal["observation_source"],
        "source_check_count": int(signal["source_check_count"]),
        "existing_rs_challenger_passed": (
            float(signal["relative_strength_5m"])
            >= float(POLICY["existing_rs_challenger"])
        ),
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
    }
    if quantity < 1000:
        return {**base, "quality_label": "ENTRY_NOT_EVALUABLE"}
    path = _path(books, entry_time, entry_price, quantity)
    return {
        **base,
        **_track(signal, books, "original", entry_time, entry_price, quantity),
        **path,
        "quality_label": quality_label(path["mfe_30m_net_pnl_twd"]),
    }


def _rs_bucket(row: dict[str, Any]) -> str:
    value = float(row["relative_strength_5m"])
    if value < 0.005:
        return "LT_0_5PCT"
    if value < 0.01:
        return "GE_0_5_LT_1PCT"
    if value < 0.02:
        return "GE_1_LT_2PCT"
    return "GE_2PCT"


def _time_bucket(row: dict[str, Any]) -> str:
    stamp = row["signal_time"]
    if (stamp.hour, stamp.minute) < (10, 30):
        return "BEFORE_1030"
    if (stamp.hour, stamp.minute) < (11, 30):
        return "1030_TO_1129"
    return "1130_OR_LATER"


def _rank_bucket(row: dict[str, Any]) -> str:
    rank = int(row["daily_ready_rank"])
    if rank == 1:
        return "FIRST"
    if rank <= 5:
        return "RANK_2_TO_5"
    if rank <= 10:
        return "RANK_6_TO_10"
    return "RANK_11_PLUS"


def _price_bucket(row: dict[str, Any]) -> str:
    price = float(row["entry_price"])
    if price <= 50:
        return "LE_50"
    if price <= 100:
        return "GT_50_LE_100"
    return "GT_100_LE_190"


def group_metrics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dimensions: dict[str, Callable[[dict[str, Any]], str]] = {
        "RELATIVE_STRENGTH": _rs_bucket,
        "MARKET_REGIME": lambda row: str(row["market_regime"]),
        "SIGNAL_TIME": _time_bucket,
        "DAILY_READY_RANK": _rank_bucket,
        "ENTRY_PRICE": _price_bucket,
        "OBSERVATION_SOURCE": lambda row: str(row["observation_source"]),
        "RS_CHALLENGER": lambda row: (
            "PASS_GE_1PCT" if row["existing_rs_challenger_passed"]
            else "REJECT_LT_1PCT"
        ),
    }
    output = []
    for dimension, classify in dimensions.items():
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(classify(row), []).append(row)
        for group, items in sorted(groups.items()):
            evaluable = [row for row in items if row.get("mfe_30m_net_pnl_twd") is not None]
            strong = [row for row in evaluable if row["quality_label"] == "STRONG_RISE_1R"]
            covering = [row for row in evaluable if float(row["mfe_30m_net_pnl_twd"]) > 0]
            fixed5 = [row for row in evaluable
                      if row.get("original_5m_net_pnl_twd") is not None
                      and float(row["original_5m_net_pnl_twd"]) > 0]
            fixed30 = [row for row in evaluable
                       if row.get("original_30m_net_pnl_twd") is not None
                       and float(row["original_30m_net_pnl_twd"]) > 0]
            output.append({
                "dimension": dimension, "group": group,
                "signal_count": len(items), "evaluable_count": len(evaluable),
                "strong_rise_1r_count": len(strong),
                "strong_rise_1r_rate": len(strong) / len(evaluable) if evaluable else None,
                "cost_covering_count": len(covering),
                "cost_covering_rate": len(covering) / len(evaluable) if evaluable else None,
                "positive_5m_count": len(fixed5),
                "positive_5m_rate": len(fixed5) / len(evaluable) if evaluable else None,
                "positive_30m_count": len(fixed30),
                "positive_30m_rate": len(fixed30) / len(evaluable) if evaluable else None,
                "median_mfe_30m_twd": median(
                    float(row["mfe_30m_net_pnl_twd"]) for row in evaluable
                ) if evaluable else None,
                "median_mae_30m_twd": median(
                    float(row["mae_30m_net_pnl_twd"]) for row in evaluable
                ) if evaluable else None,
                "note": "ALL_SIGNALS_INDEPENDENT_DIAGNOSTIC_NOT_ACCOUNT_PNL",
            })
    return output


def run(
    signals_path: Path = EARLY_RESULTS_DIR / "signals.csv",
    replay_cache_dir: Path = EARLY_RESULTS_DIR / "replay_cache",
    runs_dir: Path = DEFAULT_RUNS_DIR,
    results_dir: Path = DEFAULT_RESULTS_DIR,
) -> dict[str, Any]:
    with signals_path.open(encoding="utf-8", newline="") as handle:
        signals = list(csv.DictReader(handle))
    outcomes = []
    for day in sorted({row["session_date"] for row in signals}):
        cache = json.loads((replay_cache_dir / f"{day}.json").read_text(encoding="utf-8"))
        replay = cache["replay"]
        run_dir = runs_dir / replay["source_run_id"]
        manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        if replay["source_manifest_hash"] != manifest["manifest_hash"]:
            raise RuntimeError(f"source manifest changed: {day}")
        day_rows = [row for row in signals if row["session_date"] == day]
        books = _verified_selected_books(run_dir, {row["stock_id"] for row in day_rows})
        outcomes.extend(outcome_row(row, books[row["stock_id"]]) for row in day_rows)
    groups = group_metrics(outcomes)
    evaluable = [row for row in outcomes if row.get("mfe_30m_net_pnl_twd") is not None]
    labels = {label: sum(row["quality_label"] == label for row in outcomes)
              for label in ("STRONG_RISE_1R", "COST_COVERING_RISE_BELOW_1R",
                            "NO_COST_COVERING_RISE", "DATA_INSUFFICIENT",
                            "ENTRY_NOT_EVALUABLE")}
    false_negatives = [row for row in outcomes
                       if not row["existing_rs_challenger_passed"]
                       and row["quality_label"] == "STRONG_RISE_1R"]
    rs_pass = [row for row in evaluable if row["existing_rs_challenger_passed"]]
    rs_pass_strong = [row for row in rs_pass
                      if row["quality_label"] == "STRONG_RISE_1R"]
    summary = {
        "analysis_id": POLICY["analysis_id"], "policy_hash": POLICY_HASH,
        "session_count": len({row["session_date"] for row in outcomes}),
        "signal_count": len(outcomes), "evaluable_count": len(evaluable),
        "labels": labels,
        "rs_lt_1pct_strong_rise_false_negative_count": len(false_negatives),
        "rs_lt_1pct_strong_rise_false_negative_rate_of_all_strong": (
            len(false_negatives) / labels["STRONG_RISE_1R"]
            if labels["STRONG_RISE_1R"] else None
        ),
        "rs_ge_1pct_evaluable_count": len(rs_pass),
        "rs_ge_1pct_strong_rise_count": len(rs_pass_strong),
        "rs_ge_1pct_strong_rise_precision": (
            len(rs_pass_strong) / len(rs_pass) if rs_pass else None
        ),
        "rs_ge_1pct_strong_rise_recall": (
            len(rs_pass_strong) / labels["STRONG_RISE_1R"]
            if labels["STRONG_RISE_1R"] else None
        ),
        "median_mfe_30m_twd": median(
            float(row["mfe_30m_net_pnl_twd"]) for row in evaluable
        ) if evaluable else None,
        "mean_mfe_30m_twd": mean(
            float(row["mfe_30m_net_pnl_twd"]) for row in evaluable
        ) if evaluable else None,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
        "interpretation": POLICY["interpretation"],
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(results_dir / "signal_quality_labels.csv", outcomes)
    _write_csv(results_dir / "feature_quality.csv", groups)
    _write_csv(results_dir / "rs_false_negatives.csv", false_negatives)
    (results_dir / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    manifest = {
        "policy": POLICY, "policy_hash": POLICY_HASH,
        "signals_path": str(signals_path), "summary": summary,
    }
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (results_dir / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
