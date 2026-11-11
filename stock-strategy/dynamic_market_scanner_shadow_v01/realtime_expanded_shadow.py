"""In-process realtime expanded-universe shadow observer.

The observer consumes the collector's already-normalised quote callbacks.  It
has no broker object, account, credential, order adapter, or mutation method.
Its only side effects are append-only evidence files inside the quote run.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from queue import Empty, Full, Queue
import threading
from typing import Any
from zoneinfo import ZoneInfo

from volume_flow_entry_study_v01.analysis import evaluate_volume_gate, load_config
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file, utc_now
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC, _archived_exchange_time, _exit_quote, _projected_net_pnl,
)
from yuanta_intraday_shadow_v01.exploratory_backtest import _tick_size
from yuanta_live_runtime_v01.strategy import (
    LONG_MARKET_REGIME_POLICY, LiveDirectionEngine,
)

from .strict_replay import PASS_REASONS


TAIPEI = ZoneInfo("Asia/Taipei")
VOLUME_CONFIG = load_config()
POLICY = {
    "policy_id": "REALTIME_EXPANDED_SHADOW_V1",
    "mode": "SHADOW_ONLY_NO_BROKER_NO_ORDER",
    "candidate_gate": "V2_RECENT_VOLUME_ACCELERATION",
    "challenger_relative_strength_5m_minimum": 0.01,
    "challenger_continuation_seconds": 180,
    "challenger_continuation_net_pnl": "GREATER_THAN_ZERO_AFTER_COSTS",
    "manual_review_expiry_seconds": 60,
    "manual_review_price_cap_twd": 190,
    "manual_review_stop_loss_net_twd": int(SPEC["stop_loss_net_twd"]),
    "capital_twd": 190_000,
    "decision_interval_seconds": int(SPEC["decision_interval_seconds"]),
    "entry_start": str(SPEC["entry_start"]),
    "last_entry_time": str(SPEC["last_entry_time"]),
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
    "live_enabled": False,
}
POLICY_HASH = hashlib.sha256(canonical_bytes(POLICY)).hexdigest()
EVENTS_NAME = "realtime_expanded_shadow_events.jsonl"
SUMMARY_NAME = "realtime_expanded_shadow_summary.json"


def _stamp(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(TAIPEI)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _clock(day, value: str) -> datetime:
    hour, minute = map(int, value.split(":"))
    return datetime.combine(day, datetime.min.time(), tzinfo=TAIPEI).replace(
        hour=hour, minute=minute,
    )


def _reference_stop_price(entry_price: float, quantity: int) -> float:
    """First lower legal tick whose modelled net loss reaches the frozen cap."""
    price = float(entry_price)
    for _ in range(100_000):
        tick = _tick_size(max(price - 1e-9, 0.01))
        price = round(max(tick, price - tick), 2)
        net = float(_projected_net_pnl("LONG", entry_price, price, quantity)[3])
        if net <= -float(POLICY["manual_review_stop_loss_net_twd"]):
            return price
    raise RuntimeError("unable to derive reference stop price")


class RealtimeExpandedShadow:
    """Bounded worker that cannot interfere with raw quote persistence."""

    def __init__(self, *, queue_size: int = 200_000, manual_review_callback=None):
        self.queue_size = int(queue_size)
        if self.queue_size <= 0:
            raise ValueError("queue_size must be positive")
        self._queue: Queue = Queue(maxsize=self.queue_size)
        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._dropped = 0
        self._errors: list[str] = []
        self._offered = 0
        self._processed = 0
        self._decisions = 0
        self._run_dir: Path | None = None
        self._run_id = ""
        self._events = None
        self._engine: LiveDirectionEngine | None = None
        self._data: dict[str, dict[str, Any]] = {}
        self._names: dict[str, str] = {}
        self._seen: set[str] = set()
        self._selected: dict[str, Any] | None = None
        self._continuation: dict[str, Any] | None = None
        self._next_decision: datetime | None = None
        self._last_event_at: datetime | None = None
        self._manual_review_callback = manual_review_callback
        self._manual_review_notification: dict[str, Any] | None = None

    def start(self, *, run_dir: Path, run_id: str, expanded_items: list) -> None:
        if self._started:
            raise RuntimeError("observer already started")
        if not expanded_items:
            raise ValueError("expanded realtime observer requires candidates")
        self._run_dir = Path(run_dir)
        self._run_id = str(run_id)
        benchmark = str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"])
        self._names = {str(item.stock_id): str(item.stock_name) for item in expanded_items}
        self._names[benchmark] = str(LONG_MARKET_REGIME_POLICY["benchmark_name"])
        candidates = set(self._names) - {benchmark}
        self._engine = LiveDirectionEngine(
            self._names,
            capital_twd=int(POLICY["capital_twd"]),
            candidate_symbols=candidates,
            benchmark_symbol=benchmark,
        )
        self._data = {
            symbol: {"ticks": deque(), "books": deque()}
            for symbol in self._names
        }
        self._events = (self._run_dir / EVENTS_NAME).open(
            "x", encoding="utf-8", buffering=1,
        )
        self._started = True
        self._write("OBSERVER_STARTED", candidate_count=len(candidates))
        self._thread = threading.Thread(
            target=self._worker,
            name="expanded-realtime-shadow",
            daemon=True,
        )
        self._thread.start()

    def offer(self, kind: str, event: dict[str, Any]) -> bool:
        """Never raise into the quote callback; overflow makes results invalid."""
        if not self._started or self._closed:
            return False
        try:
            self._queue.put_nowait((str(kind), dict(event)))
            self._offered += 1
            return True
        except Full:
            self._dropped += 1
            return False
        except Exception as exc:  # pragma: no cover - defensive callback fence
            self._errors.append(type(exc).__name__)
            return False

    def _write(self, event: str, **details: Any) -> None:
        assert self._events is not None
        payload = {
            "at": utc_now(), "event": event, "run_id": self._run_id,
            "policy_hash": POLICY_HASH, "actual_orders": 0,
            "actual_fills": 0, "broker_order_calls": 0,
            **_jsonable(details),
        }
        self._events.write(
            json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n"
        )

    def _worker(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except Empty:
                continue
            if item is None:
                self._queue.task_done()
                return
            kind, event = item
            try:
                self._process(kind, event)
                self._processed += 1
            except Exception as exc:
                self._errors.append(type(exc).__name__)
                self._write(
                    "OBSERVER_EVENT_ERROR", error_type=type(exc).__name__,
                    quote_kind=kind,
                )
            finally:
                self._queue.task_done()

    def _advance_before(self, stamp: datetime) -> None:
        if self._next_decision is None:
            self._next_decision = _clock(stamp.date(), str(POLICY["entry_start"]))
        while self._next_decision < stamp:
            self._decide(self._next_decision)
            self._next_decision += timedelta(
                seconds=int(POLICY["decision_interval_seconds"]),
            )

    def _advance_equal(self, stamp: datetime) -> None:
        if self._next_decision is not None and self._next_decision == stamp:
            self._decide(self._next_decision)
            self._next_decision += timedelta(
                seconds=int(POLICY["decision_interval_seconds"]),
            )

    def _process(self, kind: str, event: dict[str, Any]) -> None:
        assert self._engine is not None
        stamp = _stamp(event["received_at"])
        if self._last_event_at is not None and stamp < self._last_event_at:
            self._write("CALLBACK_RECEIVE_TIME_REGRESSION", received_at=stamp)
            return
        self._advance_before(stamp)
        symbol = str(event["stock_id"])
        data = self._data.get(symbol)
        if data is None:
            return
        if kind.endswith("ticks"):
            exchange_stamp = _archived_exchange_time(
                event.get("quote_time"), stamp,
            )
            row = {
                "time": stamp, "received_at": stamp,
                "exchange_time": exchange_stamp,
                "price": float(event["deal_price"]),
                "volume": float(event["deal_volume"]),
                "bid": float(event["buy_price"]),
                "ask": float(event["sell_price"]),
                "flag": str(event.get("in_out_flag", "")),
                "serial": int(event.get("serial_no", 0)),
            }
            outcome = self._engine.ingest_tick(
                symbol, at=exchange_stamp, received_at=stamp, price=row["price"],
                volume=row["volume"], bid=row["bid"], ask=row["ask"],
                flag=row["flag"], serial=row["serial"],
            )
            if outcome.accepted:
                data["ticks"].append(row)
                cutoff = stamp - timedelta(seconds=360)
                while data["ticks"] and data["ticks"][0]["time"] < cutoff:
                    data["ticks"].popleft()
        elif kind.endswith("books"):
            required = ("buy_prices", "buy_volumes", "sell_prices", "sell_volumes")
            if all(key in event for key in required):
                outcome = self._engine.ingest_book_combined(
                    symbol, at=stamp,
                    buy_prices=event["buy_prices"],
                    buy_volumes=event["buy_volumes"],
                    sell_prices=event["sell_prices"],
                    sell_volumes=event["sell_volumes"],
                )
                if outcome.accepted:
                    row = {
                        "time": stamp,
                        "buy_prices": [float(value) for value in event["buy_prices"]],
                        "buy_volumes": [float(value) for value in event["buy_volumes"]],
                        "sell_prices": [float(value) for value in event["sell_prices"]],
                        "sell_volumes": [float(value) for value in event["sell_volumes"]],
                    }
                    data["books"].append(row)
                    cutoff = stamp - timedelta(seconds=360)
                    while data["books"] and data["books"][0]["time"] < cutoff:
                        data["books"].popleft()
        self._last_event_at = stamp
        self._advance_equal(stamp)

    def _gate_data(self, symbol: str) -> dict[str, Any]:
        return {"ticks": list(self._data[symbol]["ticks"])}

    def _decide(self, decision: datetime) -> None:
        assert self._engine is not None
        final = _clock(decision.date(), str(POLICY["last_entry_time"]))
        if decision > final:
            self._check_continuation(decision)
            return
        self._decisions += 1
        self._engine.choose_entry(decision, allow_short=False)
        diagnostics = self._engine.last_entry_diagnostics or {}
        newly_passed: list[dict[str, Any]] = []
        for candidate in diagnostics.get("candidates") or []:
            symbol = str(candidate["stock_id"])
            if symbol in self._seen or str(candidate.get("gate_reason")) not in PASS_REASONS:
                continue
            self._seen.add(symbol)
            gate = evaluate_volume_gate(
                self._gate_data(symbol), decision,
                "V2_RECENT_VOLUME_ACCELERATION", VOLUME_CONFIG,
            )
            row = {
                "decision_time": decision, "stock_id": symbol,
                "stock_name": self._names.get(symbol, symbol),
                "score": candidate.get("score"),
                "relative_strength_5m": candidate.get("relative_strength_5m"),
                "entry_price": candidate.get("entry_price"),
                "quantity": candidate.get("quantity"),
                "gate_evaluable": gate["evaluable"],
                "gate_passed": gate["passed"], "gate_reason": gate["reason"],
                **{key: value for key, value in gate.items()
                   if key not in {"evaluable", "passed", "reason"}},
            }
            self._write("CANDIDATE_SEEN", **row)
            if gate["passed"]:
                newly_passed.append({**candidate, **row})
        if self._selected is None and newly_passed:
            newly_passed.sort(
                key=lambda row: (-float(row.get("score") or 0), str(row["stock_id"])),
            )
            selected = newly_passed[0]
            rs = selected.get("relative_strength_5m")
            selected["rs_challenger_passed"] = bool(
                rs is not None and float(rs) >= float(
                    POLICY["challenger_relative_strength_5m_minimum"]
                )
            )
            selected["continuation_due_at"] = decision + timedelta(
                seconds=int(POLICY["challenger_continuation_seconds"]),
            )
            self._selected = selected
            self._write("SHADOW_SIGNAL_SELECTED", **selected)
        self._check_continuation(decision)

    def _check_continuation(self, decision: datetime) -> None:
        if self._selected is None or self._continuation is not None:
            return
        due = self._selected["continuation_due_at"]
        if decision < due:
            return
        ticks = list(self._data[str(self._selected["stock_id"])]["ticks"])
        latest = next((row for row in reversed(ticks) if row["time"] <= decision), None)
        if latest is None or not 0 <= (decision - latest["time"]).total_seconds() <= float(
            SPEC["maximum_tick_staleness_seconds"]
        ):
            self._continuation = {
                "status": "DATA_INSUFFICIENT", "decision_time": decision,
                "net_pnl_twd": None, "passed": None,
            }
        else:
            price = _exit_quote("LONG", latest)
            net = float(_projected_net_pnl(
                "LONG", float(self._selected["entry_price"]), price,
                int(self._selected["quantity"]),
            )[3])
            self._continuation = {
                "status": "EVALUATED", "decision_time": decision,
                "conservative_exit_price": price,
                "conservative_net_pnl_twd": net,
                "passed": net > 0,
            }
            books = list(self._data[str(self._selected["stock_id"])]["books"])
            book = next((row for row in reversed(books) if row["time"] <= decision), None)
            if book is not None and 0 <= (decision - book["time"]).total_seconds() <= float(
                SPEC["maximum_book_staleness_seconds"]
            ) and book["buy_prices"] and float(book["buy_prices"][0]) > 0:
                best_bid = float(book["buy_prices"][0])
                self._continuation["best_bid_price"] = best_bid
                self._continuation["best_bid_net_pnl_twd"] = float(
                    _projected_net_pnl(
                        "LONG", float(self._selected["entry_price"]), best_bid,
                        int(self._selected["quantity"]),
                    )[3]
                )
                best_ask = (
                    float(book["sell_prices"][0])
                    if book["sell_prices"] and float(book["sell_prices"][0]) > 0
                    else None
                )
                manual_entry = (
                    round(best_ask + _tick_size(best_ask), 2)
                    if best_ask is not None else None
                )
                quantity = (
                    math.floor(float(POLICY["capital_twd"]) / (manual_entry * 1000)) * 1000
                    if manual_entry is not None and manual_entry > 0 else 0
                )
                ready = bool(
                    self._selected.get("rs_challenger_passed")
                    and self._continuation["passed"]
                    and manual_entry is not None
                    and manual_entry <= float(POLICY["manual_review_price_cap_twd"])
                    and quantity > 0
                )
                self._continuation.update({
                    "manual_review_ready": ready,
                    "manual_entry_reference_price": manual_entry,
                    "manual_reference_quantity": quantity,
                    "manual_reference_stop_price": (
                        _reference_stop_price(manual_entry, quantity) if ready else None
                    ),
                    "manual_review_expires_at": (
                        decision + timedelta(
                            seconds=int(POLICY["manual_review_expiry_seconds"])
                        )
                    ) if ready else None,
                })
            else:
                self._continuation["best_bid_price"] = None
                self._continuation["best_bid_net_pnl_twd"] = None
                self._continuation["manual_review_ready"] = False
        self._write("THREE_MINUTE_CONTINUATION", **self._continuation)
        if self._continuation.get("manual_review_ready"):
            payload = {
                **self._selected, **self._continuation,
                "signal_decision_time": self._selected["decision_time"],
                "review_decision_time": self._continuation["decision_time"],
                "mode": "NOTIFICATION_ONLY_MANUAL_ORDER",
                "policy_hash": POLICY_HASH,
                "actual_orders": 0, "actual_fills": 0,
                "broker_order_calls": 0,
            }
            if self._manual_review_callback is None:
                self._manual_review_notification = {"status": "NO_CALLBACK_CONFIGURED"}
            else:
                try:
                    result = self._manual_review_callback(_jsonable(payload))
                    self._manual_review_notification = {
                        "status": "ATTEMPTED", "delivery": _jsonable(result),
                    }
                except Exception as exc:
                    self._manual_review_notification = {
                        "status": "FAILED", "error_type": type(exc).__name__,
                    }
            self._write(
                "MANUAL_REVIEW_NOTIFICATION", **self._manual_review_notification,
            )

    def close(self, *, timeout: float = 30.0) -> list[Path]:
        if not self._started or self._closed:
            return []
        self._closed = True
        try:
            self._queue.put(None, timeout=max(0.1, timeout))
        except Full:
            self._dropped += 1
        if self._thread is not None:
            self._thread.join(timeout=max(0.1, timeout))
            if self._thread.is_alive():
                self._errors.append("WORKER_CLOSE_TIMEOUT")
        status = (
            "VALID_SHADOW_EVIDENCE"
            if self._dropped == 0 and not self._errors and self._processed == self._offered
            else "INVALID_INCOMPLETE_SHADOW_EVIDENCE"
        )
        self._write(
            "OBSERVER_FINAL", status=status, offered=self._offered,
            processed=self._processed, dropped=self._dropped,
            errors=self._errors, decision_count=self._decisions,
        )
        assert self._events is not None and self._run_dir is not None
        self._events.flush()
        self._events.close()
        summary = {
            "schema_version": 1, "run_id": self._run_id,
            "policy": POLICY, "policy_hash": POLICY_HASH, "status": status,
            "offered_events": self._offered, "processed_events": self._processed,
            "dropped_events": self._dropped, "errors": self._errors,
            "decision_count": self._decisions,
            "selected_signal": _jsonable(self._selected),
            "three_minute_continuation": _jsonable(self._continuation),
            "manual_review_notification": _jsonable(self._manual_review_notification),
            "events_sha256": sha256_file(self._run_dir / EVENTS_NAME),
            "actual_orders": 0, "actual_fills": 0,
            "broker_order_calls": 0, "live_enabled": False,
        }
        summary["summary_hash"] = hashlib.sha256(canonical_bytes(summary)).hexdigest()
        summary_path = self._run_dir / SUMMARY_NAME
        summary_path.write_bytes(canonical_bytes(summary) + b"\n")
        return [self._run_dir / EVENTS_NAME, summary_path]
