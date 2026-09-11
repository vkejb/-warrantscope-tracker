from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .config import CFG
from .schema import Tick


FEATURE_NAMES = (
    "gap_vs_signal_close", "open_vs_signal_close", "opening_range_5m", "opening_range_15m",
    "price_vs_vwap_5m", "price_vs_vwap_15m", "vwap_slope_5m", "vwap_reclaim",
    "volume_5m", "volume_15m", "volume_acceleration", "relative_volume_vs_intraday_history",
    "return_5m", "return_15m", "return_30m", "intraday_drawdown_from_open",
    "recovery_from_intraday_low", "distance_from_opening_high", "distance_from_opening_low",
    "spread_bps", "top1_bid_ask_imbalance", "top5_bid_ask_imbalance", "depth_ratio",
    "stock_return_minus_market_return", "stock_return_minus_stageA_pool_median",
)


def _cutoff(trading_date: str, snapshot: str) -> datetime:
    return datetime.fromisoformat(f"{trading_date}T{snapshot}:00").replace(tzinfo=ZoneInfo(CFG.timezone))


def _vwap(ticks: list[Tick]) -> float | None:
    volume = sum(t.last_size for t in ticks)
    return sum(t.last_price * t.last_size for t in ticks) / volume if volume else None


def _return_since(ticks: list[Tick], cutoff: datetime, minutes: int) -> float | None:
    eligible = [tick for tick in ticks if datetime.fromisoformat(tick.event_timestamp_exchange) <= cutoff]
    prior = [tick for tick in eligible if datetime.fromisoformat(tick.event_timestamp_exchange).timestamp() <= cutoff.timestamp() - minutes * 60]
    return eligible[-1].last_price / prior[-1].last_price - 1 if eligible and prior else None


def snapshot_features(
    ticks: list[Tick], trading_date: str, snapshot: str, signal_close: float,
    market_return: float | None = None, pool_median_return: float | None = None,
) -> dict:
    if snapshot not in CFG.snapshot_times:
        raise ValueError("snapshot time is not preregistered")
    cutoff = _cutoff(trading_date, snapshot)
    past = sorted(
        [tick for tick in ticks if datetime.fromisoformat(tick.event_timestamp_exchange) <= cutoff],
        key=lambda tick: (tick.event_timestamp_exchange, tick.sequence_id or ""),
    )
    future = sorted(
        [tick for tick in ticks if datetime.fromisoformat(tick.event_timestamp_exchange) > cutoff],
        key=lambda tick: (tick.event_timestamp_exchange, tick.sequence_id or ""),
    )
    if not past:
        return {"snapshot_time": snapshot, "features": {name: None for name in FEATURE_NAMES}, "entry_proxy": None, "quality_flags": ["INSUFFICIENT_PRE_SNAPSHOT_DATA"]}
    first, last = past[0], past[-1]
    opening5 = [t for t in past if datetime.fromisoformat(t.event_timestamp_exchange) <= _cutoff(trading_date, "09:05")]
    opening15 = [t for t in past if datetime.fromisoformat(t.event_timestamp_exchange) <= _cutoff(trading_date, "09:15")]
    recent5 = [t for t in past if datetime.fromisoformat(t.event_timestamp_exchange).timestamp() > cutoff.timestamp() - 300]
    recent15 = [t for t in past if datetime.fromisoformat(t.event_timestamp_exchange).timestamp() > cutoff.timestamp() - 900]
    previous5 = [
        t for t in past
        if cutoff.timestamp() - 600 < datetime.fromisoformat(t.event_timestamp_exchange).timestamp() <= cutoff.timestamp() - 300
    ]
    vwap5, vwap15 = _vwap(recent5), _vwap(recent15)
    previous_vwap5 = _vwap(previous5)
    spread = None
    imbalance = None
    if last.bid_price_1 is not None and last.ask_price_1 is not None:
        spread = (last.ask_price_1 - last.bid_price_1) / ((last.ask_price_1 + last.bid_price_1) / 2) * 10000
    if last.bid_size_1 is not None and last.ask_size_1 is not None and last.bid_size_1 + last.ask_size_1:
        imbalance = (last.bid_size_1 - last.ask_size_1) / (last.bid_size_1 + last.ask_size_1)
    stock_return = last.last_price / first.last_price - 1
    features = {
        "gap_vs_signal_close": first.last_price / signal_close - 1,
        "open_vs_signal_close": first.last_price / signal_close - 1,
        "opening_range_5m": (max(t.last_price for t in opening5) / min(t.last_price for t in opening5) - 1) if opening5 else None,
        "opening_range_15m": (max(t.last_price for t in opening15) / min(t.last_price for t in opening15) - 1) if opening15 else None,
        "price_vs_vwap_5m": last.last_price / vwap5 - 1 if vwap5 else None,
        "price_vs_vwap_15m": last.last_price / vwap15 - 1 if vwap15 else None,
        "vwap_slope_5m": vwap5 / previous_vwap5 - 1 if vwap5 and previous_vwap5 else None,
        "vwap_reclaim": bool(vwap15 and last.last_price >= vwap15 and any(t.last_price < vwap15 for t in recent15)),
        "volume_5m": sum(t.last_size for t in recent5),
        "volume_15m": sum(t.last_size for t in recent15),
        "volume_acceleration": (sum(t.last_size for t in recent5) / max(sum(t.last_size for t in recent15) / 3, 1)),
        "relative_volume_vs_intraday_history": None,
        "return_5m": _return_since(past, cutoff, 5),
        "return_15m": _return_since(past, cutoff, 15),
        "return_30m": _return_since(past, cutoff, 30),
        "intraday_drawdown_from_open": min(t.last_price for t in past) / first.last_price - 1,
        "recovery_from_intraday_low": last.last_price / min(t.last_price for t in past) - 1,
        "distance_from_opening_high": last.last_price / max(t.last_price for t in opening15) - 1 if opening15 else None,
        "distance_from_opening_low": last.last_price / min(t.last_price for t in opening15) - 1 if opening15 else None,
        "spread_bps": spread, "top1_bid_ask_imbalance": imbalance,
        "top5_bid_ask_imbalance": None, "depth_ratio": None,
        "stock_return_minus_market_return": stock_return - market_return if market_return is not None else None,
        "stock_return_minus_stageA_pool_median": stock_return - pool_median_return if pool_median_return is not None else None,
    }
    flags = []
    if market_return is None: flags.append("MARKET_REFERENCE_UNAVAILABLE")
    if pool_median_return is None: flags.append("POOL_MEDIAN_UNAVAILABLE")
    if last.bid_price_1 is None or last.ask_price_1 is None: flags.append("TOP1_BOOK_UNAVAILABLE")
    flags.extend(["TOP5_BOOK_UNAVAILABLE", "INTRADAY_HISTORY_UNAVAILABLE"])
    entry = None
    if future:
        tick = future[0]
        entry = {"price": tick.last_price, "timestamp": tick.event_timestamp_exchange, "sequence_id": tick.sequence_id}
    return {"snapshot_time": snapshot, "cutoff": cutoff.isoformat(), "features": features, "entry_proxy": entry, "quality_flags": flags}


def build_daily_snapshots(ticks_by_symbol: dict[str, list[Tick]], watchlist: dict) -> list[dict]:
    closes = {item["stock_code"]: item.get("signal_close") for item in watchlist["symbols"]}
    rows = []
    for symbol, ticks in sorted(ticks_by_symbol.items()):
        signal_close = closes.get(symbol)
        signal_close_is_mock_proxy = signal_close is None
        if signal_close is None:
            # Stage A store has no raw close; mock infrastructure uses its first
            # generated reference price and records that limitation explicitly.
            signal_close = ticks[0].last_price
        for snapshot in CFG.snapshot_times:
            row = snapshot_features(ticks, watchlist["subscription_trading_date_formatted"], snapshot, signal_close)
            if signal_close_is_mock_proxy:
                row["quality_flags"].append("SIGNAL_CLOSE_PROXY_FROM_FIRST_MOCK_TICK")
            row.update({"stock_code": symbol, "signal_close_proxy": signal_close})
            rows.append(row)
    return rows
