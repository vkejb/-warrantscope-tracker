"""Causal microstructure features from already-collected full five-level books."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence


def _numbers(values: Sequence[object], *, positive: bool) -> tuple[float, ...]:
    result = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("book contains a non-numeric value") from exc
        if not math.isfinite(number) or (number <= 0 if positive else number < 0):
            raise ValueError("book contains an invalid value")
        result.append(number)
    if not result:
        raise ValueError("book side is empty")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class DepthFeatures:
    spread_bps: float
    l1_imbalance: float
    total_imbalance: float
    weighted_imbalance: float
    microprice: float
    microprice_edge_bps: float
    bid_l1_share: float
    ask_l1_share: float


def depth_features(
    buy_prices: Sequence[object],
    buy_volumes: Sequence[object],
    sell_prices: Sequence[object],
    sell_volumes: Sequence[object],
) -> DepthFeatures:
    bid_px = _numbers(buy_prices, positive=True)
    ask_px = _numbers(sell_prices, positive=True)
    bid_vol = _numbers(buy_volumes, positive=False)
    ask_vol = _numbers(sell_volumes, positive=False)
    levels = min(len(bid_px), len(ask_px), len(bid_vol), len(ask_vol), 5)
    bid_px, ask_px = bid_px[:levels], ask_px[:levels]
    bid_vol, ask_vol = bid_vol[:levels], ask_vol[:levels]
    if ask_px[0] < bid_px[0]:
        raise ValueError("crossed book")
    midpoint = (bid_px[0] + ask_px[0]) / 2
    l1_total = bid_vol[0] + ask_vol[0]
    total_bid, total_ask = sum(bid_vol), sum(ask_vol)
    total = total_bid + total_ask
    if midpoint <= 0 or l1_total <= 0 or total <= 0:
        raise ValueError("book has no usable liquidity")
    weights = tuple(1 / (index + 1) for index in range(levels))
    weighted_bid = sum(v * w for v, w in zip(bid_vol, weights))
    weighted_ask = sum(v * w for v, w in zip(ask_vol, weights))
    weighted_total = weighted_bid + weighted_ask
    microprice = (ask_px[0] * bid_vol[0] + bid_px[0] * ask_vol[0]) / l1_total
    return DepthFeatures(
        spread_bps=(ask_px[0] - bid_px[0]) / midpoint * 10_000,
        l1_imbalance=(bid_vol[0] - ask_vol[0]) / l1_total,
        total_imbalance=(total_bid - total_ask) / total,
        weighted_imbalance=(weighted_bid - weighted_ask) / weighted_total,
        microprice=microprice,
        microprice_edge_bps=(microprice / midpoint - 1) * 10_000,
        bid_l1_share=bid_vol[0] / total_bid if total_bid else 0.0,
        ask_l1_share=ask_vol[0] / total_ask if total_ask else 0.0,
    )


def signed_depth_depletion(previous: DepthFeatures, current: DepthFeatures) -> float:
    """Positive means the book shifted toward bids; negative toward asks."""
    return current.weighted_imbalance - previous.weighted_imbalance
