"""Fail-closed same-day quote warm-up for a late realtime start.

The read-only shadow collector starts before the market and writes the same
normalized Yuanta tick fields consumed by the live direction engine.  A late
runtime may reuse that evidence only after checking the sealed watchlist,
trading date, opening coverage, callback health, and source freshness.

No broker or order API is imported here.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import gzip
import json
import math
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from .strategy import ANTI_CHASE_ENTRY_POLICY, SPEC, LiveDirectionEngine


TAIPEI = ZoneInfo("Asia/Taipei")
ALLOWED_SOURCE_MODES = frozenset({"SHADOW_ONLY_READ_ONLY_QUOTES"})
RETAIN_SECONDS = max(360, int(SPEC["large_trade_reference_seconds"]) + 30)


@dataclass(frozen=True, slots=True)
class LateStartWarmupResult:
    required: bool
    status: str
    source_run_id: str | None
    source_mode: str | None
    trading_date: str
    signal_date: str
    symbols_required: int
    symbols_warmed: int
    accepted_ticks: int
    rejected_ticks: int
    first_exchange_time: str | None
    latest_exchange_time: str | None
    latest_received_at: str | None
    incomplete_gzip_tail_ignored: bool

    def public_snapshot(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class _SymbolHistory:
    opening_price: float | None = None
    opening_time: datetime | None = None
    cumulative_volume: float = 0.0
    cumulative_pv: float = 0.0
    last_serial: int = 0
    previous_time: datetime | None = None
    retained: deque | None = None
    accepted: int = 0
    rejected: int = 0

    def __post_init__(self) -> None:
        self.retained = deque()


def warmup_required(now: datetime) -> bool:
    local = now.astimezone(TAIPEI).time().replace(tzinfo=None)
    opening_deadline = datetime.strptime(
        str(ANTI_CHASE_ENTRY_POLICY["opening_reference_must_arrive_by"]), "%H:%M"
    ).time()
    entry_close = datetime.strptime(str(SPEC["last_entry_time"]), "%H:%M").time()
    return opening_deadline < local <= entry_close


def _parse_stamp(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(TAIPEI)


def _exchange_stamp(value: object, trading_date) -> datetime:
    clock = datetime.strptime(str(value), "%H:%M:%S.%f").time()
    return datetime.combine(trading_date, clock, tzinfo=TAIPEI)


def _iter_jsonl(path: Path) -> tuple[Iterable[dict], list[bool]]:
    """Return an iterator plus a mutable flag for an active gzip tail.

    An active collector's gzip member has not written its final CRC yet.  All
    complete newline-delimited records before that tail remain usable; the
    unfinished final record is never yielded.
    """

    incomplete_tail = [False]

    def rows():
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                while True:
                    try:
                        line = handle.readline()
                    except (EOFError, OSError):
                        incomplete_tail[0] = True
                        return
                    if not line:
                        return
                    if not line.endswith("\n"):
                        incomplete_tail[0] = True
                        return
                    try:
                        value = json.loads(line)
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(
                            f"invalid complete JSON record in warm-up source: {path.name}"
                        ) from exc
                    if isinstance(value, dict):
                        yield value

        except (EOFError, OSError):
            incomplete_tail[0] = True

    return rows(), incomplete_tail


def _candidate_metadata(
    runtime_dir: Path,
    *,
    now: datetime,
    signal_date: str,
    seal_hash: str,
    required_symbols: set[str],
) -> list[tuple[datetime, Path, dict]]:
    root = Path(runtime_dir) / "runs"
    if not root.is_dir():
        return []
    candidates: list[tuple[datetime, Path, dict]] = []
    for run_dir in root.iterdir():
        watchlist_path = run_dir / "watchlist.json"
        if not run_dir.is_dir() or not watchlist_path.is_file():
            continue
        try:
            watch = json.loads(watchlist_path.read_text(encoding="utf-8"))
            created = _parse_stamp(watch["created_at"])
            stocks = {str(row["stock_id"]) for row in watch["stocks"]}
            context = {str(row["stock_id"]) for row in watch.get("market_context", [])}
        except (KeyError, TypeError, ValueError, OSError):
            continue
        if created.date() != now.astimezone(TAIPEI).date():
            continue
        if str(watch.get("signal_date")) != str(signal_date):
            continue
        if str(watch.get("stage_a_seal_hash")) != str(seal_hash):
            continue
        if str(watch.get("mode")) not in ALLOWED_SOURCE_MODES:
            continue
        if stocks | context != required_symbols:
            continue
        callback_errors = run_dir / "callback_errors.jsonl"
        try:
            if not callback_errors.is_file() or callback_errors.stat().st_size != 0:
                continue
        except OSError:
            continue
        tick_path = run_dir / "ticks.jsonl.gz"
        market_path = run_dir / "market_context_ticks.jsonl.gz"
        if not tick_path.is_file() or not market_path.is_file():
            continue
        candidates.append((created, run_dir, watch))
    return sorted(candidates, key=lambda row: row[0])


def _consume(
    rows: Iterable[dict],
    *,
    histories: dict[str, _SymbolHistory],
    trading_date,
    now: datetime,
) -> tuple[int, int, datetime | None, datetime | None, datetime | None]:
    accepted = rejected = 0
    first_exchange = latest_exchange = latest_received = None
    for row in rows:
        symbol = str(row.get("stock_id", ""))
        history = histories.get(symbol)
        if history is None or str(row.get("event_type", "")) != "STOCK_TICK":
            rejected += 1
            continue
        try:
            stamp = _exchange_stamp(row["quote_time"], trading_date)
            received = _parse_stamp(row["received_at"])
            price = float(row["deal_price"])
            volume = float(row["deal_volume"])
            bid = float(row["buy_price"])
            ask = float(row["sell_price"])
            serial = int(row.get("serial_no", 0) or 0)
        except (KeyError, TypeError, ValueError, OverflowError):
            history.rejected += 1
            rejected += 1
            continue
        age = (received - stamp).total_seconds()
        valid = (
            stamp.date() == trading_date
            and received.date() == trading_date
            and stamp <= now
            and -0.5 <= age <= float(SPEC["maximum_tick_staleness_seconds"])
            and all(math.isfinite(value) for value in (price, volume, bid, ask))
            and min(price, bid, ask) > 0
            and volume >= 0
            and ask >= bid
            and (history.previous_time is None or stamp >= history.previous_time)
            and not (serial > 0 and history.last_serial > 0 and serial <= history.last_serial)
        )
        if not valid:
            history.rejected += 1
            rejected += 1
            continue
        if history.opening_time is None:
            history.opening_time = stamp
            history.opening_price = price
        history.previous_time = stamp
        if serial > 0:
            history.last_serial = serial
        history.cumulative_volume += volume
        history.cumulative_pv += price * volume
        history.retained.append({
            "time": stamp,
            "received_at": received,
            "price": price,
            "volume": volume,
            "bid": bid,
            "ask": ask,
            "flag": str(row.get("in_out_flag", "")),
            "serial": serial,
        })
        cutoff = stamp - timedelta(seconds=RETAIN_SECONDS)
        while history.retained and history.retained[0]["time"] < cutoff:
            history.retained.popleft()
        history.accepted += 1
        accepted += 1
        first_exchange = stamp if first_exchange is None else min(first_exchange, stamp)
        latest_exchange = stamp if latest_exchange is None else max(latest_exchange, stamp)
        latest_received = received if latest_received is None else max(latest_received, received)
    return accepted, rejected, first_exchange, latest_exchange, latest_received


def warm_start_from_collector(
    engine: LiveDirectionEngine,
    *,
    runtime_dir: Path,
    now: datetime,
    signal_date: str,
    seal_hash: str,
    required_symbols: set[str],
    max_source_age_seconds: float = 30.0,
) -> LateStartWarmupResult:
    """Load the earliest valid pre-open collector run into a fresh engine."""

    now = now.astimezone(TAIPEI)
    required = warmup_required(now)
    empty = LateStartWarmupResult(
        required=required,
        status="NOT_REQUIRED" if not required else "SOURCE_NOT_FOUND",
        source_run_id=None,
        source_mode=None,
        trading_date=now.date().isoformat(),
        signal_date=str(signal_date),
        symbols_required=len(required_symbols),
        symbols_warmed=0,
        accepted_ticks=0,
        rejected_ticks=0,
        first_exchange_time=None,
        latest_exchange_time=None,
        latest_received_at=None,
        incomplete_gzip_tail_ignored=False,
    )
    if not required:
        return empty

    metadata = _candidate_metadata(
        runtime_dir,
        now=now,
        signal_date=str(signal_date),
        seal_hash=str(seal_hash),
        required_symbols=set(required_symbols),
    )
    opening_deadline = datetime.strptime(
        str(ANTI_CHASE_ENTRY_POLICY["opening_reference_must_arrive_by"]), "%H:%M"
    ).time()
    last_error = "SOURCE_NOT_FOUND"
    for _created, run_dir, watch in metadata:
        histories = {symbol: _SymbolHistory() for symbol in required_symbols}
        totals = [0, 0]
        first_exchange = latest_exchange = latest_received = None
        incomplete = False
        for name in ("ticks.jsonl.gz", "market_context_ticks.jsonl.gz"):
            rows, incomplete_flag = _iter_jsonl(run_dir / name)
            result = _consume(
                rows,
                histories=histories,
                trading_date=now.date(),
                now=now,
            )
            totals[0] += result[0]
            totals[1] += result[1]
            if result[2] is not None:
                first_exchange = result[2] if first_exchange is None else min(first_exchange, result[2])
            if result[3] is not None:
                latest_exchange = result[3] if latest_exchange is None else max(latest_exchange, result[3])
            if result[4] is not None:
                latest_received = result[4] if latest_received is None else max(latest_received, result[4])
            incomplete = incomplete or incomplete_flag[0]

        missing = [
            symbol for symbol, history in histories.items()
            if history.opening_time is None
            or history.opening_time.time() > opening_deadline
            or history.accepted <= 0
            or not history.retained
        ]
        if missing:
            last_error = "OPENING_COVERAGE_INCOMPLETE"
            continue
        if latest_received is None:
            last_error = "NO_VALID_TICKS"
            continue
        source_age = (now - latest_received).total_seconds()
        if source_age < -0.5 or source_age > float(max_source_age_seconds):
            last_error = "SOURCE_STALE"
            continue

        for symbol in sorted(required_symbols):
            history = histories[symbol]
            outcome = engine.seed_session_history(
                symbol,
                session_date=now.date(),
                opening_price=history.opening_price,
                opening_time=history.opening_time,
                cumulative_volume=history.cumulative_volume,
                cumulative_pv=history.cumulative_pv,
                retained_ticks=list(history.retained),
                last_serial=history.last_serial,
                source=f"COLLECTOR:{run_dir.name}",
            )
            if not outcome.accepted:
                raise RuntimeError(
                    f"late-start warm-up rejected {symbol}: {outcome.reason}"
                )

        return LateStartWarmupResult(
            required=True,
            status="READY",
            source_run_id=run_dir.name,
            source_mode=str(watch.get("mode")),
            trading_date=now.date().isoformat(),
            signal_date=str(signal_date),
            symbols_required=len(required_symbols),
            symbols_warmed=len(required_symbols),
            accepted_ticks=totals[0],
            rejected_ticks=totals[1],
            first_exchange_time=first_exchange.isoformat() if first_exchange else None,
            latest_exchange_time=latest_exchange.isoformat() if latest_exchange else None,
            latest_received_at=latest_received.isoformat() if latest_received else None,
            incomplete_gzip_tail_ignored=incomplete,
        )

    return LateStartWarmupResult(
        **{
            **empty.public_snapshot(),
            "status": last_error,
        }
    )
