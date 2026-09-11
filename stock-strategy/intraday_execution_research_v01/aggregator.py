from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import statistics

from .schema import Tick


def _bucket(timestamp: str, minutes: int) -> str:
    value = datetime.fromisoformat(timestamp)
    minute = value.minute - value.minute % minutes
    return value.replace(minute=minute, second=0, microsecond=0).isoformat()


def aggregate_ticks(ticks: list[Tick], minutes: int) -> list[dict]:
    if minutes not in {1, 5}:
        raise ValueError("only frozen 1m/5m bars are supported")
    groups: dict[tuple[str, str], list[Tick]] = defaultdict(list)
    for tick in ticks:
        tick.validate()
        groups[(tick.stock_code, _bucket(tick.event_timestamp_exchange, minutes))].append(tick)
    rows = []
    for (symbol, start), values in sorted(groups.items()):
        values.sort(key=lambda tick: (tick.event_timestamp_exchange, tick.sequence_id or ""))
        prices = [tick.last_price for tick in values]
        sizes = [tick.last_size for tick in values]
        weight = sum(sizes)
        spreads = [tick.ask_price_1 - tick.bid_price_1 for tick in values if tick.bid_price_1 is not None and tick.ask_price_1 is not None]
        imbalances = [
            (tick.bid_size_1 - tick.ask_size_1) / (tick.bid_size_1 + tick.ask_size_1)
            for tick in values
            if tick.bid_size_1 is not None and tick.ask_size_1 is not None and tick.bid_size_1 + tick.ask_size_1 > 0
        ]
        rows.append({
            "schema_version": 1, "bar_minutes": minutes, "stock_code": symbol,
            "bar_start": start, "open": prices[0], "high": max(prices),
            "low": min(prices), "close": prices[-1], "volume": weight,
            "trade_count": len(values),
            "vwap": sum(tick.last_price * tick.last_size for tick in values) / weight if weight else None,
            "first_timestamp": values[0].event_timestamp_exchange,
            "last_timestamp": values[-1].event_timestamp_exchange,
            "mean_spread": statistics.fmean(spreads) if spreads else None,
            "median_spread": statistics.median(spreads) if spreads else None,
            "bid_ask_imbalance": statistics.fmean(imbalances) if imbalances else None,
        })
    return rows


def validate_no_future_bar(ticks: list[Tick], bars: list[dict], minutes: int) -> None:
    lookup = defaultdict(list)
    for tick in ticks:
        lookup[(tick.stock_code, _bucket(tick.event_timestamp_exchange, minutes))].append(tick)
    for bar in bars:
        source = lookup[(bar["stock_code"], bar["bar_start"])]
        if not source or bar["last_timestamp"] != max(t.event_timestamp_exchange for t in source):
            raise RuntimeError("bar used ticks outside its deterministic bucket")
