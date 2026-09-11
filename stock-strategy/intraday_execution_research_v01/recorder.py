from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path

from .schema import Tick
from .storage import append_jsonl, atomic_json, read_jsonl, sha256_file


STATES = {"PREPARED", "RECORDING", "INTERRUPTED", "RESUMED", "SEALED", "FAILED"}


class TickRecorder:
    def __init__(self, runtime: Path, trading_date: str, symbols: list[str], watchlist_hash: str):
        self.runtime = runtime
        self.trading_date = trading_date
        self.symbols = tuple(symbols)
        self.watchlist_hash = watchlist_hash
        self.raw_dir = runtime / "intraday_raw" / trading_date.replace("-", "")
        self.state_path = runtime / "intraday_state" / f"{trading_date.replace('-', '')}.json"
        self.seal_path = runtime / "intraday_seals" / f"{trading_date.replace('-', '')}.json"
        self.seen: set[str] = set()
        self.counts = defaultdict(int)
        self.duplicates = defaultdict(int)
        self.out_of_order = defaultdict(int)
        self.cumulative_regressions = defaultdict(int)
        self.first_event: dict[str, str] = {}
        self.last_event: dict[str, str] = {}
        self.last_exchange: dict[str, str] = {}
        self.last_cumulative: dict[str, int] = {}
        self.was_interrupted = False
        self.state = "PREPARED"
        self._restore()

    def _restore(self) -> None:
        if self.seal_path.exists():
            self.state = "SEALED"
            return
        saved_duplicate_counts: dict[str, int] = {}
        if self.state_path.exists():
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if payload["watchlist_hash"] != self.watchlist_hash:
                raise RuntimeError("watchlist hash mismatch during recorder recovery")
            self.state = payload["state"]
            self.was_interrupted = bool(payload.get("was_interrupted", False))
            saved_duplicate_counts = {
                str(symbol): int(count)
                for symbol, count in payload.get("duplicate_counts", {}).items()
            }
        for symbol in self.symbols:
            for payload in read_jsonl(self.raw_dir / f"{symbol}.jsonl"):
                tick = Tick.from_payload(payload)
                self._remember(tick, restored=True)
        self.duplicates.update(saved_duplicate_counts)

    def _write_state(self) -> None:
        atomic_json(self.state_path, {
            "state": self.state,
            "trading_date": self.trading_date,
            "watchlist_hash": self.watchlist_hash,
            "symbols": list(self.symbols),
            "tick_counts": dict(self.counts),
            "duplicate_counts": dict(self.duplicates),
            "out_of_order_counts": dict(self.out_of_order),
            "cumulative_volume_regressions": dict(self.cumulative_regressions),
            "was_interrupted": self.was_interrupted,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })

    def prepare(self) -> None:
        if self.state == "SEALED":
            raise RuntimeError("sealed day is immutable")
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.state = "PREPARED"
        self._write_state()

    def start(self) -> None:
        if self.state not in {"PREPARED", "INTERRUPTED"}:
            raise RuntimeError(f"cannot start from {self.state}")
        self.state = "RESUMED" if self.state == "INTERRUPTED" else "RECORDING"
        self._write_state()

    def interrupt(self) -> None:
        if self.state not in {"RECORDING", "RESUMED"}:
            raise RuntimeError("only active recorder can be interrupted")
        self.was_interrupted = True
        self.state = "INTERRUPTED"
        self._write_state()

    def fail(self, reason: str) -> None:
        self.state = "FAILED"
        self._write_state()
        append_jsonl(self.runtime / "intraday_failures.jsonl", {
            "trading_date": self.trading_date, "reason": reason,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        })

    def _remember(self, tick: Tick, restored: bool = False) -> None:
        key = tick.duplicate_key()
        self.seen.add(key)
        symbol = tick.stock_code
        self.counts[symbol] += 1
        self.first_event.setdefault(symbol, tick.event_timestamp_exchange)
        previous = self.last_exchange.get(symbol)
        if previous is not None and tick.event_timestamp_exchange < previous:
            self.out_of_order[symbol] += 1
        previous_volume = self.last_cumulative.get(symbol)
        if previous_volume is not None and tick.cumulative_volume < previous_volume:
            self.cumulative_regressions[symbol] += 1
        self.last_event[symbol] = tick.event_timestamp_exchange
        self.last_exchange[symbol] = max(previous or tick.event_timestamp_exchange, tick.event_timestamp_exchange)
        self.last_cumulative[symbol] = max(previous_volume or 0, tick.cumulative_volume)

    def record(self, tick: Tick) -> bool:
        if self.state == "SEALED" or self.seal_path.exists():
            raise RuntimeError("sealed day is immutable")
        if self.state not in {"RECORDING", "RESUMED"}:
            raise RuntimeError("recorder is not active")
        tick.validate()
        if tick.trading_date != self.trading_date or tick.stock_code not in self.symbols:
            raise ValueError("tick outside watchlist/trading date")
        key = tick.duplicate_key()
        if key in self.seen:
            self.duplicates[tick.stock_code] += 1
            self._write_state()
            return False
        append_jsonl(self.raw_dir / f"{tick.stock_code}.jsonl", tick.payload())
        self._remember(tick)
        self._write_state()
        return True

    def raw_manifest(self) -> dict:
        files = []
        total_large_gaps = 0
        largest_gap = 0.0
        for symbol in self.symbols:
            path = self.raw_dir / f"{symbol}.jsonl"
            if path.exists():
                event_times = sorted(
                    datetime.fromisoformat(row["event_timestamp_exchange"])
                    for row in read_jsonl(path)
                )
                gaps = [
                    (later - earlier).total_seconds()
                    for earlier, later in zip(event_times, event_times[1:])
                ]
                symbol_largest_gap = max(gaps, default=0.0)
                symbol_large_gaps = sum(gap > 600 for gap in gaps)
                largest_gap = max(largest_gap, symbol_largest_gap)
                total_large_gaps += symbol_large_gaps
                files.append({
                    "stock_code": symbol, "path": str(path.relative_to(self.runtime)),
                    "sha256": sha256_file(path), "tick_count": self.counts[symbol],
                    "first_event_timestamp": self.first_event.get(symbol),
                    "last_event_timestamp": self.last_event.get(symbol),
                    "duplicate_count": self.duplicates[symbol],
                    "out_of_order_count": self.out_of_order[symbol],
                    "cumulative_volume_regressions": self.cumulative_regressions[symbol],
                    "max_event_gap_seconds": symbol_largest_gap,
                    "gaps_over_10_minutes": symbol_large_gaps,
                })
        return {
            "watchlist_hash": self.watchlist_hash,
            "trading_date": self.trading_date,
            "raw_files": files,
            "missing_symbols": [symbol for symbol in self.symbols if not self.counts[symbol]],
            "interrupted_symbols": [symbol for symbol in self.symbols if self.was_interrupted and self.counts[symbol]],
            "tick_count": sum(self.counts.values()),
            "duplicate_count": sum(self.duplicates.values()),
            "out_of_order_count": sum(self.out_of_order.values()),
            "cumulative_volume_regressions": sum(self.cumulative_regressions.values()),
            "gap_diagnostics": {
                "max_event_gap_seconds": largest_gap,
                "gaps_over_10_minutes": total_large_gaps,
                "threshold_seconds": 600,
            },
        }

    def mark_sealed(self) -> None:
        if not self.seal_path.exists():
            raise RuntimeError("cannot mark sealed before immutable seal exists")
        self.state = "SEALED"
        self._write_state()
