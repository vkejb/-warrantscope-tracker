"""Offline received-time replay of the frozen V2 observer on sealed sessions."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

from paper_shadow_v01.runner import _load_market_context
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import _projected_net_pnl

from dynamic_market_scanner_shadow_v01.causal_auction import _verified_selected_quotes
from dynamic_market_scanner_shadow_v01.early_entry_backtest import (
    _expanded_items, merged_quote_events,
)
from dynamic_market_scanner_shadow_v01.realtime_expanded_shadow import (
    EVENTS_NAME, POLICY_HASH as OBSERVER_POLICY_HASH,
    RealtimeExpandedShadow, SUMMARY_NAME,
)
from dynamic_market_scanner_shadow_v01.strict_replay import (
    DEFAULT_RUNS_DIR, discover_sources, _write_csv,
)
from .followup_replay import fixed_hold, metrics, replay_trade
from .replay import choose_immediate, continuation, fill_after


EXTRA_RUNS = {
    "20261112": "20261112T005011.064631Z_fcdaa5e4",
    "20261113": "20261113T005010.446883Z_c876f743",
}
POLICY = {
    "id": "V2_COHORT_CAUSAL_REPLAY_20261115",
    "mode": "SHADOW_ONLY_NO_LIVE_ORDER",
    "observer_policy_hash": OBSERVER_POLICY_HASH,
    "early_telemetry_only_disabled_for_replay": True,
    "volume_gate": "V2_RECENT_VOLUME_ACCELERATION",
    "relative_strength_minimum": 0.01,
    "entry_delays_seconds": [1, 181],
    "fixed_hold_minutes": [1, 3, 5, 15],
    "maximum_trades_per_day": 1,
    "capital_twd": 190000,
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
POLICY_HASH = hashlib.sha256(canonical_bytes(POLICY)).hexdigest()


def sources() -> dict[str, Path]:
    selected = {str(paper["session_date"]): run_dir
                for run_dir, paper in discover_sources()}
    selected.update({day: DEFAULT_RUNS_DIR / run_id
                     for day, run_id in EXTRA_RUNS.items()})
    return dict(sorted(selected.items()))


def audit_source(day: str, run_dir: Path) -> tuple[dict, dict | None]:
    try:
        manifest = json.loads((run_dir / "run_manifest.json").read_text())
        watchlist = json.loads((run_dir / "watchlist.json").read_text())
        unsigned = {key: value for key, value in manifest.items()
                    if key != "manifest_hash"}
        if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != manifest["manifest_hash"]:
            raise ValueError("MANIFEST_HASH_MISMATCH")
        names = ("expanded_ticks.jsonl.gz", "expanded_books.jsonl.gz",
                 "market_context_ticks.jsonl.gz", "market_context_books.jsonl.gz",
                 "watchlist.json")
        for name in names:
            if name not in manifest.get("artifacts", {}):
                raise ValueError(f"MISSING_{name}")
            if sha256_file(run_dir / name) != manifest["artifacts"][name]:
                raise ValueError(f"HASH_MISMATCH_{name}")
        expanded = watchlist.get("expanded_shadow_universe") or {}
        if expanded.get("seal_hash") != manifest.get("expanded_universe_seal_hash"):
            raise ValueError("PREOPEN_SEAL_MISMATCH")
        if "0050" not in {str(row.get("stock_id")) for row in
                          watchlist.get("market_context", [])}:
            raise ValueError("0050_NOT_IN_SEALED_CONTEXT")
        start = datetime.fromisoformat(manifest["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(manifest["ended_at"].replace("Z", "+00:00"))
        created = datetime.fromisoformat(watchlist["created_at"].replace("Z", "+00:00"))
        if not (start.hour == 0 and start.minute < 60 and
                created.hour == 0 and created.minute < 60 and
                end.hour >= 5 and end.minute >= 25):
            raise ValueError("COVERAGE_NOT_PREOPEN_TO_POST_1325_UTC")
        counts = manifest.get("event_counts", {})
        if manifest.get("status") != "COMPLETE" or counts.get("callback_errors", 0):
            raise ValueError("SOURCE_NOT_COMPLETE_OR_CALLBACK_ERRORS")
        if min(counts.get("expanded_ticks", 0), counts.get("expanded_books", 0),
               counts.get("market_context_ticks", 0),
               counts.get("market_context_books", 0)) <= 0:
            raise ValueError("REQUIRED_QUOTE_CHANNEL_EMPTY")
        row = {"day": day, "run_id": run_dir.name,
               "status": "REPLAYABLE_ARCHIVE_COMPLETE",
               "started_at": manifest["started_at"],
               "ended_at": manifest["ended_at"],
               "preopen_pool_created_at": watchlist["created_at"],
               "expanded_pool_count": len(expanded.get("stocks", [])),
               "expanded_ticks": counts["expanded_ticks"],
               "expanded_books": counts["expanded_books"],
               "market_0050_ticks": counts["market_context_ticks"],
               "market_0050_books": counts["market_context_books"],
               "callback_errors": counts.get("callback_errors", 0),
               "manifest_hash": manifest["manifest_hash"],
               "intraday_quote_gap_status": "NOT_PROVEN_CONTINUOUS"}
        return row, manifest
    except (OSError, KeyError, ValueError, TypeError) as exc:
        return {"day": day, "run_id": run_dir.name,
                "status": "NOT_REPLAYABLE", "reason": str(exc)}, None


def replay_observer(day: str, run_dir: Path, target: Path) -> list[dict]:
    target.mkdir(parents=True, exist_ok=False)
    observer = RealtimeExpandedShadow(queue_size=200000)
    observer.start(run_dir=target, run_id=f"offline-{day}-{run_dir.name}",
                   expanded_items=_expanded_items(run_dir))
    # This only emits early-watch telemetry; _decide still evaluates the
    # unchanged production candidate engine, V2 gate, RS and occupancy.
    observer._observe_all_early = lambda _decision: None
    for kind, row in merged_quote_events(run_dir):
        observer._queue.put((kind, row))
        observer._offered += 1
    observer.close(timeout=600.0)
    summary = json.loads((target / SUMMARY_NAME).read_text())
    if summary.get("status") != "VALID_SHADOW_EVIDENCE":
        raise RuntimeError(f"observer invalid {day}: {summary.get('status')}")
    if summary.get("policy_hash") != OBSERVER_POLICY_HASH:
        raise RuntimeError(f"observer hash changed {day}")
    path = target / EVENTS_NAME
    if sha256_file(path) != summary["events_sha256"]:
        raise RuntimeError(f"observer events hash mismatch {day}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def quote_path_15m(data: dict, entry_at: datetime,
                   entry_price: float, quantity: int) -> dict:
    limit = entry_at + timedelta(minutes=15)
    path = []
    for source, rows, field in (("TICK", data["ticks"], "bid"),
                                ("BOOK", data["books"], "best_bid")):
        for row in rows:
            at = row["time"]
            if entry_at <= at <= limit and float(row[field]) > 0:
                net = _projected_net_pnl("LONG", entry_price,
                                         float(row[field]), quantity)[3]
                path.append((at, source, float(row[field]), net))
    if not path:
        return {"first_net_positive_status": "NA_NO_BID_15M",
                "maximum_bid_net_status": "NA_NO_BID_15M"}
    path.sort(key=lambda item: (item[0], item[1]))
    first = next((row for row in path if row[3] > 0), None)
    best = max(path, key=lambda item: (item[3], -item[0].timestamp()))
    return {
        "first_net_positive_status": "OBSERVED" if first else "NOT_OBSERVED_15M",
        "first_net_positive_at": first[0].isoformat() if first else None,
        "first_net_positive_bid": first[2] if first else None,
        "first_net_positive_twd": first[3] if first else None,
        "maximum_bid_net_status": "BID_QUOTE_PROXY",
        "maximum_bid_net_twd": best[3],
        "maximum_bid_at": best[0].isoformat(),
        "maximum_bid_price": best[2],
        "maximum_bid_source": best[1],
    }


def candidate_diagnostic(day: str, row: dict, data: dict,
                         *, arm: str, stress: bool) -> dict:
    chosen_time = row["decision_time"] if arm == "B_IMMEDIATE" else (
        datetime.fromisoformat(row["decision_time"]) +
        timedelta(seconds=180)).isoformat()
    fill, error = fill_after({**row, "decision_time": chosen_time}, data,
                             stress=stress)
    common = {"day": day, "decision_time": row["decision_time"],
              "stock_id": row["stock_id"], "stock_name": row["stock_name"],
              "score": row.get("score"), "relative_strength_5m": row.get("relative_strength_5m"),
              "arm": arm, "scenario": "STRESS_5S_2T" if stress else "BASE_1S_1T"}
    if fill is None:
        return {**common, "status": "UNFILLED_PROXY", "reason": error}
    result = {**common, **fill, "status": "BID_QUOTE_PROXY",
              "strict_executable": "NA"}
    for minute in POLICY["fixed_hold_minutes"]:
        hold = fixed_hold(data, fill["entry_time"], fill["entry_price"],
                          fill["quantity"], minute)
        result[f"hold_{minute}m_status"] = hold["status"]
        result[f"hold_{minute}m_net_twd"] = hold.get("net_pnl_twd")
        result[f"hold_{minute}m_bid"] = hold.get("bid")
        result[f"hold_{minute}m_bid_age_seconds"] = hold.get("bid_age_seconds")
    return {**result, **quote_path_15m(data, fill["entry_time"],
                                       fill["entry_price"], fill["quantity"])}


def run(output: Path, days_filter: set[str] | None = None,
        source_map: dict[str, Path] | None = None) -> dict:
    if output.exists():
        raise FileExistsError(f"output exists: {output}")
    output.mkdir(parents=True)
    audited, candidate_rows, diagnostics, account_trades, daily = [], [], [], [], []
    selected_sources = {day: run_dir for day, run_dir in
                        (source_map if source_map is not None else sources()).items()
                        if days_filter is None or day in days_filter}
    for day, run_dir in selected_sources.items():
        audit, manifest = audit_source(day, run_dir)
        audited.append(audit)
        if manifest is None:
            print(f"{day}: NOT_REPLAYABLE {audit['reason']}", flush=True)
            continue
        print(f"{day}: replaying {run_dir.name}", flush=True)
        rows = replay_observer(day, run_dir, output / "observer_runs" / day)
        seen = [row for row in rows if row.get("event") == "CANDIDATE_SEEN"]
        eligible = [row for row in seen if row.get("gate_passed") is True
                    and row.get("relative_strength_5m") is not None
                    and float(row["relative_strength_5m"]) >= 0.01]
        selected = choose_immediate(rows)
        by_time = defaultdict(list)
        for row in eligible:
            by_time[row["decision_time"]].append(row)
        rank = {}
        for when, group in by_time.items():
            for index, row in enumerate(sorted(group, key=lambda item: (
                -float(item.get("score") or 0), str(item["stock_id"]))), 1):
                rank[(when, str(row["stock_id"]))] = (index, len(group))
        for row in seen:
            symbol = str(row["stock_id"])
            volume = row.get("gate_passed") is True
            rs = row.get("relative_strength_5m")
            strength = rs is not None and float(rs) >= 0.01
            same_rank, same_size = rank.get((row["decision_time"], symbol),
                                             (None, 0))
            candidate_rows.append({"day": day, "decision_time": row["decision_time"],
                "stock_id": symbol, "stock_name": row.get("stock_name"),
                "score": row.get("score"), "volume_passed": volume,
                "relative_strength_5m": rs, "rs_gte_0_01": strength,
                "eligible": volume and strength,
                "rank_at_same_time": same_rank, "eligible_same_time": same_size,
                "daily_selected": bool(selected and symbol == str(selected["stock_id"])),
                "selection_reason": "VOLUME_REJECTED" if not volume else
                    "RS_REJECTED" if not strength else
                    "DAILY_SELECTED" if selected and symbol == str(selected["stock_id"])
                    else "SAME_TIME_SCORE_LOSER" if same_rank and same_rank > 1
                    else "LATER_DAILY_SLOT_OCCUPIED"})
        prices = {}
        if eligible:
            symbols = {str(row["stock_id"]) for row in eligible}
            prices = _verified_selected_quotes(run_dir, symbols)
            for row in eligible:
                symbol = str(row["stock_id"])
                cont = continuation(row, prices[symbol])
                for arm in ("B_IMMEDIATE", "A_WAIT_180S"):
                    for stress in (False, True):
                        diag = candidate_diagnostic(day, row, prices[symbol],
                                                    arm=arm, stress=stress)
                        diag.update({"a_continuation_status": cont["status"],
                                     "a_continuation_passed": cont.get("passed"),
                                     "a_continuation_net_twd": cont.get("conservative_net_pnl_twd"),
                                     "rank_at_same_time": rank[(row["decision_time"], symbol)][0],
                                     "eligible_same_time": rank[(row["decision_time"], symbol)][1],
                                     "daily_selected": bool(selected and symbol == str(selected["stock_id"]))})
                        diagnostics.append(diag)
        selected_cont = None
        if selected:
            symbol = str(selected["stock_id"])
            selected_cont = continuation(selected, prices[symbol])
            market = _load_market_context(run_dir, manifest)
            if selected_cont["passed"]:
                for stress in (False, True):
                    account_trades.append(replay_trade(
                        day, selected, run_dir, manifest, prices[symbol], market,
                        arm="A_WAIT_180S", stress=stress,
                        decision_time=selected_cont["due_at"]))
            for stress in (False, True):
                account_trades.append(replay_trade(
                    day, selected, run_dir, manifest, prices[symbol], market,
                    arm="B_IMMEDIATE", stress=stress))
        record = {"day": day, "run_id": run_dir.name,
                  "coverage": "REPLAYED_COMPLETE_ARCHIVE",
                  "candidate_seen": len(seen), "eligible_candidate_count": len(eligible),
                  "daily_selected_symbol": selected["stock_id"] if selected else None,
                  "a_continuation_status": selected_cont["status"] if selected_cont else
                      "NO_ELIGIBLE_SIGNAL",
                  "a_continuation_passed": selected_cont.get("passed") if selected_cont else None}
        for arm, prefix in (("A_WAIT_180S", "a"), ("B_IMMEDIATE", "b")):
            for scenario, suffix in (("BASE_1S_1T", "base"),
                                     ("STRESS_5S_2T", "stress")):
                match = next((item for item in account_trades if item["day"] == day
                              and item["arm"] == arm and item["scenario"] == scenario), None)
                record[f"{prefix}_{suffix}_status"] = match["status"] if match else (
                    "NO_ELIGIBLE_SIGNAL" if selected is None else
                    "CONTINUATION_REJECTED_OR_UNSCORABLE")
                record[f"{prefix}_{suffix}_net_twd"] = (
                    match.get("net_pnl") if match and match["status"] == "SCORED"
                    else 0 if selected is None or arm == "A_WAIT_180S" and
                        selected_cont and selected_cont.get("passed") is False
                    else None)
        daily.append(record)
        print(f"{day}: candidates={len(seen)} eligible={len(eligible)} selected={record['daily_selected_symbol']}", flush=True)
    scored_days = [row["day"] for row in daily]
    ab = [metrics(account_trades, scored_days, arm, scenario)
          for arm in ("A_WAIT_180S", "B_IMMEDIATE")
          for scenario in ("BASE_1S_1T", "STRESS_5S_2T")]
    summary = {"policy_hash": POLICY_HASH,
               "observer_policy_hash": OBSERVER_POLICY_HASH,
               "source_dates": len(selected_sources),
               "replayable_complete_dates": len(daily),
               "dates_with_eligible_candidate": sum(row["eligible_candidate_count"] > 0
                                                   for row in daily),
               "eligible_candidate_count": sum(row["eligible_candidate_count"]
                                               for row in daily),
               "same_time_competition_groups": sum(1 for row in candidate_rows
                   if row["rank_at_same_time"] == 1 and row["eligible_same_time"] >= 2),
               "ab": ab, "actual_orders": 0, "actual_fills": 0,
               "broker_order_calls": 0, "strict_executable": "NA"}
    _write_csv(output / "DATA_AUDIT.csv", audited)
    _write_csv(output / "candidate_events.csv", candidate_rows)
    _write_csv(output / "candidate_returns.csv", diagnostics)
    _write_csv(output / "ab_trades.csv", account_trades)
    _write_csv(output / "daily_results.csv", daily)
    (output / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    (output / "run_manifest.json").write_bytes(canonical_bytes({
        "policy": POLICY, "policy_hash": POLICY_HASH,
        "source_manifest_hashes": {row["day"]: row.get("manifest_hash")
                                   for row in audited},
        "reproduce": "PYTHONPATH=stock-strategy python3 -m dynamic_market_scanner_shadow_v01.v2_gate_exit_diagnostic.cohort_replay --output /new/private/path",
        "results": summary,
    }) + b"\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dates", nargs="*")
    args = parser.parse_args()
    print(json.dumps(run(args.output, set(args.dates) if args.dates else None),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
