"""Frozen follow-up: complete-session V2 A/B and post-exit fixed holds.

This module only reads archived files. It has no broker imports or order calls.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from paper_shadow_v01.runner import _load_market_context
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    _projected_net_pnl, _tick_size,
)
from yuanta_live_runtime_v01.strategy import ManagedPosition

from dynamic_market_scanner_shadow_v01.causal_auction import _verified_selected_quotes
from dynamic_market_scanner_shadow_v01.strict_replay import (
    DEFAULT_RUNS_DIR, _engine, _simulate_exit, _write_csv,
)
from .replay import choose_immediate, continuation, events, fill_after


RUNS = {
    "20261111": ("20261111T005011.762860Z_d43f8694", "OLD_OBSERVER_CONTEXT"),
    "20261112": ("20261112T005011.064631Z_fcdaa5e4", "PRIMARY_V2"),
    "20261113": ("20261113T005010.446883Z_c876f743", "PRIMARY_V2"),
}
POLICY = {
    "id": "V2_CANDIDATE_FIXED_HOLD_FOLLOWUP_20261115",
    "mode": "SHADOW_ONLY_NO_LIVE_ORDER",
    "runs": RUNS,
    "selection": "V2_VOLUME_AND_RS_GTE_0_01_BEFORE_RESERVATION",
    "entry_arms": ["A_WAIT_180S", "B_IMMEDIATE"],
    "entry_scenarios": ["BASE_1S_1T", "STRESS_5S_2T"],
    "fixed_hold_minutes": [5, 15],
    "fixed_hold_bid_max_age_seconds": 3,
    "capital_twd": 190000,
    "maximum_trades_per_day": 1,
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
POLICY_HASH = hashlib.sha256(canonical_bytes(POLICY)).hexdigest()


def fixed_hold(data: dict, entry_at: datetime, entry_price: float,
               quantity: int, minutes: int) -> dict:
    """Value an independent hypothetical hold using only bids received by T."""
    target = entry_at + timedelta(minutes=minutes)
    candidates = []
    for source, rows, times, field in (
        ("TICK_BUY_PRICE", data["ticks"], data["tick_times"], "bid"),
        ("BOOK_BEST_BID", data["books"], data["book_times"], "best_bid"),
    ):
        index = bisect_right(times, target) - 1
        if index >= 0:
            row = rows[index]
            age = (target - row["time"]).total_seconds()
            if age >= 0 and float(row[field]) > 0:
                candidates.append((age, source, float(row[field]), row["time"]))
    if not candidates:
        return {"status": "NA_NO_PRIOR_BID", "net_pnl_twd": None,
                "target_at": target.isoformat()}
    age, source, bid, received_at = min(candidates, key=lambda row: row[0])
    if age > POLICY["fixed_hold_bid_max_age_seconds"]:
        return {"status": "NA_STALE_BID", "net_pnl_twd": None,
                "target_at": target.isoformat(), "latest_bid_age_seconds": age}
    gross, fee, tax, net = _projected_net_pnl("LONG", entry_price, bid, quantity)
    return {"status": "BID_QUOTE_PROXY", "net_pnl_twd": net,
            "gross_pnl_twd": gross, "commission_twd": fee, "sell_tax_twd": tax,
            "bid": bid, "bid_source": source, "bid_received_at": received_at.isoformat(),
            "bid_age_seconds": age, "target_at": target.isoformat()}


def one_r_diagnostic(symbol: str, name: str, entry_at: datetime,
                     entry_price: float, quantity: int, mfe_net_pnl: float | None,
                     exit_reason: str | None) -> dict:
    """Use the original engine's initial-stop definition; no new risk rule."""
    engine = _engine(
        {symbol: {"meta": {"stock_name": name}}},
        {"0050": {"meta": {"stock_name": "元大台灣50"}}},
    )
    position = ManagedPosition(symbol, name, "LONG", quantity,
                               entry_price, "SHADOW_ONLY", entry_at)
    stop = engine._derive_initial_stop_price(position)
    risk_per_share = entry_price - stop
    raw_one_r = entry_price + risk_per_share
    tick = _tick_size(raw_one_r)
    one_r_tick = round(math.ceil(raw_one_r / tick - 1e-9) * tick, 4)
    one_r_net = _projected_net_pnl("LONG", entry_price,
                                   one_r_tick, quantity)[3]
    armed = exit_reason == "MFE_PROFIT_PROTECTION" or (
        mfe_net_pnl is not None and float(mfe_net_pnl) >= one_r_net
    )
    return {"initial_stop_price": stop, "one_r_price": one_r_tick,
            "one_r_projected_net_twd": one_r_net,
            "one_r_observed_or_armed": armed,
            "one_r_basis": "ENTRY_TO_NET_3500_STOP_PRICE"}


def replay_trade(day: str, row: dict, run_dir: Path, manifest: dict,
                 data: dict, market: dict, *, arm: str, stress: bool,
                 decision_time: str | None = None) -> dict:
    signal = {**row, "decision_time": decision_time or row["decision_time"]}
    fill, error = fill_after(signal, data, stress=stress)
    scenario = "STRESS_5S_2T" if stress else "BASE_1S_1T"
    common = {"day": day, "arm": arm, "scenario": scenario,
              "stock_id": row["stock_id"], "stock_name": row["stock_name"],
              "signal_time": row["decision_time"], "actual_orders": 0,
              "actual_fills": 0, "broker_order_calls": 0}
    if fill is None:
        return {**common, "status": "UNFILLED_PROXY", "reason": error,
                "net_pnl": None, "gross_pnl": None}
    selected = {"stock_id": str(row["stock_id"]), "stock_name": row["stock_name"],
                "signal_time": datetime.fromisoformat(row["decision_time"]),
                "confirmation_time": fill["entry_time"],
                "delayed_entry_price": fill["entry_price"],
                "delayed_entry_quantity": fill["quantity"]}
    end = datetime.fromisoformat(manifest["ended_at"].replace("Z", "+00:00"))
    coverage = {"session_date": day,
                "ended_at_taipei": end.astimezone(ZoneInfo("Asia/Taipei")).isoformat()}
    result = _simulate_exit(selected, {str(row["stock_id"]): data}, market, coverage)
    holds = {f"fixed_{minutes}m": fixed_hold(data, fill["entry_time"],
               fill["entry_price"], fill["quantity"], minutes)
             for minutes in POLICY["fixed_hold_minutes"]}
    risk = one_r_diagnostic(str(row["stock_id"]), row["stock_name"],
                            fill["entry_time"], fill["entry_price"],
                            fill["quantity"], result.get("mfe_net_pnl"),
                            result.get("exit_reason"))
    flat_holds = {}
    for label, hold in holds.items():
        for key, value in hold.items():
            flat_holds[f"{label}_{key}"] = value
    return {**common, **fill, **result, **risk, **flat_holds,
            "status": result["status"], "reason": result.get("exit_reason"),
            "strict_executable": "NA"}


def metrics(trades: list[dict], days: list[str], arm: str, scenario: str) -> dict:
    selected = [row for row in trades if row["arm"] == arm
                and row["scenario"] == scenario and row["day"] in days]
    scored = [row for row in selected if row["status"] == "SCORED"]
    by_day = {row["day"]: float(row["net_pnl"]) for row in scored}
    net = [float(row["net_pnl"]) for row in scored]
    positive = sum(value for value in net if value > 0)
    negative = -sum(value for value in net if value < 0)
    equity = peak = 190000.0
    drawdown = 0.0
    for day in days:
        equity += by_day.get(day, 0.0)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {"arm": arm, "scenario": scenario, "complete_days": len(days),
            "proxy_trades": len(scored), "unfilled_proxy": len(selected) - len(scored),
            "no_trade_days": len(days) - len(selected),
            "gross_pnl_twd": sum(float(row["gross_pnl"]) for row in scored),
            "net_pnl_twd": sum(net),
            "profit_factor": positive / negative if negative else None,
            "maximum_daily_drawdown_twd": drawdown,
            "strict_executable": "NA"}


def run(output: Path) -> dict:
    if output.exists():
        raise FileExistsError(f"output exists: {output}")
    output.mkdir(parents=True)
    daily, trades, candidate_rows, hashes = [], [], [], {}
    for day, (run_id, class_name) in RUNS.items():
        run_dir = DEFAULT_RUNS_DIR / run_id
        manifest = json.loads((run_dir / "run_manifest.json").read_text())
        if manifest.get("status") != "COMPLETE":
            raise RuntimeError(f"not complete: {run_id}")
        rows = events(run_dir)
        hashes[day] = manifest["manifest_hash"]
        seen = [row for row in rows if row["event"] == "CANDIDATE_SEEN"]
        chosen = choose_immediate(rows)
        for row in seen:
            volume = row.get("gate_passed") is True
            rs = row.get("relative_strength_5m")
            strength = rs is not None and float(rs) >= 0.01
            candidate_rows.append({"day": day, "observer_policy": class_name,
                                   "decision_time": row["decision_time"],
                                   "stock_id": row["stock_id"],
                                   "stock_name": row["stock_name"],
                                   "score": row["score"],
                                   "volume_passed": volume,
                                   "relative_strength_5m": rs,
                                   "rs_gte_0_01": strength,
                                   "corrected_selection": bool(chosen and
                                    row["stock_id"] == chosen["stock_id"]),
                                   "reason": "VOLUME_REJECTED" if not volume else
                                   "RS_REJECTED_BEFORE_RESERVATION" if not strength else
                                   "SELECTED" if chosen and row["stock_id"] == chosen["stock_id"]
                                   else "LATER_OR_LOWER_RANK"})
        continuation_result = None
        if chosen:
            selected_data = _verified_selected_quotes(run_dir, {str(chosen["stock_id"])})
            data = selected_data[str(chosen["stock_id"])]
            continuation_result = continuation(chosen, data)
            market = _load_market_context(run_dir, manifest)
            if continuation_result["passed"]:
                for stress in (False, True):
                    trades.append(replay_trade(day, chosen, run_dir, manifest,
                        data, market, arm="A_WAIT_180S", stress=stress,
                        decision_time=continuation_result["due_at"]))
            for stress in (False, True):
                trades.append(replay_trade(day, chosen, run_dir, manifest,
                    data, market, arm="B_IMMEDIATE", stress=stress))
        record = {"day": day, "run_id": run_id, "observer_policy": class_name,
                  "coverage": "COMPLETE_08_50_TO_13_35", "candidate_seen": len(seen),
                  "corrected_selected_symbol": chosen["stock_id"] if chosen else None,
                  "a_continuation_status": continuation_result["status"]
                      if continuation_result else "NO_ELIGIBLE_SIGNAL",
                  "a_continuation_net_twd": continuation_result.get("conservative_net_pnl_twd")
                      if continuation_result else None}
        for arm, prefix in (("A_WAIT_180S", "a"), ("B_IMMEDIATE", "b")):
            for scenario, suffix in (("BASE_1S_1T", "base"),
                                     ("STRESS_5S_2T", "stress")):
                match = next((trade for trade in trades if trade["day"] == day
                              and trade["arm"] == arm and trade["scenario"] == scenario), None)
                record[f"{prefix}_{suffix}_status"] = match["status"] if match else (
                    "NO_ELIGIBLE_SIGNAL" if chosen is None else "CONTINUATION_REJECTED_OR_UNKNOWN")
                record[f"{prefix}_{suffix}_net_pnl_twd"] = (
                    match["net_pnl"] if match and match["status"] == "SCORED"
                    else 0 if chosen is None or arm == "A_WAIT_180S" and
                        continuation_result and continuation_result["passed"] is False
                    else None)
        daily.append(record)
    primary = [day for day, (_, class_name) in RUNS.items() if class_name == "PRIMARY_V2"]
    ab = [metrics(trades, primary, arm, scenario)
          for arm in ("A_WAIT_180S", "B_IMMEDIATE")
          for scenario in ("BASE_1S_1T", "STRESS_5S_2T")]
    summary = {"policy_hash": POLICY_HASH, "complete_archives": len(RUNS),
               "primary_comparable_complete_days": len(primary),
               "primary_dates": primary, "ab": ab,
               "actual_orders": 0, "actual_fills": 0,
               "broker_order_calls": 0, "strict_executable": "NA"}
    _write_csv(output / "candidate_events.csv", candidate_rows)
    _write_csv(output / "ab_trades.csv", trades)
    _write_csv(output / "daily_results.csv", daily)
    (output / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    (output / "run_manifest.json").write_bytes(canonical_bytes({
        "policy": POLICY, "policy_hash": POLICY_HASH,
        "source_manifest_hashes": hashes,
        "reproduce": "PYTHONPATH=stock-strategy python3 -m dynamic_market_scanner_shadow_v01.v2_gate_exit_diagnostic.followup_replay --output /new/private/path",
        "results": summary,
    }) + b"\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
