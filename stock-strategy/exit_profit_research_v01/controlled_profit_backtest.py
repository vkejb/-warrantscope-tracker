"""Controlled A/R/U/RU profit-giveback replay (research only).

The module consumes immutable quote archives and fixed, already-declared entry
records.  It does not import the broker adapter, mutate the live strategy, or
submit orders.  Variant A is the healthy production exit policy; R and U are
independent profit-exit overlays which compete with A on causal event time.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from shadow_daily_runner.normalize import _decode_tpex
from stage_a_t1_extreme_upside_study_v01.official_limits import twse_limits
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.exploratory_backtest import _tick_size
from yuanta_live_runtime_v01.strategy import LiveDirectionEngine, ManagedPosition

from .resistance_overlay import Book, Tick, load_books, load_ticks, signed_flow


TAIPEI = ZoneInfo("Asia/Taipei")
ANALYSIS_ID = "CONTROLLED_PROFIT_GIVEBACK_A_R_U_RU_V0_2"
VARIANTS = ("A", "R", "U", "RU")
LATENCIES_MS = (0, 250, 1000)
SESSION_OPEN = time(9, 0)
SESSION_CLOSE = time(13, 30)


@dataclass(frozen=True, slots=True)
class FixedEntry:
    trade_id: str
    session_date: str
    symbol: str
    stock_name: str
    entry_time: datetime
    entry_price: float
    quantity: int
    source: str
    post_hoc_case: bool = False


@dataclass(frozen=True, slots=True)
class ResistanceConfig:
    name: str
    formation_pullback_ticks: int
    proximity_bps: float
    minimum_proximity_ticks: int
    departure_bps: float
    minimum_departure_ticks: int
    failure_ticks: int
    bid_failure_ticks: int
    pressure_distance_ticks: int
    minimum_pressure_updates: int
    minimum_pressure_seconds: float
    minimum_sell_buy_ratio: float
    maximum_bid_depth_ratio: float
    flow_seconds: int
    maximum_buy_flow_delta: float
    pressure_lookback_seconds: int


# Declared before rerunning outcomes.  The primary rule deliberately reflects
# the user's example without encoding its symbol, price, or clock time:
# a prior high is confirmed after a two-tick pullback, then price must make a
# meaningful departure of 100 bps (and at least three ticks).  Observation
# begins only on a later rebound to within 50 bps (and at least two ticks) of
# the frozen high.  A two-tick price retreat plus one-tick best-bid retreat
# identifies rejection.  Five-level pressure is measured as confirmation,
# never required by the primary R exit.
R_PRIMARY = ResistanceConfig(
    "R_PRIMARY", 2, 50.0, 2, 100.0, 3, 2, 1, 1, 3, 1.0, 1.25, 0.80, 5, 0.10, 10,
)
R_LOOSE_SENSITIVITY = ResistanceConfig(
    "R_WIDER_ZONE_SENSITIVITY", 2, 75.0, 2, 100.0, 3, 2, 1, 1, 3, 1.0, 1.25, 0.80, 5, 0.10, 10,
)
R_CONFIGS = (R_PRIMARY, R_LOOSE_SENSITIVITY)


@dataclass(slots=True)
class ResistanceState:
    candidate_high_price: float
    candidate_high_time: datetime
    anchor_high_price: float | None = None
    anchor_high_time: datetime | None = None
    anchor_confirmed_time: datetime | None = None
    departed_zone_time: datetime | None = None
    observation_time: datetime | None = None
    observation_price: float | None = None
    observation_peak_price: float | None = None
    observation_peak_bid: float | None = None
    last_confirmation_book_time: datetime | None = None
    triggered: bool = False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _portable_path(path: Path) -> str:
    """Prefer a repository-relative provenance path when possible."""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(resolved)


def _positive_levels(prices: Iterable[float], volumes: Iterable[int]) -> list[tuple[float, int]]:
    return [
        (float(price), int(volume))
        for price, volume in zip(prices, volumes)
        if float(price) > 0 and int(volume) > 0
    ]


def _book_times(books: list[Book]) -> list[datetime]:
    return [item.received_at for item in books]


def _latest_book(
    books: list[Book], times: list[datetime], at: datetime,
) -> Book | None:
    index = bisect_right(times, at) - 1
    return books[index] if index >= 0 else None


def market_ioc_fill(
    books: list[Book],
    trigger_time: datetime,
    quantity_shares: int,
    latency_ms: int,
    *,
    maximum_book_wait_ms: int = 3000,
    maximum_legal_price: float | None = None,
) -> dict[str, Any]:
    """Walk the first causal buy book after order arrival, preserving partials."""
    if quantity_shares <= 0 or latency_ms < 0:
        raise ValueError("quantity must be positive and latency non-negative")
    arrival = trigger_time + timedelta(milliseconds=latency_ms)
    times = _book_times(books)
    index = bisect_left(times, arrival)
    chosen = books[index] if index < len(books) else None
    if chosen is None:
        return {
            "status": "NO_BOOK_AFTER_ARRIVAL", "arrival_time": arrival.isoformat(),
            "filled_shares": 0, "remaining_shares": quantity_shares,
            "fill_price": None, "fill_time": None, "levels": [],
        }
    wait_ms = (chosen.received_at - arrival).total_seconds() * 1000
    if wait_ms > maximum_book_wait_ms:
        return {
            "status": "STALE_BOOK_AFTER_ARRIVAL", "arrival_time": arrival.isoformat(),
            "book_time": chosen.received_at.isoformat(), "book_wait_ms": wait_ms,
            "filled_shares": 0, "remaining_shares": quantity_shares,
            "fill_price": None, "fill_time": None, "levels": [],
        }
    remaining_lots = quantity_shares / 1000.0
    notional_lots = 0.0
    used: list[dict[str, float]] = []
    for price, volume_lots in _positive_levels(chosen.buy_prices, chosen.buy_volumes):
        # The SPARK archive uses 99999.9999 as a market/locked-book sentinel.
        # It is not an executable stock price.  The verified official limit is
        # the authoritative ceiling; skip sentinels rather than filling them.
        if maximum_legal_price is not None and price > maximum_legal_price + 1e-9:
            continue
        take = min(remaining_lots, float(volume_lots))
        if take <= 0:
            continue
        used.append({"price": price, "lots": take, "shares": take * 1000})
        notional_lots += price * take
        remaining_lots -= take
        if remaining_lots <= 1e-9:
            break
    filled_lots = quantity_shares / 1000.0 - remaining_lots
    filled_shares = int(round(filled_lots * 1000))
    remaining_shares = max(0, quantity_shares - filled_shares)
    return {
        "status": (
            "FILLED" if remaining_shares == 0 else
            ("PARTIALLY_FILLED" if filled_shares > 0 else "UNFILLED_NO_BID_DEPTH")
        ),
        "arrival_time": arrival.isoformat(),
        "book_time": chosen.received_at.isoformat(),
        "book_wait_ms": wait_ms,
        "fill_time": chosen.received_at.isoformat() if filled_shares else None,
        "fill_price": notional_lots / filled_lots if filled_lots else None,
        "filled_shares": filled_shares,
        "remaining_shares": remaining_shares,
        "levels": used,
        "legal_order_type": (
            "SELL_MARKET_IOC_BOARD_LOT"
            if quantity_shares % 1000 == 0 else "SELL_MARKET_IOC_ODD_LOT_UNSUPPORTED"
        ),
        "ask_side_absent": not bool(_positive_levels(chosen.sell_prices, chosen.sell_volumes)),
    }


def _five_level_resistance(
    books: list[Book],
    *,
    book_times: list[datetime] | None = None,
    start: datetime,
    now: datetime,
    peak_price: float,
    config: ResistanceConfig,
) -> dict[str, Any]:
    tick = _tick_size(peak_price)
    times = _book_times(books) if book_times is None else book_times
    left, right = bisect_left(times, start), bisect_right(times, now)
    window = books[left:right]
    if not window:
        return {"pressure": False, "buyer_weak": False, "reason": "NO_CAUSAL_BOOK"}
    candidate_counts: dict[float, list[Book]] = {}
    for book in window:
        for price, _volume in _positive_levels(book.sell_prices, book.sell_volumes):
            if abs(price - peak_price) <= config.pressure_distance_ticks * tick + 1e-9:
                candidate_counts.setdefault(price, []).append(book)
    if not candidate_counts:
        return {
            "pressure": False, "buyer_weak": False,
            "reason": "NO_SAME_PRICE_SELL_LEVEL_NEAR_PEAK",
        }
    pressure_price, observations = max(
        candidate_counts.items(), key=lambda item: (len(item[1]), -abs(item[0] - peak_price))
    )
    elapsed = (
        observations[-1].received_at - observations[0].received_at
    ).total_seconds() if len(observations) > 1 else 0.0
    latest = window[-1]
    current_sell = sum(
        volume for price, volume in _positive_levels(latest.sell_prices, latest.sell_volumes)
        if abs(price - pressure_price) <= 1e-9
    )
    current_buy = sum(volume for _price, volume in _positive_levels(
        latest.buy_prices, latest.buy_volumes
    ))
    sell_buy_ratio = current_sell / current_buy if current_buy > 0 else math.inf
    pressure = (
        len(observations) >= config.minimum_pressure_updates
        and elapsed >= config.minimum_pressure_seconds
        and current_sell > 0
        and sell_buy_ratio >= config.minimum_sell_buy_ratio
    )
    initial_buy = sum(volume for _price, volume in _positive_levels(
        window[0].buy_prices, window[0].buy_volumes
    ))
    bid_depth_ratio = current_buy / initial_buy if initial_buy > 0 else None
    buyer_weak = (
        bid_depth_ratio is not None
        and bid_depth_ratio <= config.maximum_bid_depth_ratio
    )
    return {
        "pressure": pressure,
        "buyer_weak": buyer_weak,
        "reason": "PASS" if pressure and buyer_weak else (
            "SELL_PRESSURE_NOT_PERSISTENT" if not pressure else "BID_DEPTH_NOT_WEAKENING"
        ),
        "pressure_price": pressure_price,
        "pressure_updates": len(observations),
        "pressure_seconds": elapsed,
        "sell_buy_ratio": sell_buy_ratio,
        "initial_bid_depth_lots": initial_buy,
        "current_bid_depth_lots": current_buy,
        "bid_depth_ratio": bid_depth_ratio,
        "used_all_five_levels": True,
    }


def observe_resistance(
    *,
    state: ResistanceState,
    config: ResistanceConfig,
    tick: Tick,
    ticks_seen: list[Tick],
    books: list[Book],
    position: ManagedPosition,
    engine: LiveDirectionEngine,
    layers: dict[str, bool],
    book_times: list[datetime] | None = None,
    require_five_level_confirmation: bool = False,
    allow_trigger: bool = True,
) -> dict[str, Any] | None:
    """Causal confirmed-high -> near-zone -> price/bid rejection state machine.

    A high is not usable until a later pullback confirms that it really was a
    prior high.  Once confirmed, a lower rebound high never replaces it.
    Crossing above the anchor invalidates that resistance and starts formation
    of a new, higher candidate.  The trigger uses only information available at
    the current tick; the eventual rebound high is never back-filled.
    """
    if state.triggered:
        return None

    # A genuine breakout invalidates the old resistance.  A lower rebound does
    # not alter the frozen anchor.
    if state.anchor_high_price is not None and tick.price > state.anchor_high_price + 1e-9:
        state.candidate_high_price = tick.price
        state.candidate_high_time = tick.received_at
        state.anchor_high_price = None
        state.anchor_high_time = None
        state.anchor_confirmed_time = None
        state.departed_zone_time = None
        state.observation_time = None
        state.observation_price = None
        state.observation_peak_price = None
        state.observation_peak_bid = None
        state.last_confirmation_book_time = None
    elif state.anchor_high_price is None and tick.price > state.candidate_high_price + 1e-9:
        state.candidate_high_price = tick.price
        state.candidate_high_time = tick.received_at

    candidate_tick = _tick_size(state.candidate_high_price)
    peak_executable = max(candidate_tick, state.candidate_high_price - candidate_tick)
    peak_net = engine.projected_net(position, peak_executable)
    if peak_net <= 0:
        return None
    layers["profitable_peak"] = True

    if state.anchor_high_price is None:
        if tick.price <= (
            state.candidate_high_price
            - config.formation_pullback_ticks * candidate_tick
            + 1e-9
        ):
            state.anchor_high_price = state.candidate_high_price
            state.anchor_high_time = state.candidate_high_time
            state.anchor_confirmed_time = tick.received_at
            layers["prior_high_confirmed"] = True
        return None

    layers["prior_high_confirmed"] = True
    anchor = state.anchor_high_price
    price_tick = _tick_size(anchor)
    proximity = max(
        anchor * config.proximity_bps / 10_000.0,
        config.minimum_proximity_ticks * price_tick,
    )
    zone_floor = anchor - proximity
    departure = max(
        anchor * config.departure_bps / 10_000.0,
        config.minimum_departure_ticks * price_tick,
    )
    departure_floor = anchor - departure
    if state.departed_zone_time is None:
        # The first drop that confirms the high is not itself a failed rebound.
        # Price must first leave the near-high zone, then approach it again from
        # below before rejection can be evaluated.
        if tick.price <= departure_floor + 1e-9:
            state.departed_zone_time = tick.received_at
            layers["departed_prior_high_zone"] = True
        return None
    layers["departed_prior_high_zone"] = True
    if state.observation_time is None:
        if tick.price < zone_floor - 1e-9:
            return None
        state.observation_time = tick.received_at
        state.observation_price = tick.price
        state.observation_peak_price = tick.price
        state.observation_peak_bid = tick.bid if tick.bid > 0 else None
        layers["near_prior_high_observed"] = True
        return None

    layers["near_prior_high_observed"] = True
    if state.observation_peak_price is None or tick.price > state.observation_peak_price:
        state.observation_peak_price = tick.price
    if tick.bid > 0 and (
        state.observation_peak_bid is None or tick.bid > state.observation_peak_bid
    ):
        state.observation_peak_bid = tick.bid

    price_failed = tick.price <= (
        state.observation_peak_price - config.failure_ticks * price_tick + 1e-9
    )
    bid_failed = (
        tick.bid > 0
        and state.observation_peak_bid is not None
        and tick.bid <= state.observation_peak_bid - config.bid_failure_ticks * price_tick + 1e-9
    )
    if not (price_failed and bid_failed):
        return None
    layers["price_bid_failure"] = True

    causal_times = _book_times(books) if book_times is None else book_times
    causal_book = _latest_book(books, causal_times, tick.received_at)
    five = (
        _five_level_resistance(
            books, book_times=causal_times,
            start=max(
                state.observation_time,
                tick.received_at - timedelta(seconds=config.pressure_lookback_seconds),
            ),
            now=tick.received_at,
            peak_price=anchor, config=config,
        )
        if causal_book is not None else
        {"pressure": False, "buyer_weak": False, "reason": "NO_CAUSAL_BOOK"}
    )
    if five.get("pressure"):
        layers["same_price_sell_pressure"] = True
    if five.get("buyer_weak"):
        layers["bid_depth_weakening"] = True
    flow = signed_flow(ticks_seen, tick.received_at, config.flow_seconds)
    flow_weak = flow is not None and flow <= config.maximum_buy_flow_delta
    if flow_weak:
        layers["buyer_flow_weakening"] = True
    if not flow_weak:
        return None
    if require_five_level_confirmation and not (
        five.get("pressure") and five.get("buyer_weak")
    ):
        return None
    if not allow_trigger:
        return None
    executable = tick.bid
    if executable <= 0 or engine.projected_net(position, executable) <= 0:
        return None
    layers["positive_executable_net"] = True
    layers["triggered"] = True
    state.triggered = True
    return {
        "overlay": "R",
        "trigger_time": tick.received_at.isoformat(),
        "trigger_exchange_time": tick.exchange_time.isoformat(),
        "trigger_price": tick.price,
        "trigger_bid": tick.bid,
        "anchor_high_price": anchor,
        "anchor_high_time": (
            None if state.anchor_high_time is None else state.anchor_high_time.isoformat()
        ),
        "anchor_confirmed_time": (
            None if state.anchor_confirmed_time is None
            else state.anchor_confirmed_time.isoformat()
        ),
        "departed_zone_time": state.departed_zone_time.isoformat(),
        "observation_zone_floor": zone_floor,
        "meaningful_pullback_floor": departure_floor,
        "observation_time": state.observation_time.isoformat(),
        "observation_price": state.observation_price,
        "causal_rebound_peak_price": state.observation_peak_price,
        "causal_rebound_peak_bid": state.observation_peak_bid,
        "buyer_flow_delta": flow,
        "five_level_confirmed": bool(five.get("pressure") and five.get("buyer_weak")),
        "five_level_required": require_five_level_confirmation,
        "five_level_confirmation": five,
        "config": asdict(config),
    }


def _deepest_r_reason(
    layers: Mapping[str, bool], *, require_five_level_confirmation: bool = False,
) -> str:
    order = (
        ("profitable_peak", "NO_PROFITABLE_HIGH"),
        ("prior_high_confirmed", "NO_PRIOR_HIGH_CONFIRMED_BY_PULLBACK"),
        ("departed_prior_high_zone", "PRIOR_HIGH_ZONE_NOT_LEFT_BEFORE_REBOUND"),
        ("near_prior_high_observed", "NEVER_ENTERED_PRIOR_HIGH_OBSERVATION_ZONE"),
        ("price_bid_failure", "NO_CAUSAL_PRICE_AND_BID_REJECTION"),
        ("buyer_flow_weakening", "NO_BUYER_FLOW_WEAKENING"),
        ("positive_executable_net", "NO_POSITIVE_EXECUTABLE_NET"),
        ("triggered", "NO_TRIGGER"),
    )
    for key, reason in order:
        if not layers.get(key, False):
            return reason
        if key == "buyer_flow_weakening" and require_five_level_confirmation and not (
            layers.get("same_price_sell_pressure", False)
            and layers.get("bid_depth_weakening", False)
        ):
            return "NO_FIVE_LEVEL_CONFIRMATION"
    return "TRIGGERED"


def _twse_limit_record(day: str, symbol: str, cache: Path) -> dict[str, Any]:
    limits, provenance = twse_limits(day, cache)
    if symbol not in limits:
        return {"status": "UNVERIFIED_NOT_IN_TWSE_TABLE", "upper_limit": None}
    raw = json.loads((cache / provenance["filename"]).read_text(encoding="utf-8-sig"))
    fields = raw["fields"]
    code_i = fields.index("證券代號")
    ref_i = fields.index("開盤競價基準")
    matching = [row for row in raw["data"] if str(row[code_i]).strip() == symbol]
    reference = float(str(matching[0][ref_i]).replace(",", "")) if matching else None
    upper = float(limits[symbol])
    tick = _tick_size(upper)
    legal_tick = abs(round(upper / tick) * tick - upper) <= 1e-8
    return {
        "status": "VERIFIED" if legal_tick else "INVALID_TICK_GRID",
        "upper_limit": upper,
        "opening_reference_price": reference,
        "legal_tick_size_at_limit": tick,
        "legal_tick_verified": legal_tick,
        "source_kind": "TWSE_TWT84U_OFFICIAL_SAME_DAY",
        "source_url": provenance["url"],
        "source_sha256": provenance["sha256"],
    }


def _tpex_limit_record(watchlist: dict[str, Any], symbol: str) -> dict[str, Any]:
    signal_date = str(watchlist["signal_date"])
    source = watchlist["market_provenance"]["sources"]["TPEX"]
    path = Path(source["path"])
    if not path.exists() or _sha256(path) != source["sha256"]:
        return {"status": "UNVERIFIED_TPEX_SOURCE_HASH", "upper_limit": None}
    lines = _decode_tpex(path.read_bytes()).replace("\r\n", "\n").splitlines()
    header = next((i for i, line in enumerate(lines) if line.lstrip().startswith("代號,")), None)
    if header is None:
        return {"status": "UNVERIFIED_TPEX_SCHEMA", "upper_limit": None}
    reader = csv.DictReader(lines[header:])
    if reader.fieldnames is None:
        return {"status": "UNVERIFIED_TPEX_SCHEMA", "upper_limit": None}
    names = {name.strip(): name for name in reader.fieldnames}
    if not {"代號", "次日漲停價"}.issubset(names):
        return {"status": "UNVERIFIED_TPEX_SCHEMA", "upper_limit": None}
    row = next((item for item in reader if str(item[names["代號"]]).strip() == symbol), None)
    if row is None:
        return {"status": "UNVERIFIED_NOT_IN_TPEX_TABLE", "upper_limit": None}
    upper = float(str(row[names["次日漲停價"]]).replace(",", "").strip())
    tick = _tick_size(upper)
    legal_tick = abs(round(upper / tick) * tick - upper) <= 1e-8
    return {
        "status": "VERIFIED" if legal_tick else "INVALID_TICK_GRID",
        "upper_limit": upper,
        "opening_reference_price": None,
        "legal_tick_size_at_limit": tick,
        "legal_tick_verified": legal_tick,
        "source_kind": "TPEX_OFFICIAL_PRIOR_DAY_NEXT_LIMIT",
        "source_signal_date": signal_date,
        "source_url": source.get("request_url"),
        "source_sha256": source["sha256"],
    }


def official_limit_record(run_dir: Path, entry: FixedEntry, cache: Path) -> dict[str, Any]:
    watchlist = json.loads((run_dir / "watchlist.json").read_text(encoding="utf-8"))
    stocks = {str(row["stock_id"]): row for row in watchlist.get("stocks", [])}
    metadata = stocks.get(entry.symbol)
    if metadata is None:
        return {"status": "UNVERIFIED_SYMBOL_NOT_IN_WATCHLIST", "upper_limit": None}
    market = str(metadata.get("market"))
    record = (
        _twse_limit_record(entry.session_date, entry.symbol, cache)
        if market == "TWSE" else
        _tpex_limit_record(watchlist, entry.symbol)
        if market == "TPEX" else
        {"status": "NO_DAILY_PRICE_LIMIT_OR_UNKNOWN_MARKET", "upper_limit": None}
    )
    return {"session_date": entry.session_date, "symbol": entry.symbol,
            "market": market, **record}


def _first_limit_touch(
    ticks: list[Tick], entry_time: datetime, official: Mapping[str, Any],
) -> Tick | None:
    if official.get("status") != "VERIFIED" or official.get("upper_limit") is None:
        return None
    limit = float(official["upper_limit"])
    for tick in ticks:
        local = tick.exchange_time.astimezone(TAIPEI).time()
        if tick.received_at < entry_time or not (SESSION_OPEN <= local <= SESSION_CLOSE):
            continue
        if tick.volume > 0 and math.isclose(tick.price, limit, abs_tol=1e-8):
            return tick
    return None


def _touch_status(
    first_touch: Tick | None,
    official: Mapping[str, Any],
    exit_trigger_time: datetime | None,
) -> str:
    if official.get("status") != "VERIFIED":
        return "UNSCORABLE"
    if first_touch is None:
        return "NO_TRIGGER"
    if exit_trigger_time is not None and exit_trigger_time < first_touch.received_at:
        return "AFTER_EARLIER_EXIT"
    return "TRIGGER"


def _mfe_path(
    engine: LiveDirectionEngine, position: ManagedPosition, ticks: list[Tick],
    *, through: datetime | None = None, after: datetime | None = None,
    maximum_legal_price: float | None = None,
) -> float | None:
    values = []
    for tick in ticks:
        if tick.received_at < position.entry_time or tick.bid <= 0:
            continue
        if through is not None and tick.received_at > through:
            continue
        if after is not None and tick.received_at <= after:
            continue
        executable = (
            min(tick.bid, maximum_legal_price)
            if maximum_legal_price is not None else tick.bid
        )
        values.append(engine.projected_net(position, executable))
    return max(values) if values else None


def replay_variant(
    entry: FixedEntry,
    run_dir: Path,
    official: Mapping[str, Any],
    *,
    variant: str,
    latency_ms: int,
    resistance_config: ResistanceConfig = R_PRIMARY,
    include_original_exit: bool = True,
    require_five_level_confirmation: bool = False,
    r_trigger_not_before: datetime | None = None,
    ticks: list[Tick] | None = None,
    books: list[Book] | None = None,
) -> dict[str, Any]:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant}")
    ticks = load_ticks(run_dir, entry.symbol, entry.session_date) if ticks is None else ticks
    books = load_books(run_dir, entry.symbol) if books is None else books
    engine = LiveDirectionEngine({entry.symbol: entry.stock_name}, capital_twd=190_000)
    position = ManagedPosition(
        entry.symbol, entry.stock_name, "LONG", entry.quantity, entry.entry_price,
        entry.trade_id, entry.entry_time,
    )
    r_state = ResistanceState(entry.entry_price, entry.entry_time)
    r_layers = {
        key: False for key in (
            "profitable_peak", "prior_high_confirmed", "departed_prior_high_zone",
            "near_prior_high_observed",
            "price_bid_failure",
            "same_price_sell_pressure", "bid_depth_weakening",
            "buyer_flow_weakening", "positive_executable_net", "triggered",
        )
    }
    ticks_seen: list[Tick] = []
    book_times = _book_times(books)
    first_touch = _first_limit_touch(ticks, entry.entry_time, official)
    chosen: dict[str, Any] | None = None
    a_trigger: dict[str, Any] | None = None
    r_trigger: dict[str, Any] | None = None
    u_trigger: dict[str, Any] | None = None
    for tick in ticks:
        local = tick.exchange_time.astimezone(TAIPEI).time()
        if tick.received_at < entry.entry_time or not (SESSION_OPEN <= local <= SESSION_CLOSE):
            continue
        outcome = engine.ingest_tick(
            entry.symbol, at=tick.exchange_time, received_at=tick.received_at,
            price=tick.price, volume=tick.volume, bid=tick.bid, ask=tick.ask,
            flag=tick.flag, serial=tick.serial,
        )
        ticks_seen.append(tick)
        if include_original_exit and outcome.accepted:
            decision = engine.evaluate_exit(position, tick.received_at, reversal=False)
            if decision is not None:
                a_trigger = {
                    "overlay": "A", "trigger_time": tick.received_at.isoformat(),
                    "trigger_exchange_time": tick.exchange_time.isoformat(),
                    "trigger_price": tick.price, "trigger_bid": tick.bid,
                    "reason": decision.reason,
                }
        if variant in {"R", "RU"}:
            r_trigger = observe_resistance(
                state=r_state, config=resistance_config, tick=tick,
                ticks_seen=ticks_seen, books=books, position=position,
                engine=engine, layers=r_layers, book_times=book_times,
                require_five_level_confirmation=require_five_level_confirmation,
                allow_trigger=(
                    r_trigger_not_before is None
                    or tick.received_at >= r_trigger_not_before
                ),
            )
        if variant in {"U", "RU"} and first_touch is tick:
            u_trigger = {
                "overlay": "U", "trigger_time": tick.received_at.isoformat(),
                "trigger_exchange_time": tick.exchange_time.isoformat(),
                "trigger_price": tick.price, "trigger_bid": tick.bid,
                "official_upper_limit": official.get("upper_limit"),
                "reason": "FIRST_ACTUAL_TRADE_AT_OFFICIAL_UPPER_LIMIT",
            }
        available = [item for item in (a_trigger, r_trigger, u_trigger) if item is not None]
        if available:
            chosen = min(available, key=lambda item: datetime.fromisoformat(item["trigger_time"]))
            break
    if chosen is None:
        return {
            "trade_id": entry.trade_id, "session_date": entry.session_date,
            "symbol": entry.symbol, "stock_name": entry.stock_name,
            "source": entry.source, "post_hoc_case": entry.post_hoc_case,
            "variant": variant, "latency_ms": latency_ms,
            "entry_time": entry.entry_time.isoformat(), "entry_price": entry.entry_price,
            "quantity": entry.quantity, "official_limit_status": official.get("status"),
            "official_upper_limit": official.get("upper_limit"),
            "upper_limit_touch_status": _touch_status(first_touch, official, None),
            "exit_status": "UNSCORABLE_NO_EXIT", "exit_reason": None,
            "trigger_time": None, "fill_time": None, "fill_price": None,
            "filled_quantity": 0, "remaining_quantity": entry.quantity,
            "net_pnl_twd": None, "partial_realized_pnl_twd": None,
            "pre_exit_mfe_net_pnl_twd": None, "profit_giveback_twd": None,
            "post_exit_opportunity_twd": None,
            "r_observation_time": (
                None if r_state.observation_time is None
                else r_state.observation_time.isoformat()
            ),
            "r_observation_price": r_state.observation_price,
            "r_anchor_high_price": r_state.anchor_high_price,
            "r_triggered": False, "r_no_trigger_reason": _deepest_r_reason(
                r_layers,
                require_five_level_confirmation=require_five_level_confirmation,
            ),
            "r_layers": r_layers, "trigger_details": None, "execution": None,
        }
    trigger_time = datetime.fromisoformat(chosen["trigger_time"])
    maximum_legal_price = (
        float(official["upper_limit"])
        if official.get("status") == "VERIFIED" and official.get("upper_limit") is not None
        else None
    )
    execution = market_ioc_fill(
        books, trigger_time, entry.quantity, latency_ms,
        maximum_legal_price=maximum_legal_price,
    )
    fill_price = execution.get("fill_price")
    filled = int(execution["filled_shares"])
    remaining = int(execution["remaining_shares"])
    full_net = (
        engine.projected_net(position, float(fill_price))
        if fill_price is not None and remaining == 0 else None
    )
    partial_net = (
        LiveDirectionEngine._projected_net(
            "LONG", entry.entry_price, float(fill_price), filled
        ) if fill_price is not None and filled > 0 else None
    )
    fill_time = (
        datetime.fromisoformat(str(execution["fill_time"]))
        if execution.get("fill_time") else None
    )
    mfe_cutoff = fill_time or trigger_time
    pre_mfe = _mfe_path(
        engine, position, ticks, through=mfe_cutoff,
        maximum_legal_price=maximum_legal_price,
    )
    realized_for_compare = full_net if full_net is not None else partial_net
    giveback = (
        max(0.0, pre_mfe - realized_for_compare)
        if pre_mfe is not None and realized_for_compare is not None and remaining == 0
        else None
    )
    post_best = _mfe_path(
        engine, position, ticks, after=mfe_cutoff,
        maximum_legal_price=maximum_legal_price,
    )
    post_opportunity = (
        max(0.0, post_best - full_net)
        if post_best is not None and full_net is not None else None
    )
    overlay = str(chosen["overlay"])
    return {
        "trade_id": entry.trade_id, "session_date": entry.session_date,
        "symbol": entry.symbol, "stock_name": entry.stock_name,
        "source": entry.source, "post_hoc_case": entry.post_hoc_case,
        "variant": variant, "latency_ms": latency_ms,
        "entry_time": entry.entry_time.isoformat(), "entry_price": entry.entry_price,
        "quantity": entry.quantity, "official_limit_status": official.get("status"),
        "official_upper_limit": official.get("upper_limit"),
        "upper_limit_touch_status": _touch_status(first_touch, official, trigger_time),
        "first_upper_limit_touch_time": (
            None if first_touch is None else first_touch.received_at.isoformat()
        ),
        "exit_status": execution["status"],
        "exit_reason": (
            str(chosen.get("reason")) if overlay == "A" else
            "PRIOR_HIGH_REJECTION_PROFIT_PROTECTION" if overlay == "R" else
            "FIRST_OFFICIAL_UPPER_LIMIT_TOUCH"
        ),
        "winning_trigger": overlay,
        "trigger_time": trigger_time.isoformat(),
        "trigger_exchange_time": chosen.get("trigger_exchange_time"),
        "trigger_price": chosen.get("trigger_price"),
        "fill_time": execution.get("fill_time"), "fill_price": fill_price,
        "filled_quantity": filled, "remaining_quantity": remaining,
        "net_pnl_twd": full_net, "partial_realized_pnl_twd": partial_net,
        "pre_exit_mfe_net_pnl_twd": pre_mfe,
        "profit_giveback_twd": giveback,
        "post_exit_best_counterfactual_net_pnl_twd": post_best,
        "post_exit_opportunity_twd": post_opportunity,
        "r_observation_time": (
            None if r_state.observation_time is None
            else r_state.observation_time.isoformat()
        ),
        "r_observation_price": r_state.observation_price,
        "r_anchor_high_price": r_state.anchor_high_price,
        "r_triggered": overlay == "R",
        "r_no_trigger_reason": "TRIGGERED" if overlay == "R" else _deepest_r_reason(
            r_layers,
            require_five_level_confirmation=require_five_level_confirmation,
        ),
        "r_layers": r_layers,
        "trigger_details": chosen,
        "execution": execution,
    }


def _load_fixed_entries(path: Path) -> list[FixedEntry]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    reference = str(payload["reference_variant"])
    entries: dict[tuple[str, str, str], FixedEntry] = {}
    for row in payload["per_trade"]:
        if row["variant"] != reference or row["source_signal_type"] == "NEAR_MISS":
            continue
        key = (str(row["session_date"]), str(row["symbol"]), str(row["entry_time"]))
        entries[key] = FixedEntry(
            trade_id=str(row["validation_trade_id"]),
            session_date=str(row["session_date"]), symbol=str(row["symbol"]),
            stock_name=str(row.get("stock_name") or row["symbol"]),
            entry_time=datetime.fromisoformat(str(row["entry_time"])).astimezone(TAIPEI),
            entry_price=float(row["entry_price"]), quantity=int(row["quantity"]),
            source="EXISTING_FIXED_BASE_ENTRY",
        )
    return sorted(entries.values(), key=lambda item: (item.session_date, item.entry_time))


def _summary(rows: list[dict[str, Any]], variant: str, latency_ms: int) -> dict[str, Any]:
    selected = [row for row in rows if row["variant"] == variant and row["latency_ms"] == latency_ms]
    scored = [row for row in selected if row["net_pnl_twd"] is not None]
    pnls = [float(row["net_pnl_twd"]) for row in scored]
    givebacks = [float(row["profit_giveback_twd"]) for row in scored if row["profit_giveback_twd"] is not None]
    opportunities = [float(row["post_exit_opportunity_twd"]) for row in scored if row["post_exit_opportunity_twd"] is not None]
    return {
        "variant": variant, "latency_ms": latency_ms,
        "trade_count": len(selected), "scorable_trades": len(scored),
        "unscorable_trades": len(selected) - len(scored),
        "net_pnl_twd": round(sum(pnls), 2) if pnls else None,
        "average_pnl_twd": round(mean(pnls), 2) if pnls else None,
        "wins": sum(value > 0 for value in pnls),
        "losses": sum(value < 0 for value in pnls),
        "r_triggers": sum(row.get("winning_trigger") == "R" for row in selected),
        "upper_limit_touch_cases": sum(row["upper_limit_touch_status"] == "TRIGGER" for row in selected),
        "u_winning_triggers": sum(row.get("winning_trigger") == "U" for row in selected),
        "partial_fills": sum(row["exit_status"] == "PARTIALLY_FILLED" for row in selected),
        "unfilled_intents": sum(row["exit_status"] in {
            "UNFILLED_NO_BID_DEPTH", "NO_BOOK_AFTER_ARRIVAL", "STALE_BOOK_AFTER_ARRIVAL"
        } for row in selected),
        "total_profit_giveback_twd": round(sum(givebacks), 2),
        "average_profit_giveback_twd": round(mean(givebacks), 2) if givebacks else None,
        "total_post_exit_opportunity_twd": round(sum(opportunities), 2),
    }


def build_report(
    *,
    fixed_entries_path: Path,
    runs_by_date: Mapping[str, Path],
    official_cache: Path,
) -> dict[str, Any]:
    entries = _load_fixed_entries(fixed_entries_path)
    entries.append(FixedEntry(
        trade_id="POST_HOC_20261002_3094_ACTUAL_FILL",
        session_date="20261002", symbol="3094", stock_name="聯傑",
        entry_time=datetime.fromisoformat("2026-10-02T09:13:31.013+08:00"),
        entry_price=70.05, quantity=2000,
        source="INCIDENT_ACTUAL_TWO_FILLS", post_hoc_case=True,
    ))
    rows: list[dict[str, Any]] = []
    limit_audit: list[dict[str, Any]] = []
    r_sensitivity: list[dict[str, Any]] = []
    conditional_still_holding: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for entry in entries:
        run_dir = runs_by_date.get(entry.session_date)
        if run_dir is None or not run_dir.exists():
            unavailable.append({"trade_id": entry.trade_id, "reason": "SOURCE_RUN_MISSING"})
            continue
        official = official_limit_record(run_dir, entry, official_cache)
        limit_audit.append({"trade_id": entry.trade_id, **official})
        ticks = load_ticks(run_dir, entry.symbol, entry.session_date)
        books = load_books(run_dir, entry.symbol)
        if not ticks or not books:
            unavailable.append({
                "trade_id": entry.trade_id,
                "reason": "TICK_OR_FIVE_LEVEL_ARCHIVE_MISSING",
                "tick_count": len(ticks), "book_count": len(books),
            })
            continue
        for latency in LATENCIES_MS:
            for variant in VARIANTS:
                rows.append(replay_variant(
                    entry, run_dir, official, variant=variant, latency_ms=latency,
                    resistance_config=R_PRIMARY, ticks=ticks, books=books,
                ))
        for config, confirmation_mode in (
            (R_PRIMARY, "PRICE_BID_ONLY"),
            (R_PRIMARY, "PRICE_BID_PLUS_FIVE_LEVEL"),
            (R_LOOSE_SENSITIVITY, "PRICE_BID_ONLY"),
        ):
            result = replay_variant(
                entry, run_dir, official, variant="R", latency_ms=250,
                resistance_config=config, ticks=ticks, books=books,
                require_five_level_confirmation=(
                    confirmation_mode == "PRICE_BID_PLUS_FIVE_LEVEL"
                ),
            )
            r_sensitivity.append({
                "trade_id": entry.trade_id, "session_date": entry.session_date,
                "symbol": entry.symbol, "config": config.name,
                "confirmation_mode": confirmation_mode,
                "winning_trigger": result.get("winning_trigger"),
                "r_triggered": result["r_triggered"],
                "r_no_trigger_reason": result["r_no_trigger_reason"],
                "r_layers": result["r_layers"],
                "net_pnl_twd": result["net_pnl_twd"],
            })
        a_250 = next(
            row for row in rows
            if row["trade_id"] == entry.trade_id
            and row["variant"] == "A" and row["latency_ms"] == 250
        )
        a_exit_time = (
            datetime.fromisoformat(a_250["trigger_time"])
            if a_250.get("trigger_time") else None
        )
        conditional = replay_variant(
            entry, run_dir, official, variant="R", latency_ms=250,
            resistance_config=R_PRIMARY, ticks=ticks, books=books,
            include_original_exit=False,
            r_trigger_not_before=a_exit_time,
        )
        conditional_still_holding.append({
            "trade_id": entry.trade_id,
            "session_date": entry.session_date,
            "symbol": entry.symbol,
            "scope": "CONDITIONAL_IF_STILL_HOLDING_IGNORE_A",
            "r_triggered": conditional["r_triggered"],
            "r_no_trigger_reason": conditional["r_no_trigger_reason"],
            "observation_time": conditional.get("r_observation_time"),
            "observation_price": conditional.get("r_observation_price"),
            "anchor_high_price": conditional.get("r_anchor_high_price"),
            "trigger_time": conditional.get("trigger_time"),
            "trigger_price": conditional.get("trigger_price"),
            "fill_time": conditional.get("fill_time"),
            "fill_price": conditional.get("fill_price"),
            "net_pnl_twd": conditional.get("net_pnl_twd"),
            "five_level_confirmed": bool(
                (conditional.get("trigger_details") or {}).get("five_level_confirmed")
            ),
            "earlier_a_exit_time": a_250.get("trigger_time"),
        })
    summaries = [
        _summary(rows, variant, latency)
        for latency in LATENCIES_MS for variant in VARIANTS
    ]
    a_lookup = {
        (row["trade_id"], row["latency_ms"]): row for row in rows if row["variant"] == "A"
    }
    for row in rows:
        baseline = a_lookup.get((row["trade_id"], row["latency_ms"]))
        row["pnl_vs_a_twd"] = (
            None if baseline is None or baseline["net_pnl_twd"] is None or row["net_pnl_twd"] is None
            else round(float(row["net_pnl_twd"]) - float(baseline["net_pnl_twd"]), 2)
        )
    for summary in summaries:
        baseline = next(
            item for item in summaries
            if item["variant"] == "A" and item["latency_ms"] == summary["latency_ms"]
        )
        summary["net_pnl_vs_a_twd"] = (
            None if summary["net_pnl_twd"] is None or baseline["net_pnl_twd"] is None
            else round(float(summary["net_pnl_twd"]) - float(baseline["net_pnl_twd"]), 2)
        )
    report = {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "BACKTEST_ONLY_CONTROLLED_PROFIT_GIVEBACK_DIAGNOSTIC",
        "variants": {
            "A": "healthy production exit: stop loss, MFE V1, reversal input unchanged (archives expose no reversal), hard exit",
            "R": "A plus confirmed prior-high near-zone price/bid rejection profit exit; five-level pressure is diagnostic only",
            "U": "A plus first actual trade at verified official upper limit, immediate market IOC intent",
            "RU": "A plus both overlays; earliest causal trigger wins",
        },
        "latencies_ms": LATENCIES_MS,
        "primary_r_config": asdict(R_PRIMARY),
        "r_sensitivity_configs": [asdict(item) for item in R_CONFIGS],
        "entry_count": len(entries),
        "post_hoc_trade_ids": [entry.trade_id for entry in entries if entry.post_hoc_case],
        "source_runs": {day: _portable_path(path) for day, path in runs_by_date.items()},
        "source_fixed_entries": _portable_path(fixed_entries_path),
        "official_limit_audit": limit_audit,
        "unavailable": unavailable,
        "summaries": summaries,
        "per_trade": rows,
        "r_sensitivity": r_sensitivity,
        "conditional_still_holding": conditional_still_holding,
        "limitations": [
            "The 20261002 3094 incident is post-hoc inspiration, not out-of-sample evidence.",
            "Only fixed entries with matching tick and five-level archives are included.",
            "Archived five-level timestamps are local receive times, not exchange book timestamps.",
            "Displayed depth is not guaranteed fill; replay uses only the first causal book after arrival.",
            "A partial IOC fill leaves the remainder explicit and the total trade PnL unscorable; no fill is invented.",
            "The archive-backed fixed-entry research path has no reversal flags, so A preserves stop/MFE/EOD but cannot score signal-reversal exits.",
            "No result changes live or production behavior.",
        ],
        "production_behavior_changed": False,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
    }
    report["report_hash"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


def _fmt(value: Any) -> str:
    return "-" if value is None else str(value)


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Controlled profit-giveback A/R/U/RU replay", "",
        "Research only. No broker login, order, process restart, LIVE change, or deployment occurred.", "",
        "## Fixed definitions", "",
        "- A: healthy original production stop/MFE/EOD exit path.",
        "- R: freeze a causally confirmed prior high after its pullback; enter observation before touching it; exit only after current price and best bid weaken, buyer flow weakens, and executable net profit remains positive.",
        "- Five-level persistent sell pressure is reported in parallel and is not required by the primary R rule.",
        "- U: first actual trade at the verified official daily upper limit immediately creates one market-IOC sell intent for the remaining quantity.",
        "- RU: earliest valid A, R, or U trigger wins. No duplicate sell is created.", "",
        "## Four-version comparison", "",
        "| Latency | Variant | Scorable/All | Net PnL | vs A | R exits | Limit-touch cases | U exits | Partial | Giveback | Post-exit opportunity |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["summaries"]:
        lines.append(
            f"| {row['latency_ms']}ms | {row['variant']} | {row['scorable_trades']}/{row['trade_count']} | "
            f"{_fmt(row['net_pnl_twd'])} | {_fmt(row['net_pnl_vs_a_twd'])} | {row['r_triggers']} | "
            f"{row['upper_limit_touch_cases']} | {row['u_winning_triggers']} | {row['partial_fills']} | "
            f"{row['total_profit_giveback_twd']} | {row['total_post_exit_opportunity_twd']} |"
        )
    base_250 = next(row for row in report["summaries"] if row["variant"] == "A" and row["latency_ms"] == 250)
    r_250 = next(row for row in report["summaries"] if row["variant"] == "R" and row["latency_ms"] == 250)
    u_250 = next(row for row in report["summaries"] if row["variant"] == "U" and row["latency_ms"] == 250)
    lines.extend([
        "", "## Fixed-rule result", "",
        f"- At 250ms, primary R changes net PnL from {base_250['net_pnl_twd']} to {r_250['net_pnl_twd']} TWD ({r_250['net_pnl_vs_a_twd']} vs A); it is not supported for deployment.",
        f"- U changes the same comparable total to {u_250['net_pnl_twd']} TWD ({u_250['net_pnl_vs_a_twd']} vs A), but only {u_250['u_winning_triggers']} trade triggered U, so this is not broad evidence.",
        "- The conditional still-holding table is descriptive only and is excluded from these totals.",
    ])
    lines.extend(["", "## Resistance filter impact (250ms)", ""])
    profiles = sorted({
        (row["config"], row["confirmation_mode"])
        for row in report["r_sensitivity"]
    })
    for config, mode in profiles:
        selected = [
            row for row in report["r_sensitivity"]
            if row["config"] == config and row["confirmation_mode"] == mode
        ]
        lines.append(f"### {config} / {mode}")
        lines.append("")
        lines.append(f"- R won the exit race on {sum(row['r_triggered'] for row in selected)} / {len(selected)} trades.")
        scored_profile = [row["net_pnl_twd"] for row in selected if row["net_pnl_twd"] is not None]
        lines.append(f"- Total scorable net PnL: {round(sum(scored_profile), 2)} TWD across {len(scored_profile)} trades.")
        for key in (
            "profitable_peak", "prior_high_confirmed", "departed_prior_high_zone",
            "near_prior_high_observed",
            "price_bid_failure",
            "same_price_sell_pressure", "bid_depth_weakening",
            "buyer_flow_weakening", "positive_executable_net", "triggered",
        ):
            lines.append(f"- {key}: {sum(row['r_layers'].get(key, False) for row in selected)}")
        reasons: dict[str, int] = {}
        for row in selected:
            reason = row["r_no_trigger_reason"]
            reasons[reason] = reasons.get(reason, 0) + 1
        lines.append("- terminal reasons: " + ", ".join(f"{key}={value}" for key, value in sorted(reasons.items())))
        lines.append("")
    touched = {
        row["trade_id"] for row in report["per_trade"]
        if row["upper_limit_touch_status"] == "TRIGGER"
    }
    lines.extend([
        "## Limit-up evidence", "",
        f"- Verified cases touching the official upper limit while the simulated position was still open: {len(touched)}.",
        "- A full-day high is never used as a substitute for the official limit price.",
        "- A bid-only locked-limit book is retained as legal execution evidence; a missing ask is never fabricated.", "",
        "## 250ms material trade impacts", "",
        "| Date | Symbol | Variant | Winner | First observed | Observed px | Trigger | Trigger px | Fill | Fill px | Net PnL | vs A |",
        "|---|---|---|---|---|---:|---|---:|---|---:|---:|---:|",
    ])
    material = [
        row for row in report["per_trade"]
        if row["latency_ms"] == 250
        and row["variant"] in {"R", "U"}
        and (row.get("winning_trigger") in {"R", "U"} or row.get("pnl_vs_a_twd") not in {None, 0, 0.0})
    ]
    for row in material:
        lines.append(
            f"| {row['session_date']} | {row['symbol']} | {row['variant']} | "
            f"{_fmt(row.get('winning_trigger'))} | {_fmt(row.get('r_observation_time'))} | "
            f"{_fmt(row.get('r_observation_price'))} | {_fmt(row.get('trigger_time'))} | "
            f"{_fmt(row.get('trigger_price'))} | {_fmt(row.get('fill_time'))} | "
            f"{_fmt(row.get('fill_price'))} | {_fmt(row.get('net_pnl_twd'))} | "
            f"{_fmt(row.get('pnl_vs_a_twd'))} |"
        )
    lines.extend([
        "", "## Conditional still-holding cases (not part of A/R/U/RU PnL)", "",
        "These rows deliberately ignore an earlier A exit only to answer what R would have done if the position still existed. They are not improvements to the complete strategy.", "",
        "| Date | Symbol | Earlier A exit | Anchor | First observed | Observed px | R trigger | Trigger px | Fill | Fill px | Book confirmed |",
        "|---|---|---|---:|---|---:|---|---:|---|---:|---|",
    ])
    for row in report["conditional_still_holding"]:
        if not row["r_triggered"]:
            continue
        lines.append(
            f"| {row['session_date']} | {row['symbol']} | {_fmt(row['earlier_a_exit_time'])} | "
            f"{_fmt(row['anchor_high_price'])} | {_fmt(row['observation_time'])} | "
            f"{_fmt(row['observation_price'])} | {_fmt(row['trigger_time'])} | "
            f"{_fmt(row['trigger_price'])} | {_fmt(row['fill_time'])} | "
            f"{_fmt(row['fill_price'])} | {row['five_level_confirmed']} |"
        )
    post_hoc = [
        row for row in report["per_trade"]
        if row["post_hoc_case"] and row["latency_ms"] == 250
    ]
    lines.extend(["", "## 2026-10-02 3094 reproduction", ""])
    for row in post_hoc:
        lines.append(
            f"- {row['variant']}: {row['exit_reason']} at {row['trigger_time']}; "
            f"fill {row['filled_quantity']} shares at {row['fill_price']} on {row['fill_time']}; "
            f"net {row['net_pnl_twd']} TWD; pre-exit MFE {row['pre_exit_mfe_net_pnl_twd']} TWD; "
            f"giveback {row['profit_giveback_twd']} TWD."
        )
    unscorable = [
        row for row in report["per_trade"]
        if row["variant"] == "A" and row["net_pnl_twd"] is None
    ]
    lines.extend(["", "## Unscorable outcomes", ""])
    if unscorable:
        for row in unscorable:
            lines.append(
                f"- {row['session_date']} {row['symbol']} at {row['latency_ms']}ms: "
                f"{row['exit_status']}; remaining {row['remaining_quantity']} shares."
            )
    else:
        lines.append("- None.")
    lines.extend([
        "",
        "## Interpretation boundaries", "",
        "- Profit giveback measures MFE available before the actual simulated exit fill.",
        "- Post-exit opportunity measures later counterfactual upside and is reported separately.",
        "- NO_TRIGGER is retained as NO_TRIGGER; entries are never changed to manufacture a limit-up example.",
        "- 3094 on 2026-10-02 is explicitly post-hoc and cannot validate generalization.", "",
        "## Limitations", "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "controlled_profit_exit_results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(markdown_report(report), encoding="utf-8")
    summary_fields = list(report["summaries"][0].keys())
    with (output_dir / "controlled_profit_exit_summary.csv").open(
        "w", encoding="utf-8", newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=summary_fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(report["summaries"])
    trade_fields = [
        "trade_id", "session_date", "symbol", "stock_name", "source", "post_hoc_case",
        "variant", "latency_ms", "entry_time", "entry_price", "quantity",
        "official_limit_status", "official_upper_limit", "upper_limit_touch_status",
        "first_upper_limit_touch_time", "winning_trigger", "exit_status", "exit_reason",
        "trigger_time", "trigger_exchange_time", "trigger_price", "fill_time", "fill_price",
        "filled_quantity", "remaining_quantity", "net_pnl_twd", "partial_realized_pnl_twd",
        "pre_exit_mfe_net_pnl_twd", "profit_giveback_twd",
        "post_exit_best_counterfactual_net_pnl_twd", "post_exit_opportunity_twd",
        "pnl_vs_a_twd", "r_anchor_high_price", "r_observation_time",
        "r_observation_price", "r_triggered", "r_no_trigger_reason",
    ]
    with (output_dir / "controlled_profit_exit_per_trade.csv").open(
        "w", encoding="utf-8", newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=trade_fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(
            {field: row.get(field) for field in trade_fields} for row in report["per_trade"]
        )
    layer_fields = [
        "trade_id", "session_date", "symbol", "config", "confirmation_mode",
        "winning_trigger",
        "r_triggered", "r_no_trigger_reason", "net_pnl_twd",
        "profitable_peak", "prior_high_confirmed", "departed_prior_high_zone",
        "near_prior_high_observed",
        "price_bid_failure",
        "same_price_sell_pressure", "bid_depth_weakening",
        "buyer_flow_weakening", "positive_executable_net", "triggered",
    ]
    with (output_dir / "resistance_filter_impact.csv").open(
        "w", encoding="utf-8", newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=layer_fields, lineterminator="\n")
        writer.writeheader()
        for row in report["r_sensitivity"]:
            writer.writerow({
                **{field: row.get(field) for field in layer_fields},
                **{key: row["r_layers"].get(key, False) for key in layer_fields if key in row["r_layers"]},
            })
    conditional_fields = [
        "trade_id", "session_date", "symbol", "scope", "r_triggered",
        "r_no_trigger_reason", "earlier_a_exit_time", "anchor_high_price",
        "observation_time", "observation_price", "trigger_time", "trigger_price",
        "fill_time", "fill_price", "net_pnl_twd", "five_level_confirmed",
    ]
    with (output_dir / "conditional_still_holding.csv").open(
        "w", encoding="utf-8", newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=conditional_fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(
            {field: row.get(field) for field in conditional_fields}
            for row in report["conditional_still_holding"]
        )
    audit_fields = sorted({key for row in report["official_limit_audit"] for key in row})
    with (output_dir / "official_limit_audit.csv").open(
        "w", encoding="utf-8", newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=audit_fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(report["official_limit_audit"])
    artifact_names = (
        "controlled_profit_exit_results.json", "controlled_profit_exit_summary.csv",
        "controlled_profit_exit_per_trade.csv", "resistance_filter_impact.csv",
        "conditional_still_holding.csv", "official_limit_audit.csv", "report.md",
    )
    manifest = {
        "analysis_id": report["analysis_id"],
        "report_hash": report["report_hash"],
        "artifact_sha256": {
            name: _sha256(output_dir / name) for name in artifact_names
        },
        "production_behavior_changed": False,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
    }
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixed-entries", required=True, type=Path)
    parser.add_argument("--run", action="append", required=True, help="YYYYMMDD=/path/to/run")
    parser.add_argument("--official-cache", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    runs: dict[str, Path] = {}
    for item in args.run:
        day, separator, raw_path = item.partition("=")
        if not separator or len(day) != 8:
            parser.error("--run must be YYYYMMDD=/path/to/run")
        runs[day] = Path(raw_path).resolve()
    report = build_report(
        fixed_entries_path=args.fixed_entries.resolve(), runs_by_date=runs,
        official_cache=args.official_cache.resolve(),
    )
    write_report(report, args.output_dir.resolve())
    print(json.dumps(report["summaries"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
