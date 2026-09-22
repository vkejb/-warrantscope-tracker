"""Fixed-checkpoint intraday features and post-checkpoint path outcomes."""

from __future__ import annotations

import csv
from datetime import datetime, time
import hashlib
import json
from pathlib import Path
import statistics
from zoneinfo import ZoneInfo

from .analysis import _book_metrics, _number, _read_jsonl
from .collector import canonical_bytes, sha256_file, utc_now


TAIPEI = ZoneInfo("Asia/Taipei")
CHECKPOINTS = ("09:05", "09:15", "09:30", "10:00", "10:30")
SPEC = {
    "analysis_id": "STAGE_A_INTRADAY_FIXED_CHECKPOINTS_V0_1",
    "checkpoints": CHECKPOINTS,
    "feature_window_start": "09:00",
    "full_session_start_deadline": "09:00:30",
    "outcome_maturity_time": "13:30",
    "market_wide_max_gap_seconds": 120,
    "features_use_events_received_at_or_before_checkpoint": True,
    "outcomes_use_events_after_checkpoint": True,
    "partial_runs_never_mark_outcomes_mature": True,
    "interpretation": "SHADOW_RESEARCH_ONLY_NOT_A_TRADING_SIGNAL",
}
SPEC_HASH = hashlib.sha256(canonical_bytes(SPEC)).hexdigest()

FEATURE_FIELDS = (
    "signal_date", "session_date", "checkpoint", "stock_id", "stock_name", "stage_a_rank", "stage_a_score",
    "snapshot_status", "tick_count", "book_snapshot_count", "first_price", "checkpoint_price", "vwap",
    "return_from_first", "max_upside_from_first", "max_drawdown_from_first", "deal_volume_units",
    "median_spread_bps", "median_book_imbalance", "spec_hash",
)
OUTCOME_FIELDS = (
    "signal_date", "session_date", "checkpoint", "stock_id", "stock_name", "stage_a_rank",
    "outcome_status", "reference_price", "future_last_price", "future_mfe", "future_mae", "future_end_return", "spec_hash",
)


def _local_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TAIPEI)


def _clock(value: str) -> time:
    parts = [int(item) for item in value.split(":")]
    return time(*parts)


def _source_files(run_dir: Path, manifest: dict) -> tuple[Path, Path]:
    tick_name = "ticks.jsonl.gz" if "ticks.jsonl.gz" in manifest["artifacts"] else "ticks.jsonl"
    book_name = "books.jsonl.gz" if "books.jsonl.gz" in manifest["artifacts"] else "books.jsonl"
    for name in ("watchlist.json", tick_name, book_name):
        if sha256_file(run_dir / name) != manifest["artifacts"][name]:
            raise RuntimeError(f"source artifact hash mismatch: {name}")
    return run_dir / tick_name, run_dir / book_name


def build_session_rows(run_dir: Path) -> tuple[list[dict], list[dict], dict]:
    manifest = json.loads((run_dir / "run_manifest.json").read_text())
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != manifest.get("manifest_hash"):
        raise RuntimeError("source run manifest hash mismatch")
    watch = json.loads((run_dir / "watchlist.json").read_text())
    tick_path, book_path = _source_files(run_dir, manifest)
    ticks, books = _read_jsonl(tick_path), _read_jsonl(book_path)
    started = _local_stamp(manifest["started_at"])
    ended = _local_stamp(manifest["ended_at"])
    session_date = ended.strftime("%Y%m%d")
    by_tick: dict[str, list[dict]] = {}
    by_book: dict[str, list[dict]] = {}
    for row in ticks:
        row = {**row, "_stamp": _local_stamp(row["received_at"])}
        by_tick.setdefault(str(row["stock_id"]), []).append(row)
    for row in books:
        row = {**row, "_stamp": _local_stamp(row["received_at"])}
        by_book.setdefault(str(row["stock_id"]), []).append(row)

    session_open = datetime.combine(ended.date(), _clock("09:00"), tzinfo=TAIPEI)
    maturity = datetime.combine(ended.date(), _clock("13:30"), tzinfo=TAIPEI)
    market_stamps = sorted(
        row["_stamp"]
        for grouped in (by_tick, by_book)
        for rows in grouped.values()
        for row in rows
        if session_open <= row["_stamp"] <= maturity
    )
    gaps = [(right - left).total_seconds() for left, right in zip(market_stamps, market_stamps[1:])]
    max_market_gap = max(gaps, default=float("inf"))
    stream_coverage = (
        bool(market_stamps)
        and market_stamps[0] <= datetime.combine(ended.date(), _clock("09:02"), tzinfo=TAIPEI)
        and market_stamps[-1] >= datetime.combine(ended.date(), _clock("13:29"), tzinfo=TAIPEI)
        and max_market_gap <= 120
    )
    full_session = started.time() <= _clock("09:00:30") and ended.time() >= _clock("13:30") and manifest["status"] == "COMPLETE" and stream_coverage

    features, outcomes = [], []
    for checkpoint in CHECKPOINTS:
        cutoff = datetime.combine(ended.date(), _clock(checkpoint), tzinfo=TAIPEI)
        run_reached_cutoff = ended >= cutoff
        for stock in watch["stocks"]:
            stock_id = str(stock["stock_id"])
            before_ticks = [row for row in by_tick.get(stock_id, []) if row["_stamp"] <= cutoff]
            before_books = [row for row in by_book.get(stock_id, []) if row["_stamp"] <= cutoff]
            pv = [(_number(row.get("deal_price")), _number(row.get("deal_volume"))) for row in before_ticks]
            pv = [(price, volume) for price, volume in pv if price is not None and price > 0 and volume is not None and volume >= 0]
            bm = [metric for metric in (_book_metrics(row) for row in before_books) if metric is not None]
            if not run_reached_cutoff:
                status = "RUN_ENDED_BEFORE_CHECKPOINT"
            elif not pv:
                status = "NO_TRADE_BEFORE_CHECKPOINT"
            else:
                status = "AVAILABLE"
            prices = [price for price, _ in pv]
            volume = sum(item[1] for item in pv)
            first = prices[0] if prices else None
            last = prices[-1] if prices else None
            vwap = sum(price * vol for price, vol in pv) / volume if volume > 0 else (statistics.fmean(prices) if prices else None)
            features.append({
                "signal_date": watch["signal_date"], "session_date": session_date, "checkpoint": checkpoint,
                "stock_id": stock_id, "stock_name": stock["stock_name"], "stage_a_rank": stock["rank"], "stage_a_score": stock["score"],
                "snapshot_status": status, "tick_count": len(pv), "book_snapshot_count": len(bm),
                "first_price": first, "checkpoint_price": last, "vwap": vwap,
                "return_from_first": last / first - 1 if first and last else None,
                "max_upside_from_first": max(prices) / first - 1 if first else None,
                "max_drawdown_from_first": min(prices) / first - 1 if first else None,
                "deal_volume_units": volume, "median_spread_bps": statistics.median(x[0] for x in bm) if bm else None,
                "median_book_imbalance": statistics.median(x[1] for x in bm) if bm else None, "spec_hash": SPEC_HASH,
            })
            future_prices = [_number(row.get("deal_price")) for row in by_tick.get(stock_id, []) if row["_stamp"] > cutoff]
            future_prices = [price for price in future_prices if price is not None and price > 0]
            outcome_status = "MATURE" if full_session and last is not None and future_prices else "PARTIAL_NOT_MATURED"
            outcomes.append({
                "signal_date": watch["signal_date"], "session_date": session_date, "checkpoint": checkpoint,
                "stock_id": stock_id, "stock_name": stock["stock_name"], "stage_a_rank": stock["rank"],
                "outcome_status": outcome_status, "reference_price": last,
                "future_last_price": future_prices[-1] if future_prices else None,
                "future_mfe": max(future_prices) / last - 1 if last and future_prices else None,
                "future_mae": min(future_prices) / last - 1 if last and future_prices else None,
                "future_end_return": future_prices[-1] / last - 1 if last and future_prices else None, "spec_hash": SPEC_HASH,
            })
    summary = {
        "analysis_id": SPEC["analysis_id"], "spec_hash": SPEC_HASH, "created_at": utc_now(),
        "source_run_id": manifest["run_id"], "source_manifest_hash": manifest["manifest_hash"],
        "signal_date": watch["signal_date"], "session_date": session_date,
        "coverage_status": "FULL_SESSION" if full_session else "PARTIAL_SESSION",
        "stream_coverage_pass": stream_coverage,
        "max_market_event_gap_seconds": max_market_gap if max_market_gap != float("inf") else None,
        "started_at_taipei": started.isoformat(), "ended_at_taipei": ended.isoformat(),
        "feature_rows": len(features), "outcome_rows": len(outcomes),
        "mature_outcome_rows": sum(row["outcome_status"] == "MATURE" for row in outcomes),
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
    }
    return features, outcomes, summary


def publish_session_analysis(run_dir: Path, analysis_root: Path) -> Path:
    features, outcomes, summary = build_session_rows(run_dir)
    output = analysis_root / summary["source_run_id"]
    output.mkdir(parents=True, exist_ok=False)
    (output / "checkpoint_spec.json").write_bytes(canonical_bytes(SPEC) + b"\n")
    for name, fields, rows in (("checkpoint_features.csv", FEATURE_FIELDS, features), ("checkpoint_outcomes.csv", OUTCOME_FIELDS, outcomes)):
        with (output / name).open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    summary["artifacts"] = {name: sha256_file(output / name) for name in ("checkpoint_spec.json", "checkpoint_features.csv", "checkpoint_outcomes.csv")}
    summary["analysis_hash"] = hashlib.sha256(canonical_bytes(summary)).hexdigest()
    (output / "session_manifest.json").write_bytes(canonical_bytes(summary) + b"\n")
    return output
