"""Immutable post-session prospective V2 validation publisher."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any

from expanded_shadow_universe_v01.paper import load_expanded_candidates
from paper_shadow_v01.runner import _load_market_context
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file

from .expanded_volume_gate import (
    VOLUME_POLICY, VOLUME_POLICY_HASH, replay_volume_day,
)
from .strict_replay import _write_csv, replay_session


MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_PROSPECTIVE_RUNTIME_DIR = MODULE_DIR / "prospective_v2_runtime"
FREEZE_CUTOFF_DATE = "20261109"
PROSPECTIVE_POLICY = {
    "policy_id": "EXPANDED_V2_PROSPECTIVE_VALIDATION_V1",
    "freeze_cutoff_date": FREEZE_CUTOFF_DATE,
    "source_volume_policy_hash": VOLUME_POLICY_HASH,
    "candidate_variant": "V2_RECENT_VOLUME_ACCELERATION",
    "scenarios": ["BASE", "STRESS"],
    "minimum_scored_trades_each_scenario": 10,
    "minimum_distinct_scored_dates_each_scenario": 5,
    "required_net_pnl_twd": "GREATER_THAN_ZERO",
    "required_profit_factor": "GREATER_THAN_ONE",
    "required_net_pnl_ex_best_trade_twd": "GREATER_THAN_ZERO",
    "review_rule": "EARLIEST_PREFIX_MEETING_SAMPLE_MINIMUMS_IS_FINAL",
    "passing_status": "ELIGIBLE_FOR_SEPARATE_UAT_REVIEW_ONLY",
    "mode": "POST_SESSION_SHADOW_ONLY_NO_BROKER",
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
PROSPECTIVE_POLICY_HASH = hashlib.sha256(canonical_bytes(PROSPECTIVE_POLICY)).hexdigest()


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _verify_day(path: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != manifest.get("manifest_hash"):
        raise RuntimeError(f"prospective manifest hash mismatch: {path}")
    if manifest.get("prospective_policy_hash") != PROSPECTIVE_POLICY_HASH:
        raise RuntimeError(f"prospective policy identity mismatch: {path}")
    if str(manifest.get("session_date", "")) <= FREEZE_CUTOFF_DATE:
        raise RuntimeError(f"known date in prospective runtime: {path}")
    for field in ("actual_orders", "actual_fills", "broker_order_calls"):
        if int(manifest.get(field, 0)) != 0:
            raise RuntimeError(f"nonzero {field} in prospective runtime: {path}")
    for name, digest in manifest.get("artifacts", {}).items():
        if sha256_file(path / name) != digest:
            raise RuntimeError(f"prospective artifact hash mismatch: {path}/{name}")
    return manifest, _read_csv(path / "trades.csv")


def _track_metrics(rows: list[dict[str, str]], scenario: str) -> dict[str, Any]:
    matching = [row for row in rows
                if row.get("variant") == "V2_RECENT_VOLUME_ACCELERATION"
                and row.get("scenario") == scenario]
    scored = [row for row in matching
              if row.get("status") == "SCORED" and row.get("net_pnl") not in {None, ""}]
    values = [float(row["net_pnl"]) for row in scored]
    wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
    best = max(values) if values else None
    profit_factor = sum(wins) / abs(sum(losses)) if losses else None
    profit_factor_pass = bool(values and ((losses and profit_factor is not None and profit_factor > 1)
                                          or (not losses and sum(wins) > 0)))
    return {
        "scenario": scenario, "selection_count": len(matching),
        "scored_trade_count": len(scored),
        "distinct_scored_dates": len({row["session_date"] for row in scored}),
        "unfilled_or_unscorable_count": len(matching) - len(scored),
        "wins": len(wins), "losses": len(losses),
        "net_pnl_twd": sum(values), "profit_factor": profit_factor,
        "profit_factor_pass": profit_factor_pass,
        "best_trade_twd": best,
        "net_pnl_ex_best_trade_twd": sum(values) - best if best is not None else None,
    }


def _sample_ready(metrics: dict[str, dict[str, Any]]) -> bool:
    return all(
        item["scored_trade_count"] >= int(PROSPECTIVE_POLICY["minimum_scored_trades_each_scenario"])
        and item["distinct_scored_dates"] >= int(
            PROSPECTIVE_POLICY["minimum_distinct_scored_dates_each_scenario"]
        )
        for item in metrics.values()
    )


def aggregate_prospective(runtime_dir: Path = DEFAULT_PROSPECTIVE_RUNTIME_DIR) -> dict[str, Any]:
    records: list[tuple[str, list[dict[str, str]]]] = []
    for path in sorted((runtime_dir / "days").glob("*/*")):
        if (path / "manifest.json").is_file():
            manifest, trades = _verify_day(path)
            records.append((str(manifest["session_date"]), trades))
    dates = sorted({day for day, _trades in records})
    if len(dates) != len(records):
        raise RuntimeError("multiple prospective V2 runs exist for one session date")
    prefix_rows: list[dict[str, str]] = []
    review_cutoff = None
    review_metrics = None
    for day, trades in sorted(records):
        prefix_rows.extend(trades)
        current = {scenario: _track_metrics(prefix_rows, scenario)
                   for scenario in PROSPECTIVE_POLICY["scenarios"]}
        if _sample_ready(current):
            review_cutoff, review_metrics = day, current
            break
    current_rows = [row for _day, trades in records for row in trades]
    current_metrics = {scenario: _track_metrics(current_rows, scenario)
                       for scenario in PROSPECTIVE_POLICY["scenarios"]}
    if review_metrics is None:
        status = "PENDING_MINIMUM_NEW_SAMPLE"
        gates = None
    else:
        gates = {
            scenario: {
                "net_pnl_positive": item["net_pnl_twd"] > 0,
                "profit_factor_above_one": item["profit_factor_pass"],
                "net_ex_best_positive": (
                    item["net_pnl_ex_best_trade_twd"] is not None
                    and item["net_pnl_ex_best_trade_twd"] > 0
                ),
            }
            for scenario, item in review_metrics.items()
        }
        passed = all(all(values.values()) for values in gates.values())
        status = ("ELIGIBLE_FOR_SEPARATE_UAT_REVIEW_ONLY" if passed
                  else "REJECTED_AT_FROZEN_PROSPECTIVE_REVIEW")
    result = {
        "policy_id": PROSPECTIVE_POLICY["policy_id"],
        "prospective_policy_hash": PROSPECTIVE_POLICY_HASH,
        "status": status, "prospective_session_count": len(records),
        "first_session_date": dates[0] if dates else None,
        "latest_session_date": dates[-1] if dates else None,
        "review_cutoff_date": review_cutoff,
        "review_metrics": review_metrics, "review_gates": gates,
        "current_metrics": current_metrics,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
        "live_enabled": False,
    }
    runtime_dir.mkdir(parents=True, exist_ok=True)
    temporary = runtime_dir / f".validation_status.tmp-{os.getpid()}"
    temporary.write_bytes(canonical_bytes(result) + b"\n")
    temporary.replace(runtime_dir / "validation_status.json")
    return result


def publish_prospective_v2(
    run_dir: Path, session_manifest: dict[str, Any], *,
    runtime_dir: Path = DEFAULT_PROSPECTIVE_RUNTIME_DIR,
) -> dict[str, Any]:
    day = str(session_manifest.get("session_date", ""))
    if day <= FREEZE_CUTOFF_DATE:
        raise RuntimeError(f"prospective V2 rejects known date {day}")
    if session_manifest.get("coverage_status") != "FULL_SESSION":
        raise RuntimeError("prospective V2 requires FULL_SESSION")
    source = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    for field in ("actual_orders", "actual_fills", "broker_order_calls"):
        if int(source.get(field, 0)) != 0 or int(session_manifest.get(field, 0)) != 0:
            raise RuntimeError(f"prospective V2 rejects nonzero {field}")
    candidates, expanded_coverage = load_expanded_candidates(run_dir)
    market = _load_market_context(run_dir, source)
    coverage = {
        "session_date": day, "source_run_ids": [source["run_id"]],
        "source_statuses": [source["status"]],
        "started_at_taipei": source["started_at"],
        "ended_at_taipei": source["ended_at"],
        "callback_errors": int(source.get("event_counts", {}).get("callback_errors", 0)),
        "coverage_status": "FULL_SESSION",
    }
    parent = replay_session(candidates, market, coverage)
    volume = replay_volume_day(day, parent["ledger"], candidates, market, coverage)
    identity = {
        "source_manifest_hash": source["manifest_hash"],
        "session_analysis_hash": session_manifest["analysis_hash"],
        "prospective_policy_hash": PROSPECTIVE_POLICY_HASH,
    }
    run_id = hashlib.sha256(canonical_bytes(identity)).hexdigest()[:24]
    target = runtime_dir / "days" / day / run_id
    if target.exists():
        _verify_day(target)
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        aggregate = aggregate_prospective(runtime_dir)
        return {**manifest, "validation_status": aggregate["status"]}
    temporary = target.parent / f".{run_id}.tmp-{os.getpid()}"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir()
    try:
        _write_csv(temporary / "candidate_ledger.csv", parent["ledger"])
        _write_csv(temporary / "signals.csv", volume["signals"])
        _write_csv(temporary / "trades.csv", volume["trades"])
        v2_trades = [row for row in volume["trades"]
                     if row.get("variant") == "V2_RECENT_VOLUME_ACCELERATION"]
        summary = {
            "analysis_id": PROSPECTIVE_POLICY["policy_id"],
            "prospective_run_id": run_id, "session_date": day,
            "source_run_id": source["run_id"],
            "candidate_count": len(parent["ledger"]),
            "expanded_coverage": expanded_coverage,
            "v2_selection_count": sum(
                int(value) for (variant, _scenario), value in volume["selections"].items()
                if variant == "V2_RECENT_VOLUME_ACCELERATION"
            ) // len(PROSPECTIVE_POLICY["scenarios"]),
            "v2_trade_record_count": len(v2_trades),
            "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
            "live_enabled": False,
        }
        (temporary / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
        artifacts = {name: sha256_file(temporary / name) for name in (
            "candidate_ledger.csv", "signals.csv", "trades.csv", "summary.json"
        )}
        manifest = {**summary, **identity, "policy": PROSPECTIVE_POLICY,
                    "artifacts": artifacts}
        manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        (temporary / "manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
        temporary.rename(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    aggregate = aggregate_prospective(runtime_dir)
    return {**manifest, "validation_status": aggregate["status"]}
