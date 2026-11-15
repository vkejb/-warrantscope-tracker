"""Append-only, post-close forward A/B shadow on the existing archived feed.

No broker client, order adapter, or quote subscription is imported here.
"""

from __future__ import annotations

from bisect import bisect_right
import csv
from datetime import datetime, timedelta
import fcntl
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from zoneinfo import ZoneInfo

from yuanta_intraday_shadow_v01.collector import (
    DEFAULT_RUNTIME_DIR, canonical_bytes, sha256_file,
)
from . import cohort_replay


TAIPEI = ZoneInfo("Asia/Taipei")
ROOT = DEFAULT_RUNTIME_DIR / "ab_forward_v01"
ACTIVATION = ROOT / "activation.json"
START_DATE = "20261116"
BASELINE_COMMIT = "7efafe0b2d7e9097030ad2bbc523de64cd80d6e0"
SOURCE_FILES = (
    "yuanta_intraday_shadow_v01/auto_runner.py",
    "yuanta_intraday_shadow_v01/direction_follow_backtest.py",
    "yuanta_live_runtime_v01/strategy.py",
    "paper_shadow_v01/runner.py",
    "dynamic_market_scanner_shadow_v01/policy.py",
    "dynamic_market_scanner_shadow_v01/scanner.py",
    "dynamic_market_scanner_shadow_v01/early_entry_backtest.py",
    "dynamic_market_scanner_shadow_v01/causal_auction.py",
    "dynamic_market_scanner_shadow_v01/v2_gate_exit_diagnostic/cohort_replay.py",
    "dynamic_market_scanner_shadow_v01/v2_gate_exit_diagnostic/forward_shadow.py",
    "dynamic_market_scanner_shadow_v01/v2_gate_exit_diagnostic/replay.py",
    "dynamic_market_scanner_shadow_v01/v2_gate_exit_diagnostic/followup_replay.py",
    "dynamic_market_scanner_shadow_v01/realtime_expanded_shadow.py",
    "dynamic_market_scanner_shadow_v01/strict_replay.py",
)
STOCK_STRATEGY = Path(__file__).resolve().parents[2]
CONFIG = {
    "id": "V2_AB_FORWARD_SHADOW_20261115",
    "mode": "SHADOW_ONLY_NO_LIVE_ORDER",
    "baseline_commit": BASELINE_COMMIT,
    "start_session_date": START_DATE,
    "arms": ["A_WAIT_180S", "B_IMMEDIATE"],
    "scenarios": ["BASE_1S_1T", "STRESS_5S_2T"],
    "capital_twd": 190000,
    "one_trade_per_day": True,
    "collector": "existing_yuanta_intraday_shadow_full_session",
    "calculation": "post_close_received_time_replay",
    "observer_policy_hash": cohort_replay.OBSERVER_POLICY_HASH,
    "cohort_policy_hash": cohort_replay.POLICY_HASH,
    "book_volume_unit": "UNKNOWN_RAW_SDK_VALUE",
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
CONFIG_HASH = hashlib.sha256(canonical_bytes(CONFIG)).hexdigest()


def _head() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=STOCK_STRATEGY,
                            check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _file_hashes() -> dict[str, str]:
    return {name: sha256_file(STOCK_STRATEGY / name) for name in SOURCE_FILES}


def _atomic_json(path: Path, data: dict) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False,
                                     prefix=".activation-") as handle:
        handle.write(canonical_bytes(data) + b"\n")
        temporary = Path(handle.name)
    if path.exists():
        raise FileExistsError(path)
    temporary.rename(path)


def activate() -> dict:
    """Freeze code/config once; does not launch a collector or subscribe quotes."""
    if ACTIVATION.exists():
        data = json.loads(ACTIVATION.read_text())
        _require_activation(data["start_session_date"])
        return data
    current = datetime.now(TAIPEI)
    if current.strftime("%Y%m%d") >= START_DATE:
        raise RuntimeError("activation would retroactively include a viewed date")
    data = {"activated_at": current.isoformat(), "head": _head(),
            "source_baseline_commit": BASELINE_COMMIT,
            "start_session_date": START_DATE, "config": CONFIG,
            "config_hash": CONFIG_HASH, "file_hashes": _file_hashes(),
            "actual_orders": 0, "broker_order_calls": 0}
    _atomic_json(ACTIVATION, data)
    return data


def _require_activation(day: str) -> dict:
    if not ACTIVATION.exists():
        raise RuntimeError("AB_FORWARD_NOT_ACTIVATED")
    data = json.loads(ACTIVATION.read_text())
    if day < data["start_session_date"]:
        raise RuntimeError("DAY_BEFORE_FORWARD_FREEZE")
    if data.get("config_hash") != CONFIG_HASH or data.get("config") != CONFIG:
        raise RuntimeError("FORWARD_CONFIG_DRIFT")
    if data.get("head") != _head() or data.get("file_hashes") != _file_hashes():
        raise RuntimeError("FORWARD_CODE_DRIFT")
    if data.get("actual_orders") != 0 or data.get("broker_order_calls") != 0:
        raise RuntimeError("FORWARD_ORDER_CAPABILITY_NOT_ZERO")
    return data


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _received(row: dict) -> datetime:
    return datetime.fromisoformat(str(row["received_at"]).replace("Z", "+00:00")).astimezone(TAIPEI)


def _raw_quotes(run_dir: Path, symbols: set[str]) -> dict[str, dict]:
    quotes = {symbol: {"ticks": [], "books": []} for symbol in symbols}
    if not symbols:
        return quotes
    for filename, kind in (("expanded_ticks.jsonl.gz", "ticks"),
                           ("expanded_books.jsonl.gz", "books")):
        with gzip.open(run_dir / filename, "rt", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                symbol = str(row.get("stock_id", ""))
                if symbol in quotes:
                    quotes[symbol][kind].append((_received(row), row))
    for stock in quotes.values():
        for kind in ("ticks", "books"):
            stock[kind].sort(key=lambda item: item[0])
            time_key = "tick_times" if kind == "ticks" else "book_times"
            stock[time_key] = [item[0] for item in stock[kind]]
    return quotes


def _latest(stock: dict, kind: str, at: datetime) -> tuple[datetime, dict] | None:
    time_key = "tick_times" if kind == "ticks" else "book_times"
    index = bisect_right(stock[time_key], at) - 1
    return stock[kind][index] if index >= 0 else None


def quote_checkpoint(stock: dict, at: datetime) -> dict:
    tick = _latest(stock, "ticks", at)
    book = _latest(stock, "books", at)
    tick_age = (at - tick[0]).total_seconds() if tick else None
    book_age = (at - book[0]).total_seconds() if book else None
    raw_tick = tick[1] if tick else {}
    raw_book = book[1] if book else {}
    return {"at": at.isoformat(),
            "tick_received_at": tick[0].isoformat() if tick else None,
            "tick_exchange_time_raw": raw_tick.get("quote_time"),
            "tick_age_seconds": tick_age,
            "tick_fresh_5s": tick_age is not None and tick_age <= 5,
            "tick_buy_price": raw_tick.get("buy_price"),
            "tick_sell_price": raw_tick.get("sell_price"),
            "book_received_at": book[0].isoformat() if book else None,
            "book_exchange_time": None,
            "book_age_seconds": book_age,
            "book_fresh_10s": book_age is not None and book_age <= 10,
            "buy_prices": raw_book.get("buy_prices"),
            "buy_volumes_raw": raw_book.get("buy_volumes"),
            "sell_prices": raw_book.get("sell_prices"),
            "sell_volumes_raw": raw_book.get("sell_volumes"),
            "book_volume_unit": "UNKNOWN_RAW_SDK_VALUE",
            "capacity_shares": None,
            "capacity_status": "UNKNOWN_BOOK_UNITS_AND_QUEUE"}


def _candidate_checkpoints(run_dir: Path, result: Path) -> int:
    candidates = [row for row in _read_csv(result / "candidate_events.csv")
                  if row.get("eligible") == "True"]
    diagnostic = _read_csv(result / "candidate_returns.csv")
    lookup = {(row["day"], row["decision_time"], row["stock_id"],
               row["arm"], row["scenario"]): row for row in diagnostic}
    quotes = _raw_quotes(run_dir, {row["stock_id"] for row in candidates})
    path = result / "candidate_quote_checkpoints.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in candidates:
            key = (row["day"], row["decision_time"], row["stock_id"])
            at = datetime.fromisoformat(row["decision_time"])
            b = lookup.get((*key, "B_IMMEDIATE", "BASE_1S_1T"), {})
            a = lookup.get((*key, "A_WAIT_180S", "BASE_1S_1T"), {})
            if row["daily_selected"] != "True":
                a_reason = "DAILY_SLOT_OCCUPIED"
            elif a.get("a_continuation_passed") == "False":
                a_reason = "CONTINUATION_NOT_NET_POSITIVE"
            elif a.get("a_continuation_passed") not in ("True", "False"):
                a_reason = "CONTINUATION_UNSCORABLE"
            elif a.get("status") != "BID_QUOTE_PROXY":
                a_reason = a.get("reason") or "A_PROXY_FILL_UNSCORABLE"
            else:
                a_reason = "A_PROXY_ENTRY_AVAILABLE"
            record = {"day": row["day"], "stock_id": row["stock_id"],
                      "decision_time": row["decision_time"],
                      "rank_at_same_time": row["rank_at_same_time"],
                      "daily_selected": row["daily_selected"] == "True",
                      "signal": quote_checkpoint(quotes[row["stock_id"]], at),
                      "after_180s": quote_checkpoint(
                          quotes[row["stock_id"]], at + timedelta(seconds=180)),
                      "a_continuation_status": a.get("a_continuation_status"),
                      "a_continuation_net_twd": a.get("a_continuation_net_twd"),
                      "a_no_trade_reason": a_reason,
                      "a_proxy_status": a.get("status"),
                      "a_proxy_fill_reason": a.get("reason"),
                      "b_proxy_status": b.get("status"),
                      "b_proxy_fill_reason": b.get("reason"),
                      "actual_orders": 0, "broker_order_calls": 0}
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return len(candidates)


def _number(value: object) -> float | None:
    return None if value in (None, "", "None") else float(value)


def _missing_calendar_days(day: str, observed: set[str]) -> list[str]:
    path = STOCK_STRATEGY / "shadow_daily_runner/runtime/trading_calendar.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        expected = [row["date"].replace("-", "") for row in csv.DictReader(handle)]
    return [date for date in expected if START_DATE <= date <= day
            and date not in observed]


def _daily_report(day: str, run_id: str, result: Path, activation: dict,
                  prior: list[dict]) -> dict:
    summary = json.loads((result / "summary.json").read_text())
    row = _read_csv(result / "daily_results.csv")[0]
    trades = _read_csv(result / "ab_trades.csv")
    by_arm = {(item["arm"], item["scenario"]): item for item in trades}
    a = by_arm.get(("A_WAIT_180S", "BASE_1S_1T"))
    b = by_arm.get(("B_IMMEDIATE", "BASE_1S_1T"))
    a_net = _number(row["a_base_net_twd"])
    b_net = _number(row["b_base_net_twd"])
    if a_net is None and a and a["status"] == "UNFILLED_PROXY":
        a_net = 0.0
    if b_net is None and b and b["status"] == "UNFILLED_PROXY":
        b_net = 0.0
    if a_net is None or b_net is None:
        raise RuntimeError("ACCOUNT_PNL_UNSCORABLE_DO_NOT_PUBLISH_AS_ZERO")
    if a and a["status"] == "SCORED":
        a_no_trade_reason = None
    elif a:
        a_no_trade_reason = a.get("reason") or a["status"]
    elif row["daily_selected_symbol"] in (None, ""):
        a_no_trade_reason = "NO_ELIGIBLE_SIGNAL"
    elif row["a_continuation_passed"] == "False":
        a_no_trade_reason = "CONTINUATION_NOT_NET_POSITIVE"
    else:
        a_no_trade_reason = "CONTINUATION_UNSCORABLE"
    if not b or b["status"] != "SCORED":
        category = "NO_B_PROXY_TRADE"
    elif not a or a["status"] != "SCORED":
        category = "B_EXTRA_TRADE"
    elif a["stock_id"] == b["stock_id"]:
        category = "SAME_SYMBOL_EARLY_ENTRY_AND_EXIT_PATH"
    else:
        category = "DIFFERENT_SYMBOL_UNKNOWN"
    entry_diff = (float(a["entry_price"]) - float(b["entry_price"])) if (
        a and b and a["status"] == "SCORED" and b["status"] == "SCORED") else None
    days = prior + [{"day": day, "a_net_twd": a_net, "b_net_twd": b_net,
                     "difference_twd": b_net - a_net, "difference_category": category}]
    def aggregate(field: str) -> dict:
        values = [float(item[field]) for item in days]
        best = max(values) if values else None
        return {"total_twd": sum(values), "best_day_twd": best,
                "excluding_best_day_twd": sum(values) - best if best is not None else None}
    categories = {name: sum(item["difference_twd"] for item in days
                            if item["difference_category"] == name)
                  for name in ("B_EXTRA_TRADE", "SAME_SYMBOL_EARLY_ENTRY_AND_EXIT_PATH",
                               "NO_B_PROXY_TRADE", "DIFFERENT_SYMBOL_UNKNOWN")}
    return {"day": day, "run_id": run_id, "status": "FORWARD_FULL_SESSION_PROXY",
            "source_baseline_commit": BASELINE_COMMIT,
            "activation_head": activation["head"],
            "activation_config_hash": activation["config_hash"],
            "source_manifest_hash": json.loads((run_dir :=
                DEFAULT_RUNTIME_DIR / "runs" / run_id / "run_manifest.json").read_text()).get("manifest_hash"),
            "observer_policy_hash": summary["observer_policy_hash"],
            "cohort_policy_hash": summary["policy_hash"],
            "eligible_candidates": summary["eligible_candidate_count"],
            "selected_symbol": row["daily_selected_symbol"],
            "a": {"proxy_trades": int(bool(a and a["status"] == "SCORED")),
                  "status": row["a_base_status"],
                  "no_trade_reason": a_no_trade_reason,
                  "net_twd": a_net},
            "b": {"proxy_trades": int(bool(b and b["status"] == "SCORED")),
                  "status": row["b_base_status"], "no_trade_reason":
                  None if b and b["status"] == "SCORED" else
                  (b.get("reason") if b else row["b_base_status"]),
                  "net_twd": b_net},
            "a_minus_b_entry_price_twd": entry_diff,
            "b_minus_a_net_twd": b_net - a_net,
            "difference_category": category,
            "cumulative": {"complete_forward_days": len(days),
                           "missing_calendar_days": _missing_calendar_days(
                               day, {item["day"] for item in days}),
                           "a": aggregate("a_net_twd"),
                           "b": aggregate("b_net_twd"),
                           "b_minus_a": aggregate("difference_twd"),
                           "b_minus_a_by_category_twd": categories},
            "stress": {item["arm"]: item for item in summary["ab"]
                       if item["scenario"] == "STRESS_5S_2T"},
            "actual_orders": 0, "actual_fills": 0,
            "broker_order_calls": 0, "strict_executable": "NA"}


def publish_forward_day(run_dir: Path, session: dict) -> dict:
    """Run after existing collector full-session seal; never touch live orders."""
    day = str(session["session_date"])
    activation = _require_activation(day)
    if session.get("coverage_status") != "FULL_SESSION":
        raise RuntimeError("NOT_A_COMPLETE_TRADING_DAY")
    if run_dir.parent.resolve() != (DEFAULT_RUNTIME_DIR / "runs").resolve():
        raise RuntimeError("SOURCE_OUTSIDE_INSTALLED_COLLECTOR_RUNTIME")
    if datetime.fromisoformat(json.loads((run_dir / "run_manifest.json").read_text())[
        "started_at"].replace("Z", "+00:00")).astimezone(TAIPEI).strftime("%Y%m%d") != day:
        raise RuntimeError("SOURCE_DATE_MISMATCH")
    root = ROOT / "days"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{day}_{run_dir.name}"
    lock_path = ROOT / ".publish.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if target.exists():
            return json.loads((target / "FORWARD_DAILY.json").read_text())
        previous = []
        for path in sorted(root.glob("*/FORWARD_DAILY.json")):
            record = json.loads(path.read_text())
            if record["day"] >= day:
                raise RuntimeError("FORWARD_DAY_ORDER_OR_DUPLICATE")
            previous.append({"day": record["day"],
                "a_net_twd": record["a"]["net_twd"],
                "b_net_twd": record["b"]["net_twd"],
                "difference_twd": record["b_minus_a_net_twd"],
                "difference_category": record["difference_category"]})
        temporary = Path(tempfile.mkdtemp(prefix=".incomplete-", dir=ROOT))
        result = temporary / "replay"
        summary = cohort_replay.run(result, source_map={day: run_dir})
        if summary["replayable_complete_dates"] != 1:
            raise RuntimeError("SOURCE_AUDIT_FAILED")
        _candidate_checkpoints(run_dir, result)
        report = _daily_report(day, run_dir.name, result, activation, previous)
        (temporary / "FORWARD_DAILY.json").write_bytes(canonical_bytes(report) + b"\n")
        (temporary / "activation_snapshot.json").write_bytes(canonical_bytes(activation) + b"\n")
        temporary.rename(target)
        return report


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("activate", "status"))
    args = parser.parse_args()
    if args.action == "activate":
        data = activate()
        print(json.dumps({"status": "ACTIVATED", "start_session_date":
            data["start_session_date"], "head": data["head"],
            "config_hash": data["config_hash"], "actual_orders": 0}))
    else:
        print(json.dumps({"activation_exists": ACTIVATION.exists(),
            "activation_path": str(ACTIVATION),
            "days": len(list((ROOT / "days").glob("*/FORWARD_DAILY.json")))}))


if __name__ == "__main__":
    main()
