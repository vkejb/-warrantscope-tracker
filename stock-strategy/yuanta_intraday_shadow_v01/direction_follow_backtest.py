"""Causal tick-and-book direction-following replay for the Stage A Top30.

The strategy is frozen before replay, uses only information received by each
decision timestamp, and has no broker or order-SDK integration.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from .collector import canonical_bytes, sha256_file
from .exploratory_backtest import _commission, _read_jsonl, _stamp, _tick_size


SPEC = {
    "analysis_id": "YUANTA_TOP30_CAUSAL_DIRECTION_FOLLOW_V0_1",
    "decision_interval_seconds": 30,
    "direction_window_seconds": 60,
    "large_trade_reference_seconds": 300,
    "large_trade_percentile": 0.90,
    "breakout_lookback_seconds": 180,
    "breakout_excludes_latest_seconds": 30,
    "minimum_ticks_in_direction_window": 10,
    "maximum_tick_staleness_seconds": 5,
    "maximum_book_staleness_seconds": 10,
    "maximum_spread_bps": 40,
    "volume_delta_threshold": 0.35,
    "large_trade_delta_threshold": 0.25,
    "book_imbalance_threshold": 0.10,
    "entry_confirmations": 1,
    "reversal_confirmations": 2,
    "entry_start": "09:05",
    "last_entry_time": "13:10",
    "hard_exit_time": "13:20",
    "maximum_hard_exit_quote_staleness_seconds": 60,
    "stop_loss_net_twd": 5000,
    "trailing_profit_activation": 0.02,
    "trailing_profit_drawdown": 0.02,
    "loss_recovery_required": 0.02,
    "portfolio_notional_cap_twd": 190000,
    "position_size": "maximum_whole_1000_share_board_lots_within_cap",
    "maximum_trades_per_session": 1,
    "fill_proxy": "displayed_bid_or_ask_plus_one_adverse_tick",
    "commission_rate_each_side": 0.000855,
    "minimum_commission_twd": 20,
    "day_trade_sell_tax_rate": 0.0015,
    "short_limitation": "THEORETICAL_SHORT_REPLAY_ELIGIBILITY_NOT_CHECKED",
    "interpretation": "IN_SAMPLE_EXPLORATORY_ONLY_NOT_EXPECTED_RETURN_OR_TRADING_SIGNAL",
}
SPEC_HASH = hashlib.sha256(canonical_bytes(SPEC)).hexdigest()
TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class DirectionSignal:
    stock_id: str
    stock_name: str
    side: str
    decision_time: datetime
    score: float
    volume_delta: float
    large_trade_delta: float
    vwap_gap: float
    book_imbalance: float
    spread_bps: float
    large_trade_threshold: float


@dataclass(frozen=True)
class DirectionTrade:
    session_date: str
    stock_id: str
    stock_name: str
    side: str
    decision_time: str
    entry_time: str
    exit_time: str
    exit_reason: str
    score: float
    volume_delta: float
    large_trade_delta: float
    vwap_gap: float
    book_imbalance: float
    spread_bps: float
    entry_price: float
    exit_price: float
    quantity: int
    notional_used: float
    gross_pnl: float
    commission: int
    sell_tax: int
    net_pnl: float


def _verify_run(run_dir: Path) -> tuple[dict, dict, Path, Path]:
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != manifest.get("manifest_hash"):
        raise RuntimeError(f"source run manifest hash mismatch: {run_dir.name}")
    tick_name = "ticks.jsonl.gz" if "ticks.jsonl.gz" in manifest["artifacts"] else "ticks.jsonl"
    book_name = "books.jsonl.gz" if "books.jsonl.gz" in manifest["artifacts"] else "books.jsonl"
    for name in (tick_name, book_name, "watchlist.json"):
        if sha256_file(run_dir / name) != manifest["artifacts"][name]:
            raise RuntimeError(f"source artifact hash mismatch: {run_dir.name}/{name}")
    watchlist = json.loads((run_dir / "watchlist.json").read_text(encoding="utf-8"))
    return manifest, watchlist, run_dir / tick_name, run_dir / book_name


def _number_list(values: list[str]) -> list[float]:
    result = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            result.append(number)
    return result


def load_session(run_dirs: list[Path]) -> tuple[dict[str, dict], dict]:
    stocks: dict[str, dict] = {}
    metadata: dict[str, dict] = {}
    manifests = []
    session_date = None
    seal_hash = None
    for run_dir in run_dirs:
        manifest, watchlist, tick_path, book_path = _verify_run(run_dir)
        current_date = _stamp(manifest["ended_at"]).strftime("%Y%m%d")
        current_seal = manifest["stage_a_seal_hash"]
        if session_date not in (None, current_date) or seal_hash not in (None, current_seal):
            raise ValueError("combined runs must share a session date and Stage A seal")
        session_date, seal_hash = current_date, current_seal
        manifests.append(manifest)
        metadata.update({str(item["stock_id"]): item for item in watchlist["stocks"]})
        for row in _read_jsonl(tick_path):
            try:
                item = {
                    "time": _stamp(row["received_at"]), "price": float(row["deal_price"]),
                    "volume": float(row["deal_volume"]), "bid": float(row["buy_price"]),
                    "ask": float(row["sell_price"]), "flag": str(row.get("in_out_flag", "")),
                    "serial": int(row.get("serial_no", 0)),
                }
            except (KeyError, TypeError, ValueError):
                continue
            if min(item["price"], item["bid"], item["ask"]) <= 0 or item["volume"] < 0:
                continue
            stocks.setdefault(str(row["stock_id"]), {"ticks": [], "books": []})["ticks"].append(item)
        for row in _read_jsonl(book_path):
            buys, sells = _number_list(row.get("buy_volumes", [])), _number_list(row.get("sell_volumes", []))
            buy_prices, sell_prices = _number_list(row.get("buy_prices", [])), _number_list(row.get("sell_prices", []))
            if not buys or not sells or not buy_prices or not sell_prices:
                continue
            # Match LiveDirectionEngine: an invalid top-of-book quote is not
            # executable and must fail closed instead of entering the replay.
            if buy_prices[0] <= 0 or sell_prices[0] <= 0:
                continue
            stocks.setdefault(str(row["stock_id"]), {"ticks": [], "books": []})["books"].append({
                "time": _stamp(row["received_at"]), "buy_volume": sum(buys), "sell_volume": sum(sells),
                "best_bid": buy_prices[0], "best_ask": sell_prices[0],
            })
    assert session_date is not None
    for stock_id, data in stocks.items():
        data["ticks"].sort(key=lambda row: (row["time"], row["serial"]))
        data["books"].sort(key=lambda row: row["time"])
        data["tick_times"] = [row["time"] for row in data["ticks"]]
        data["book_times"] = [row["time"] for row in data["books"]]
        data["meta"] = metadata.get(stock_id, {"stock_name": stock_id})
    coverage = {
        "session_date": session_date,
        "source_run_ids": [manifest["run_id"] for manifest in manifests],
        "source_statuses": [manifest["status"] for manifest in manifests],
        "started_at_taipei": min(_stamp(manifest["started_at"]) for manifest in manifests).isoformat(),
        "ended_at_taipei": max(_stamp(manifest["ended_at"]) for manifest in manifests).isoformat(),
        "callback_errors": sum(manifest["event_counts"]["callback_errors"] for manifest in manifests),
        "coverage_status": "PARTIAL_SESSION",
    }
    return stocks, coverage


def _slice(data: dict, key: str, start: datetime, end: datetime) -> list[dict]:
    times = data[f"{key[:-1]}_times"]
    rows = data[key]
    return rows[bisect_left(times, start):bisect_right(times, end)]


def _percentile90(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(SPEC["large_trade_percentile"] * len(ordered)) - 1)
    return ordered[index]


def _signed_volume(row: dict) -> float:
    if row["flag"] == "1":
        return row["volume"]
    if row["flag"] == "0":
        return -row["volume"]
    midpoint = (row["bid"] + row["ask"]) / 2
    return row["volume"] if row["price"] >= midpoint else -row["volume"]


def signal_at(stock_id: str, data: dict, decision: datetime) -> DirectionSignal | None:
    window = _slice(data, "ticks", decision - timedelta(seconds=SPEC["direction_window_seconds"]), decision)
    if len(window) < SPEC["minimum_ticks_in_direction_window"]:
        return None
    if (decision - window[-1]["time"]).total_seconds() > SPEC["maximum_tick_staleness_seconds"]:
        return None
    books = _slice(data, "books", decision - timedelta(seconds=SPEC["maximum_book_staleness_seconds"]), decision)
    if not books:
        return None
    book = books[-1]
    spread_mid = (book["best_bid"] + book["best_ask"]) / 2
    if spread_mid <= 0:
        return None
    spread_bps = (book["best_ask"] - book["best_bid"]) / spread_mid * 10000
    if spread_bps > SPEC["maximum_spread_bps"]:
        return None
    book_total = book["buy_volume"] + book["sell_volume"]
    if book_total <= 0:
        return None
    book_imbalance = (book["buy_volume"] - book["sell_volume"]) / book_total
    total_volume = sum(row["volume"] for row in window)
    if total_volume <= 0:
        return None
    volume_delta = sum(_signed_volume(row) for row in window) / total_volume

    reference = _slice(data, "ticks", decision - timedelta(seconds=SPEC["large_trade_reference_seconds"]), decision)
    if len(reference) < SPEC["minimum_ticks_in_direction_window"]:
        return None
    large_threshold = _percentile90([row["volume"] for row in reference])
    large = [row for row in window if row["volume"] >= large_threshold]
    large_total = sum(row["volume"] for row in large)
    if large_total <= 0:
        return None
    large_delta = sum(_signed_volume(row) for row in large) / large_total

    session_ticks = data["ticks"][:bisect_right(data["tick_times"], decision)]
    session_volume = sum(row["volume"] for row in session_ticks)
    if session_volume <= 0:
        return None
    vwap = sum(row["price"] * row["volume"] for row in session_ticks) / session_volume
    current = window[-1]["price"]
    vwap_gap = current / vwap - 1
    prior = _slice(
        data, "ticks", decision - timedelta(seconds=SPEC["breakout_lookback_seconds"]),
        decision - timedelta(seconds=SPEC["breakout_excludes_latest_seconds"]),
    )
    if not prior:
        return None
    long_breakout = current > max(row["price"] for row in prior)
    short_breakout = current < min(row["price"] for row in prior)
    long_ok = (
        volume_delta >= SPEC["volume_delta_threshold"]
        and large_delta >= SPEC["large_trade_delta_threshold"]
        and vwap_gap > 0 and book_imbalance >= SPEC["book_imbalance_threshold"] and long_breakout
    )
    short_ok = (
        volume_delta <= -SPEC["volume_delta_threshold"]
        and large_delta <= -SPEC["large_trade_delta_threshold"]
        and vwap_gap < 0 and book_imbalance <= -SPEC["book_imbalance_threshold"] and short_breakout
    )
    if not (long_ok or short_ok):
        return None
    score = (
        0.35 * abs(volume_delta) + 0.25 * abs(large_delta)
        + 0.20 * min(abs(vwap_gap) / 0.005, 1.0) + 0.20 * abs(book_imbalance)
    )
    return DirectionSignal(
        stock_id=stock_id, stock_name=data["meta"]["stock_name"], side="LONG" if long_ok else "SHORT",
        decision_time=decision, score=score, volume_delta=volume_delta, large_trade_delta=large_delta,
        vwap_gap=vwap_gap, book_imbalance=book_imbalance, spread_bps=spread_bps,
        large_trade_threshold=large_threshold,
    )


def _decision_times(session_date: str, start: str, end: str) -> list[datetime]:
    base = datetime.strptime(session_date, "%Y%m%d").replace(tzinfo=TAIPEI)
    start_hour, start_minute = map(int, start.split(":"))
    end_hour, end_minute = map(int, end.split(":"))
    current = base.replace(hour=start_hour, minute=start_minute)
    final = base.replace(hour=end_hour, minute=end_minute)
    result = []
    while current <= final:
        result.append(current)
        current += timedelta(seconds=SPEC["decision_interval_seconds"])
    return result


def _entry_tick(data: dict, decision: datetime) -> dict | None:
    index = bisect_right(data["tick_times"], decision)
    if index >= len(data["ticks"]):
        return None
    row = data["ticks"][index]
    return row if (row["time"] - decision).total_seconds() <= SPEC["decision_interval_seconds"] else None


def _exit_quote(side: str, row: dict) -> float:
    if side == "LONG":
        return max(_tick_size(row["bid"]), row["bid"] - _tick_size(row["bid"]))
    return row["ask"] + _tick_size(row["ask"])


def _projected_net_pnl(side: str, entry_price: float, exit_price: float, quantity: int) -> tuple[float, int, int, float]:
    buy_notional = entry_price * quantity if side == "LONG" else exit_price * quantity
    sell_notional = exit_price * quantity if side == "LONG" else entry_price * quantity
    buy_fee, sell_fee = _commission(buy_notional), _commission(sell_notional)
    sell_tax = math.ceil(sell_notional * SPEC["day_trade_sell_tax_rate"])
    gross = round(sell_notional - buy_notional, 2)
    return gross, buy_fee + sell_fee, sell_tax, round(gross - buy_fee - sell_fee - sell_tax, 2)


def _loss_recovery_exit(worst_return: float, current_return: float) -> bool:
    return (
        worst_return < 0
        and current_return > 0
        and current_return - worst_return >= SPEC["loss_recovery_required"]
    )


def _reversal_exit_tick(data: dict, signal: DirectionSignal, entry_time: datetime, hard_exit: datetime) -> dict | None:
    prior_time = None
    streak = 0
    for decision in _decision_times(signal.decision_time.strftime("%Y%m%d"), signal.decision_time.strftime("%H:%M"), SPEC["hard_exit_time"]):
        if decision <= entry_time:
            continue
        current = signal_at(signal.stock_id, data, decision)
        if current and current.side != signal.side:
            streak = streak + 1 if prior_time is not None and decision - prior_time == timedelta(seconds=SPEC["decision_interval_seconds"]) else 1
            if streak >= SPEC["reversal_confirmations"]:
                row = _entry_tick(data, decision)
                return row if row and row["time"] <= hard_exit else None
            prior_time = decision
        else:
            prior_time = None
            streak = 0
    return None


def simulate_signal_trade(
    session_date: str, data: dict, signal: DirectionSignal, entry_tick: dict,
    entry_price: float, quantity: int,
) -> tuple[DirectionTrade | None, dict]:
    diagnostics: dict = {}
    hard_hour, hard_minute = map(int, SPEC["hard_exit_time"].split(":"))
    hard_exit = entry_tick["time"].replace(hour=hard_hour, minute=hard_minute, second=0, microsecond=0)
    future = [row for row in data["ticks"] if entry_tick["time"] < row["time"] <= hard_exit]
    reversal_row = _reversal_exit_tick(data, signal, entry_tick["time"], hard_exit)
    exit_row, exit_reason = None, "HARD_EXIT"
    peak_return = 0.0
    worst_return = 0.0
    entry_notional = entry_price * quantity
    for row in future:
        executable = _exit_quote(signal.side, row)
        _, _, _, projected_net = _projected_net_pnl(signal.side, entry_price, executable, quantity)
        current_return = projected_net / entry_notional
        peak_return = max(peak_return, current_return)
        worst_return = min(worst_return, current_return)
        if projected_net <= -SPEC["stop_loss_net_twd"]:
            exit_row, exit_reason = row, "STOP_LOSS"; break
        if (
            peak_return >= SPEC["trailing_profit_activation"]
            and current_return <= peak_return - SPEC["trailing_profit_drawdown"]
        ):
            exit_row, exit_reason = row, "TRAILING_PROFIT"; break
        if _loss_recovery_exit(worst_return, current_return):
            exit_row, exit_reason = row, "LOSS_RECOVERY_TO_PROFIT"; break
        if reversal_row is not None and row["time"] >= reversal_row["time"]:
            exit_row, exit_reason = row, "SIGNAL_REVERSAL"; break
    if exit_row is None:
        hard_exit_staleness = (hard_exit - future[-1]["time"]).total_seconds() if future else float("inf")
        if not future or hard_exit_staleness > SPEC["maximum_hard_exit_quote_staleness_seconds"]:
            diagnostics["exit_data_insufficient"] = True
            diagnostics["last_exit_quote_staleness_seconds"] = hard_exit_staleness if future else None
            return None, diagnostics
        exit_row = future[-1]
    exit_price = _exit_quote(signal.side, exit_row)
    gross, commission, sell_tax, net_pnl = _projected_net_pnl(signal.side, entry_price, exit_price, quantity)
    return DirectionTrade(
        session_date=session_date, stock_id=signal.stock_id, stock_name=signal.stock_name, side=signal.side,
        decision_time=signal.decision_time.isoformat(), entry_time=entry_tick["time"].isoformat(),
        exit_time=exit_row["time"].isoformat(), exit_reason=exit_reason, score=signal.score,
        volume_delta=signal.volume_delta, large_trade_delta=signal.large_trade_delta,
        vwap_gap=signal.vwap_gap, book_imbalance=signal.book_imbalance, spread_bps=signal.spread_bps,
        entry_price=entry_price, exit_price=exit_price, quantity=quantity, notional_used=entry_price * quantity,
        gross_pnl=gross, commission=commission, sell_tax=sell_tax, net_pnl=net_pnl,
    ), diagnostics


def replay_session(stocks: dict[str, dict], coverage: dict, capital: int = 190000) -> tuple[DirectionTrade | None, dict]:
    session_date = coverage["session_date"]
    previous: dict[str, tuple[str, int, datetime]] = {}
    selected = None
    diagnostics = {"session_date": session_date, "raw_signal_windows": 0, "confirmed_signal_windows": 0,
                   "confirmed_but_unaffordable": 0, "selected_trade": False}
    for decision in _decision_times(session_date, SPEC["entry_start"], SPEC["last_entry_time"]):
        confirmed = []
        for stock_id, data in stocks.items():
            signal = signal_at(stock_id, data, decision)
            if signal:
                diagnostics["raw_signal_windows"] += 1
            prior = previous.get(stock_id)
            if signal:
                streak = (
                    prior[1] + 1
                    if prior and prior[0] == signal.side and decision - prior[2] == timedelta(seconds=SPEC["decision_interval_seconds"])
                    else 1
                )
            else:
                streak = 0
            if signal and streak >= SPEC["entry_confirmations"]:
                diagnostics["confirmed_signal_windows"] += 1
                entry_tick = _entry_tick(data, decision)
                if entry_tick:
                    entry_price = (
                        entry_tick["ask"] + _tick_size(entry_tick["ask"])
                        if signal.side == "LONG"
                        else max(_tick_size(entry_tick["bid"]), entry_tick["bid"] - _tick_size(entry_tick["bid"]))
                    )
                    if entry_price * 1000 <= capital:
                        lots = math.floor(capital / (entry_price * 1000))
                        quantity = lots * 1000
                        notional = entry_price * quantity
                        confirmed.append((signal.score, notional, signal, entry_tick, entry_price, quantity))
                    else:
                        diagnostics["confirmed_but_unaffordable"] += 1
            previous[stock_id] = (signal.side, streak, decision) if signal else ("", 0, decision)
        if confirmed:
            selected = max(confirmed, key=lambda item: (item[0], item[1]))
            break
    if selected is None:
        return None, diagnostics
    _, _, signal, entry_tick, entry_price, quantity = selected
    trade, exit_diagnostics = simulate_signal_trade(
        session_date, stocks[signal.stock_id], signal, entry_tick, entry_price, quantity,
    )
    diagnostics.update(exit_diagnostics)
    diagnostics["selected_trade"] = trade is not None
    return trade, diagnostics


def build_report(session_runs: dict[str, list[Path]], capital: int = 190000) -> dict:
    coverages, diagnostics, trades = [], [], []
    for _, run_dirs in sorted(session_runs.items()):
        stocks, coverage = load_session(run_dirs)
        coverages.append(coverage)
        trade, session_diagnostics = replay_session(stocks, coverage, capital)
        diagnostics.append(session_diagnostics)
        if trade:
            trades.append(trade)
    return {
        "spec": SPEC, "spec_hash": SPEC_HASH, "capital_twd": capital, "coverage": coverages,
        "diagnostics": diagnostics,
        "trades": [asdict(trade) for trade in trades], "trade_count": len(trades),
        "winning_trades": sum(trade.net_pnl > 0 for trade in trades),
        "net_pnl_twd": round(sum(trade.net_pnl for trade in trades), 2),
        "return_on_capital": round(sum(trade.net_pnl for trade in trades) / capital, 8),
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
    }


def _session_arg(value: str) -> tuple[str, list[Path]]:
    try:
        date, paths = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use YYYYMMDD=RUN_DIR[,RUN_DIR]") from exc
    return date, [Path(path) for path in paths.split(",")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Research-only causal Top30 direction replay")
    parser.add_argument("--session", action="append", required=True, type=_session_arg)
    parser.add_argument("--capital", type=int, default=190000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report(dict(args.session), args.capital)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
