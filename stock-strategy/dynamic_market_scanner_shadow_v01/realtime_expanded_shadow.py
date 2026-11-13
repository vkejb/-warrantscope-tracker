"""In-process realtime expanded-universe shadow observer.

The observer consumes the collector's already-normalised quote callbacks.  It
has no broker object, account, credential, order adapter, or mutation method.
Its only side effects are append-only evidence files inside the quote run.
"""

from __future__ import annotations

from collections import Counter, deque
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
    ANTI_CHASE_ENTRY_POLICY, LONG_MARKET_REGIME_POLICY, LiveDirectionEngine,
)

from .strict_replay import PASS_REASONS


TAIPEI = ZoneInfo("Asia/Taipei")
VOLUME_CONFIG = load_config()
POLICY = {
    "policy_id": "REALTIME_EXPANDED_SHADOW_V2_REARM",
    "mode": "SHADOW_ONLY_NO_BROKER_NO_ORDER",
    "candidate_gate": "V2_RECENT_VOLUME_ACCELERATION",
    "challenger_relative_strength_5m_minimum": 0.01,
    "challenger_continuation_seconds": 180,
    "challenger_continuation_net_pnl": "GREATER_THAN_ZERO_AFTER_COSTS",
    "manual_review_expiry_seconds": 60,
    "manual_review_price_cap_twd": 190,
    "manual_review_stop_loss_net_twd": int(SPEC["stop_loss_net_twd"]),
    "selection_state_machine": (
        "RS_BEFORE_RESERVATION_RELEASE_AFTER_FAILED_CONTINUATION"
    ),
    "maximum_mature_candidate_callbacks": 1,
    "capital_twd": 190_000,
    "decision_interval_seconds": int(SPEC["decision_interval_seconds"]),
    "entry_start": str(SPEC["entry_start"]),
    "last_entry_time": str(SPEC["last_entry_time"]),
    "early_observation": {
        "mode": "TELEMETRY_ONLY_NO_NOTIFICATION_NO_ORDER",
        "full_universe_interval_seconds": int(SPEC["decision_interval_seconds"]),
        "armed_event_throttle_seconds": 5,
        "maximum_full_feature_symbols_per_interval": 40,
        "full_feature_prefilter": (
            "FRESH_PRICE_CAP_ABOVE_VWAP_EXISTING_ANTI_CHASE_AND_MARKET_RS"
        ),
        "prewatch_minimum_exact_base_checks": 3,
        "armed_minimum_exact_base_checks": 4,
        "entry_ready_requires_all_exact_base_checks": True,
        "entry_ready_requires_unchanged_v2_gate": True,
        "market_relative_strength_and_existing_anti_chase_required": True,
    },
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
    "live_enabled": False,
}
POLICY_HASH = hashlib.sha256(canonical_bytes(POLICY)).hexdigest()
EVENTS_NAME = "realtime_expanded_shadow_events.jsonl"
SUMMARY_NAME = "realtime_expanded_shadow_summary.json"
EARLY_STATES = ("NONE", "PREWATCH", "ARMED", "EARLY_ENTRY_READY_OBSERVED")


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

    def __init__(
        self,
        *,
        queue_size: int = 200_000,
        manual_review_callback=None,
        candidate_ready_callback=None,
        candidate_status_callback=None,
    ):
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
        self._reported: set[str] = set()
        self._selected: dict[str, Any] | None = None
        self._continuation: dict[str, Any] | None = None
        self._selection_attempts: list[dict[str, Any]] = []
        self._continuation_attempts: list[dict[str, Any]] = []
        self._mature_candidate_emitted = False
        self._next_decision: datetime | None = None
        self._last_event_at: datetime | None = None
        self._manual_review_callback = manual_review_callback
        self._candidate_ready_callback = candidate_ready_callback
        self._candidate_status_callback = candidate_status_callback
        self._manual_review_notification: dict[str, Any] | None = None
        self._candidate_ready_delivery: dict[str, Any] | None = None
        self._candidate_status_deliveries: list[dict[str, Any]] = []
        self._restoring_history = False
        self._restored_mature_candidate_count = 0
        self._restore_lock = threading.Lock()
        self._restore_live_buffer: list[tuple[str, dict[str, Any]]] = []
        self._early: dict[str, dict[str, Any]] = {}
        self._early_last_eval: dict[str, datetime] = {}
        self._early_prefilter_runs = 0
        self._early_prefilter_max_selected = 0
        self._early_market_cache_at: datetime | None = None
        self._early_market_cache: dict[str, Any] | None = None

    def _deliver_candidate_status(self, payload: dict[str, Any]) -> None:
        """Deliver non-trading candidate telemetry without replaying old alerts."""
        if self._restoring_history:
            return
        delivery: dict[str, Any] = {
            "stock_id": str(payload.get("stock_id", "")),
            "reason": str(payload.get("reason", "UNKNOWN")),
        }
        if self._candidate_status_callback is None:
            delivery["status"] = "NO_CALLBACK_CONFIGURED"
        else:
            try:
                result = self._candidate_status_callback(_jsonable(payload))
                delivery.update({
                    "status": "ATTEMPTED",
                    "delivery": _jsonable(result),
                })
            except Exception as exc:
                delivery.update({
                    "status": "FAILED",
                    "error_type": type(exc).__name__,
                })
        self._candidate_status_deliveries.append(delivery)
        self._write("CANDIDATE_STATUS_CALLBACK", **delivery)

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
            symbol: {
                "ticks": deque(), "books": deque(), "cumulative_volume": 0.0,
                "cumulative_pv": 0.0, "opening_price": None,
                "opening_time": None,
            }
            for symbol in self._names
        }
        self._early = {
            symbol: {
                "state": "NONE", "first_prewatch_at": None,
                "first_armed_at": None, "first_early_ready_at": None,
                "official_candidate_seen_at": None,
                "official_selected_at": None,
            }
            for symbol in candidates
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
            with self._restore_lock:
                if self._restoring_history:
                    if len(self._restore_live_buffer) >= self.queue_size:
                        self._dropped += 1
                        return False
                    self._restore_live_buffer.append((str(kind), dict(event)))
                    self._offered += 1
                    return True
            self._queue.put_nowait((str(kind), dict(event)))
            self._offered += 1
            return True
        except Full:
            self._dropped += 1
            return False
        except Exception as exc:  # pragma: no cover - defensive callback fence
            self._errors.append(type(exc).__name__)
            return False

    def health_snapshot(self) -> dict[str, Any]:
        """Return the worker state used by a separate fail-closed consumer.

        The shadow observer still has no broker capability.  A caller that
        chooses to consume its mature-candidate callback must independently
        refuse admission whenever this evidence pipeline is incomplete.
        """
        thread_alive = bool(self._thread is not None and self._thread.is_alive())
        return {
            "started": self._started,
            "closed": self._closed,
            "thread_alive": thread_alive,
            "offered": self._offered,
            "processed": self._processed,
            "dropped": self._dropped,
            "errors": list(self._errors),
            "healthy": bool(
                self._started
                and not self._closed
                and thread_alive
                and self._dropped == 0
                and not self._errors
            ),
            "restoring_history": self._restoring_history,
            "restored_mature_candidate_count": self._restored_mature_candidate_count,
        }

    def restore_from_events(
        self,
        events,
        *,
        source_run_id: str,
        progress_callback=None,
        start_live_callback=None,
    ) -> dict[str, Any]:
        """Causally rebuild a fresh observer before live subscription starts.

        Historical events deliberately exercise the normal state machine, but
        a mature historical candidate is consumed without invoking either the
        live-admission or manual-notification callback.  A restart must never
        turn an expired signal into a new order opportunity.
        """
        if not self._started or self._closed or self._thread is None:
            raise RuntimeError("observer must be running before history restore")
        if self._offered or self._processed or self._last_event_at is not None:
            raise RuntimeError("history restore requires a fresh observer")
        with self._restore_lock:
            self._restoring_history = True
        restored = 0
        self._write(
            "OBSERVER_HISTORY_RESTORE_STARTED",
            source_run_id=str(source_run_id),
        )
        try:
            if start_live_callback is not None:
                start_live_callback()
            for kind, event in events:
                self._queue.put((str(kind), dict(event)))
                with self._restore_lock:
                    self._offered += 1
                restored += 1
                if progress_callback is not None and restored % 10_000 == 0:
                    progress_callback(restored)
            self._queue.join()
            while True:
                with self._restore_lock:
                    buffered = self._restore_live_buffer
                    self._restore_live_buffer = []
                    if not buffered:
                        self._restoring_history = False
                        break
                buffered.sort(key=lambda item: _stamp(item[1]["received_at"]))
                for item in buffered:
                    self._queue.put(item)
                self._queue.join()
            if self._dropped or self._errors or self._processed != self._offered:
                raise RuntimeError("expanded V2 history restore is incomplete")
        finally:
            with self._restore_lock:
                self._restoring_history = False
        self._write(
            "OBSERVER_HISTORY_RESTORE_COMPLETED",
            source_run_id=str(source_run_id),
            restored_events=restored,
            last_event_at=self._last_event_at,
            restored_mature_candidate_count=self._restored_mature_candidate_count,
        )
        return {
            "source_run_id": str(source_run_id),
            "restored_events": restored,
            "last_event_at": (
                None if self._last_event_at is None else self._last_event_at.isoformat()
            ),
            "restored_mature_candidate_count": self._restored_mature_candidate_count,
        }

    def invalidate(self, reason: str) -> None:
        """Permanently fail closed after an upstream persistence failure."""
        code = str(reason).strip() or "UPSTREAM_EVIDENCE_INVALID"
        if code not in self._errors:
            self._errors.append(code)

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
                data["cumulative_volume"] += row["volume"]
                data["cumulative_pv"] += row["price"] * row["volume"]
                if data["opening_price"] is None:
                    data["opening_price"] = row["price"]
                    data["opening_time"] = row["time"]
                if symbol == str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"]):
                    self._early_market_cache_at = None
                    self._early_market_cache = None
                cutoff = stamp - timedelta(seconds=360)
                while data["ticks"] and data["ticks"][0]["time"] < cutoff:
                    data["ticks"].popleft()
                if symbol in self._early and self._early[symbol]["state"] in {
                    "ARMED", "EARLY_ENTRY_READY_OBSERVED",
                }:
                    prior = self._early_last_eval.get(symbol)
                    throttle = int(
                        POLICY["early_observation"]["armed_event_throttle_seconds"]
                    )
                    if prior is None or (stamp - prior).total_seconds() >= throttle:
                        self._observe_early(symbol, stamp, source="EVENT_DRIVEN_TICK")
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

    @staticmethod
    def _signed_volume(row: dict[str, Any]) -> float:
        if str(row.get("flag", "")) == "1":
            return float(row["volume"])
        if str(row.get("flag", "")) == "0":
            return -float(row["volume"])
        midpoint = (float(row["bid"]) + float(row["ask"])) / 2
        return float(row["volume"]) if float(row["price"]) >= midpoint else -float(row["volume"])

    @staticmethod
    def _return_over(rows: list[dict[str, Any]], at: datetime, seconds: int) -> float | None:
        cutoff = at - timedelta(seconds=seconds)
        earlier = [row for row in rows if row["time"] <= cutoff]
        latest = [row for row in rows if row["time"] <= at]
        if not earlier or not latest or float(earlier[-1]["price"]) <= 0:
            return None
        return float(latest[-1]["price"]) / float(earlier[-1]["price"]) - 1

    def _early_market(self, at: datetime) -> dict[str, Any] | None:
        if self._early_market_cache_at == at:
            return self._early_market_cache
        self._early_market_cache_at = at
        self._early_market_cache = None
        benchmark = str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"])
        data = self._data.get(benchmark)
        if data is None:
            return None
        rows = [row for row in data["ticks"] if row["time"] <= at]
        if not rows:
            return None
        age = (at - rows[-1]["time"]).total_seconds()
        if not 0 <= age <= float(LONG_MARKET_REGIME_POLICY["maximum_staleness_seconds"]):
            return None
        benchmark_return = self._return_over(
            rows, at, int(LONG_MARKET_REGIME_POLICY["lookback_seconds"]),
        )
        total = float(data["cumulative_volume"])
        if benchmark_return is None or total <= 0:
            return None
        vwap = float(data["cumulative_pv"]) / total
        gap = float(rows[-1]["price"]) / vwap - 1
        regime = (
            "BULLISH" if gap > 0 and benchmark_return >= 0
            else "BEARISH" if gap < 0 and benchmark_return < 0
            else "NEUTRAL"
        )
        result = {
            "market_regime": regime, "benchmark_return_5m": benchmark_return,
            "benchmark_vwap_gap": gap, "benchmark_quote_age_seconds": age,
        }
        self._early_market_cache = result
        return result

    def _early_features(self, symbol: str, at: datetime) -> dict[str, Any] | None:
        data = self._data[symbol]
        rows = [row for row in data["ticks"] if row["time"] <= at]
        direction_start = at - timedelta(seconds=int(SPEC["direction_window_seconds"]))
        window = [row for row in rows if row["time"] >= direction_start]
        minimum = int(SPEC["minimum_ticks_in_direction_window"])
        if len(window) < minimum:
            return None
        tick_age = (at - window[-1]["time"]).total_seconds()
        if not 0 <= tick_age <= float(SPEC["maximum_tick_staleness_seconds"]):
            return None
        books = [row for row in data["books"] if row["time"] <= at]
        if not books:
            return None
        book = books[-1]
        book_age = (at - book["time"]).total_seconds()
        if not 0 <= book_age <= float(SPEC["maximum_book_staleness_seconds"]):
            return None
        bid, ask = float(book["buy_prices"][0]), float(book["sell_prices"][0])
        midpoint = (bid + ask) / 2
        spread_bps = (ask - bid) / midpoint * 10_000 if midpoint > 0 else math.inf
        if spread_bps < 0 or spread_bps > float(SPEC["maximum_spread_bps"]):
            return None
        buy_volume = sum(float(value) for value in book["buy_volumes"])
        sell_volume = sum(float(value) for value in book["sell_volumes"])
        if buy_volume + sell_volume <= 0:
            return None
        book_imbalance = (buy_volume - sell_volume) / (buy_volume + sell_volume)
        total = sum(float(row["volume"]) for row in window)
        if total <= 0:
            return None
        volume_delta = sum(self._signed_volume(row) for row in window) / total
        ref_start = at - timedelta(seconds=int(SPEC["large_trade_reference_seconds"]))
        reference = [row for row in rows if row["time"] >= ref_start]
        if len(reference) < minimum:
            return None
        ordered = sorted(float(row["volume"]) for row in reference)
        percentile_index = max(
            0, math.ceil(float(SPEC["large_trade_percentile"]) * len(ordered)) - 1,
        )
        large_threshold = ordered[percentile_index]
        large = [row for row in window if float(row["volume"]) >= large_threshold]
        large_total = sum(float(row["volume"]) for row in large)
        if large_total <= 0 or float(data["cumulative_volume"]) <= 0:
            return None
        large_delta = sum(self._signed_volume(row) for row in large) / large_total
        vwap = float(data["cumulative_pv"]) / float(data["cumulative_volume"])
        current = float(window[-1]["price"])
        vwap_gap = current / vwap - 1
        breakout_start = at - timedelta(seconds=int(SPEC["breakout_lookback_seconds"]))
        breakout_end = at - timedelta(seconds=int(SPEC["breakout_excludes_latest_seconds"]))
        prior = [row for row in rows if breakout_start <= row["time"] <= breakout_end]
        opening = data.get("opening_price")
        opening_time = data.get("opening_time")
        stock_return = self._return_over(
            rows, at, int(LONG_MARKET_REGIME_POLICY["lookback_seconds"]),
        )
        market = self._early_market(at)
        if (
            not prior or opening is None or opening_time is None
            or opening_time > _clock(at.date(), str(POLICY["entry_start"]))
            or stock_return is None or market is None
        ):
            return None
        relative_strength = stock_return - float(market["benchmark_return_5m"])
        minimum_rs = float(LONG_MARKET_REGIME_POLICY[
            f'{str(market["market_regime"]).lower()}_min_relative_strength'
        ])
        opening_extension = current / float(opening) - 1
        entry_price = ask + _tick_size(ask)
        checks = {
            "volume_delta": volume_delta >= float(SPEC["volume_delta_threshold"]),
            "large_trade_delta": large_delta >= float(SPEC["large_trade_delta_threshold"]),
            "above_vwap": vwap_gap > 0,
            "book_imbalance": book_imbalance >= float(SPEC["book_imbalance_threshold"]),
            "breakout": current > max(float(row["price"]) for row in prior),
        }
        market_passed = relative_strength >= minimum_rs
        anti_chase_passed = bool(
            opening_extension <= float(
                ANTI_CHASE_ENTRY_POLICY["maximum_directional_opening_extension"]
            ) + 1e-12
            and vwap_gap <= float(
                ANTI_CHASE_ENTRY_POLICY["maximum_directional_vwap_extension"]
            ) + 1e-12
            and entry_price <= float(POLICY["manual_review_price_cap_twd"]) + 1e-12
        )
        gate = evaluate_volume_gate(
            self._gate_data(symbol), at, "V2_RECENT_VOLUME_ACCELERATION", VOLUME_CONFIG,
        )
        return {
            "stock_id": symbol, "stock_name": self._names.get(symbol, symbol),
            "observed_at": at, "source_check_count": sum(checks.values()),
            "source_checks": checks, "volume_delta": volume_delta,
            "large_trade_delta": large_delta, "vwap_gap": vwap_gap,
            "book_imbalance": book_imbalance, "spread_bps": spread_bps,
            "relative_strength_5m": relative_strength,
            "minimum_relative_strength": minimum_rs,
            "market_regime": market["market_regime"],
            "market_passed": market_passed, "anti_chase_passed": anti_chase_passed,
            "entry_price": entry_price, "tick_age_seconds": tick_age,
            "book_age_seconds": book_age, "volume_gate_passed": gate["passed"],
            "volume_gate_reason": gate["reason"],
            "recent_to_previous_volume_ratio": gate.get("recent_to_previous_volume_ratio"),
        }

    def _early_prefilter_score(self, symbol: str, at: datetime) -> float | None:
        data = self._data[symbol]
        rows = [row for row in data["ticks"] if row["time"] <= at]
        market = self._early_market(at)
        if not rows or market is None or float(data["cumulative_volume"]) <= 0:
            return None
        age = (at - rows[-1]["time"]).total_seconds()
        if not 0 <= age <= float(SPEC["maximum_tick_staleness_seconds"]):
            return None
        opening = data.get("opening_price")
        opening_time = data.get("opening_time")
        stock_return = self._return_over(
            rows, at, int(LONG_MARKET_REGIME_POLICY["lookback_seconds"]),
        )
        if (
            opening is None or opening_time is None
            or opening_time > _clock(at.date(), str(POLICY["entry_start"]))
            or stock_return is None
        ):
            return None
        current = float(rows[-1]["price"])
        ask = float(rows[-1]["ask"])
        entry_price = ask + _tick_size(ask)
        vwap = float(data["cumulative_pv"]) / float(data["cumulative_volume"])
        vwap_gap = current / vwap - 1
        opening_extension = current / float(opening) - 1
        relative_strength = stock_return - float(market["benchmark_return_5m"])
        minimum_rs = float(LONG_MARKET_REGIME_POLICY[
            f'{str(market["market_regime"]).lower()}_min_relative_strength'
        ])
        if (
            vwap_gap <= 0
            or relative_strength < minimum_rs
            or opening_extension > float(
                ANTI_CHASE_ENTRY_POLICY["maximum_directional_opening_extension"]
            ) + 1e-12
            or vwap_gap > float(
                ANTI_CHASE_ENTRY_POLICY["maximum_directional_vwap_extension"]
            ) + 1e-12
            or entry_price > float(POLICY["manual_review_price_cap_twd"]) + 1e-12
        ):
            return None
        return relative_strength

    def _observe_early(self, symbol: str, at: datetime, *, source: str) -> None:
        if not (
            _clock(at.date(), str(POLICY["entry_start"]))
            <= at
            <= _clock(at.date(), str(POLICY["last_entry_time"]))
        ):
            return
        self._early_last_eval[symbol] = at
        features = self._early_features(symbol, at)
        if features is None or not features["market_passed"] or not features["anti_chase_passed"]:
            return
        count = int(features["source_check_count"])
        target = "NONE"
        if count >= int(POLICY["early_observation"]["prewatch_minimum_exact_base_checks"]):
            target = "PREWATCH"
        if count >= int(POLICY["early_observation"]["armed_minimum_exact_base_checks"]):
            target = "ARMED"
        if count == len(features["source_checks"]) and features["volume_gate_passed"]:
            target = "EARLY_ENTRY_READY_OBSERVED"
        timeline = self._early[symbol]
        current = str(timeline["state"])
        if EARLY_STATES.index(target) <= EARLY_STATES.index(current):
            return
        if timeline["first_prewatch_at"] is None:
            timeline["first_prewatch_at"] = at
        if EARLY_STATES.index(target) >= EARLY_STATES.index("ARMED") and timeline["first_armed_at"] is None:
            timeline["first_armed_at"] = at
        if target == "EARLY_ENTRY_READY_OBSERVED" and timeline["first_early_ready_at"] is None:
            timeline["first_early_ready_at"] = at
        timeline["state"] = target
        self._write(
            "EARLY_STATE_TRANSITION", previous_state=current, state=target,
            observation_source=source, **features,
        )

    def _observe_all_early(self, decision: datetime) -> None:
        ranked = []
        for symbol in self._early:
            score = self._early_prefilter_score(symbol, decision)
            if score is not None:
                ranked.append((float(score), symbol))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        limit = int(
            POLICY["early_observation"]["maximum_full_feature_symbols_per_interval"]
        )
        selected = [symbol for _score, symbol in ranked[:limit]]
        self._early_prefilter_runs += 1
        self._early_prefilter_max_selected = max(
            self._early_prefilter_max_selected, len(selected),
        )
        for symbol in selected:
            self._observe_early(symbol, decision, source="FULL_UNIVERSE_INTERVAL")

    def _official_timeline(self, symbol: str, at: datetime, field: str) -> dict[str, Any]:
        timeline = self._early.get(symbol)
        if timeline is None:
            return {}
        if timeline.get(field) is None:
            timeline[field] = at
        ready = timeline.get("first_early_ready_at")
        return {
            **timeline,
            "early_ready_lead_seconds": (
                (at - ready).total_seconds() if ready is not None else None
            ),
        }

    def _decide(self, decision: datetime) -> None:
        assert self._engine is not None
        final = _clock(decision.date(), str(POLICY["last_entry_time"]))
        if decision > final:
            self._check_continuation(decision)
            return
        self._decisions += 1
        self._observe_all_early(decision)
        self._engine.choose_entry(decision, allow_short=False)
        diagnostics = self._engine.last_entry_diagnostics or {}
        newly_passed: list[dict[str, Any]] = []
        for candidate in diagnostics.get("candidates") or []:
            symbol = str(candidate["stock_id"])
            if symbol in self._seen or str(candidate.get("gate_reason")) not in PASS_REASONS:
                continue
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
            if symbol not in self._reported:
                self._reported.add(symbol)
                row["early_timeline"] = self._official_timeline(
                    symbol, decision, "official_candidate_seen_at",
                )
                self._write("CANDIDATE_SEEN", **row)
            if not gate["passed"]:
                self._seen.add(symbol)
                continue
            combined = {**candidate, **row}
            rs = combined.get("relative_strength_5m")
            combined["rs_challenger_passed"] = bool(
                rs is not None and float(rs) >= float(
                    POLICY["challenger_relative_strength_5m_minimum"]
                )
            )
            if not combined["rs_challenger_passed"]:
                self._seen.add(symbol)
                self._write(
                    "CANDIDATE_REJECTED_RELATIVE_STRENGTH",
                    **combined,
                )
                self._deliver_candidate_status({
                    **combined,
                    "status": "NOT_BOUGHT",
                    "stage": "RELATIVE_STRENGTH",
                    "reason": "RELATIVE_STRENGTH_BELOW_THRESHOLD",
                    "required_relative_strength_5m": float(
                        POLICY["challenger_relative_strength_5m_minimum"]
                    ),
                    "actual_orders": 0,
                    "actual_fills": 0,
                    "broker_order_calls": 0,
                })
                continue
            newly_passed.append(combined)
        if self._selected is None and newly_passed:
            newly_passed.sort(
                key=lambda row: (-float(row.get("score") or 0), str(row["stock_id"])),
            )
            selected = newly_passed[0]
            self._seen.add(str(selected["stock_id"]))
            selected["continuation_due_at"] = decision + timedelta(
                seconds=int(POLICY["challenger_continuation_seconds"]),
            )
            self._selected = selected
            selected["early_timeline"] = self._official_timeline(
                str(selected["stock_id"]), decision, "official_selected_at",
            )
            self._selection_attempts.append(dict(selected))
            self._write("SHADOW_SIGNAL_SELECTED", **selected)
        elif self._selected is not None:
            for deferred in newly_passed:
                self._write(
                    "CANDIDATE_DEFERRED_ACTIVE_CONTINUATION",
                    active_stock_id=str(self._selected["stock_id"]),
                    **deferred,
                )
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
        continuation_record = {
            "stock_id": str(self._selected["stock_id"]),
            "stock_name": str(self._selected["stock_name"]),
            "signal_decision_time": self._selected["decision_time"],
            **self._continuation,
        }
        self._continuation_attempts.append({
            **dict(self._selected), **dict(self._continuation),
        })
        self._write("THREE_MINUTE_CONTINUATION", **continuation_record)
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
            if self._restoring_history:
                if not self._mature_candidate_emitted:
                    self._mature_candidate_emitted = True
                    self._restored_mature_candidate_count += 1
                    self._write(
                        "HISTORICAL_MATURE_CANDIDATE_CONSUMED",
                        stock_id=str(self._selected["stock_id"]),
                        review_decision_time=self._continuation["decision_time"],
                        reason="RESTART_REPLAY_NEVER_DELIVERS_EXPIRED_SIGNAL",
                    )
                self._manual_review_notification = {
                    "status": "SUPPRESSED_DURING_HISTORY_RESTORE",
                }
                return
            if (
                self._candidate_ready_callback is not None
                and not self._mature_candidate_emitted
            ):
                try:
                    candidate_payload = {
                        **payload,
                        "mode": "EXPANDED_V2_MATURE_CANDIDATE",
                        "source_observer_live_enabled": False,
                    }
                    result = self._candidate_ready_callback(
                        _jsonable(candidate_payload)
                    )
                    self._candidate_ready_delivery = {
                        "status": "ATTEMPTED", "delivery": _jsonable(result),
                    }
                except Exception as exc:
                    self._candidate_ready_delivery = {
                        "status": "FAILED", "error_type": type(exc).__name__,
                    }
                self._write(
                    "MATURE_CANDIDATE_CALLBACK",
                    **self._candidate_ready_delivery,
                )
                self._mature_candidate_emitted = True
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
            return

        released = dict(self._selected)
        if self._continuation.get("status") != "EVALUATED":
            release_reason = "CONTINUATION_DATA_INSUFFICIENT"
        elif not self._continuation.get("passed"):
            release_reason = "CONTINUATION_NOT_PROFITABLE_AFTER_COSTS"
        elif self._continuation.get("manual_entry_reference_price") is None:
            release_reason = "EXECUTABLE_ASK_UNAVAILABLE"
        elif float(self._continuation["manual_entry_reference_price"]) > float(
            POLICY["manual_review_price_cap_twd"]
        ):
            release_reason = "PRICE_CAP_EXCEEDED"
        elif int(self._continuation.get("manual_reference_quantity") or 0) <= 0:
            release_reason = "INSUFFICIENT_CAPITAL_FOR_BOARD_LOT"
        else:
            release_reason = "CONTINUATION_OR_EXECUTION_GATE_FAILED"
        self._deliver_candidate_status({
            **released,
            **self._continuation,
            "status": "NOT_BOUGHT",
            "stage": "THREE_MINUTE_CONTINUATION",
            "reason": release_reason,
            "signal_decision_time": released["decision_time"],
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_order_calls": 0,
        })
        self._write(
            "SHADOW_SELECTION_RELEASED",
            stock_id=str(released["stock_id"]),
            stock_name=str(released["stock_name"]),
            reason=release_reason,
        )
        self._selected = None
        self._continuation = None

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
            "selection_attempts": _jsonable(self._selection_attempts),
            "continuation_attempts": _jsonable(self._continuation_attempts),
            "manual_review_notification": _jsonable(self._manual_review_notification),
            "candidate_ready_delivery": _jsonable(self._candidate_ready_delivery),
            "candidate_status_deliveries": _jsonable(
                self._candidate_status_deliveries
            ),
            "history_restore": {
                "restored_mature_candidate_count": self._restored_mature_candidate_count,
            },
            "early_observation": {
                "policy": POLICY["early_observation"],
                "state_counts": dict(Counter(
                    str(row["state"]) for row in self._early.values()
                )),
                "timelines": _jsonable({
                    symbol: row for symbol, row in self._early.items()
                    if row["state"] != "NONE"
                }),
                "notification_enabled": False,
                "order_enabled": False,
                "prefilter_runs": self._early_prefilter_runs,
                "prefilter_max_selected": self._early_prefilter_max_selected,
            },
            "events_sha256": sha256_file(self._run_dir / EVENTS_NAME),
            "actual_orders": 0, "actual_fills": 0,
            "broker_order_calls": 0, "live_enabled": False,
        }
        summary["summary_hash"] = hashlib.sha256(canonical_bytes(summary)).hexdigest()
        summary_path = self._run_dir / SUMMARY_NAME
        summary_path.write_bytes(canonical_bytes(summary) + b"\n")
        return [self._run_dir / EVENTS_NAME, summary_path]
