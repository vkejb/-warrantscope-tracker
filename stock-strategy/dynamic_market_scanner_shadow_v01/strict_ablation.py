"""Small, frozen ablation of STRICT_MARKET_WIDE_V1 on known archives."""

from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
from statistics import mean, median
from typing import Any

from expanded_shadow_universe_v01.paper import load_expanded_candidates
from paper_shadow_v01.runner import _load_market_context
from yuanta_intraday_shadow_v01.collector import canonical_bytes

from .strict_policy import STRICT_POLICY_HASH
from .strict_replay import (
    DEFAULT_PAPER_DIR, DEFAULT_RESULTS_DIR, DEFAULT_RUNS_DIR,
    _simulate_exit, _write_csv, discover_sources,
)


ABLATION_POLICY = {
    "policy_id": "STRICT_MARKET_WIDE_V1_ABLATION_V1",
    "frozen_at_taipei": "2026-11-10T00:00:00+08:00",
    "parent_policy_hash": STRICT_POLICY_HASH,
    "mode": "SHADOW_ONLY_KNOWN_ARCHIVE_DIAGNOSTIC",
    "selection": "EARLIEST_CAUSAL_CONFIRMATION_THEN_PARENT_RANKING",
    "variants": {
        "A0_ALL_STRICT_GATES": {"removed_failure_components": []},
        "A1_REMOVE_NET_PROGRESS_ONLY": {
            "removed_failure_components": ["NET_PROGRESS_NOT_POSITIVE"]
        },
        "A2_REMOVE_FLOW_ONLY": {
            "removed_failure_components": ["FLOW_TURNED_NEGATIVE"]
        },
        "A3_REMOVE_ANTI_CHASE_FAMILY_ONLY": {
            "removed_failure_components": [
                "ANTI_CHASE_OPENING_EXTENSION", "ANTI_CHASE_VWAP_EXTENSION"
            ]
        },
    },
    "non_removable_safety_failures": [
        "TICK_STALE", "BOOK_STALE", "QUOTE_OR_BOOK_MISSING",
        "SPREAD_TOO_WIDE", "DELAYED_ENTRY_ABOVE_190",
        "DELAYED_ENTRY_UNAFFORDABLE",
    ],
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
ABLATION_POLICY_HASH = hashlib.sha256(canonical_bytes(ABLATION_POLICY)).hexdigest()
DEFAULT_ABLATION_RESULTS_DIR = DEFAULT_RESULTS_DIR.parent / "strict_ablation_results"


def load_ledger(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    numeric = {
        "production_score", "relative_strength_5m", "original_entry_price",
        "spread_bps", "confirmation_bid", "confirmation_ask",
        "confirmation_vwap", "confirmation_flow_delta",
        "hypothetical_original_quantity", "hypothetical_net_progress_twd",
        "delayed_entry_price", "delayed_entry_quantity",
        "delayed_entry_fee_twd", "delayed_entry_cash_required_twd",
        "delayed_opening_extension", "delayed_vwap_extension",
    }
    for row in rows:
        row["signal_time"] = datetime.fromisoformat(str(row["signal_time"]))
        row["confirmation_time"] = datetime.fromisoformat(str(row["confirmation_time"]))
        for key in numeric:
            row[key] = float(row[key]) if row.get(key) not in {None, ""} else None
    return rows


def failure_components(row: dict[str, Any]) -> frozenset[str]:
    if str(row.get("strict_passed", "")).lower() == "true":
        return frozenset()
    reason = str(row.get("strict_reason") or "")
    return frozenset(item for item in reason.split("+") if item)


def accepted_by_variant(row: dict[str, Any], variant: str) -> bool:
    removed = frozenset(ABLATION_POLICY["variants"][variant]["removed_failure_components"])
    failures = failure_components(row)
    return failures <= removed


def select_daily(rows: list[dict[str, Any]], variant: str) -> list[dict[str, Any]]:
    accepted = [row for row in rows if accepted_by_variant(row, variant)]
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in accepted:
        by_day.setdefault(str(row["session_date"]), []).append(row)
    selected = []
    for day, items in sorted(by_day.items()):
        first = min(row["confirmation_time"] for row in items)
        simultaneous = [row for row in items if row["confirmation_time"] == first]
        simultaneous.sort(key=lambda row: (
            -float(row.get("hypothetical_net_progress_twd") or float("-inf")),
            -float(row.get("relative_strength_5m") or float("-inf")),
            -float(row.get("production_score") or 0), str(row["stock_id"]),
        ))
        chosen = dict(simultaneous[0])
        chosen["ablation_variant"] = variant
        selected.append(chosen)
    return selected


def _metrics(variant: str, trades: list[dict[str, Any]], selected_count: int) -> dict[str, Any]:
    scored = [row for row in trades if row.get("status") == "SCORED"]
    values = [float(row["net_pnl"]) for row in scored]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    equity, peak, drawdown = 190_000.0, 190_000.0, 0.0
    for row in sorted(scored, key=lambda item: item["session_date"]):
        equity += float(row["net_pnl"])
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "variant": variant, "selected_signal_count": selected_count,
        "trade_count": len(trades), "scored_trade_count": len(scored),
        "net_pnl_twd": sum(values),
        "mean_trade_twd": mean(values) if values else None,
        "median_trade_twd": median(values) if values else None,
        "win_rate": len(wins) / len(values) if values else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "wins": len(wins), "losses": len(losses),
        "max_drawdown_twd": drawdown, "ending_equity_twd": equity,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
    }


def run_ablation(
    ledger_path: Path = DEFAULT_RESULTS_DIR / "candidate_ledger.csv",
    results_dir: Path = DEFAULT_ABLATION_RESULTS_DIR,
    paper_dir: Path = DEFAULT_PAPER_DIR,
    runs_dir: Path = DEFAULT_RUNS_DIR,
) -> dict[str, Any]:
    rows = load_ledger(ledger_path)
    parent_summary_path = ledger_path.parent / "summary.json"
    parent_summary = json.loads(parent_summary_path.read_text(encoding="utf-8"))
    source_by_date = {
        str(paper["session_date"]): (run_dir, paper)
        for run_dir, paper in discover_sources(paper_dir, runs_dir)
    }
    selections = {
        variant: select_daily(rows, variant)
        for variant in ABLATION_POLICY["variants"]
    }
    needed_dates = sorted({str(row["session_date"])
                           for items in selections.values() for row in items})
    trades: list[dict[str, Any]] = []
    for day in needed_dates:
        run_dir, paper = source_by_date[day]
        source = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        candidates, _coverage = load_expanded_candidates(run_dir)
        market = _load_market_context(run_dir, source)
        coverage = {
            "session_date": day, "source_run_ids": [source["run_id"]],
            "source_statuses": [source["status"]],
            "started_at_taipei": source["started_at"],
            "ended_at_taipei": source["ended_at"],
            "callback_errors": int(source.get("event_counts", {}).get("callback_errors", 0)),
            "coverage_status": "FULL_SESSION",
        }
        for variant, items in selections.items():
            selected = next((row for row in items if str(row["session_date"]) == day), None)
            if selected is None:
                continue
            symbol = str(selected["stock_id"])
            trade = _simulate_exit(selected, {symbol: candidates[symbol]}, market, coverage)
            trade["strategy_variant"] = variant
            trade["removed_failure_components"] = "+".join(
                ABLATION_POLICY["variants"][variant]["removed_failure_components"])
            trades.append(trade)
    metrics = [_metrics(variant,
                        [row for row in trades if row["strategy_variant"] == variant],
                        len(selections[variant]))
               for variant in ABLATION_POLICY["variants"]]
    results_dir.mkdir(parents=True, exist_ok=True)
    selection_rows = []
    for variant, items in selections.items():
        for row in items:
            selection_rows.append({
                "variant": variant, "session_date": row["session_date"],
                "stock_id": row["stock_id"], "stock_name": row["stock_name"],
                "signal_time": row["signal_time"],
                "confirmation_time": row["confirmation_time"],
                "parent_failure_components": "+".join(sorted(failure_components(row))),
                "hypothetical_net_progress_twd": row.get("hypothetical_net_progress_twd"),
                "confirmation_flow_delta": row.get("confirmation_flow_delta"),
                "delayed_entry_price": row.get("delayed_entry_price"),
                "delayed_entry_quantity": row.get("delayed_entry_quantity"),
            })
    _write_csv(results_dir / "selected_signals.csv", selection_rows)
    _write_csv(results_dir / "trades.csv", trades)
    _write_csv(results_dir / "experiments.csv", metrics)
    summary = {
        "analysis_id": ABLATION_POLICY["policy_id"],
        "ablation_policy_hash": ABLATION_POLICY_HASH,
        "parent_policy_hash": STRICT_POLICY_HASH,
        "known_archive_session_count": int(parent_summary["session_count"]),
        "sessions_with_parent_candidates": len({str(row["session_date"]) for row in rows}),
        "variants": metrics, "actual_orders": 0, "actual_fills": 0,
        "broker_order_calls": 0,
        "interpretation": "KNOWN_ARCHIVE_ABLATION_NOT_OUT_OF_SAMPLE_NOT_LIVE_APPROVAL",
    }
    (results_dir / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    manifest = {"policy": ABLATION_POLICY, "summary": summary,
                "parent_ledger": str(ledger_path)}
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (results_dir / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return summary
