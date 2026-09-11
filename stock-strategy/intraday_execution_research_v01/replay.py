from __future__ import annotations

from pathlib import Path
import time
from typing import Callable

from .schema import Tick
from .storage import read_jsonl


def load_ticks(raw_dir: Path) -> list[Tick]:
    ticks = [Tick.from_payload(row) for path in sorted(raw_dir.glob("*.jsonl")) for row in read_jsonl(path)]
    return sorted(ticks, key=lambda tick: (tick.event_timestamp_exchange, tick.stock_code, tick.sequence_id or ""))


def replay(raw_dir: Path, handler: Callable[[Tick], None], speed: str = "instant", acceleration: float = 60.0) -> int:
    if speed not in {"realtime", "accelerated", "instant"}:
        raise ValueError("unsupported replay speed")
    ticks = load_ticks(raw_dir)
    previous = None
    for tick in ticks:
        current = Tick._time(tick.event_timestamp_exchange)
        if previous is not None and speed != "instant":
            delay = max(0.0, (current - previous).total_seconds())
            time.sleep(delay if speed == "realtime" else delay / acceleration)
        handler(tick)
        previous = current
    return len(ticks)
