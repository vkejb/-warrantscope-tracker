"""Paper-only resistance/ask-pressure exit overlay replay.

This module never imports the broker adapter and never submits an order.  It
replays one already-filled position against an immutable quote archive while
leaving the production strategy (variant A) unchanged.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import gzip
import json
import math
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from yuanta_intraday_shadow_v01.exploratory_backtest import _tick_size
from yuanta_live_runtime_v01.strategy import (
    LiveDirectionEngine,
    ManagedPosition,
)


TAIPEI = ZoneInfo("Asia/Taipei")
ANALYSIS_ID = "RESISTANCE_EXIT_OVERLAY_DIAGNOSTIC_V0_1"


@dataclass(frozen=True, slots=True)
class OverlayConfig:
    variant: str
    family: str
    rejection_ticks: int
    weakening_seconds: int
    maximum_buy_flow_delta: float
    ask_pressure_ratio: float = 0.0
    minimum_same_ask_updates: int = 0
    minimum_same_ask_seconds: float = 0.0


# Fixed before reading outcomes.  This is a diagnostic grid, not fitting.
PREREGISTERED_CONFIGS = (
    OverlayConfig("PRIOR_HIGH_1", "PRIOR_HIGH", 1, 5, 0.10),
    OverlayConfig("PRIOR_HIGH_2", "PRIOR_HIGH", 2, 10, 0.10),
    OverlayConfig("PRIOR_HIGH_3", "PRIOR_HIGH", 3, 20, 0.00),
    OverlayConfig("SAME_PRICE_ASK_1", "SAME_PRICE_ASK", 1, 5, 0.10, 1.25, 3, 1.0),
    OverlayConfig("SAME_PRICE_ASK_2", "SAME_PRICE_ASK", 2, 10, 0.10, 1.50, 5, 2.0),
    OverlayConfig("SAME_PRICE_ASK_3", "SAME_PRICE_ASK", 3, 20, 0.00, 2.00, 8, 3.0),
)


@dataclass(frozen=True, slots=True)
class Tick:
    exchange_time: datetime
    received_at: datetime
    price: float
    bid: float
    ask: float
    volume: int
    flag: str
    serial: int


@dataclass(frozen=True, slots=True)
class Book:
    received_at: datetime
    buy_prices: tuple[float, ...]
    buy_volumes: tuple[int, ...]
    sell_prices: tuple[float, ...]
    sell_volumes: tuple[int, ...]


def _read(path: Path) -> Iterable[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _received(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TAIPEI)


def load_ticks(run_dir: Path, symbol: str, session_date: str) -> list[Tick]:
    result: list[Tick] = []
    for row in _read(run_dir / "ticks.jsonl.gz"):
        if str(row.get("stock_id")) != symbol:
            continue
        exchange = datetime.strptime(
            f"{session_date} {row['quote_time']}", "%Y%m%d %H:%M:%S.%f"
        ).replace(tzinfo=TAIPEI)
        result.append(Tick(
            exchange_time=exchange,
            received_at=_received(str(row["received_at"])),
            price=float(row["deal_price"]),
            bid=float(row["buy_price"]),
            ask=float(row["sell_price"]),
            volume=int(row["deal_volume"]),
            flag=str(row.get("in_out_flag", "")),
            serial=int(row.get("serial_no") or 0),
        ))
    return sorted(result, key=lambda item: (item.received_at, item.serial))


def load_books(run_dir: Path, symbol: str) -> list[Book]:
    result: list[Book] = []
    for row in _read(run_dir / "books.jsonl.gz"):
        if str(row.get("stock_id")) != symbol:
            continue
        buy_prices = tuple(float(value) for value in row.get("buy_prices", ()))
        sell_prices = tuple(float(value) for value in row.get("sell_prices", ()))
        buy_volumes = tuple(int(value) for value in row.get("buy_volumes", ()))
        sell_volumes = tuple(int(value) for value in row.get("sell_volumes", ()))
        if not buy_prices or not sell_prices or not buy_volumes or not sell_volumes:
            continue
        if buy_prices[0] <= 0 or sell_prices[0] <= 0 or sell_prices[0] < buy_prices[0]:
            continue
        result.append(Book(
            received_at=_received(str(row["received_at"])),
            buy_prices=buy_prices,
            buy_volumes=buy_volumes,
            sell_prices=sell_prices,
            sell_volumes=sell_volumes,
        ))
    return sorted(result, key=lambda item: item.received_at)


def signed_flow(ticks: list[Tick], now: datetime, seconds: int) -> float | None:
    start = now - timedelta(seconds=seconds)
    rows = [item for item in ticks if start <= item.received_at <= now]
    total = sum(item.volume for item in rows)
    if total <= 0:
        return None
    signed = 0
    for item in rows:
        if item.flag == "1":
            signed += item.volume
        elif item.flag == "0":
            signed -= item.volume
        else:
            signed += item.volume if item.price >= (item.bid + item.ask) / 2 else -item.volume
    return signed / total


def _latest_book(books: list[Book], now: datetime) -> Book | None:
    eligible = [book for book in books if book.received_at <= now]
    return eligible[-1] if eligible else None


def _same_ask_pressure(
    books: list[Book], now: datetime, config: OverlayConfig
) -> tuple[bool, float | None, int, float]:
    if config.family != "SAME_PRICE_ASK":
        return True, None, 0, 0.0
    latest = _latest_book(books, now)
    if latest is None:
        return False, None, 0, 0.0
    ask = latest.sell_prices[0]
    observations = []
    for book in reversed(books):
        if book.received_at > now:
            continue
        if book.sell_prices[0] != ask:
            break
        observations.append(book)
    observations.reverse()
    elapsed = (
        observations[-1].received_at - observations[0].received_at
    ).total_seconds() if len(observations) > 1 else 0.0
    latest_sell = sum(latest.sell_volumes)
    latest_buy = sum(latest.buy_volumes)
    ratio = latest_sell / latest_buy if latest_buy > 0 else math.inf
    return (
        len(observations) >= config.minimum_same_ask_updates
        and elapsed >= config.minimum_same_ask_seconds
        and ratio >= config.ask_pressure_ratio,
        ask,
        len(observations),
        elapsed,
    )


def overlay_trigger(
    *,
    config: OverlayConfig,
    ticks_seen: list[Tick],
    books: list[Book],
    prior_high: float,
    breakout_seen: bool,
    position: ManagedPosition,
    engine: LiveDirectionEngine,
) -> dict[str, Any] | None:
    current = ticks_seen[-1]
    if not breakout_seen:
        return None
    tick = _tick_size(prior_high)
    if current.price > prior_high - config.rejection_ticks * tick:
        return None
    flow = signed_flow(ticks_seen, current.received_at, config.weakening_seconds)
    if flow is None or flow > config.maximum_buy_flow_delta:
        return None
    executable = current.bid
    pnl = engine.projected_net(position, executable)
    if pnl <= 0:
        return None
    pressure, ask, updates, elapsed = _same_ask_pressure(
        books, current.received_at, config
    )
    if not pressure:
        return None
    return {
        "trigger_time": current.received_at.isoformat(),
        "trigger_exchange_time": current.exchange_time.isoformat(),
        "trigger_price": current.price,
        "trigger_bid": current.bid,
        "known_prior_high": prior_high,
        "buyer_flow_delta": flow,
        "persistent_ask_price": ask,
        "same_ask_updates": updates,
        "same_ask_elapsed_seconds": elapsed,
    }


def executable_fill(
    books: list[Book], trigger: datetime, quantity_shares: int, latency_ms: int
) -> dict[str, Any] | None:
    target = trigger + timedelta(milliseconds=latency_ms)
    lots = quantity_shares / 1000
    for book in books:
        if book.received_at < target:
            continue
        remaining = lots
        notional = 0.0
        used = []
        for price, volume in zip(book.buy_prices, book.buy_volumes):
            if price <= 0 or volume <= 0:
                continue
            take = min(remaining, float(volume))
            notional += take * price
            used.append({"price": price, "lots": take})
            remaining -= take
            if remaining <= 1e-9:
                return {
                    "fill_time": book.received_at.isoformat(),
                    "fill_price": notional / lots,
                    "book_latency_ms": (book.received_at - trigger).total_seconds() * 1000,
                    "levels": used,
                }
        # Never assume an unobserved fill when five levels cannot cover size.
        return None
    return None


def replay(
    run_dir: Path,
    *,
    symbol: str,
    stock_name: str,
    entry_time: datetime,
    entry_price: float,
    quantity: int,
    latency_ms: int = 250,
) -> dict[str, Any]:
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    session_date = _received(str(manifest["ended_at"])).strftime("%Y%m%d")
    ticks = load_ticks(run_dir, symbol, session_date)
    books = load_books(run_dir, symbol)
    pre = [tick.price for tick in ticks if tick.received_at < entry_time]
    if not pre:
        raise ValueError("no causal pre-entry tick history")
    prior_high = max(pre)
    configs: tuple[OverlayConfig | None, ...] = (None, *PREREGISTERED_CONFIGS)
    rows = []

    for config in configs:
        engine = LiveDirectionEngine({symbol: stock_name}, capital_twd=190_000)
        position = ManagedPosition(
            symbol, stock_name, "LONG", quantity, entry_price,
            "paper-only-incident-replay", entry_time,
        )
        seen: list[Tick] = []
        breakout_seen = False
        overlay = None
        baseline = None
        mfe_bid = entry_price
        mfe_time = entry_time
        for tick in ticks:
            outcome = engine.ingest_tick(
                symbol,
                at=tick.exchange_time,
                received_at=tick.received_at,
                price=tick.price,
                volume=tick.volume,
                bid=tick.bid,
                ask=tick.ask,
                flag=tick.flag,
                serial=tick.serial,
            )
            if not outcome.accepted or tick.received_at < entry_time:
                continue
            seen.append(tick)
            if tick.bid > mfe_bid:
                mfe_bid = tick.bid
                mfe_time = tick.received_at
            # Touching a known high and failing to exceed it is a failed
            # breakout attempt; no future high is used.
            breakout_seen = breakout_seen or tick.price >= prior_high
            baseline_decision = engine.evaluate_exit(
                position, tick.received_at, reversal=False, max_quote_age_seconds=3.0
            )
            if baseline_decision is not None:
                baseline = {
                    "trigger_time": tick.received_at.isoformat(),
                    "reason": baseline_decision.reason,
                    "trigger_price": baseline_decision.price,
                }
            if config is not None:
                overlay = overlay_trigger(
                    config=config,
                    ticks_seen=seen,
                    books=books,
                    prior_high=prior_high,
                    breakout_seen=breakout_seen,
                    position=position,
                    engine=engine,
                )
            if overlay is not None or baseline is not None:
                break

        chosen = overlay if overlay is not None else baseline
        reason = (
            "RESISTANCE_PROFIT_PROTECTION" if overlay is not None
            else str((baseline or {}).get("reason") or "UNSCORABLE_NO_EXIT")
        )
        trigger_time = (
            datetime.fromisoformat(str(chosen["trigger_time"]))
            if chosen is not None else None
        )
        fill = (
            executable_fill(books, trigger_time, quantity, latency_ms)
            if trigger_time is not None else None
        )
        fill_price = None if fill is None else float(fill["fill_price"])
        net_pnl = None if fill_price is None else engine.projected_net(position, fill_price)
        mfe_pnl = engine.projected_net(position, mfe_bid)
        rows.append({
            "analysis_id": ANALYSIS_ID,
            "variant": "A_BASELINE" if config is None else config.variant,
            "family": "BASELINE" if config is None else config.family,
            "symbol": symbol,
            "entry_time": entry_time.isoformat(),
            "entry_price": entry_price,
            "quantity": quantity,
            "known_prior_high_at_entry": prior_high,
            "exit_reason": reason,
            "exit_trigger_time": None if trigger_time is None else trigger_time.isoformat(),
            "overlay_triggered": overlay is not None,
            "fill_scorable": fill is not None,
            "fill_price": fill_price,
            "fill_time": None if fill is None else fill["fill_time"],
            "book_latency_ms": None if fill is None else fill["book_latency_ms"],
            "net_pnl": net_pnl,
            "mfe_bid": mfe_bid,
            "mfe_time": mfe_time.isoformat(),
            "mfe_net_pnl": mfe_pnl,
            "giveback_twd": None if net_pnl is None else mfe_pnl - net_pnl,
            "trigger_details": overlay,
            "execution_levels": None if fill is None else fill["levels"],
            "config": None if config is None else asdict(config),
        })

    baseline_pnl = rows[0]["net_pnl"]
    for row in rows:
        row["pnl_vs_a"] = (
            None if row["net_pnl"] is None or baseline_pnl is None
            else row["net_pnl"] - baseline_pnl
        )
    return {
        "analysis_id": ANALYSIS_ID,
        "paper_only": True,
        "source_run": str(run_dir),
        "source_run_id": manifest.get("run_id"),
        "source_manifest_hash": manifest.get("manifest_hash"),
        "source_status": manifest.get("status"),
        "source_mode": manifest.get("mode"),
        "five_level_fields_present": True,
        "five_level_exchange_timestamp_present": False,
        "latency_ms": latency_ms,
        "rows": rows,
    }


def write_results(result: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "resistance_overlay_results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    fields = (
        "variant", "family", "exit_reason", "exit_trigger_time",
        "overlay_triggered", "fill_scorable", "fill_price", "fill_time",
        "book_latency_ms", "net_pnl", "mfe_net_pnl", "giveback_twd", "pnl_vs_a",
    )
    with (output_dir / "resistance_overlay_comparison.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in fields} for row in result["rows"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--symbol", default="3094")
    parser.add_argument("--stock-name", default="聯傑")
    parser.add_argument("--entry-time", default="2026-10-02T09:13:31+08:00")
    parser.add_argument("--entry-price", type=float, default=70.05)
    parser.add_argument("--quantity", type=int, default=2000)
    parser.add_argument("--latency-ms", type=int, default=250)
    args = parser.parse_args(argv)
    result = replay(
        args.run_dir.resolve(),
        symbol=args.symbol,
        stock_name=args.stock_name,
        entry_time=datetime.fromisoformat(args.entry_time).astimezone(TAIPEI),
        entry_price=args.entry_price,
        quantity=args.quantity,
        latency_ms=args.latency_ms,
    )
    write_results(result, args.output_dir.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
