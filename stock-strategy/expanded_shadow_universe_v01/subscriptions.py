"""Vendor-limit-aware, deterministic quote subscription batching."""

from __future__ import annotations

from typing import Iterable, TypeVar


T = TypeVar("T")
MAX_SYMBOLS_PER_SUBSCRIPTION_CALL = 200


def deduplicate_items(*groups: Iterable[T]) -> list[T]:
    result: list[T] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            symbol = str(getattr(item, "stock_id"))
            if symbol in seen:
                continue
            seen.add(symbol)
            result.append(item)
    return result


def batches(items: list[T], size: int = MAX_SYMBOLS_PER_SUBSCRIPTION_CALL) -> list[list[T]]:
    if size <= 0 or size > MAX_SYMBOLS_PER_SUBSCRIPTION_CALL:
        raise ValueError("subscription batch size must be between 1 and 200")
    return [items[index:index + size] for index in range(0, len(items), size)]
