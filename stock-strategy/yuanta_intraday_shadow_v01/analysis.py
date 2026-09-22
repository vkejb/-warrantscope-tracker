"""Deterministic V0.1 intraday entry-quality diagnostics.

These states are preregistered engineering labels, not trading signals.  They
are intentionally simple until enough prospective sessions exist to evaluate
whether any feature has predictive value.
"""

from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics

from .collector import canonical_bytes, sha256_file, utc_now


ANALYSIS_ID = "STAGE_A_INTRADAY_ENTRY_QUALITY_SHADOW_V0_1"
SPEC = {
    "analysis_id": ANALYSIS_ID,
    "minimum_ticks": 10,
    "minimum_book_snapshots": 10,
    "chase_risk": {"max_upside_from_first_gte": 0.03, "last_vs_vwap_gte": 0.015},
    "supported_momentum": {
        "first_to_last_gte": 0.01, "last_vs_vwap_gte": 0.0,
        "median_book_imbalance_gte": 0.0, "second_half_volume_ratio_gte": 1.0,
    },
    "stable_above_vwap": {
        "first_to_last_gte": 0.0, "last_vs_vwap_gte": 0.0,
        "max_drawdown_from_first_gte": -0.01,
    },
    "weak_or_reversing": {"first_to_last_lt": 0.0, "last_vs_vwap_lt": 0.0},
    "priority": ["DATA_INSUFFICIENT", "CHASE_RISK", "SUPPORTED_MOMENTUM", "STABLE_ABOVE_VWAP", "WEAK_OR_REVERSING", "MIXED"],
    "interpretation": "DESCRIPTIVE_SHADOW_ONLY_NOT_A_BUY_OR_SELL_SIGNAL",
}
SPEC_HASH = hashlib.sha256(canonical_bytes(SPEC)).hexdigest()


FIELDS = (
    "signal_date", "stock_id", "stock_name", "market", "stage_a_rank", "stage_a_score",
    "tick_count", "book_snapshot_count", "first_price", "last_price", "high_price", "low_price",
    "vwap", "first_to_last_return", "max_upside_from_first", "max_drawdown_from_first",
    "last_vs_vwap", "total_deal_volume_units", "second_half_volume_ratio",
    "median_spread_bps", "latest_spread_bps", "median_book_imbalance", "latest_book_imbalance",
    "state", "spec_hash",
)


def _number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"malformed JSONL {path.name}:{line_number}") from exc
    return rows


def _book_metrics(row: dict) -> tuple[float, float] | None:
    buys = [_number(value) for value in row.get("buy_volumes", [])]
    sells = [_number(value) for value in row.get("sell_volumes", [])]
    buy_prices = [_number(value) for value in row.get("buy_prices", [])]
    sell_prices = [_number(value) for value in row.get("sell_prices", [])]
    if len(buys) != 5 or len(sells) != 5 or len(buy_prices) != 5 or len(sell_prices) != 5:
        return None
    if any(value is None for value in (*buys, *sells, *buy_prices, *sell_prices)):
        return None
    buy_sum = sum(buys)  # type: ignore[arg-type]
    sell_sum = sum(sells)  # type: ignore[arg-type]
    total = buy_sum + sell_sum
    midpoint = (buy_prices[0] + sell_prices[0]) / 2  # type: ignore[operator]
    if total <= 0 or midpoint <= 0:
        return None
    imbalance = (buy_sum - sell_sum) / total
    spread_bps = (sell_prices[0] - buy_prices[0]) / midpoint * 10000  # type: ignore[operator]
    return spread_bps, imbalance


def _state(row: dict) -> str:
    if row["tick_count"] < 10 or row["book_snapshot_count"] < 10:
        return "DATA_INSUFFICIENT"
    if row["max_upside_from_first"] >= 0.03 and row["last_vs_vwap"] >= 0.015:
        return "CHASE_RISK"
    if (
        row["first_to_last_return"] >= 0.01
        and row["last_vs_vwap"] >= 0
        and row["median_book_imbalance"] >= 0
        and row["second_half_volume_ratio"] is not None
        and row["second_half_volume_ratio"] >= 1
    ):
        return "SUPPORTED_MOMENTUM"
    if row["first_to_last_return"] >= 0 and row["last_vs_vwap"] >= 0 and row["max_drawdown_from_first"] >= -0.01:
        return "STABLE_ABOVE_VWAP"
    if row["first_to_last_return"] < 0 and row["last_vs_vwap"] < 0:
        return "WEAK_OR_REVERSING"
    return "MIXED"


def analyze_run(run_dir: Path) -> tuple[list[dict], dict]:
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    watch = json.loads((run_dir / "watchlist.json").read_text(encoding="utf-8"))
    manifest_without_hash = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if hashlib.sha256(canonical_bytes(manifest_without_hash)).hexdigest() != manifest.get("manifest_hash"):
        raise RuntimeError("source run manifest hash mismatch")
    if manifest.get("status") != "COMPLETE" or manifest.get("watchlist_count") != 30:
        raise RuntimeError("source quote run is not a complete 30-stock run")
    for filename in ("watchlist.json", "ticks.jsonl", "books.jsonl"):
        if sha256_file(run_dir / filename) != manifest["artifacts"][filename]:
            raise RuntimeError(f"source artifact hash mismatch: {filename}")
    ticks = _read_jsonl(run_dir / "ticks.jsonl")
    books = _read_jsonl(run_dir / "books.jsonl")
    tick_by: dict[str, list[dict]] = {}
    book_by: dict[str, list[dict]] = {}
    for row in ticks:
        tick_by.setdefault(str(row["stock_id"]), []).append(row)
    for row in books:
        book_by.setdefault(str(row["stock_id"]), []).append(row)

    results = []
    for stock in watch["stocks"]:
        stock_id = str(stock["stock_id"])
        stock_ticks = sorted(tick_by.get(stock_id, []), key=lambda row: row["received_at"])
        stock_books = sorted(book_by.get(stock_id, []), key=lambda row: row["received_at"])
        timed_prices_and_volumes = [
            (datetime.fromisoformat(str(row["received_at"]).replace("Z", "+00:00")), _number(row.get("deal_price")), _number(row.get("deal_volume")))
            for row in stock_ticks
        ]
        timed_prices_and_volumes = [(stamp, price, volume) for stamp, price, volume in timed_prices_and_volumes if price is not None and price > 0 and volume is not None and volume >= 0]
        books_valid = [(row, _book_metrics(row)) for row in stock_books]
        books_valid = [(row, metric) for row, metric in books_valid if metric is not None]
        if timed_prices_and_volumes:
            prices = [x[1] for x in timed_prices_and_volumes]
            volumes = [x[2] for x in timed_prices_and_volumes]
            first, last = prices[0], prices[-1]
            total_volume = sum(volumes)
            vwap = sum(price * volume for _, price, volume in timed_prices_and_volumes) / total_volume if total_volume > 0 else statistics.fmean(prices)
            first_stamp = timed_prices_and_volumes[0][0]
            last_stamp = timed_prices_and_volumes[-1][0]
            midpoint_stamp = first_stamp + (last_stamp - first_stamp) / 2
            first_volume = sum(volume for stamp, _, volume in timed_prices_and_volumes if stamp <= midpoint_stamp)
            second_volume = sum(volume for stamp, _, volume in timed_prices_and_volumes if stamp > midpoint_stamp)
            volume_ratio = second_volume / first_volume if first_volume > 0 else None
            price_values = {
                "first_price": first, "last_price": last, "high_price": max(prices), "low_price": min(prices), "vwap": vwap,
                "first_to_last_return": last / first - 1, "max_upside_from_first": max(prices) / first - 1,
                "max_drawdown_from_first": min(prices) / first - 1, "last_vs_vwap": last / vwap - 1,
                "total_deal_volume_units": total_volume, "second_half_volume_ratio": volume_ratio,
            }
        else:
            price_values = {key: None for key in ("first_price", "last_price", "high_price", "low_price", "vwap", "first_to_last_return", "max_upside_from_first", "max_drawdown_from_first", "last_vs_vwap", "second_half_volume_ratio")}
            price_values["total_deal_volume_units"] = 0
        metrics = [metric for _, metric in books_valid]
        row = {
            "signal_date": watch["signal_date"], "stock_id": stock_id, "stock_name": stock["stock_name"],
            "market": stock["market"], "stage_a_rank": stock["rank"], "stage_a_score": stock["score"],
            "tick_count": len(timed_prices_and_volumes), "book_snapshot_count": len(metrics), **price_values,
            "median_spread_bps": statistics.median(x[0] for x in metrics) if metrics else None,
            "latest_spread_bps": metrics[-1][0] if metrics else None,
            "median_book_imbalance": statistics.median(x[1] for x in metrics) if metrics else None,
            "latest_book_imbalance": metrics[-1][1] if metrics else None,
            "spec_hash": SPEC_HASH,
        }
        if None in (row["first_to_last_return"], row["max_upside_from_first"], row["max_drawdown_from_first"], row["last_vs_vwap"], row["median_book_imbalance"]):
            row["state"] = "DATA_INSUFFICIENT"
        else:
            row["state"] = _state(row)
        results.append(row)
    summary = {
        "analysis_id": ANALYSIS_ID, "spec_hash": SPEC_HASH, "created_at": utc_now(),
        "source_run_id": manifest["run_id"], "source_manifest_hash": manifest["manifest_hash"],
        "signal_date": manifest["signal_date"], "stocks": len(results),
        "state_counts": {state: sum(row["state"] == state for row in results) for state in SPEC["priority"]},
        "interpretation": SPEC["interpretation"], "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
    }
    return results, summary


def publish_analysis(run_dir: Path, analysis_root: Path) -> Path:
    rows, summary = analyze_run(run_dir)
    output_dir = analysis_root / summary["source_run_id"]
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "analysis_spec.json").write_bytes(canonical_bytes(SPEC) + b"\n")
    with (output_dir / "intraday_features.csv").open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader(); writer.writerows(rows)
    feature_hash = sha256_file(output_dir / "intraday_features.csv")
    summary["artifacts"] = {"analysis_spec.json": sha256_file(output_dir / "analysis_spec.json"), "intraday_features.csv": feature_hash}
    summary["analysis_hash"] = hashlib.sha256(canonical_bytes(summary)).hexdigest()
    (output_dir / "analysis_manifest.json").write_bytes(canonical_bytes(summary) + b"\n")
    return output_dir
