"""Causal, memory-only signal scanner for the frozen cash/SHORT experiment.

There is no broker, entrypoint, persistence, order sizing, or fill simulation in
this module.  The original LONG engine retains its inherited affordability gate;
its computed quantity is deliberately not returned as a research fill/position.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
import hashlib
import heapq
import json
from typing import Mapping

from yuanta_live_runtime_v01.strategy import (
    ANTI_CHASE_ENTRY_POLICY, LiveDirectionEngine, TAIPEI,
)
from .execution import shift_price


def _stamp(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware event/decision timestamp required")
    return value.astimezone(TAIPEI)


def _clock(value: str) -> time:
    return time.fromisoformat(value)


def feed_engine(engine: LiveDirectionEngine, event: dict):
    """Feed one *already received* event; affects only the supplied engine.

    Callers own the receipt-time cursor. Unknown book exchange time is mapped to
    receipt time solely for this conditional research scenario, never certified
    as a known exchange timestamp. flags 2/3/10..15 do not enter signal flow.
    """
    received = _stamp(event["received_at"])
    exchange = _stamp(event["exchange_time"]) if event.get("exchange_time") is not None else received
    symbol, kind = str(event["symbol"]), event.get("kind")
    if event.get("out_of_order") or event.get("quote_valid") is False:
        return None
    if kind == "tick":
        if str(event.get("flag", "")) not in {"0", "1"} or int(event.get("serial", 0)) < 0:
            return None
        state = engine._states.get(symbol)
        if state is None:
            return None
        # An actual first 09:00 auction print contributes to session VWAP, not
        # continuous-session directional flow. Reference/status rows were
        # excluded above. Keep its volume out of the retained flow row only.
        first_open = state.session_date != exchange.date() and time(9) <= exchange.time() <= time(9, 0, 5)
        original_volume = engine._num(event["volume"])
        opening_auction = first_open and original_volume is not None and original_volume >= 0
        outcome = engine.ingest_tick(
            symbol, at=exchange, received_at=received, price=event["price"],
            volume=0.0 if opening_auction else event["volume"], bid=event["bid"],
            ask=event["ask"], flag=str(event["flag"]), serial=event.get("serial", 0),
        )
        if outcome.accepted:
            if opening_auction:
                state.cumulative_volume += original_volume
                state.cumulative_pv += float(event["price"]) * original_volume
                state.ticks[-1]["opening_auction_volume"] = original_volume
            engine.record_exit_quote(symbol, at=exchange, received_at=received,
                                     bid=event["bid"], ask=event["ask"])
        return outcome
    if kind == "book":
        bids, asks = event.get("bids", []), event.get("asks", [])
        outcome = engine.ingest_book_combined(
            symbol, at=exchange, buy_prices=[x[0] for x in bids], buy_volumes=[x[1] for x in bids],
            sell_prices=[x[0] for x in asks], sell_volumes=[x[1] for x in asks],
        )
        if outcome.accepted:
            engine._states[symbol].latest_book["available_at"] = received
            engine.record_exit_quote(symbol, at=exchange, received_at=received,
                                     bid=bids[0][0], ask=asks[0][0])
        return outcome
    return None


@dataclass
class _Setup:
    phase: str = "IDLE"
    armed_at: datetime | None = None
    retest_at: datetime | None = None
    reference: float | None = None
    low: float | None = None
    high: float | None = None
    observed_low: float | None = None
    previous_price: float | None = None


@dataclass
class _Observed:
    day: object | None = None
    first_tick: datetime | None = None
    warm_start: datetime | None = None
    last_tick_received: datetime | None = None
    last_book_received: datetime | None = None
    book_exchange_known: bool = False
    tick_exchange_known: bool = True
    opening_ok: bool = False
    opening_low: float | None = None
    opening_high: float | None = None
    opening_last: datetime | None = None
    opening_gap: bool = False
    gap_resets: int = 0
    halted: bool = False
    conditional_reasons: set = field(default_factory=set)
    setups: dict = field(default_factory=lambda: {"S1": _Setup(), "S2": _Setup()})


class SignalScanner:
    """Scan original LONG/M0 plus fixed S1/S2 from receipt-ordered events.

    Retest high/low is formed from the first 30 seconds after the touch, then
    frozen before break confirmation. This prevents a falling price from moving
    its own break threshold. No future bar, outcome, sizing, or fill is consumed.
    """
    def __init__(self, metadata, candidates, config):
        self.config = dict(config) if isinstance(config, Mapping) else dict(vars(config))
        self.metadata = {str(k): str(v.get("name", v.get("stock_name", k))) if isinstance(v, dict) else str(v)
                         for k, v in dict(metadata).items()}
        self.candidates = {str(k) for k in (candidates.keys() if isinstance(candidates, Mapping) else candidates)}
        self.candidates.discard("0050")
        if not self.candidates <= self.metadata.keys():
            raise ValueError("candidate missing metadata")
        self.engine = LiveDirectionEngine(
            self.metadata, capital_twd=int(self.config.get("capital_twd", 190000)),
            candidate_symbols=self.candidates, benchmark_symbol="0050" if "0050" in self.metadata else None,
        )
        self.observed = {symbol: _Observed() for symbol in self.metadata}
        self._queue, self._queue_sequence = [], 0
        self._last_decision = None
        self._last_processed_receipt = None
        self._seen_ids = set()
        self.ingest_counts, self.rejection_counts = Counter(), Counter()
        self.transitions = []
        # Refreshed at each valid decision; never mixed into executable signals.
        self.long_diagnostics = []

    def _cfg(self, key, default):
        return self.config.get(key, default)

    def ingest(self, event):
        """Queue source bytes' normalized event; future receipt stays unavailable."""
        item = dict(event)
        item["received_at"] = _stamp(item["received_at"])
        item["exchange_time"] = _stamp(item["exchange_time"]) if item.get("exchange_time") is not None else None
        if str(item.get("symbol", "")) not in self.observed:
            self.rejection_counts["UNKNOWN_SYMBOL"] += 1
            return False
        if self._last_processed_receipt is not None and item["received_at"] < self._last_processed_receipt:
            self.rejection_counts["LATE_RECEIPT_AFTER_CURSOR"] += 1
            return False
        self._queue_sequence += 1
        heapq.heappush(self._queue, (item["received_at"], int(item.get("sequence", self._queue_sequence)), self._queue_sequence, item))
        return True

    def _drain(self, now):
        if self._last_processed_receipt is not None and now < self._last_processed_receipt:
            raise ValueError("past query cannot rewind an already advanced receipt cursor")
        while self._queue and self._queue[0][0] <= now:
            received, _, _, event = heapq.heappop(self._queue)
            self._last_processed_receipt = received
            self._process(event)

    def _process(self, event):
        symbol, received = str(event["symbol"]), event["received_at"]
        state = self.observed[symbol]
        if state.day != received.date():
            self.observed[symbol] = state = _Observed(day=received.date())
            self.engine._previous.pop(symbol, None)
        kind = event.get("kind")
        if kind == "status":
            if event.get("status") in {"HALT", "SUSPENDED", "PAUSED"} or event.get("tradable") is False:
                state.halted = True
                state.warm_start = None
                state.setups = {"S1": _Setup(), "S2": _Setup()}
            elif event.get("status") in {"OPEN", "RESUMED", "NORMAL"}:
                state.halted = False
                state.warm_start = received
            self.ingest_counts["STATUS"] += 1
            return
        if kind == "tick" and (str(event.get("flag", "")) not in {"0", "1"} or int(event.get("serial", 0)) < 0):
            self.rejection_counts["NON_DIRECTIONAL_OR_REFERENCE_TICK"] += 1
            return
        if kind == "tick" and state.last_tick_received is not None:
            if (received - state.last_tick_received).total_seconds() > float(self._cfg("gap_reset_seconds", 60)):
                state.warm_start = received
                state.gap_resets += 1
                state.conditional_reasons.add("OBSERVED_TICK_GAP_RESET")
                state.setups = {"S1": _Setup(), "S2": _Setup()}
                self.engine._previous.pop(symbol, None)
                if state.last_tick_received.time() < time(9, 5):
                    state.opening_gap = True
        outcome = feed_engine(self.engine, event)
        if outcome is None or not outcome.accepted:
            self.rejection_counts[getattr(outcome, "reason", "EXCLUDED_EVENT")] += 1
            return
        self.ingest_counts[kind] += 1
        if event.get("exchange_time") is None:
            state.conditional_reasons.add("EXCHANGE_TIME_UNKNOWN_RECEIPT_ONLY")
        if not event.get("sequence_known", True):
            state.conditional_reasons.add("CROSS_STREAM_SEQUENCE_UNVERIFIED")
        if not bool(self._cfg("unit_verified", False)):
            state.conditional_reasons.add("VOLUME_UNIT_UNVERIFIED")
        if kind == "book":
            state.last_book_received = received
            state.book_exchange_known = event.get("exchange_time") is not None
            return
        exchange = event.get("exchange_time") or received
        if state.first_tick is None:
            state.first_tick = received
            state.warm_start = received
            state.opening_ok = time(9) <= exchange.time() <= _clock(self._cfg("opening_first_tick_latest", "09:00:05"))
        state.last_tick_received = received
        state.tick_exchange_known = event.get("exchange_time") is not None
        if time(9) <= exchange.time() < time(9, 5):
            price = float(event["price"])
            state.opening_low = price if state.opening_low is None else min(state.opening_low, price)
            state.opening_high = price if state.opening_high is None else max(state.opening_high, price)
            state.opening_last = exchange
        if self._warm_reason(symbol, received) is None:
            self._update_setups(symbol, received)

    def _warm_reason(self, symbol, now):
        s = self.observed[symbol]
        if s.day != now.date():
            return "NO_CURRENT_SESSION"
        if s.halted:
            return "TRADING_PAUSED"
        if not s.opening_ok:
            return "OPENING_FIRST_TICK_MISSING_OR_LATE"
        if s.warm_start is None or (now - s.warm_start).total_seconds() < float(self._cfg("warmup_seconds", 300)):
            return "CONTINUOUS_300S_WARMUP_INCOMPLETE"
        if s.last_tick_received is None or (now - s.last_tick_received).total_seconds() > float(self._cfg("tick_stale_seconds", 5)):
            return "TICK_STALE"
        return None

    def features(self, symbol, now):
        """Return causal diagnostics from events received by now; never emit."""
        now, symbol = _stamp(now), str(symbol)
        self._drain(now)
        if symbol not in self.observed:
            return {"symbol": symbol, "eligible": False, "warmup_reason": "UNKNOWN_SYMBOL"}
        s, raw = self.observed[symbol], self.engine._states[symbol]
        reason = self._warm_reason(symbol, now)
        rows = self.engine._causal_rows(raw, now)
        window = [x for x in rows if now - timedelta(seconds=60) <= x["time"] <= now and x["volume"] > 0]
        reference = [x for x in rows if now - timedelta(seconds=300) <= x["time"] <= now and x["volume"] > 0]
        unavailable = [x for x in raw.ticks if x["time"] > now or x["received_at"] > now]
        volume = raw.cumulative_volume - sum(x["volume"] + x.get("opening_auction_volume", 0) for x in unavailable)
        pv = raw.cumulative_pv - sum(x["price"] * (x["volume"] + x.get("opening_auction_volume", 0)) for x in unavailable)
        vwap = pv / volume if volume > 0 else None
        price = rows[-1]["price"] if rows else None
        book = raw.latest_book
        book_age = (now - book["time"]).total_seconds() if book else None
        tick_age = (now - rows[-1]["time"]).total_seconds() if rows else None
        spread, imbalance = None, None
        if book and book.get("available_at", book["time"]) <= now:
            mid = (book["best_bid"] + book["best_ask"]) / 2
            spread = (book["best_ask"] - book["best_bid"]) / mid * 10000 if mid > 0 else None
            depth = book["buy_volume"] + book["sell_volume"]
            imbalance = (book["buy_volume"] - book["sell_volume"]) / depth if depth > 0 else None
        minimum = int(self._cfg("minimum_ticks_60s", 10))
        if reason is None and len(window) < minimum:
            reason = "INSUFFICIENT_60S_DIRECTION_TICKS"
        if reason is None and (tick_age is None or tick_age < -0.5 or tick_age > float(self._cfg("tick_stale_seconds", 5))):
            reason = "EXCHANGE_TICK_STALE"
        if reason is None and (book_age is None or book_age < -0.5 or book_age > float(self._cfg("book_stale_seconds", 10))):
            reason = "BOOK_STALE_OR_MISSING"
        if reason is None and (spread is None or spread < 0 or spread > float(self._cfg("maximum_spread_bps", 40))):
            reason = "SPREAD_TOO_WIDE_OR_MISSING"
        if reason is None and (vwap is None or imbalance is None or len(reference) < minimum):
            reason = "VWAP_BOOK_OR_REFERENCE_UNAVAILABLE"
        total = sum(x["volume"] for x in window)
        flow = sum(self.engine._signed_volume(x) for x in window) / total if total > 0 else None
        threshold = self.engine._percentile90([x["volume"] for x in reference]) if reference else None
        large = [x for x in window if threshold is not None and x["volume"] >= threshold]
        large_total = sum(x["volume"] for x in large)
        large_flow = sum(self.engine._signed_volume(x) for x in large) / large_total if large_total > 0 else None
        market = None
        if "0050" in self.observed and self._warm_reason("0050", now) is None:
            market = self.engine._market_context(now)
        benchmark_return = market.get("benchmark_return_5m") if market else None
        stock_return = self.engine._return_over(rows, now, 300)
        market_pass = (market["benchmark_vwap_gap"] < 0 and benchmark_return <= 0) if market else None
        opening_complete = bool(s.opening_ok and not s.opening_gap and s.opening_last is not None
                                and now.time() >= time(9, 5))
        conditional = sorted(s.conditional_reasons)
        return {
            "symbol": symbol, "time": now.isoformat(), "price": price, "vwap": vwap,
            "vwap_gap": price / vwap - 1 if price is not None and vwap else None,
            "volume_delta": flow, "large_trade_delta": large_flow,
            "large_trade_threshold_p90": threshold, "book_imbalance": imbalance,
            "bookimbalance": imbalance, "spread_bps": spread,
            "tick_count_60s": len(window), "tick_age_seconds": tick_age,
            "book_age_seconds": book_age, "book_received_at": s.last_book_received.isoformat() if s.last_book_received else None,
            "last_tick_received_at": s.last_tick_received.isoformat() if s.last_tick_received else None,
            "market_filter_pass": market_pass, "benchmark_return_5m": benchmark_return,
            "benchmark_vwap_gap": market.get("benchmark_vwap_gap") if market else None,
            "relative_strength_5m": stock_return - benchmark_return if stock_return is not None and benchmark_return is not None else None,
            "stock_return_5m": stock_return, "market_regime": market.get("market_regime") if market else "UNAVAILABLE",
            "opening_low": s.opening_low, "opening_high": s.opening_high,
            "opening_range_complete": opening_complete, "opening_first_tick_observed": s.opening_ok,
            "warmup_start": s.warm_start.isoformat() if s.warm_start else None,
            "gap_resets": s.gap_resets, "eligible": reason is None, "warmup_reason": reason,
            "entry_timing_regime": "EXCHANGE_AND_RECEIPT" if s.book_exchange_known and s.tick_exchange_known else "CONDITIONAL_RECEIPT_ONLY",
            "conditional": bool(conditional), "conditional_reasons": conditional,
            "source_engine": "yuanta_live_runtime_v01.strategy.LiveDirectionEngine",
            "warmup_difference_from_production": "REQUIRES_300S_CONTINUOUS_RECEIPTS_AND_OBSERVED_0900_OPEN",
        }

    def _set_phase(self, symbol, model, setup, phase, at, reason):
        self.transitions.append({"symbol": symbol, "entry_model": model, "time": at.isoformat(),
                                 "from": setup.phase, "to": phase, "reason": reason})
        setup.phase = phase

    def _within_retest_band(self, price, reference):
        """Reference is analytical (VWAP need not lie on the legal stock grid).

        Anchor tick distances at the actual legal observed price; a reference
        within its two legal steps is in-band. The percentage tolerance remains
        an analytical distance, never an executable/order price.
        """
        ticks = int(self._cfg("retest_tolerance_ticks", 2))
        try:
            lower, upper = shift_price(price, ticks, -1), shift_price(price, ticks, 1)
        except ValueError:
            return False
        return (lower - 1e-10 <= reference <= upper + 1e-10
                or abs(price - reference) <= reference * float(self._cfg("retest_tolerance_fraction", 0.0015)) + 1e-10)

    def _update_setups(self, symbol, now):
        if symbol not in self.candidates:
            return
        raw = self.engine._states[symbol]
        if raw.cumulative_volume <= 0 or not raw.ticks:
            return
        price, vwap = raw.ticks[-1]["price"], raw.cumulative_pv / raw.cumulative_volume
        observed = self.observed[symbol]
        try:
            shift_price(price, 0, 1)
        except ValueError:
            observed.conditional_reasons.add("ILLEGAL_STOCK_SIGNAL_PRICE_GRID")
            return
        for model, setup in list(observed.setups.items()):
            if setup.armed_at and (now - setup.armed_at).total_seconds() > float(self._cfg("setup_ttl_seconds", 300)):
                self._set_phase(symbol, model, setup, "IDLE", now, "SETUP_TTL_EXPIRED")
                observed.setups[model] = setup = _Setup()
            reference = vwap if model == "S1" else observed.opening_low
            if reference is None:
                continue
            if model == "S1" and setup.phase != "IDLE" and price > vwap * (1 + float(self._cfg("retest_tolerance_fraction", 0.0015))):
                self._set_phase(symbol, model, setup, "IDLE", now, "VWAP_RECLAIM_INVALIDATED")
                observed.setups[model] = _Setup()
                continue
            if model == "S2" and setup.phase != "IDLE":
                try:
                    upper = max(shift_price(reference, int(self._cfg("retest_tolerance_ticks", 2)), 1),
                                reference * (1 + float(self._cfg("retest_tolerance_fraction", 0.0015))))
                except ValueError:
                    observed.conditional_reasons.add("ILLEGAL_OPENING_RANGE_PRICE_GRID")
                    continue
                if price >= upper - 1e-10:
                    self._set_phase(symbol, model, setup, "IDLE", now, "OPENING_LOW_RECLAIM_INVALIDATED")
                    observed.setups[model] = _Setup()
                    continue
            if setup.phase == "IDLE":
                try:
                    opening_break = price <= shift_price(reference, 1, -1) + 1e-10 if model == "S2" else False
                except ValueError:
                    observed.conditional_reasons.add("ILLEGAL_OPENING_RANGE_PRICE_GRID")
                    continue
                valid = price < vwap if model == "S1" else (
                    observed.opening_ok and not observed.opening_gap and observed.opening_last is not None
                    and now.time() >= time(9, 5)
                    and opening_break)
                if valid:
                    setup.armed_at = now
                    setup.observed_low = setup.previous_price = price
                    self._set_phase(symbol, model, setup, "ARMED", now, "BELOW_VWAP" if model == "S1" else "OPENING_LOW_BROKEN")
                continue
            if setup.phase == "ARMED":
                setup.observed_low = min(setup.observed_low, price)
                bounced = (price >= shift_price(setup.observed_low, 1, 1) - 1e-10
                           and price > setup.previous_price + 1e-10)
                if (bounced and self._within_retest_band(price, reference)
                        and (model != "S1" or price <= vwap * (1 + float(self._cfg("retest_tolerance_fraction", 0.0015))))):
                    setup.retest_at, setup.reference = now, reference
                    setup.low = setup.high = price
                    self._set_phase(symbol, model, setup, "RETEST", now, "REFERENCE_RETEST_OBSERVED")
                setup.previous_price = price
                continue
            if setup.phase == "RETEST":
                elapsed = (now - setup.retest_at).total_seconds()
                if elapsed > float(self._cfg("confirmation_max_seconds", 120)):
                    self._set_phase(symbol, model, setup, "IDLE", now, "RETEST_CONFIRMATION_EXPIRED")
                    observed.setups[model] = _Setup()
                elif elapsed < float(self._cfg("confirmation_min_seconds", 30)):
                    setup.low, setup.high = min(setup.low, price), max(setup.high, price)

    def _emit(self, model, symbol, now, features, score, setup=None):
        identity = {"model": model, "symbol": symbol, "time": now.isoformat(),
                    "retest": setup.retest_at.isoformat() if setup and setup.retest_at else None}
        signal_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
        if signal_id in self._seen_ids:
            return None
        self._seen_ids.add(signal_id)
        own = dict(features)
        high = float(setup.high) if setup and setup.high is not None else None
        distance = high - features["price"] if high is not None else None
        if setup:
            own.update(retest_at=setup.retest_at.isoformat(), retest_reference=setup.reference,
                       retest_low=setup.low, retest_high=setup.high,
                       armed_observed_low=setup.observed_low,
                       rebound_definition="AT_LEAST_ONE_LEGAL_TICK_FROM_OBSERVED_LOW_AND_ABOVE_PREVIOUS_RECEIVED_PRICE",
                       retest_range_definition="FIRST_30_SECONDS_THEN_FROZEN",
                       book_imbalance_is_auxiliary=True,
                       entry_distance_fraction=(setup.reference - features["price"]) / setup.reference,
                       structure_stop_fraction=distance / features["price"])
        return {"signal_id": signal_id, "time": now.isoformat(), "symbol": symbol,
                "name": self.metadata[symbol], "side": "LONG" if model == "LONG" else "SHORT",
                "entry_model": model, "score": float(score), "features": own,
                "structure_high": high, "stop_distance": distance}

    def decide(self, now):
        now = _stamp(now)
        if self._last_decision is not None and now <= self._last_decision:
            return []
        self._drain(now)
        if (now.microsecond or now.second % int(self._cfg("decision_interval_seconds", 30)) != 0
                or not _clock(self._cfg("entry_start", "09:05")) <= now.time() <= _clock(self._cfg("last_entry", "13:10"))):
            return []
        self._last_decision = now
        self.long_diagnostics = []
        snapshots = {symbol: self.features(symbol, now) for symbol in sorted(self.candidates)}
        eligible = {symbol for symbol, feature in snapshots.items() if feature["eligible"]}
        output = []
        self.engine._candidate_symbols = eligible
        if "0050" in self.observed and self._warm_reason("0050", now) is None:
            signal = self.engine.choose_entry(now, allow_short=False)
            selected_output = None
            if signal is not None:
                selected_output = self._emit("LONG", signal.stock_id, now, snapshots[signal.stock_id], signal.score)
                if selected_output: output.append(selected_output)
            for candidate in self.engine.last_entry_diagnostics.get("candidates", []):
                # The original engine rewrites gate_reason for confirmation or
                # anti-chase failures; do not infer approval from raw features.
                if (candidate.get("side") != "LONG"
                        or candidate.get("gate_reason") not in {"BASE_SIGNAL_CONFIRMED", "MARKET_GATE_PASSED"}
                        or int(candidate.get("streak", 0)) < int(candidate.get("required_confirmations", 1))):
                    continue
                symbol = candidate["stock_id"]
                selected = signal is not None and symbol == signal.stock_id
                diagnostic = selected_output if selected else self._emit("LONG", symbol, now, snapshots[symbol], candidate["score"])
                if diagnostic is not None:
                    self.long_diagnostics.append(dict(diagnostic, selection_status="SELECTED_TOP_RANK" if selected else "DIAGNOSTIC_NOT_TOP_RANK"))
        for symbol in sorted(eligible):
            feature = snapshots[symbol]
            base = self.engine._signal_for(symbol, now)
            if (base is not None and base["side"] == "SHORT"
                    and base["directional_opening_extension"] <= float(ANTI_CHASE_ENTRY_POLICY["maximum_directional_opening_extension"]) + 1e-12
                    and base["directional_vwap_extension"] <= float(ANTI_CHASE_ENTRY_POLICY["maximum_directional_vwap_extension"]) + 1e-12):
                emitted = self._emit("M0", symbol, now, feature, base["score"])
                if emitted: output.append(emitted)
            for model, setup in self.observed[symbol].setups.items():
                if setup.phase != "RETEST":
                    continue
                elapsed = (now - setup.retest_at).total_seconds()
                price = feature["price"]
                if not float(self._cfg("confirmation_min_seconds", 30)) <= elapsed <= float(self._cfg("confirmation_max_seconds", 120)):
                    continue
                try:
                    break_threshold = shift_price(setup.low, 1, -1)
                except ValueError:
                    continue
                if not (price <= break_threshold + 1e-10 and price < feature["vwap"]):
                    continue
                if model == "S2" and not (feature["opening_range_complete"] and price < feature["opening_low"]):
                    continue
                if feature["volume_delta"] is None or feature["large_trade_delta"] is None:
                    continue
                if feature["volume_delta"] > float(self._cfg("flow_threshold", -0.35)) or feature["large_trade_delta"] > float(self._cfg("large_flow_threshold", -0.25)):
                    continue
                distance = setup.high - price
                if ((setup.reference - price) / setup.reference > float(self._cfg("maximum_entry_distance_fraction", 0.005))
                        or distance <= 0 or distance / price > float(self._cfg("maximum_structure_stop_fraction", 0.01))):
                    continue
                score = 0.35 * abs(feature["volume_delta"]) + 0.25 * abs(feature["large_trade_delta"]) + 0.20 * min(abs(feature["vwap_gap"]) / 0.005, 1) + 0.20 * abs(feature["book_imbalance"])
                emitted = self._emit(model, symbol, now, feature, score, setup)
                if emitted:
                    output.append(emitted)
                    self._set_phase(symbol, model, setup, "IDLE", now, "SIGNAL_EMITTED")
                    self.observed[symbol].setups[model] = _Setup()
        return sorted(output, key=lambda row: (-row["score"], row["side"] != "LONG", row["symbol"], row["entry_model"]))
