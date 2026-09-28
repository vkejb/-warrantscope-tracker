"""Realtime form of the frozen Top30 direction-following entry rule.

The thresholds come from ``yuanta_intraday_shadow_v01.direction_follow_backtest.SPEC``.
Entry selection remains frozen. Runtime exits use an explicit, independently logged
policy; broker submission remains in ``main.py`` and still passes through the
reviewed broker gate.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from functools import wraps
import math
import threading
from typing import Deque, Mapping
from zoneinfo import ZoneInfo

from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC
from yuanta_intraday_shadow_v01.exploratory_backtest import _tick_size

TAIPEI = ZoneInfo("Asia/Taipei")

LIVE_EXIT_POLICY = {
    "policy_id": "HARD_3500_PLUS_MFE_V1_NO_LOSS_RECOVERY",
    "stop_loss_net_twd": 3500.0,
    "one_r_definition": "entry_to_net_3500_stop_price",
    "mfe_variant": "MFE_V1",
    "retain_1_5": 0.60,
    "retain_2_0": 0.70,
    "retain_3_0": 0.75,
}


def _candidate_locked_r(mfe_r: float) -> float | None:
    """Return the fixed MFE_V1 floor; this policy is not runtime-optimized."""
    if not math.isfinite(mfe_r) or mfe_r < 1.0:
        return None
    if mfe_r < 1.5:
        return 0.0
    if mfe_r < 2.0:
        return float(LIVE_EXIT_POLICY["retain_1_5"]) * mfe_r
    if mfe_r < 3.0:
        return float(LIVE_EXIT_POLICY["retain_2_0"]) * mfe_r
    return float(LIVE_EXIT_POLICY["retain_3_0"]) * mfe_r


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


@dataclass(frozen=True, slots=True)
class LiveSignal:
    stock_id: str
    stock_name: str
    side: str
    decision_time: datetime
    score: float
    volume_delta: float
    large_trade_delta: float
    vwap_gap: float
    book_imbalance: float
    spread_bps: float
    entry_price: float
    quantity: int


@dataclass(slots=True)
class ManagedPosition:
    stock_id: str
    stock_name: str
    side: str  # LONG or SHORT
    quantity: int
    entry_price: float
    entry_order_id: str
    entry_time: datetime
    peak_return: float = 0.0
    worst_return: float = 0.0
    reversal_streak: int = 0
    last_reversal_decision: datetime | None = None
    exit_submitted: bool = False
    initial_stop_price: float | None = None
    mfe_basis_entry_price: float | None = None
    mfe_basis_quantity: int = 0
    mfe_price: float | None = None
    mfe_pnl: float | None = None
    mfe_r: float = 0.0
    mfe_time: datetime | None = None
    locked_profit_r: float = 0.0
    locked_profit_price: float | None = None
    locked_profit_pnl: float | None = None
    mfe_protection_armed: bool = False
    mfe_activation_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class ExitDecision:
    reason: str
    price: float
    projected_net_pnl: float
    current_return: float


@dataclass(frozen=True, slots=True)
class SafeExitQuote:
    price: float
    received_at: datetime
    age_seconds: float
    best_bid: float
    best_ask: float


@dataclass(slots=True)
class _StockState:
    stock_name: str
    ticks: Deque[dict]
    cumulative_volume: float = 0.0
    cumulative_pv: float = 0.0
    latest_book: dict | None = None
    buy_side: dict | None = None
    sell_side: dict | None = None
    last_serial: int = 0


class LiveDirectionEngine:
    """Memory-bounded realtime equivalent of the causal replay signal rule."""

    def __init__(self, metadata: Mapping[str, str], *, capital_twd: int = 190_000):
        self._lock = threading.RLock()
        self.capital_twd = int(capital_twd)
        if self.capital_twd <= 0:
            raise ValueError("capital_twd must be positive")
        self._states = {
            str(symbol): _StockState(str(name), deque()) for symbol, name in metadata.items()
        }
        self._previous: dict[str, tuple[str, int, datetime]] = {}
        self.last_decision: datetime | None = None

    @staticmethod
    def _num(value: object) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @_locked
    def record_tick(
        self,
        symbol: str,
        *,
        at: datetime,
        price: object,
        volume: object,
        bid: object,
        ask: object,
        flag: object = "",
        serial: object = 0,
        received_at: datetime | None = None,
    ) -> bool:
        state = self._states.get(str(symbol))
        if state is None:
            return False
        px, vol, bp, ap = (self._num(value) for value in (price, volume, bid, ask))
        if px is None or vol is None or bp is None or ap is None:
            return False
        if min(px, bp, ap) <= 0 or vol < 0 or ap < bp:
            return False
        stamp = at.astimezone(TAIPEI)
        received = stamp if received_at is None else received_at.astimezone(TAIPEI)
        age = (received - stamp).total_seconds()
        if age < 0 or age > float(SPEC["maximum_tick_staleness_seconds"]):
            return False
        sequence = int(self._num(serial) or 0)
        if state.ticks:
            previous = state.ticks[-1]
            if stamp < previous["time"]:
                return False
            if stamp.date() == previous["time"].date():
                if sequence > 0 and sequence <= state.last_serial:
                    return False
                if sequence == 0 and (stamp, px, vol, bp, ap) == (
                    previous["time"], previous["price"], previous["volume"],
                    previous["bid"], previous["ask"],
                ):
                    return False
            else:
                state.ticks.clear()
                state.cumulative_volume = state.cumulative_pv = 0.0
                state.latest_book = state.buy_side = state.sell_side = None
                state.last_serial = 0
        state.last_serial = max(state.last_serial, sequence)
        row = {
            "time": stamp,
            "received_at": received,
            "price": px,
            "volume": vol,
            "bid": bp,
            "ask": ap,
            "flag": str(flag),
            "serial": int(self._num(serial) or 0),
        }
        state.ticks.append(row)
        state.cumulative_volume += vol
        state.cumulative_pv += px * vol
        cutoff = stamp - timedelta(seconds=max(360, int(SPEC["large_trade_reference_seconds"]) + 30))
        while state.ticks and state.ticks[0]["time"] < cutoff:
            state.ticks.popleft()
        return True

    @_locked
    def record_book_combined(
        self,
        symbol: str,
        *,
        at: datetime,
        buy_prices: list[object],
        buy_volumes: list[object],
        sell_prices: list[object],
        sell_volumes: list[object],
    ) -> None:
        state = self._states.get(str(symbol))
        if state is None:
            return
        if state.latest_book and at < state.latest_book["time"]:
            return
        buys = [self._num(v) for v in buy_volumes]
        sells = [self._num(v) for v in sell_volumes]
        buy_px = [self._num(v) for v in buy_prices]
        sell_px = [self._num(v) for v in sell_prices]
        if not buys or not sells or not buy_px or not sell_px:
            return
        if any(v is None for v in buys + sells + buy_px + sell_px):
            return
        assert all(v is not None for v in buys + sells + buy_px + sell_px)
        if (float(buy_px[0]) <= 0 or float(sell_px[0]) < float(buy_px[0])
                or any(v < 0 for v in buys + sells)):
            return
        state.latest_book = {
            "time": at.astimezone(TAIPEI),
            "buy_volume": float(sum(buys)),
            "sell_volume": float(sum(sells)),
            "best_bid": float(buy_px[0]),
            "best_ask": float(sell_px[0]),
        }

    @_locked
    def record_book_side(
        self,
        symbol: str,
        *,
        at: datetime,
        side: str,
        prices: list[object],
        volumes: list[object],
    ) -> None:
        state = self._states.get(str(symbol))
        if state is None:
            return
        px = [self._num(v) for v in prices]
        vol = [self._num(v) for v in volumes]
        if not px or not vol or any(v is None for v in px + vol):
            return
        assert all(v is not None for v in px + vol)
        item = {"time": at.astimezone(TAIPEI), "best": float(px[0]), "volume": float(sum(vol))}
        prior_side = state.buy_side if side == "BUY" else state.sell_side
        if item["best"] <= 0 or any(v < 0 for v in vol) or (prior_side and at < prior_side["time"]):
            return
        if side == "BUY":
            state.buy_side = item
        elif side == "SELL":
            state.sell_side = item
        else:
            return
        if state.buy_side is None or state.sell_side is None:
            return
        age = abs((state.buy_side["time"] - state.sell_side["time"]).total_seconds())
        if age > float(SPEC["maximum_book_staleness_seconds"]):
            return
        state.latest_book = {
            "time": min(state.buy_side["time"], state.sell_side["time"]),
            "available_at": max(state.buy_side["time"], state.sell_side["time"]),
            "buy_volume": state.buy_side["volume"],
            "sell_volume": state.sell_side["volume"],
            "best_bid": state.buy_side["best"],
            "best_ask": state.sell_side["best"],
        }

    @staticmethod
    def _signed_volume(row: dict) -> float:
        if row["flag"] == "1":
            return row["volume"]
        if row["flag"] == "0":
            return -row["volume"]
        midpoint = (row["bid"] + row["ask"]) / 2
        return row["volume"] if row["price"] >= midpoint else -row["volume"]

    @staticmethod
    def _percentile90(values: list[float]) -> float:
        ordered = sorted(values)
        index = max(0, math.ceil(float(SPEC["large_trade_percentile"]) * len(ordered)) - 1)
        return ordered[index]

    @_locked
    def _signal_for(self, symbol: str, decision: datetime) -> dict | None:
        state = self._states[symbol]
        rows = list(state.ticks)
        window_start = decision - timedelta(seconds=int(SPEC["direction_window_seconds"]))
        window = [row for row in rows if window_start <= row["time"] <= decision and row["received_at"] <= decision]
        if len(window) < int(SPEC["minimum_ticks_in_direction_window"]):
            return None
        if (decision - window[-1]["time"]).total_seconds() > float(SPEC["maximum_tick_staleness_seconds"]):
            return None
        book = state.latest_book
        if (book is None or book.get("available_at", book["time"]) > decision
                or (decision - book["time"]).total_seconds() > float(SPEC["maximum_book_staleness_seconds"])):
            return None
        spread_mid = (book["best_bid"] + book["best_ask"]) / 2
        if spread_mid <= 0:
            return None
        spread_bps = (book["best_ask"] - book["best_bid"]) / spread_mid * 10_000
        if spread_bps < 0 or spread_bps > float(SPEC["maximum_spread_bps"]):
            return None
        book_total = book["buy_volume"] + book["sell_volume"]
        if book_total <= 0:
            return None
        book_imbalance = (book["buy_volume"] - book["sell_volume"]) / book_total
        total_volume = sum(row["volume"] for row in window)
        if total_volume <= 0:
            return None
        volume_delta = sum(self._signed_volume(row) for row in window) / total_volume

        ref_start = decision - timedelta(seconds=int(SPEC["large_trade_reference_seconds"]))
        reference = [row for row in rows if ref_start <= row["time"] <= decision and row["received_at"] <= decision]
        if len(reference) < int(SPEC["minimum_ticks_in_direction_window"]):
            return None
        large_threshold = self._percentile90([row["volume"] for row in reference])
        large = [row for row in window if row["volume"] >= large_threshold]
        large_total = sum(row["volume"] for row in large)
        if large_total <= 0:
            return None
        large_delta = sum(self._signed_volume(row) for row in large) / large_total

        if state.cumulative_volume <= 0:
            return None
        unavailable = [row for row in rows if row["time"] > decision or row["received_at"] > decision]
        known_volume = state.cumulative_volume - sum(row["volume"] for row in unavailable)
        if known_volume <= 0:
            return None
        vwap = (state.cumulative_pv - sum(row["price"] * row["volume"] for row in unavailable)) / known_volume
        current = window[-1]["price"]
        vwap_gap = current / vwap - 1
        breakout_start = decision - timedelta(seconds=int(SPEC["breakout_lookback_seconds"]))
        breakout_end = decision - timedelta(seconds=int(SPEC["breakout_excludes_latest_seconds"]))
        prior = [row for row in rows if breakout_start <= row["time"] <= breakout_end and row["received_at"] <= decision]
        if not prior:
            return None
        long_ok = (
            volume_delta >= float(SPEC["volume_delta_threshold"])
            and large_delta >= float(SPEC["large_trade_delta_threshold"])
            and vwap_gap > 0
            and book_imbalance >= float(SPEC["book_imbalance_threshold"])
            and current > max(row["price"] for row in prior)
        )
        short_ok = (
            volume_delta <= -float(SPEC["volume_delta_threshold"])
            and large_delta <= -float(SPEC["large_trade_delta_threshold"])
            and vwap_gap < 0
            and book_imbalance <= -float(SPEC["book_imbalance_threshold"])
            and current < min(row["price"] for row in prior)
        )
        if not (long_ok or short_ok):
            return None
        score = (
            0.35 * abs(volume_delta)
            + 0.25 * abs(large_delta)
            + 0.20 * min(abs(vwap_gap) / 0.005, 1.0)
            + 0.20 * abs(book_imbalance)
        )
        side = "LONG" if long_ok else "SHORT"
        entry_price = (
            window[-1]["ask"] + _tick_size(window[-1]["ask"])
            if side == "LONG"
            else max(_tick_size(window[-1]["bid"]), window[-1]["bid"] - _tick_size(window[-1]["bid"]))
        )
        if entry_price * 1000 > self.capital_twd:
            return None
        lots = math.floor(self.capital_twd / (entry_price * 1000))
        quantity = lots * 1000
        if quantity <= 0:
            return None
        return {
            "stock_id": symbol,
            "stock_name": state.stock_name,
            "side": side,
            "decision_time": decision,
            "score": score,
            "volume_delta": volume_delta,
            "large_trade_delta": large_delta,
            "vwap_gap": vwap_gap,
            "book_imbalance": book_imbalance,
            "spread_bps": spread_bps,
            "entry_price": entry_price,
            "quantity": quantity,
        }

    @staticmethod
    def _clock(value: str) -> time:
        hour, minute = map(int, value.split(":"))
        return time(hour, minute)

    @_locked
    def choose_entry(self, decision: datetime, *, allow_short: bool) -> LiveSignal | None:
        decision = decision.astimezone(TAIPEI).replace(microsecond=0)
        if self.last_decision is not None and decision <= self.last_decision:
            return None
        self.last_decision = decision
        if not self._clock(SPEC["entry_start"]) <= decision.time() <= self._clock(SPEC["last_entry_time"]):
            return None
        confirmed: list[dict] = []
        interval = timedelta(seconds=int(SPEC["decision_interval_seconds"]))
        for symbol in sorted(self._states):
            signal = self._signal_for(symbol, decision)
            prior = self._previous.get(symbol)
            if signal is not None:
                streak = prior[1] + 1 if prior and prior[0] == signal["side"] and decision - prior[2] == interval else 1
            else:
                streak = 0
            self._previous[symbol] = (signal["side"], streak, decision) if signal else ("", 0, decision)
            if signal is None or streak < int(SPEC["entry_confirmations"]):
                continue
            if signal["side"] == "SHORT" and not allow_short:
                continue
            confirmed.append(signal)
        if not confirmed:
            return None
        selected = max(confirmed, key=lambda item: (item["score"], item["entry_price"] * item["quantity"]))
        return LiveSignal(**selected)

    @_locked
    def opposite_signal(self, position: ManagedPosition, decision: datetime) -> bool:
        decision = decision.astimezone(TAIPEI).replace(microsecond=0)
        signal = self._signal_for(position.stock_id, decision)
        opposite = signal is not None and signal["side"] != position.side
        interval = timedelta(seconds=int(SPEC["decision_interval_seconds"]))
        if opposite:
            if position.last_reversal_decision is not None and decision - position.last_reversal_decision == interval:
                position.reversal_streak += 1
            else:
                position.reversal_streak = 1
            position.last_reversal_decision = decision
        else:
            position.reversal_streak = 0
            position.last_reversal_decision = None
        return position.reversal_streak >= int(SPEC["reversal_confirmations"])

    @staticmethod
    def _projected_net(side: str, entry_price: float, exit_price: float, quantity: int) -> float:
        buy_notional = entry_price * quantity if side == "LONG" else exit_price * quantity
        sell_notional = exit_price * quantity if side == "LONG" else entry_price * quantity
        commission_rate = float(SPEC["commission_rate_each_side"])
        minimum = int(SPEC["minimum_commission_twd"])
        buy_fee = max(minimum, math.ceil(buy_notional * commission_rate))
        sell_fee = max(minimum, math.ceil(sell_notional * commission_rate))
        sell_tax = math.ceil(sell_notional * float(SPEC["day_trade_sell_tax_rate"]))
        gross = sell_notional - buy_notional
        return round(gross - buy_fee - sell_fee - sell_tax, 2)

    def projected_net(self, position: ManagedPosition, exit_price: float) -> float:
        return self._projected_net(
            position.side, position.entry_price, exit_price, position.quantity
        )

    def _derive_initial_stop_price(self, position: ManagedPosition) -> float:
        """Invert the net-TWD disaster stop into a positive per-share R."""
        target = -float(LIVE_EXIT_POLICY["stop_loss_net_twd"])
        entry = float(position.entry_price)
        quantity = int(position.quantity)
        if position.side not in {"LONG", "SHORT"} or entry <= 0 or quantity <= 0:
            raise ValueError("position cannot define a valid initial risk")
        if self._projected_net(position.side, entry, entry, quantity) <= target:
            raise ValueError("entry costs consume the complete initial risk")
        if position.side == "LONG":
            adverse, safe = max(entry / 1_000_000, 0.000001), entry
            if self._projected_net(position.side, entry, adverse, quantity) > target:
                raise ValueError("unable to derive long initial stop")
            for _ in range(100):
                middle = (adverse + safe) / 2
                if self._projected_net(position.side, entry, middle, quantity) <= target:
                    adverse = middle
                else:
                    safe = middle
        else:
            safe, adverse = entry, entry * 2
            for _ in range(30):
                if self._projected_net(position.side, entry, adverse, quantity) <= target:
                    break
                adverse *= 2
            else:
                raise ValueError("unable to derive short initial stop")
            for _ in range(100):
                middle = (safe + adverse) / 2
                if self._projected_net(position.side, entry, middle, quantity) <= target:
                    adverse = middle
                else:
                    safe = middle
        stop = (adverse + safe) / 2
        risk = entry - stop if position.side == "LONG" else stop - entry
        if not math.isfinite(stop) or stop <= 0 or risk <= 0:
            raise ValueError("derived initial stop is invalid")
        return stop

    @staticmethod
    def _favorable_move(position: ManagedPosition, price: float) -> float:
        if position.side == "LONG":
            return price - float(position.mfe_basis_entry_price)
        return float(position.mfe_basis_entry_price) - price

    def _refresh_mfe_basis(self, position: ManagedPosition) -> None:
        signature_changed = (
            position.mfe_basis_entry_price != float(position.entry_price)
            or position.mfe_basis_quantity != int(position.quantity)
            or position.initial_stop_price is None
        )
        if not signature_changed:
            return
        old_floor = position.locked_profit_price
        position.initial_stop_price = self._derive_initial_stop_price(position)
        position.mfe_basis_entry_price = float(position.entry_price)
        position.mfe_basis_quantity = int(position.quantity)
        risk = abs(position.mfe_basis_entry_price - position.initial_stop_price)
        position.mfe_r = (
            max(0.0, self._favorable_move(position, position.mfe_price) / risk)
            if position.mfe_price is not None else 0.0
        )
        candidate = _candidate_locked_r(position.mfe_r)
        if position.mfe_protection_armed and candidate is None:
            candidate = 0.0
        new_floor = None
        if candidate is not None:
            move = candidate * risk
            new_floor = (
                position.mfe_basis_entry_price + move
                if position.side == "LONG"
                else position.mfe_basis_entry_price - move
            )
        choices = [value for value in (old_floor, new_floor) if value is not None]
        if choices:
            position.locked_profit_price = (
                max(choices) if position.side == "LONG" else min(choices)
            )
            position.locked_profit_r = max(
                0.0,
                self._favorable_move(position, position.locked_profit_price) / risk,
            )
            position.locked_profit_pnl = self.projected_net(
                position, position.locked_profit_price
            )

    def _observe_mfe(
        self,
        position: ManagedPosition,
        *,
        price: float,
        projected_net_pnl: float,
        at: datetime,
    ) -> None:
        self._refresh_mfe_basis(position)
        risk = abs(float(position.mfe_basis_entry_price) - float(position.initial_stop_price))
        if position.mfe_price is None:
            position.mfe_price = float(position.mfe_basis_entry_price)
            position.mfe_pnl = self.projected_net(position, position.mfe_price)
            position.mfe_time = position.entry_time
        current_r = self._favorable_move(position, price) / risk
        if position.mfe_price is None or current_r > position.mfe_r:
            position.mfe_price = price
            position.mfe_pnl = projected_net_pnl
            position.mfe_r = max(0.0, current_r)
            position.mfe_time = at
        candidate = _candidate_locked_r(position.mfe_r)
        if candidate is None:
            return
        if not position.mfe_protection_armed:
            position.mfe_protection_armed = True
            position.mfe_activation_time = at
        move = candidate * risk
        candidate_price = (
            float(position.mfe_basis_entry_price) + move
            if position.side == "LONG"
            else float(position.mfe_basis_entry_price) - move
        )
        if position.locked_profit_price is None:
            position.locked_profit_price = candidate_price
        elif position.side == "LONG":
            position.locked_profit_price = max(position.locked_profit_price, candidate_price)
        else:
            position.locked_profit_price = min(position.locked_profit_price, candidate_price)
        position.locked_profit_r = max(
            position.locked_profit_r,
            self._favorable_move(position, position.locked_profit_price) / risk,
        )
        position.locked_profit_pnl = self.projected_net(
            position, position.locked_profit_price
        )

    @staticmethod
    def _mfe_floor_breached(position: ManagedPosition, price: float) -> bool:
        if not position.mfe_protection_armed or position.locked_profit_price is None:
            return False
        if position.side == "LONG":
            return price <= position.locked_profit_price
        return price >= position.locked_profit_price

    @_locked
    def latest_exit_price(self, position: ManagedPosition) -> float | None:
        state = self._states.get(position.stock_id)
        if state is None or not state.ticks:
            return None
        row = state.ticks[-1]
        if position.side == "LONG":
            return max(_tick_size(row["bid"]), row["bid"] - _tick_size(row["bid"]))
        return row["ask"] + _tick_size(row["ask"])

    @_locked
    def quote_age_seconds(self, symbol: str, now: datetime) -> float | None:
        state = self._states.get(str(symbol))
        if state is None or not state.ticks:
            return None
        return (now.astimezone(TAIPEI) - state.ticks[-1]["time"]).total_seconds()

    @_locked
    def safe_exit_quote(
        self,
        position: ManagedPosition,
        now: datetime,
        *,
        max_age_seconds: float = 3.0,
        max_spread_bps: float = 300.0,
    ) -> SafeExitQuote | None:
        """Return a fresh, bounded executable quote or fail closed."""
        state = self._states.get(position.stock_id)
        if state is None or not state.ticks:
            return None
        row = state.ticks[-1]
        age = (now.astimezone(TAIPEI) - row["time"]).total_seconds()
        bid, ask = float(row["bid"]), float(row["ask"])
        if age < 0 or age > max_age_seconds or bid <= 0 or ask <= 0 or ask < bid:
            return None
        midpoint = (bid + ask) / 2
        if midpoint <= 0 or (ask - bid) / midpoint * 10_000 > max_spread_bps:
            return None
        raw = (
            max(_tick_size(bid), bid - _tick_size(bid))
            if position.side == "LONG"
            else ask + _tick_size(ask)
        )
        price = raw
        if price <= 0:
            return None
        return SafeExitQuote(price, row["time"], age, bid, ask)

    def evaluate_exit(
        self,
        position: ManagedPosition,
        now: datetime,
        *,
        reversal: bool = False,
        max_quote_age_seconds: float = 3.0,
    ) -> ExitDecision | None:
        quote = self.safe_exit_quote(
            position, now, max_age_seconds=max_quote_age_seconds
        )
        if quote is None:
            return None
        price = quote.price
        projected = self._projected_net(position.side, position.entry_price, price, position.quantity)
        denominator = position.entry_price * position.quantity
        current_return = projected / denominator if denominator else 0.0
        position.peak_return = max(position.peak_return, current_return)
        position.worst_return = min(position.worst_return, current_return)
        self._observe_mfe(
            position,
            price=price,
            projected_net_pnl=projected,
            at=now.astimezone(TAIPEI),
        )
        reason = None
        if projected <= -float(LIVE_EXIT_POLICY["stop_loss_net_twd"]):
            reason = "STOP_LOSS"
        elif self._mfe_floor_breached(position, price):
            reason = "MFE_PROFIT_PROTECTION"
        elif reversal:
            reason = "SIGNAL_REVERSAL"
        elif now.astimezone(TAIPEI).time() >= self._clock(SPEC["hard_exit_time"]):
            reason = "HARD_EXIT"
        if reason is None:
            return None
        return ExitDecision(reason, price, projected, current_return)
