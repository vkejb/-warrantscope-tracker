"""Read-only, broker-free V2 canary gate and paired exit diagnostic."""

from __future__ import annotations

import argparse
from bisect import bisect_right
import csv
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from paper_shadow_v01.runner import _load_market_context
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC, _exit_quote, _projected_net_pnl, _tick_size,
)

from dynamic_market_scanner_shadow_v01.causal_auction import _verified_selected_quotes
from dynamic_market_scanner_shadow_v01.scanner import affordable_quantity
from dynamic_market_scanner_shadow_v01.strict_replay import (
    DEFAULT_RUNS_DIR, _simulate_exit, _write_csv,
)


RUNS = {
    "20261112": "20261112T005330.302663Z_ec27927c",
    "20261113": "20261113T005136.175446Z_811f9e3a",
}
POLICY = {
    "id": "V2_CANARY_GATE_EXIT_FAIR_AB_20261115",
    "mode": "SHADOW_ONLY_NO_LIVE_ORDER",
    "days": RUNS,
    "candidate_gate": "V2_RECENT_VOLUME_ACCELERATION",
    "relative_strength_minimum": 0.01,
    "fair_a_continuation_seconds": 180,
    "fair_a_continuation_requires_conservative_net_positive": True,
    "ranking": "EARLIEST_TIME_THEN_HIGHEST_SCORE_THEN_SYMBOL",
    "capital_twd": 190000,
    "maximum_trades_per_day": 1,
    "base_delay_seconds": 1,
    "stress_delay_seconds": 5,
    "base_extra_ask_ticks": 1,
    "stress_extra_ask_ticks": 2,
    "maximum_tick_age_seconds": 5,
    "maximum_book_age_seconds": 10,
    "maximum_spread_bps": 40,
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
POLICY_HASH = hashlib.sha256(canonical_bytes(POLICY)).hexdigest()


def events(run_dir: Path) -> list[dict]:
    path = run_dir / "realtime_expanded_shadow_events.jsonl"
    summary = json.loads((run_dir / "realtime_expanded_shadow_summary.json").read_text())
    if sha256_file(path) != summary["events_sha256"]:
        raise RuntimeError(f"event hash mismatch: {run_dir}")
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def choose_immediate(rows: list[dict]) -> dict | None:
    eligible = [r for r in rows if r.get("event") == "CANDIDATE_SEEN"
                and r.get("gate_passed") is True
                and r.get("relative_strength_5m") is not None
                and float(r["relative_strength_5m"]) >= 0.01]
    eligible.sort(key=lambda r: (r["decision_time"], -float(r["score"]), r["stock_id"]))
    return eligible[0] if eligible else None


def fill_after(signal: dict, data: dict, *, stress: bool) -> tuple[dict | None, str | None]:
    at = datetime.fromisoformat(signal["decision_time"])
    delay = POLICY["stress_delay_seconds" if stress else "base_delay_seconds"]
    target = at + timedelta(seconds=delay)
    ticks = data["ticks"]
    index = bisect_right(data["tick_times"], target)
    if index and ticks[index - 1]["time"] == target:
        index -= 1
    if index >= len(ticks):
        return None, "NO_RECEIVED_TICK_AFTER_DELAY"
    tick = ticks[index]
    age = (tick["time"] - target).total_seconds()
    if age < 0 or age > POLICY["maximum_tick_age_seconds"]:
        return None, "TICK_AFTER_DELAY_STALE"
    books = data["books"]
    book_index = bisect_right(data["book_times"], tick["time"]) - 1
    if book_index < 0:
        return None, "BOOK_MISSING"
    book = books[book_index]
    book_age = (tick["time"] - book["time"]).total_seconds()
    if book_age < 0 or book_age > POLICY["maximum_book_age_seconds"]:
        return None, "BOOK_STALE"
    bid, ask = float(book["best_bid"]), float(book["best_ask"])
    spread = (ask - bid) / ((ask + bid) / 2) * 10000
    if spread > POLICY["maximum_spread_bps"]:
        return None, "SPREAD_TOO_WIDE"
    price = ask + (2 if stress else 1) * _tick_size(ask)
    quantity, fee, cash = affordable_quantity(price)
    if quantity < 1000:
        return None, "BOARD_LOT_UNAFFORDABLE_OR_PRICE_CAP"
    return {
        "entry_time": tick["time"], "entry_price": round(price, 4),
        "quantity": quantity, "entry_fee_twd": fee,
        "cash_required_twd": round(cash, 2), "tick_delay_seconds": age,
        "book_age_seconds": book_age, "spread_bps": spread,
        "capacity_status": "UNKNOWN_BOOK_UNITS_AND_QUEUE",
    }, None


def checkpoint(data: dict, entry_at: datetime, entry_price: float,
               quantity: int, minutes: int) -> dict:
    at = entry_at + timedelta(minutes=minutes)
    index = bisect_right(data["tick_times"], at) - 1
    if index < 0:
        return {"status": "NO_TICK", "net_pnl_twd": None}
    tick = data["ticks"][index]
    age = (at - tick["time"]).total_seconds()
    if age < 0 or age > 3:
        return {"status": "STALE_BID", "net_pnl_twd": None, "bid_age_seconds": age}
    bid = float(tick["bid"])
    if bid <= 0:
        return {"status": "NO_BID", "net_pnl_twd": None}
    return {"status": "BID_PROXY", "at": at.isoformat(), "bid": bid,
            "bid_age_seconds": age,
            "net_pnl_twd": _projected_net_pnl("LONG", entry_price, bid, quantity)[3]}


def continuation(signal: dict, data: dict) -> dict:
    due = datetime.fromisoformat(signal["decision_time"]) + timedelta(seconds=180)
    index = bisect_right(data["tick_times"], due) - 1
    if index < 0:
        return {"status": "NO_TICK", "passed": None, "due_at": due.isoformat()}
    tick = data["ticks"][index]
    age = (due - tick["time"]).total_seconds()
    if age < 0 or age > POLICY["maximum_tick_age_seconds"]:
        return {"status": "TICK_STALE", "passed": None,
                "due_at": due.isoformat(), "tick_age_seconds": age}
    price = _exit_quote("LONG", tick)
    net = _projected_net_pnl("LONG", float(signal["entry_price"]), price,
                             int(signal["quantity"]))[3]
    return {"status": "EVALUATED", "passed": net > 0,
            "due_at": due.isoformat(), "conservative_exit_price": price,
            "conservative_net_pnl_twd": net, "tick_age_seconds": age}


def trade_for_signal(day: str, signal: dict, run_dir: Path,
                     manifest: dict, stress: bool, *, arm: str,
                     entry_decision_time: str | None = None) -> dict:
    symbol = str(signal["stock_id"])
    data = _verified_selected_quotes(run_dir, {symbol})
    entry_signal = {**signal, "decision_time": entry_decision_time or signal["decision_time"]}
    fill, failure = fill_after(entry_signal, data[symbol], stress=stress)
    scenario = "STRESS_5S_2T" if stress else "BASE_1S_1T"
    common = {"day": day, "arm": arm, "scenario": scenario,
              "stock_id": symbol, "stock_name": signal["stock_name"],
              "signal_time": signal["decision_time"],
              "score": signal["score"], "relative_strength_5m": signal["relative_strength_5m"],
              "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0}
    if fill is None:
        return {**common, "status": "UNFILLED_PROXY", "reason": failure,
                "net_pnl": None, "gross_pnl": None}
    market = _load_market_context(run_dir, manifest)
    selected = {"stock_id": symbol, "stock_name": signal["stock_name"],
                "signal_time": datetime.fromisoformat(signal["decision_time"]),
                "confirmation_time": fill["entry_time"],
                "delayed_entry_price": fill["entry_price"],
                "delayed_entry_quantity": fill["quantity"]}
    end_taipei = datetime.fromisoformat(manifest["ended_at"].replace("Z", "+00:00"))
    end_taipei = end_taipei.astimezone(ZoneInfo("Asia/Taipei"))
    coverage = {"session_date": day, "ended_at_taipei": end_taipei.isoformat()}
    result = _simulate_exit(selected, data, market, coverage)
    checkpoints = {}
    exit_at = datetime.fromisoformat(result["exit_time"]) if result.get("exit_time") else None
    for minute in (5, 15):
        target = fill["entry_time"] + timedelta(minutes=minute)
        checkpoints[f"m{minute}"] = (
            {"status": "ALREADY_EXITED", "net_pnl_twd": None}
            if exit_at is not None and exit_at < target
            else checkpoint(data[symbol], fill["entry_time"],
                            fill["entry_price"], fill["quantity"], minute)
        )
    return {**common, **fill, **result, "status": result["status"],
            "reason": result.get("exit_reason"),
            "m5_net_pnl_twd": checkpoints["m5"]["net_pnl_twd"],
            "m5_status": checkpoints["m5"]["status"],
            "m15_net_pnl_twd": checkpoints["m15"]["net_pnl_twd"],
            "m15_status": checkpoints["m15"]["status"],
            "execution_confidence": "CONDITIONAL_QUOTE_PROXY_NOT_QUEUE_FILL"}


def historical_baseline() -> tuple[list[dict], dict]:
    path = (Path(__file__).resolve().parents[1] /
            "volume_persistence_followup_results" / "trades.csv")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [r for r in csv.DictReader(handle)
                if r["variant"] == "V2_RECENT_VOLUME_ACCELERATION"
                and r["scenario"] == "BASE" and r["status"] == "SCORED"]
    result = {"count": len(rows),
              "gross_pnl_twd": sum(float(r["gross_pnl"]) for r in rows),
              "commission_twd": sum(float(r["commission"]) for r in rows),
              "tax_twd": sum(float(r["sell_tax"]) for r in rows),
              "net_pnl_twd": sum(float(r["net_pnl"]) for r in rows),
              "source_sha256": sha256_file(path)}
    if (result["count"], result["gross_pnl_twd"], result["net_pnl_twd"]) != (10, -400, -5682):
        raise RuntimeError("frozen 10-trade baseline totals differ")
    return rows, result


def breakeven_tick(entry: float, quantity: int) -> float:
    price = entry
    for _ in range(10000):
        if _projected_net_pnl("LONG", entry, price, quantity)[3] >= 0:
            return round(price, 4)
        price = round(price + _tick_size(price), 4)
    raise RuntimeError("breakeven tick not found")


def paired_breakeven(rows: list[dict]) -> list[dict]:
    # Existing historical source mapping is fixed by its dated research ledger.
    from dynamic_market_scanner_shadow_v01.strict_replay import discover_sources
    sources = {str(paper["session_date"]): run_dir
               for run_dir, paper in discover_sources()}
    output = []
    for row in rows:
        day, symbol = row["session_date"], row["stock_id"]
        base = {"day": day, "stock_id": symbol, "stock_name": row["stock_name"],
                "entry_time": row["entry_time"], "entry_price": row["entry_price"],
                "quantity": row["quantity"], "baseline_exit_time": row["exit_time"],
                "baseline_exit_price": row["exit_price"],
                "baseline_exit_reason": row["exit_reason"],
                "baseline_gross_pnl": row["gross_pnl"],
                "baseline_net_pnl": row["net_pnl"],
                "baseline_mfe_net_pnl": row["mfe_net_pnl"],
                "baseline_mae_net_pnl": row["mae_net_pnl"]}
        if day not in sources:
            output.append({**base, "status": "SOURCE_RUN_MISSING"})
            continue
        data = _verified_selected_quotes(sources[day], {symbol})[symbol]
        entry_at = datetime.fromisoformat(row["entry_time"])
        baseline_exit = datetime.fromisoformat(row["exit_time"])
        entry = float(row["entry_price"])
        quantity = int(row["quantity"])
        floor = breakeven_tick(entry, quantity)
        armed = False
        mfe, mae = None, None
        chosen = None
        for tick in data["ticks"]:
            at = tick["time"]
            if at < entry_at:
                continue
            if at > baseline_exit:
                break
            bid = float(tick["bid"])
            if bid <= 0:
                continue
            net = _projected_net_pnl("LONG", entry, bid, quantity)[3]
            mfe = net if mfe is None else max(mfe, net)
            mae = net if mae is None else min(mae, net)
            if armed and bid <= floor:
                chosen = (at, bid)
                break
            if net > 0:
                armed = True
        if chosen is None:
            new_at, new_price = baseline_exit, float(row["exit_price"])
            new_reason = row["exit_reason"]
        else:
            new_at, new_price = chosen
            new_reason = "AFTER_COST_BREAKEVEN_FLOOR_BID_PROXY"
        gross, commission, tax, net = _projected_net_pnl(
            "LONG", entry, new_price, quantity)
        output.append({**base, "status": "SCORED_PROXY",
                       "floor_tick": floor, "floor_armed": armed,
                       "new_exit_time": new_at.isoformat(),
                       "new_exit_price": new_price, "new_exit_reason": new_reason,
                       "new_gross_pnl": gross, "new_commission": commission,
                       "new_tax": tax, "new_net_pnl": net,
                       "delta_net_pnl": net - float(row["net_pnl"]),
                       "observed_bid_mfe_net_pnl": mfe,
                       "observed_bid_mae_net_pnl": mae,
                       "capacity_status": "UNKNOWN_BOOK_UNITS_AND_QUEUE",
                       "actual_orders": 0, "actual_fills": 0})
    return output


def metrics(trades: list[dict], days: list[str], arm: str, scenario: str) -> dict:
    selected = [r for r in trades if r.get("arm") == arm and r.get("scenario") == scenario]
    scored = [r for r in selected if r.get("status") == "SCORED"]
    by_day = {r["day"]: float(r["net_pnl"]) for r in scored}
    values = [float(r["net_pnl"]) for r in scored]
    wins = [x for x in values if x > 0]
    losses = [x for x in values if x < 0]
    equity = peak = 190000.0
    drawdown = 0.0
    for day in days:
        equity += by_day.get(day, 0.0)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {"arm": arm, "scenario": scenario, "covered_dates": len(days),
            "conditional_proxy_trades": len(scored),
            "unfilled_proxy": len(selected) - len(scored),
            "gross_pnl_twd": sum(float(r["gross_pnl"]) for r in scored),
            "net_pnl_twd": sum(values),
            "profit_factor": (sum(wins) / -sum(losses)) if losses else None,
            "max_daily_drawdown_twd": drawdown,
            "strict_executable": "NA",
            "actual_orders": 0, "actual_fills": 0}


def run(output: Path) -> dict:
    if output.exists():
        raise FileExistsError(f"independent output directory already exists: {output}")
    output.mkdir(parents=True)
    daily, trades, events_out, source_hashes = [], [], [], {}
    for day, run_id in RUNS.items():
        run_dir = DEFAULT_RUNS_DIR / run_id
        manifest = json.loads((run_dir / "run_manifest.json").read_text())
        rows = events(run_dir)
        source_hashes[day] = manifest["manifest_hash"]
        seen = [r for r in rows if r["event"] == "CANDIDATE_SEEN"]
        selected_a = [r for r in rows if r["event"] == "SHADOW_SIGNAL_SELECTED"]
        continuation_events = [r for r in rows if r["event"] == "THREE_MINUTE_CONTINUATION"]
        mature = [r for r in rows if r["event"] in
                  ("MATURE_CANDIDATE_CALLBACK", "HISTORICAL_MATURE_CANDIDATE_CONSUMED")]
        selected_b = choose_immediate(rows)
        selected_fair_a = selected_b
        fair_continuation = None
        if selected_fair_a:
            quote = _verified_selected_quotes(run_dir, {str(selected_fair_a["stock_id"])})
            fair_continuation = continuation(selected_fair_a,
                                             quote[str(selected_fair_a["stock_id"])])
        for row in seen:
            events_out.append({"day": day, "decision_time": row["decision_time"],
                               "stock_id": row["stock_id"], "stock_name": row["stock_name"],
                               "score": row["score"],
                               "relative_strength_5m": row["relative_strength_5m"],
                               "v2_volume_passed": row["gate_passed"],
                               "v2_reason": row["gate_reason"],
                               "rs_challenger_passed": (float(row["relative_strength_5m"]) >= 0.01
                                                        if row.get("relative_strength_5m") is not None else None),
                               "actual_a_selected": bool(selected_a and row["stock_id"] == selected_a[0]["stock_id"]),
                               "fair_a_candidate": bool(selected_fair_a and row["stock_id"] == selected_fair_a["stock_id"]),
                               "b_selected": bool(selected_b and row["stock_id"] == selected_b["stock_id"]),
                               "actual_orders": 0, "actual_fills": 0})
        coverage = "TO_13_20_NOT_FULL_CLOSE" if day == "20261112" else "PARTIAL_TO_10_18"
        daily.append({"day": day, "run_id": run_id, "coverage": coverage,
                      "candidate_seen": len(seen),
                      "actual_a_selected_symbol": selected_a[0]["stock_id"] if selected_a else None,
                      "actual_a_continuation_net": continuation_events[0].get("conservative_net_pnl_twd")
                      if continuation_events else None,
                      "actual_a_mature_count": len(mature),
                      "fair_a_candidate_symbol": selected_fair_a["stock_id"] if selected_fair_a else None,
                      "fair_a_continuation_status": fair_continuation["status"] if fair_continuation else "NO_ELIGIBLE_SIGNAL",
                      "fair_a_continuation_net_twd": fair_continuation.get("conservative_net_pnl_twd") if fair_continuation else None,
                      "fair_a_continuation_passed": fair_continuation["passed"] if fair_continuation else None,
                      "b_selected_symbol": selected_b["stock_id"] if selected_b else None,
                      "actual_a_net_pnl_twd": 0 if not mature else None,
                      "actual_orders": 0, "actual_fills": 0})
        if mature:
            raise RuntimeError("mature A candidate needs explicit fill replay, not handled by this audit")
        if selected_fair_a and fair_continuation and fair_continuation["passed"]:
            for stress in (False, True):
                trades.append(trade_for_signal(
                    day, selected_fair_a, run_dir, manifest, stress,
                    arm="A_WAIT_180S", entry_decision_time=fair_continuation["due_at"]))
        if selected_b:
            for stress in (False, True):
                trades.append(trade_for_signal(day, selected_b, run_dir, manifest, stress,
                                               arm="B_IMMEDIATE"))
    old_rows, baseline = historical_baseline()
    paired = paired_breakeven(old_rows)
    for row in daily:
        for arm, prefix in (("A_WAIT_180S", "fair_a"), ("B_IMMEDIATE", "b")):
            for scenario, suffix in (("BASE_1S_1T", "base"),
                                     ("STRESS_5S_2T", "stress")):
                match = next((trade for trade in trades if trade["day"] == row["day"]
                              and trade["scenario"] == scenario and trade["arm"] == arm), None)
                row[f"{prefix}_{suffix}_status"] = (
                    match["status"] if match else
                    "CONTINUATION_REJECTED_OR_UNSCORABLE" if arm == "A_WAIT_180S"
                    and row["fair_a_candidate_symbol"] else "NO_ELIGIBLE_SIGNAL")
                row[f"{prefix}_{suffix}_net_pnl_twd"] = (
                    match.get("net_pnl") if match and match["status"] == "SCORED"
                    else 0 if match is None and (arm == "B_IMMEDIATE"
                                                 or row["fair_a_continuation_status"] in
                                                 ("NO_ELIGIBLE_SIGNAL", "EVALUATED"))
                    else None
                )
    _write_csv(output / "candidate_events.csv", events_out)
    _write_csv(output / "ab_trades.csv", trades)
    _write_csv(output / "daily_results.csv", daily)
    _write_csv(output / "historical_10_paired_breakeven.csv", paired)
    summary = {"policy_hash": POLICY_HASH, "days": list(RUNS),
               "ab": [metrics(trades, list(RUNS), arm, scenario)
                      for arm in ("A_WAIT_180S", "B_IMMEDIATE")
                      for scenario in ("BASE_1S_1T", "STRESS_5S_2T")],
               "historical_baseline": baseline,
               "historical_breakeven": {
                   "scored": sum(r["status"] == "SCORED_PROXY" for r in paired),
                   "net_pnl_twd": sum(float(r.get("new_net_pnl", 0)) for r in paired),
                   "delta_twd": sum(float(r.get("delta_net_pnl", 0)) for r in paired),
               }, "strict_executable": "NA", "actual_orders": 0,
               "actual_fills": 0, "broker_order_calls": 0}
    # A has no archived mature candidates on either covered period; record its
    # zero proxy PnL explicitly without pretending to have a scored fill.
    (output / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    manifest = {"policy": POLICY, "policy_hash": POLICY_HASH,
                "source_manifest_hashes": source_hashes,
                "reproduce": "PYTHONPATH=stock-strategy python3 -m dynamic_market_scanner_shadow_v01.v2_gate_exit_diagnostic.replay --output /private/output/path",
                "results": summary}
    (output / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
