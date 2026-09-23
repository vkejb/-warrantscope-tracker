"""Cost-aware replay of incomplete Yuanta intraday quote sessions.

This module is intentionally research-only.  It reads immutable quote artifacts,
never imports the order SDK, and never exposes a live-trading mode.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import gzip
import hashlib
import json
import math
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from .collector import canonical_bytes, sha256_file


TAIPEI = ZoneInfo("Asia/Taipei")
SPEC = {
    "analysis_id": "YUANTA_TWO_DAY_EXPLORATORY_REPLAY_V0_1",
    "signal_window": ["09:30", "09:35"],
    "entry_window": ["09:35", "09:36"],
    "exit_cutoff": "13:25",
    "position_size": "one_1000_share_board_lot_per_selected_stock",
    "portfolio_notional_cap_twd": 190000,
    "maximum_positions": 3,
    "fill_proxy": "displayed_bid_or_ask_plus_one_adverse_tick",
    "commission_rate_each_side": 0.000855,
    "minimum_commission_twd": 20,
    "day_trade_sell_tax_rate": 0.0015,
    "strategies": ["stage_a_rank_long", "momentum_long", "reversal_long", "momentum_short"],
    "interpretation": "IN_SAMPLE_EXPLORATORY_ONLY_NOT_EXPECTED_RETURN_OR_TRADING_SIGNAL",
    "short_limitation": "THEORETICAL_SHORT_REPLAY_ELIGIBILITY_NOT_CHECKED",
}
SPEC_HASH = hashlib.sha256(canonical_bytes(SPEC)).hexdigest()


@dataclass(frozen=True)
class Candidate:
    session_date: str
    stock_id: str
    stock_name: str
    stage_a_rank: int
    momentum: float
    vwap_gap: float
    entry_bid: float
    entry_ask: float
    exit_bid: float
    exit_ask: float


@dataclass(frozen=True)
class Trade:
    session_date: str
    strategy: str
    side: str
    stock_id: str
    stock_name: str
    stage_a_rank: int
    signal_momentum: float
    entry_price: float
    exit_price: float
    quantity: int
    notional_used: float
    gross_pnl: float
    commission: int
    sell_tax: int
    net_pnl: float


def _read_jsonl(path: Path) -> Iterable[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TAIPEI)


def _tick_size(price: float) -> float:
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def _commission(notional: float) -> int:
    return max(SPEC["minimum_commission_twd"], math.ceil(notional * SPEC["commission_rate_each_side"]))


def _verify_run(run_dir: Path) -> tuple[dict, dict, Path]:
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != manifest.get("manifest_hash"):
        raise RuntimeError(f"source run manifest hash mismatch: {run_dir.name}")
    tick_name = "ticks.jsonl.gz" if "ticks.jsonl.gz" in manifest["artifacts"] else "ticks.jsonl"
    for name in (tick_name, "watchlist.json"):
        if sha256_file(run_dir / name) != manifest["artifacts"][name]:
            raise RuntimeError(f"source artifact hash mismatch: {run_dir.name}/{name}")
    watchlist = json.loads((run_dir / "watchlist.json").read_text(encoding="utf-8"))
    return manifest, watchlist, run_dir / tick_name


def build_candidates(run_dirs: list[Path]) -> tuple[list[Candidate], dict]:
    if not run_dirs:
        raise ValueError("at least one run directory is required")
    rows: dict[str, list[dict]] = {}
    metadata: dict[str, dict] = {}
    manifests = []
    all_tick_stamps: list[datetime] = []
    session_date = None
    seal_hash = None
    for run_dir in run_dirs:
        manifest, watchlist, tick_path = _verify_run(run_dir)
        current_date = _stamp(manifest["ended_at"]).strftime("%Y%m%d")
        current_seal = manifest["stage_a_seal_hash"]
        if session_date not in (None, current_date) or seal_hash not in (None, current_seal):
            raise ValueError("combined runs must share a session date and Stage A seal")
        session_date, seal_hash = current_date, current_seal
        manifests.append(manifest)
        metadata.update({str(item["stock_id"]): item for item in watchlist["stocks"]})
        for row in _read_jsonl(tick_path):
            try:
                normalized = {
                    **row,
                    "_stamp": _stamp(row["received_at"]),
                    "_price": float(row["deal_price"]),
                    "_volume": float(row["deal_volume"]),
                    "_bid": float(row["buy_price"]),
                    "_ask": float(row["sell_price"]),
                }
            except (KeyError, TypeError, ValueError):
                continue
            if min(normalized["_price"], normalized["_bid"], normalized["_ask"]) <= 0:
                continue
            all_tick_stamps.append(normalized["_stamp"])
            rows.setdefault(str(row["stock_id"]), []).append(normalized)
    assert session_date is not None
    for stock_rows in rows.values():
        stock_rows.sort(key=lambda row: (row["_stamp"], row.get("serial_no", 0)))

    base = datetime.strptime(session_date, "%Y%m%d").replace(tzinfo=TAIPEI)
    signal_start = base.replace(hour=9, minute=30)
    signal_end = base.replace(hour=9, minute=35)
    entry_end = signal_end + timedelta(minutes=1)
    exit_cutoff = base.replace(hour=13, minute=25)
    candidates = []
    for stock_id, stock_rows in rows.items():
        signal = [row for row in stock_rows if signal_start <= row["_stamp"] < signal_end]
        entry = next((row for row in stock_rows if signal_end <= row["_stamp"] < entry_end), None)
        exits = [row for row in stock_rows if signal_end < row["_stamp"] <= exit_cutoff]
        if len(signal) < 2 or entry is None or not exits or stock_id not in metadata:
            continue
        first, last = signal[0]["_price"], signal[-1]["_price"]
        volume = sum(row["_volume"] for row in signal)
        vwap = (
            sum(row["_price"] * row["_volume"] for row in signal) / volume
            if volume > 0
            else sum(row["_price"] for row in signal) / len(signal)
        )
        item, exit_row = metadata[stock_id], exits[-1]
        candidates.append(Candidate(
            session_date=session_date, stock_id=stock_id, stock_name=item["stock_name"],
            stage_a_rank=int(item["rank"]), momentum=last / first - 1, vwap_gap=last / vwap - 1,
            entry_bid=entry["_bid"], entry_ask=entry["_ask"],
            exit_bid=exit_row["_bid"], exit_ask=exit_row["_ask"],
        ))
    all_tick_stamps.sort()
    gaps = [(right - left).total_seconds() for left, right in zip(all_tick_stamps, all_tick_stamps[1:])]
    coverage = {
        "session_date": session_date,
        "source_run_ids": [manifest["run_id"] for manifest in manifests],
        "source_statuses": [manifest["status"] for manifest in manifests],
        "started_at_taipei": min(_stamp(manifest["started_at"]) for manifest in manifests).isoformat(),
        "ended_at_taipei": max(_stamp(manifest["ended_at"]) for manifest in manifests).isoformat(),
        "candidate_count": len(candidates),
        "callback_errors": sum(manifest["event_counts"]["callback_errors"] for manifest in manifests),
        "max_tick_gap_seconds": max(gaps, default=None),
        "coverage_status": "PARTIAL_SESSION",
    }
    return candidates, coverage


def _selected(strategy: str, candidates: list[Candidate]) -> tuple[str, list[Candidate]]:
    if strategy == "stage_a_rank_long":
        return "LONG", sorted(candidates, key=lambda item: item.stage_a_rank)
    if strategy == "momentum_long":
        valid = [item for item in candidates if item.momentum > 0 and item.vwap_gap > 0]
        return "LONG", sorted(valid, key=lambda item: (-item.momentum, item.stage_a_rank))
    if strategy == "reversal_long":
        valid = [item for item in candidates if item.momentum < 0 and item.vwap_gap < 0]
        return "LONG", sorted(valid, key=lambda item: (item.momentum, item.stage_a_rank))
    if strategy == "momentum_short":
        valid = [item for item in candidates if item.momentum < 0 and item.vwap_gap < 0]
        return "SHORT", sorted(valid, key=lambda item: (item.momentum, item.stage_a_rank))
    raise ValueError(f"unknown strategy: {strategy}")


def replay_strategy(strategy: str, candidates: list[Candidate], capital: int = 190000) -> list[Trade]:
    side, ranked = _selected(strategy, candidates)
    remaining = float(capital)
    trades = []
    for item in ranked:
        if side == "LONG":
            entry = item.entry_ask + _tick_size(item.entry_ask)
            exit_price = max(_tick_size(item.exit_bid), item.exit_bid - _tick_size(item.exit_bid))
        else:
            entry = max(_tick_size(item.entry_bid), item.entry_bid - _tick_size(item.entry_bid))
            exit_price = item.exit_ask + _tick_size(item.exit_ask)
        quantity = 1000
        exposure = entry * quantity
        if exposure > remaining:
            continue
        remaining -= exposure
        buy_notional = entry * quantity if side == "LONG" else exit_price * quantity
        sell_notional = exit_price * quantity if side == "LONG" else entry * quantity
        buy_fee, sell_fee = _commission(buy_notional), _commission(sell_notional)
        sell_tax = math.ceil(sell_notional * SPEC["day_trade_sell_tax_rate"])
        gross = round(sell_notional - buy_notional, 2)
        trades.append(Trade(
            session_date=item.session_date, strategy=strategy, side=side, stock_id=item.stock_id,
            stock_name=item.stock_name, stage_a_rank=item.stage_a_rank, signal_momentum=item.momentum,
            entry_price=entry, exit_price=exit_price, quantity=quantity, notional_used=exposure,
            gross_pnl=gross, commission=buy_fee + sell_fee, sell_tax=sell_tax,
            net_pnl=round(gross - buy_fee - sell_fee - sell_tax, 2),
        ))
        if len(trades) >= SPEC["maximum_positions"]:
            break
    return trades


def build_report(session_runs: dict[str, list[Path]], capital: int = 190000) -> tuple[dict, list[Trade]]:
    coverages, all_trades = [], []
    for _, run_dirs in sorted(session_runs.items()):
        candidates, coverage = build_candidates(run_dirs)
        coverages.append(coverage)
        for strategy in SPEC["strategies"]:
            all_trades.extend(replay_strategy(strategy, candidates, capital))
    summaries = []
    for strategy in SPEC["strategies"]:
        trades = [trade for trade in all_trades if trade.strategy == strategy]
        summaries.append({
            "strategy": strategy, "trade_count": len(trades),
            "winning_trades": sum(trade.net_pnl > 0 for trade in trades),
            "net_pnl_twd": round(sum(trade.net_pnl for trade in trades), 2),
            "two_session_return_on_capital": round(sum(trade.net_pnl for trade in trades) / capital, 8),
        })
    return {
        "spec": SPEC, "spec_hash": SPEC_HASH, "capital_twd": capital,
        "coverage": coverages, "strategy_summaries": summaries,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
    }, all_trades


def _session_arg(value: str) -> tuple[str, list[Path]]:
    try:
        date, paths = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use YYYYMMDD=RUN_DIR[,RUN_DIR]") from exc
    return date, [Path(path) for path in paths.split(",")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Research-only cost-aware intraday quote replay")
    parser.add_argument("--session", action="append", required=True, type=_session_arg)
    parser.add_argument("--capital", type=int, default=190000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report, trades = build_report(dict(args.session), args.capital)
    payload = {**report, "trades": [asdict(trade) for trade in trades]}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
        trade_path = args.output.with_suffix(".trades.csv")
        with trade_path.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=Trade.__dataclass_fields__.keys())
            writer.writeheader(); writer.writerows(asdict(trade) for trade in trades)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
