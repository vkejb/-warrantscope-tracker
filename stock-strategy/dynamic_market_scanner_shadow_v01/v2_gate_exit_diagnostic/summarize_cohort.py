"""Summarize the frozen V2 candidate/exit cohort without changing its replay."""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
from statistics import mean, median

from yuanta_intraday_shadow_v01.collector import canonical_bytes


def read(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def number(value: str | None) -> float | None:
    if value in (None, "", "None", "NA"):
        return None
    return float(value)


def distribution(values: list[float]) -> dict:
    return {"n": len(values), "positive": sum(value > 0 for value in values),
            "negative": sum(value < 0 for value in values),
            "zero": sum(value == 0 for value in values),
            "mean_twd": mean(values) if values else None,
            "median_twd": median(values) if values else None,
            "sum_twd": sum(values) if values else None}


def summarize(folder: Path) -> dict:
    candidate = read(folder / "candidate_events.csv")
    returns = read(folder / "candidate_returns.csv")
    trades = read(folder / "ab_trades.csv")
    days = read(folder / "daily_results.csv")
    original = json.loads((folder / "summary.json").read_text())
    holds = {}
    for arm in ("B_IMMEDIATE", "A_WAIT_180S"):
        for scenario in ("BASE_1S_1T", "STRESS_5S_2T"):
            subset = [row for row in returns if row["arm"] == arm
                      and row["scenario"] == scenario]
            horizons = {}
            for minute in (1, 3, 5, 15):
                values = [number(row.get(f"hold_{minute}m_net_twd"))
                          for row in subset if row.get(f"hold_{minute}m_status")
                          == "BID_QUOTE_PROXY"]
                horizons[str(minute)] = {
                    **distribution([v for v in values if v is not None]),
                    "unscorable": len(subset) - len(values)}
            observed = [row for row in subset if row.get(
                "first_net_positive_status") == "OBSERVED"]
            maxima = [number(row.get("maximum_bid_net_twd"))
                      for row in subset if row.get("maximum_bid_net_status")
                      == "BID_QUOTE_PROXY"]
            holds[f"{arm}|{scenario}"] = {
                "eligible_candidates": len(subset), "proxy_entries": sum(
                    row.get("status") == "BID_QUOTE_PROXY" for row in subset),
                "first_net_positive_15m": len(observed),
                "maximum_bid_net_15m": distribution(
                    [v for v in maxima if v is not None]),
                "fixed_holds": horizons}
    groups = defaultdict(list)
    for row in returns:
        if row["arm"] == "B_IMMEDIATE" and row["scenario"] == "BASE_1S_1T":
            groups[(row["day"], row["decision_time"])].append(row)
    paired_ranks = []
    for (day, when), group in groups.items():
        if len(group) < 2:
            continue
        winner = next((row for row in group if row["rank_at_same_time"] == "1"), None)
        if winner is None:
            continue
        for loser in group:
            if loser is winner:
                continue
            for minute in (5, 15):
                selected = number(winner.get(f"hold_{minute}m_net_twd"))
                unselected = number(loser.get(f"hold_{minute}m_net_twd"))
                paired_ranks.append({"day": day, "decision_time": when,
                    "minute": minute, "winner_symbol": winner["stock_id"],
                    "loser_symbol": loser["stock_id"],
                    "winner_net_twd": selected, "loser_net_twd": unselected,
                    "winner_minus_loser_twd": selected - unselected
                    if selected is not None and unselected is not None else None})
    by_key = {(row["day"], row["arm"], row["scenario"]): row
              for row in trades}
    matched = []
    for day in days:
        for scenario in ("BASE_1S_1T", "STRESS_5S_2T"):
            a = by_key.get((day["day"], "A_WAIT_180S", scenario))
            b = by_key.get((day["day"], "B_IMMEDIATE", scenario))
            if not a or not b or a["status"] != "SCORED" or b["status"] != "SCORED":
                continue
            matched.append({"day": day["day"], "scenario": scenario,
                "symbol": a["stock_id"],
                "a_minus_b_entry_price": number(a["entry_price"]) -
                    number(b["entry_price"]),
                "a_minus_b_exit_net_twd": number(a["net_pnl"]) -
                    number(b["net_pnl"]),
                "a_minus_b_fixed_15m_net_twd": (
                    number(a.get("fixed_15m_net_pnl_twd")) -
                    number(b.get("fixed_15m_net_pnl_twd")))
                    if number(a.get("fixed_15m_net_pnl_twd")) is not None
                    and number(b.get("fixed_15m_net_pnl_twd")) is not None
                    else None})
    giveback = {}
    for arm in ("A_WAIT_180S", "B_IMMEDIATE"):
        for scenario in ("BASE_1S_1T", "STRESS_5S_2T"):
            subset = [row for row in trades if row["arm"] == arm
                      and row["scenario"] == scenario and row["status"] == "SCORED"]
            values = [max(0.0, number(row.get("mfe_net_pnl")) -
                          number(row.get("net_pnl"))) for row in subset
                      if number(row.get("mfe_net_pnl")) is not None
                      and number(row.get("net_pnl")) is not None]
            giveback[f"{arm}|{scenario}"] = distribution(values)
    rejected = [row for row in days if row["a_continuation_passed"] == "False"]
    rejected_b = [by_key.get((row["day"], "B_IMMEDIATE", "BASE_1S_1T"))
                  for row in rejected]
    rejected_b = [row for row in rejected_b if row and row["status"] == "SCORED"]
    return {"original_replay_summary": original, "candidate_holds": holds,
            "same_time_rank_pairs": paired_ranks,
            "matched_account_trades": matched, "strategy_mfe_giveback": giveback,
            "a_rejected_days": [row["day"] for row in rejected],
            "a_rejected_b_strategy_net": distribution(
                [number(row["net_pnl"]) for row in rejected_b]),
            "candidate_event_rows": len(candidate), "daily_rows": len(days)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, required=True)
    args = parser.parse_args()
    target = args.results / "cohort_diagnostics.json"
    if target.exists():
        raise FileExistsError(target)
    result = summarize(args.results)
    target.write_bytes(canonical_bytes(result) + b"\n")
    print(json.dumps({"days": result["daily_rows"],
        "eligible": result["original_replay_summary"]["eligible_candidate_count"],
        "matched": len(result["matched_account_trades"]),
        "rank_pairs": len(result["same_time_rank_pairs"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
