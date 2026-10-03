#!/usr/bin/env python3
"""Guarded realtime runner for the Yuanta SPARK broker adapter.

This is the missing runtime layer between the sealed Stage A Top30 realtime quotes,
the frozen direction-following rule, and ``yuanta_broker_execution_v01``.
Production sends are impossible unless the broker adapter's existing three-way LIVE
gate is authorized and startup reconciliation has passed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import errno
import fcntl
import hashlib
from datetime import datetime, time as datetime_time, timedelta
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
import traceback
import uuid
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from yuanta_broker_execution_v01 import (
    APCode,
    BrokerOrderStatus,
    LiveOrderStore,
    LiveTradingGate,
    PriceType,
    StockOrderType,
    TimeInForce,
    YuantaSparkExecutionAdapter,
    bridge_strategy_intent,
    load_api_types,
)
from yuanta_intraday_shadow_v01.collector import (
    AppendOnlyRun,
    DEFAULT_RUNTIME_DIR as DEFAULT_ARCHIVE_RUNTIME_DIR,
    canonical_bytes,
    load_stage_a_watchlist,
    market_context_items,
    utc_now,
)
from yuanta_intraday_shadow_v01.collector_main import _book_payload, _quote_time
from yuanta_intraday_shadow_v01.main import DEFAULT_VENDOR_DIR as SHADOW_DEFAULT_VENDOR_DIR, _safe_text
from yuanta_intraday_shadow_v01.yuanta_keychain import load_credentials, status as credential_status

from .notifications import RuntimeNotifier
from .accounting import execution_pnl
from .trading_bot_notifier import AsyncTradingNotifier
from .risk_manager import RiskLimits, RiskManager
from .strategy import (
    ANTI_CHASE_ENTRY_POLICY,
    LIVE_EXIT_POLICY,
    LONG_MARKET_REGIME_POLICY,
    ExitDecision,
    LiveDirectionEngine,
    ManagedPosition,
    SPEC,
)
from .watchdog import Heartbeat, monitor as watchdog_monitor
from .account_lock import acquire_account_lock, release_account_lock

TAIPEI = ZoneInfo("Asia/Taipei")
MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_RUNTIME_DIR = MODULE_DIR / "runtime"
DEFAULT_VENDOR_DIR = Path(os.environ.get("YUANTA_SPARK_API_DIR", str(SHADOW_DEFAULT_VENDOR_DIR)))
LOGIN_CONNECT_WAIT_SECONDS = 5
LOGIN_RETRY_DELAYS = (5, 10, 20)
TERMINAL = {
    BrokerOrderStatus.FILLED,
    BrokerOrderStatus.CANCELED,
    BrokerOrderStatus.EXPIRED,
    BrokerOrderStatus.REJECTED,
}

ACTIVE_EXIT_STATUSES = {
    BrokerOrderStatus.SEND_PENDING,
    BrokerOrderStatus.ACKNOWLEDGED,
    BrokerOrderStatus.PARTIALLY_FILLED,
    BrokerOrderStatus.CANCEL_PENDING,
}

RUNTIME_LOCK_FILENAME = "runtime.lock"
FORCE_FLAT_MARKET_TIME = datetime_time(13, 23)
FORCE_FLAT_CLOSING_TIME = datetime_time(13, 25)
FORCE_FLAT_MARKET_CUTOFF = datetime_time(13, 29, 50)


def _force_flat_market_phase(now: datetime) -> str:
    local_time = now.astimezone(TAIPEI).time().replace(tzinfo=None)
    if local_time < FORCE_FLAT_MARKET_TIME:
        return "LIMIT"
    if local_time < FORCE_FLAT_CLOSING_TIME:
        return "MARKET"
    if local_time < FORCE_FLAT_MARKET_CUTOFF:
        return "CLOSING"
    return "CLOSED"


def _quote_universe(items):
    """Add the quote-only benchmark without changing the sealed Top30 archive."""
    result = list(items)
    benchmark = str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"])
    if not any(str(item.stock_id) == benchmark for item in result):
        result.append(SimpleNamespace(
            market=str(LONG_MARKET_REGIME_POLICY["benchmark_market"]),
            stock_id=benchmark,
            stock_name=str(LONG_MARKET_REGIME_POLICY["benchmark_name"]),
        ))
    return result


def _strategy_engine(items, *, capital_twd: int = 190_000) -> LiveDirectionEngine:
    benchmark = str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"])
    candidates = {str(item.stock_id) for item in items if str(item.stock_id) != benchmark}
    metadata = {str(item.stock_id): str(item.stock_name) for item in items}
    metadata[benchmark] = str(LONG_MARKET_REGIME_POLICY["benchmark_name"])
    return LiveDirectionEngine(
        metadata,
        capital_twd=capital_twd,
        candidate_symbols=candidates,
        benchmark_symbol=benchmark,
    )


def _acquire_runtime_instance_lock(runtime_dir: Path):
    """Hold one non-blocking OS lock for the lifetime of a realtime runtime.

    The lock file itself is not authoritative. A stale file after a crash is
    harmless because the kernel releases flock ownership when the process dies.
    """
    path = Path(runtime_dir) / RUNTIME_LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise RuntimeError(
                "another realtime runtime already owns the runtime instance lock"
            ) from None
        raise

    handle.seek(0)
    handle.truncate()
    handle.write(
        json.dumps(
            {"pid": os.getpid(), "acquired_at": utc_now()},
            sort_keys=True,
        )
        + "\n"
    )
    handle.flush()
    return handle


def _release_runtime_instance_lock(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _claim_account_lock(session, args, environment: str) -> None:
    """One account owner across every runtime directory and controller."""
    session._execution_account_lock = acquire_account_lock(
        session.account, environment,
        lock_root=getattr(args, "account_lock_root", None),
        runtime_dir=args.runtime_dir.resolve(),
    )


def _validate_runtime_intervals(args) -> None:
    for name, default in (
        ("reconcile_timeout", 20.0), ("quote_readiness_timeout", 20.0),
        ("order_reconcile_seconds", 5.0), ("entry_timeout", 15.0),
        ("exit_reprice_seconds", 5.0), ("exit_retry_base_seconds", 2.0),
        ("exit_retry_max_seconds", 30.0), ("max_quote_staleness", 30.0),
        ("entry_quote_staleness", 5.0), ("exit_quote_staleness", 3.0),
        ("reconnect_cooldown", 60.0),
    ):
        value = float(getattr(args, name, default))
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")


def _release_account_session_lock(session) -> None:
    handle = getattr(session, "_execution_account_lock", None)
    if handle is not None:
        release_account_lock(handle)
        session._execution_account_lock = None


def _reconcile_order_deadline(
    *, adapter, store, client_order_id: str, now: datetime,
    next_reconcile_at: datetime | None, timeout: float, interval: float,
) -> datetime:
    """Query an active order periodically, including accepted mutations.

    Elapsed time never proves that a broker operation failed or was cancelled.
    Only the adapter's authoritative snapshot may release a mutation barrier.
    """
    if next_reconcile_at is None or now >= next_reconcile_at:
        adapter.reconcile(timeout=timeout, strict_positions=True)
    return now + timedelta(seconds=interval) if next_reconcile_at is None or now >= next_reconcile_at else next_reconcile_at


def _entry_pre_send_guard(
    *, candidate, engine, store, runtime_dir: Path, max_age_seconds: float,
    now: datetime | None = None,
) -> None:
    """Revalidate admission at the actual send boundary after broker queries."""
    now = datetime.now(TAIPEI) if now is None else now.astimezone(TAIPEI)
    if now.date() != candidate.decision_time.astimezone(TAIPEI).date():
        raise RuntimeError("ENTRY_SIGNAL_SESSION_EXPIRED")
    if now.time().replace(tzinfo=None) > time_from_text(SPEC["last_entry_time"]):
        raise RuntimeError("ENTRY_WINDOW_CLOSED")
    if any((runtime_dir / name).exists() for name in (
        "EMERGENCY_STOP", "STOP_REQUEST", "FORCE_FLAT_REQUEST",
    )):
        raise RuntimeError("ENTRY_STOP_REQUESTED_BEFORE_SEND")
    if store.control_state()["halted"]:
        raise RuntimeError("ENTRY_HALTED_BEFORE_SEND")
    age = engine.quote_age_seconds(candidate.stock_id, now)
    if age is None or not math.isfinite(age) or age < 0 or age > max_age_seconds:
        raise RuntimeError("ENTRY_QUOTE_STALE_BEFORE_SEND")
    if not engine.entry_data_ready(candidate.stock_id, now, max_age_seconds=max_age_seconds):
        raise RuntimeError("ENTRY_BOOK_STALE_BEFORE_SEND")
    benchmark = engine.benchmark_symbol
    if benchmark and not engine.entry_data_ready(benchmark, now, max_age_seconds=max_age_seconds):
        raise RuntimeError("ENTRY_BENCHMARK_STALE_BEFORE_SEND")


def _exit_pre_send_guard(
    intent, *, position, engine, quote_time: datetime | None,
    max_age_seconds: float, now: datetime | None = None,
) -> None:
    """An owned EXIT still needs a current executable price and trading phase."""
    now = datetime.now(TAIPEI) if now is None else now.astimezone(TAIPEI)
    phase = _force_flat_market_phase(now)
    if phase == "CLOSED":
        raise RuntimeError("EXIT_MARKET_CUTOFF_REACHED_BEFORE_SEND")
    if position.side not in {"LONG", "SHORT"} or intent.purpose.value != "EXIT" or intent.side.value != (
        "SELL" if position.side == "LONG" else "BUY"
    ):
        raise RuntimeError("EXIT_DIRECTION_INVALID_BEFORE_SEND")
    if phase == "CLOSING":
        expected = PriceType.LIMIT_DOWN if intent.side.value == "SELL" else PriceType.LIMIT_UP
        if (intent.price_type != expected or intent.time_in_force != TimeInForce.ROD
                or intent.ap_code != APCode.REGULAR or intent.quantity % 1000
                or intent.price not in {None, Decimal("0")}):
            raise RuntimeError("EXIT_CLOSING_LIMIT_ROD_REQUIRED")
        return
    if intent.price_type == PriceType.MARKET:
        if (phase != "MARKET" or intent.time_in_force != TimeInForce.IOC
                or intent.ap_code != APCode.REGULAR or intent.quantity % 1000):
            raise RuntimeError("EXIT_MARKET_FALLBACK_NOT_DUE")
        return
    if phase == "MARKET":
        raise RuntimeError("EXIT_MARKET_FALLBACK_REQUIRED")
    if quote_time is None:
        raise RuntimeError("EXIT_CAPTURED_QUOTE_MISSING")
    age = (now - quote_time.astimezone(TAIPEI)).total_seconds()
    if not math.isfinite(age) or age < -0.5 or age > max_age_seconds:
        raise RuntimeError("EXIT_CAPTURED_QUOTE_STALE_BEFORE_SEND")
    if engine.safe_exit_quote(position, now, max_age_seconds=max_age_seconds) is None:
        raise RuntimeError("EXIT_EXECUTABLE_QUOTE_UNAVAILABLE_BEFORE_SEND")


def _wait_for_quote_readiness(
    engine, items, *, timeout_seconds: float = 20.0,
    max_age_seconds: float = 5.0,
) -> dict[str, Any]:
    """Require actual valid callbacks, not only subscription request acceptance."""
    deadline = time.monotonic() + timeout_seconds
    symbols = {str(item.stock_id) for item in items}
    benchmark = str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"])
    while True:
        now = datetime.now(TAIPEI)
        ready = {symbol for symbol in symbols if engine.entry_data_ready(
            symbol, now, max_age_seconds=max_age_seconds,
        )}
        candidate_ready = ready - {benchmark}
        if benchmark in ready and candidate_ready:
            return {"actual_quote_data": "READY", "ready_symbols": sorted(ready),
                    "candidate_ready_count": len(candidate_ready),
                    "subscribed_symbols": len(symbols)}
        if time.monotonic() >= deadline:
            raise RuntimeError("QUOTE_DATA_NOT_READY: benchmark and candidate ticks/books not received")
        time.sleep(0.05)


def _recovery_quote_items(runtime_dir: Path, store, current_items) -> list:
    """Use saved market metadata for owned exposure, never for new selection."""
    result = list(current_items)
    known = {str(item.stock_id) for item in result}
    path = runtime_dir / "quote_market_metadata.json"
    markets = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            markets = raw
    for order in store.orders():
        if order.symbol in known:
            continue
        if (not store.positions().get(order.symbol)
                and order.status in TERMINAL):
            continue
        meta = markets.get(order.symbol, {})
        market = meta.get("market") if isinstance(meta, dict) else None
        if market in {"TWSE", "TPEX"}:
            result.append(SimpleNamespace(stock_id=order.symbol,
                                          stock_name=str(meta.get("stock_name", order.symbol)),
                                          market=market))
            known.add(order.symbol)
    return result


def _classify_reconciled_exit(
    status: BrokerOrderStatus,
    remaining_quantity: int,
) -> str:
    """Decide what to do with an EXIT after authoritative reconciliation.

    CLOSED:
        No strategy exposure remains.

    TRACK_EXISTING:
        The original EXIT is still alive at the broker. Keep tracking it and
        never create a second NEW EXIT.

    RETRY_RESCUE:
        The old EXIT is definitely terminal but strategy exposure remains.

    UNKNOWN:
        Broker reconciliation still did not resolve the order safely.
    """
    remaining = int(remaining_quantity)

    if remaining <= 0:
        return "CLOSED"

    if status in ACTIVE_EXIT_STATUSES:
        return "TRACK_EXISTING"

    if status in {
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.CANCELED,
        BrokerOrderStatus.REJECTED,
        BrokerOrderStatus.EXPIRED,
    }:
        return "RETRY_RESCUE"

    return "UNKNOWN"



def _reconcile_failed_exit(
    *,
    adapter,
    store: LiveOrderStore,
    exit_order_id: str,
    position: ManagedPosition | None,
    reconcile_timeout: float,
) -> tuple[str, BrokerOrderStatus, int]:
    """Reconcile one failed/UNKNOWN EXIT against authoritative broker state.

    Returns:
        (action, reconciled_status, remaining_quantity)

    This function never submits another broker order. The caller may only
    schedule a rescue when action == "RETRY_RESCUE".
    """
    adapter.reconcile(
        timeout=reconcile_timeout,
        strict_positions=True,
    )

    reconciled_exit = store.get(exit_order_id)

    remaining = (
        abs(int(store.positions().get(position.stock_id, 0)))
        if position is not None
        else 0
    )

    action = _classify_reconciled_exit(
        reconciled_exit.status,
        remaining,
    )

    if action == "TRACK_EXISTING" and position is not None:
        position.quantity = remaining
        position.exit_submitted = True

    return action, reconciled_exit.status, remaining


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_baseline(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("baseline must be a JSON object")
    return {str(key).strip().upper(): int(value) for key, value in raw.items() if int(value)}


def _write_baseline(path: Path, positions: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(positions, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _baseline_meta_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.meta.json")


def _write_baseline_metadata(path: Path, *, account: str, captured_at: str) -> None:
    """Persist only non-secret provenance needed to prevent intraday rebasing."""
    stamp = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    payload = {
        "version": 1,
        "trading_date": stamp.astimezone(TAIPEI).date().isoformat(),
        "captured_at": captured_at,
        "account_fingerprint": hashlib.sha256(account.encode("utf-8")).hexdigest()[:12],
    }
    meta = _baseline_meta_path(path)
    tmp = meta.with_suffix(meta.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, meta)


def _load_baseline_metadata(path: Path) -> dict[str, Any] | None:
    meta = _baseline_meta_path(path)
    if not meta.is_file():
        return None
    raw = json.loads(meta.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or int(raw.get("version", 0)) != 1:
        raise ValueError("baseline metadata is invalid")
    datetime.fromisoformat(str(raw["captured_at"]).replace("Z", "+00:00"))
    datetime.fromisoformat(str(raw["trading_date"])).date()
    if len(str(raw.get("account_fingerprint", ""))) != 12:
        raise ValueError("baseline account fingerprint is invalid")
    return raw


def _baseline_is_current(path: Path, *, account: str, now: datetime) -> bool:
    if not path.is_file():
        return False
    meta = _load_baseline_metadata(path)
    if meta is None:
        return False
    expected = hashlib.sha256(account.encode("utf-8")).hexdigest()[:12]
    if str(meta["account_fingerprint"]) != expected:
        raise RuntimeError("baseline belongs to a different broker account")
    return str(meta["trading_date"]) == now.astimezone(TAIPEI).date().isoformat()


def _validate_watchlist_day(signal_date: str) -> None:
    calendar = Path(__file__).resolve().parents[1] / "shadow_daily_runner" / "runtime" / "trading_calendar.csv"
    if not calendar.is_file():
        raise RuntimeError("trading calendar is missing")
    import csv

    with calendar.open(encoding="utf-8", newline="") as handle:
        dates = [row["date"].replace("-", "") for row in csv.DictReader(handle)]
    today = datetime.now(TAIPEI).strftime("%Y%m%d")
    if today not in dates:
        raise RuntimeError(f"{today} is not present as a trading day")
    index = dates.index(today)
    if index == 0:
        raise RuntimeError("trading calendar has no prior session")
    expected = dates[index - 1]
    if signal_date != expected:
        raise RuntimeError(f"stale Stage A seal: expected {expected}, got {signal_date}")


def _extend_quote_types(api_types: dict[str, Any]) -> dict[str, Any]:
    """Load quote types from the same already-loaded Yuanta assembly."""
    from YuantaOneAPI import FiveTickA, StockTick, enumMarketType

    api_types.update({"FiveTickA": FiveTickA, "StockTick": StockTick, "Market": enumMarketType})
    return api_types


def _exchange_tick_time(value, received_at: datetime) -> datetime | None:
    """SPARK Time has no date; bind it to receipt day, reject invalid/future times.

    Serial high-water marks in the engine survive reconnects in this process.
    They do not substitute for broker sequence recovery after process restart.
    """
    rendered = _quote_time(value)
    if not rendered:
        return None
    try:
        clock = datetime.strptime(rendered, "%H:%M:%S.%f").time()
        return datetime.combine(received_at.astimezone(TAIPEI).date(), clock, tzinfo=TAIPEI)
    except ValueError:
        return None


class _Session:
    def __init__(
        self,
        *,
        api_types: dict[str, Any],
        environment: str,
        credentials: dict[str, str],
        engine: LiveDirectionEngine | None,
        logger,
        archive: AppendOnlyRun | None = None,
        archive_signal_date: str = "",
        archive_items: dict[str, Any] | None = None,
        non_archive_symbols: set[str] | None = None,
        strategy_symbols: set[str] | None = None,
    ):
        self.api_types = api_types
        self.environment = environment
        self.credentials = credentials
        self.engine = engine
        self.logger = logger
        self.archive = archive
        self.archive_signal_date = str(archive_signal_date)
        self.archive_items = dict(archive_items or {})
        self.non_archive_symbols = {str(symbol) for symbol in (non_archive_symbols or set())}
        self.market_context_items = {
            item.stock_id: item for item in market_context_items()
        }
        self.strategy_symbols = (
            None if strategy_symbols is None
            else {str(symbol) for symbol in strategy_symbols}
        )
        self.login_event = threading.Event()
        self.login_ok = False
        self.login_code = ""
        self.api = None
        self.account = credentials["account"]
        self.stock_list = None
        self.book_list = None
        self.subscribed = False
        self.last_quote_at: datetime | None = None
        self.quote_started_at: datetime | None = None
        self.subscription_generation = 0
        self.ingest_sequence = 0
        self._ingest_lock = threading.Lock()
        self._raw_quote_status: dict[str, dict[str, Any]] = {}
        self.archive_strategy_identity: dict[str, Any] = {}

    def _archive_quote(
        self,
        *,
        kind: str,
        symbol: str,
        value,
        payload: dict[str, Any] | None = None,
        callback_received_at: datetime | None = None,
        exchange_time: datetime | None = None,
        ingest_sequence: int | None = None,
        ingest_accepted: bool | None = None,
        ingest_reason: str = "NOT_PROCESSED_NO_ENGINE",
    ) -> None:
        """Mirror one already-received quote callback into the raw archive.

        Archiving shares the same Yuanta session/callback as the live strategy.
        An archive failure is recorded but must not prevent the strategy engine
        from consuming subsequent market data.
        """
        if self.archive is None:
            return

        is_market_context = symbol in self.non_archive_symbols
        item = (
            self.market_context_items.get(symbol)
            if is_market_context
            else self.archive_items.get(symbol)
        )
        if item is None:
            try:
                self.archive.callback_error(
                    "UNKNOWN_WATCHLIST_SYMBOL", callback_name=kind,
                    stock_id=symbol, phase="ARCHIVE_ROUTING",
                )
            except Exception:
                pass
            self.logger(
                "ARCHIVE_CALLBACK_ERROR",
                stock_id=symbol,
                error="UNKNOWN_WATCHLIST_SYMBOL",
            )
            return

        received = callback_received_at or datetime.now(TAIPEI)
        base = {
            "run_id": getattr(self.archive, "run_id", "TEST_OR_LEGACY_ARCHIVE"),
            "received_at": received.isoformat(),
            "callback_received_at": received.isoformat(),
            "subscription_generation": self.subscription_generation,
            "event_kind": "STOCK_TICK" if kind == "ticks" else "FIVE_LEVEL",
            "ingest_sequence": ingest_sequence,
            "ingest_accepted": ingest_accepted,
            "ingest_reason": ingest_reason,
            "exchange_time": exchange_time.isoformat() if exchange_time else None,
            "raw_serial_no": int(getattr(value, "SerialNo", 0) or 0),
            "signal_date": self.archive_signal_date,
            "stock_id": symbol,
            "stock_name": item.stock_name,
            "market": item.market,
            "stage_a_rank": None if is_market_context else item.rank,
            "stage_a_score": None if is_market_context else item.score,
            "role": "MARKET_BENCHMARK" if is_market_context else "STAGE_A_CANDIDATE",
        }

        try:
            if kind == "ticks":
                self.archive.append(
                    "market_context_ticks" if is_market_context else "ticks",
                    {
                        **base,
                        "event_type": "STOCK_TICK",
                        "quote_time": _quote_time(
                            getattr(value, "Time", None)
                        ),
                        "serial_no": int(
                            getattr(value, "SerialNo", 0)
                        ),
                        "buy_price": _safe_text(
                            getattr(value, "BuyPrice", "")
                        ),
                        "sell_price": _safe_text(
                            getattr(value, "SellPrice", "")
                        ),
                        "deal_price": _safe_text(
                            getattr(value, "DealPrice", "")
                        ),
                        "deal_volume": _safe_text(
                            getattr(value, "DealVol", "")
                        ),
                        "in_out_flag": _safe_text(
                            getattr(value, "InOutFlag", "")
                        ),
                        "tick_type": _safe_text(
                            getattr(value, "Type", "")
                        ),
                    },
                )
                return

            if kind == "books":
                self.archive.append(
                    "market_context_books" if is_market_context else "books",
                    {
                        **base,
                        "event_type": "FIVE_LEVEL",
                        **dict(payload or {}),
                    },
                )
                return

            raise ValueError(
                f"unsupported archive quote kind: {kind}"
            )

        except Exception as exc:
            try:
                if hasattr(self.archive, "observation_failure"):
                    self.archive.observation_failure(
                        phase="ARCHIVE_QUOTE_WRITE", stock_id=symbol,
                        event_kind=kind,
                    )
                else:
                    self.archive.callback_error()
            except Exception:
                pass
            self.logger(
                "ARCHIVE_CALLBACK_ERROR",
                stock_id=symbol,
                quote_kind=kind,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _archive_subscription(self, event: str, **payload: Any) -> None:
        if self.archive is None:
            return
        try:
            self.archive.append_subscription_evidence({
                "run_id": self.archive.run_id,
                "at": datetime.now(TAIPEI).isoformat(),
                "event": event,
                "subscription_generation": self.subscription_generation,
                **payload,
            })
        except Exception as exc:
            try:
                self.archive.observation_failure(
                    phase="SUBSCRIPTION_EVIDENCE_WRITE",
                    event_kind="SUBSCRIPTION",
                )
            except Exception:
                pass
            try:
                self.logger(
                    "ARCHIVE_OBSERVATION_ERROR",
                    phase="SUBSCRIPTION_EVIDENCE_WRITE",
                    error_type=type(exc).__name__,
                )
            except Exception:
                pass

    def archive_decision_evidence(
        self, decision: datetime, *, diagnostics: dict, candidate: Any | None,
    ) -> None:
        """Persist compact watermarks; failure never changes strategy/exit flow."""
        if self.archive is None or self.engine is None:
            return
        try:
            with self._ingest_lock:
                watermark = self.ingest_sequence
                raw_status = json.loads(json.dumps(self._raw_quote_status))
                engine_summary = self.engine.observation_state_summary(decision)
            event = {
                "run_id": self.archive.run_id,
                "decision_time": decision.isoformat(),
                "ingest_sequence_watermark": watermark,
                "subscription_generation": self.subscription_generation,
                "strategy_identity": self.archive_strategy_identity,
                "stage_a_identity": {
                    "signal_date": self.archive.snapshot["signal_date"],
                    "stage_a_seal_hash": self.archive.snapshot["stage_a_seal_hash"],
                },
                "raw_quote_status": raw_status,
                "engine_state_summary": engine_summary,
                "diagnostics": diagnostics,
                "selected_signal": None if candidate is None else asdict(candidate),
            }
            self.archive.append_decision_evidence(
                json.loads(json.dumps(event, default=str))
            )
        except Exception as exc:
            try:
                self.archive.observation_failure(
                    phase="DECISION_EVIDENCE_WRITE", event_kind="DECISION",
                )
            except Exception:
                pass
            try:
                self.logger(
                    "ARCHIVE_OBSERVATION_ERROR",
                    phase="DECISION_EVIDENCE_WRITE",
                    error_type=type(exc).__name__,
                )
            except Exception:
                pass

    def _on_response(self, int_mark, _index, response_name, _handle, value) -> None:
        name = _safe_text(response_name)
        try:
            if int(int_mark) == 1 and name == "Login":
                code = _safe_text(value.LoginStatus.MsgCode)
                self.login_code = code
                self.login_ok = code in {"0001", "00001"}
                self.login_event.set()
                self.logger("LOGIN", ok=self.login_ok, code=code, content=_safe_text(value.LoginStatus.MsgContent))
                return
            if int(int_mark) != 2 or self.engine is None:
                return
            now = datetime.now(TAIPEI)
            if name in {"SubscribeStockTick", "SubscribeStocktick"}:
                symbol = _safe_text(getattr(value, "StkCode", ""))
                stamp = _exchange_tick_time(getattr(value, "Time", None), now)
                with self._ingest_lock:
                    self.ingest_sequence += 1
                    sequence = self.ingest_sequence
                    if stamp is None:
                        outcome = SimpleNamespace(
                            accepted=False,
                            reason="ADAPTER_INVALID_EXCHANGE_TIME",
                        )
                    elif hasattr(self.engine, "ingest_tick"):
                        if hasattr(self.engine, "record_exit_quote"):
                            self.engine.record_exit_quote(
                                symbol, at=stamp, received_at=now,
                                bid=getattr(value, "BuyPrice", None),
                                ask=getattr(value, "SellPrice", None),
                            )
                        outcome = self.engine.ingest_tick(
                            symbol, at=stamp, received_at=now,
                            price=getattr(value, "DealPrice", ""),
                            volume=getattr(value, "DealVol", ""),
                            bid=getattr(value, "BuyPrice", ""),
                            ask=getattr(value, "SellPrice", ""),
                            flag=getattr(value, "InOutFlag", ""),
                            serial=getattr(value, "SerialNo", 0),
                        )
                    else:
                        accepted = bool(self.engine.record_tick(
                            symbol, at=stamp, received_at=now,
                            price=getattr(value, "DealPrice", ""),
                            volume=getattr(value, "DealVol", ""),
                            bid=getattr(value, "BuyPrice", ""),
                            ask=getattr(value, "SellPrice", ""),
                            flag=getattr(value, "InOutFlag", ""),
                            serial=getattr(value, "SerialNo", 0),
                        ))
                        outcome = SimpleNamespace(
                            accepted=accepted,
                            reason="ACCEPTED" if accepted else "LEGACY_ENGINE_REJECTED",
                        )
                    status = self._raw_quote_status.setdefault(symbol, {})
                    status["last_raw_tick_callback_at"] = now.isoformat()
                    status["last_raw_tick_exchange_time"] = (
                        stamp.isoformat() if stamp else None
                    )
                    status["last_tick_ingest_sequence"] = sequence
                    status["last_tick_ingest_accepted"] = bool(outcome.accepted)
                    status["last_tick_ingest_reason"] = str(outcome.reason)
                    status["subscription_generation"] = self.subscription_generation
                    if outcome.accepted:
                        status["last_accepted_tick_exchange_time"] = stamp.isoformat()
                        status["last_accepted_tick_received_at"] = now.isoformat()
                self._archive_quote(
                    kind="ticks", symbol=symbol, value=value,
                    callback_received_at=now, exchange_time=stamp,
                    ingest_sequence=sequence,
                    ingest_accepted=bool(outcome.accepted),
                    ingest_reason=str(outcome.reason),
                )
                if outcome.accepted and (
                    self.strategy_symbols is None or symbol in self.strategy_symbols
                ):
                    self.last_quote_at = now
                return
            if name == "SubscribeFiveTickA":
                symbol = _safe_text(getattr(value, "StkCode", ""))
                stamp = _exchange_tick_time(getattr(value, "Time", None), now)
                payload = _book_payload(value)
                with self._ingest_lock:
                    self.ingest_sequence += 1
                    sequence = self.ingest_sequence
                    outcome = SimpleNamespace(
                        accepted=False, reason="ADAPTER_BOOK_PAYLOAD_INCOMPLETE",
                    )
                    if all(key in payload for key in ("buy_prices", "buy_volumes", "sell_prices", "sell_volumes")):
                        if hasattr(self.engine, "ingest_book_combined"):
                            def executable(prices, volumes):
                                for price, volume in zip(prices, volumes):
                                    if self.engine._num(volume) is not None and float(volume) > 0:
                                        return price
                                return 0
                            self.engine.record_exit_quote(
                                symbol, at=stamp or now, received_at=now,
                                bid=executable(payload["buy_prices"], payload["buy_volumes"]),
                                ask=executable(payload["sell_prices"], payload["sell_volumes"]),
                            )
                            outcome = self.engine.ingest_book_combined(
                                symbol, at=now,
                                buy_prices=payload["buy_prices"],
                                buy_volumes=payload["buy_volumes"],
                                sell_prices=payload["sell_prices"],
                                sell_volumes=payload["sell_volumes"],
                            )
                        else:
                            self.engine.record_book_combined(
                                symbol, at=now,
                                buy_prices=payload["buy_prices"],
                                buy_volumes=payload["buy_volumes"],
                                sell_prices=payload["sell_prices"],
                                sell_volumes=payload["sell_volumes"],
                            )
                            outcome = SimpleNamespace(accepted=True, reason="ACCEPTED")
                    elif "prices" in payload and "volumes" in payload:
                        flag = str(payload.get("index_flag", ""))
                        side = "BUY" if "20" in flag else "SELL" if "21" in flag else ""
                        if side and hasattr(self.engine, "ingest_book_side"):
                            executable_price = next((price for price, volume in zip(
                                payload["prices"], payload["volumes"],
                            ) if self.engine._num(volume) is not None and float(volume) > 0), 0)
                            self.engine.record_exit_quote(
                                symbol, at=stamp or now, received_at=now,
                                **({"bid": executable_price} if side == "BUY" else {"ask": executable_price}),
                            )
                            outcome = self.engine.ingest_book_side(
                                symbol, at=now, side=side,
                                prices=payload["prices"], volumes=payload["volumes"],
                            )
                        elif side:
                            self.engine.record_book_side(
                                symbol, at=now, side=side,
                                prices=payload["prices"], volumes=payload["volumes"],
                            )
                            outcome = SimpleNamespace(accepted=True, reason="ACCEPTED")
                        else:
                            outcome = SimpleNamespace(
                                accepted=False, reason="ADAPTER_UNKNOWN_BOOK_SIDE",
                            )
                    status = self._raw_quote_status.setdefault(symbol, {})
                    status["last_raw_book_callback_at"] = now.isoformat()
                    status["last_raw_book_exchange_time"] = (
                        stamp.isoformat() if stamp else None
                    )
                    status["last_book_ingest_sequence"] = sequence
                    status["last_book_ingest_accepted"] = bool(outcome.accepted)
                    status["last_book_ingest_reason"] = str(outcome.reason)
                    status["subscription_generation"] = self.subscription_generation
                    if outcome.accepted:
                        status["last_accepted_book_received_at"] = now.isoformat()
                self._archive_quote(
                    kind="books", symbol=symbol, value=value, payload=payload,
                    callback_received_at=now, exchange_time=stamp,
                    ingest_sequence=sequence,
                    ingest_accepted=bool(outcome.accepted),
                    ingest_reason=str(outcome.reason),
                )
                if outcome.accepted and (self.strategy_symbols is None or symbol in self.strategy_symbols):
                    self.last_quote_at = now
        except Exception as exc:
            if self.archive is not None:
                try:
                    self.archive.callback_error(
                        type(exc).__name__, callback_name=name,
                        stock_id=str(locals().get("symbol", "")),
                        phase="QUOTE_CALLBACK",
                    )
                except Exception:
                    pass
            self.logger("QUOTE_CALLBACK_ERROR", error=f"{type(exc).__name__}: {exc}")

    def connect(self) -> None:
        last_reason = "login failed"
        attempts = len(LOGIN_RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            api = None
            try:
                self.login_event.clear()
                self.login_ok = False
                self.login_code = ""
                api = self.api_types["Trader"]()
                try:
                    api.SetLogType(self.api_types["LogType"].NONE)
                except Exception:
                    pass
                api.OnResponse += self._on_response
                api.Open(getattr(self.api_types["Environment"], self.environment))
                time.sleep(LOGIN_CONNECT_WAIT_SECONDS)
                accepted = bool(
                    api.Login(
                        self.credentials["pfx"],
                        self.credentials["pfx_password"],
                        self.account,
                        self.credentials["trading_password"],
                    )
                )
                if not accepted:
                    last_reason = "API rejected Login request"
                elif not self.login_event.wait(20):
                    last_reason = "Login callback timed out"
                elif not self.login_ok:
                    last_reason = f"Login callback failed code={self.login_code or 'UNKNOWN'}"
                else:
                    self.api = api
                    self.logger("CONNECTED", environment=self.environment, attempt=attempt)
                    return
            except Exception as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
            if api is not None:
                try:
                    api.Close()
                except Exception:
                    pass
                try:
                    api.Dispose()
                except Exception:
                    pass
            if attempt < attempts:
                delay = LOGIN_RETRY_DELAYS[attempt - 1]
                self.logger("LOGIN_RETRY", attempt=attempt, delay_seconds=delay, reason=last_reason)
                time.sleep(delay)
        raise RuntimeError(last_reason)

    def subscribe(self, items) -> None:
        if self.api is None:
            raise RuntimeError("session is not connected")
        self.subscription_generation += 1
        generation = self.subscription_generation
        self._archive_subscription(
            "SUBSCRIBE_BEGIN", symbol_count=len(items),
        )
        self.stock_list = self.api_types["List"][self.api_types["StockTick"]]()
        self.book_list = self.api_types["List"][self.api_types["FiveTickA"]]()
        self.last_quote_at = None
        self.quote_started_at = datetime.now(TAIPEI)
        if self.engine is not None and hasattr(self.engine, "reset_exit_quotes"):
            self.engine.reset_exit_quotes()
        markets = {"TWSE": self.api_types["Market"].TWSE, "TPEX": self.api_types["Market"].TWOTC}
        for item in items:
            stock = self.api_types["StockTick"]()
            stock.MarketType = markets[item.market]
            stock.StockCode = item.stock_id
            self.stock_list.Add(stock)
            book = self.api_types["FiveTickA"]()
            book.MarketType = markets[item.market]
            book.StockCode = item.stock_id
            self.book_list.Add(book)
        stock_ok = self.api.SubscribeStockTick(
            self.account, self.stock_list, self.api_types["Language"].UTF8
        )
        book_ok = self.api.SubscribeFiveTickA(
            self.account, self.book_list, self.api_types["Language"].UTF8
        )
        if stock_ok is False or book_ok is False:
            self._archive_subscription(
                "SUBSCRIBE_REJECTED", symbol_count=len(items),
                stock_api_result=str(stock_ok), book_api_result=str(book_ok),
            )
            raise RuntimeError("quote subscription was rejected by broker API")
        self.subscribed = True
        self._archive_subscription(
            "SUBSCRIBE_ACCEPTED", symbol_count=len(items),
            stock_api_result=str(stock_ok), book_api_result=str(book_ok),
        )
        self.logger(
            "QUOTES_SUBSCRIBED", count=len(items),
            subscription_generation=generation,
        )

    def close(self) -> None:
        api = self.api
        if api is None:
            return
        if self.subscribed:
            try:
                api.UnSubscribeStockTick(self.account, self.stock_list, self.api_types["Language"].UTF8)
            except Exception:
                pass
            self._archive_subscription("UNSUBSCRIBE_REQUESTED")
            try:
                api.UnSubscribeFiveTickA(self.account, self.book_list, self.api_types["Language"].UTF8)
            except Exception:
                pass
        try:
            api.OnResponse -= self._on_response
        except Exception:
            pass
        try:
            api.LogOut()
            time.sleep(0.5)
        except Exception:
            pass
        try:
            api.Close()
        except Exception:
            pass
        try:
            api.Dispose()
        except Exception:
            pass
        self.api = None
        self.subscribed = False
        self.stock_list = None
        self.book_list = None
        self.last_quote_at = None
        self.quote_started_at = None



def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(TAIPEI)


def _realized_pnl_today(store: LiveOrderStore, engine: LiveDirectionEngine, now: datetime) -> Decimal:
    return execution_pnl(store, now, SPEC).realized


def _confirm_strategy_flat(adapter, store, timeout: float) -> None:
    """No close-success event until the broker confirms baseline-only inventory."""
    adapter.reconcile(timeout=timeout, strict_positions=True)
    snapshot = adapter.inspect_broker_state(timeout=timeout)
    if (snapshot.positions != adapter.position_baseline or snapshot.open_orders
            or store.position_buckets() or store.orders(open_only=True)):
        store.halt("FLAT_CONFIRMATION_FAILED")
        raise RuntimeError("broker has remaining exposure or unresolved orders; flat not confirmed")


def _authoritative_cash_long_delta(
    adapter,
    store: LiveOrderStore,
    *,
    baseline: dict[str, int],
    symbol: str,
    timeout: float,
) -> int:
    """Return only broker-proven cash-long shares above the frozen baseline.

    The strict reconciliation also rejects any unexpected broker order before
    the 13:23 fallback can create a new sell.  A local/broker discrepancy is a
    hard failure, never a reason to guess a quantity.
    """
    result = adapter.reconcile(timeout=timeout, strict_positions=True)
    key = f"{symbol}|0"
    broker_delta = int(result.broker_positions.get(key, 0)) - int(baseline.get(key, 0))
    local_delta = int(store.position_buckets().get(key, 0))
    if broker_delta != local_delta:
        raise RuntimeError(
            "13:23 fallback refused: broker/local baseline delta mismatch"
        )
    if broker_delta < 0:
        raise RuntimeError(
            "13:23 fallback refused: actual cash inventory is below baseline"
        )
    return broker_delta


def _authoritative_fallback_delta(adapter, store, *, baseline, position, timeout) -> int:
    """Keep force-flat sizing broker-backed for both closing directions.

    LONG retains the existing cash-only contract. SHORT requires the entry's
    explicit supported bucket and negative broker/local ownership; it does not
    enable short entry or infer a cover order type.
    """
    if position.side == "LONG":
        return _authoritative_cash_long_delta(
            adapter, store, baseline=baseline, symbol=position.stock_id, timeout=timeout,
        )
    if position.side != "SHORT":
        raise RuntimeError("FORCE_FLAT_POSITION_SIDE_UNPROVEN")
    entry = store.get(position.entry_order_id)
    kinds = {"4": "4", "5": "6", "6": "6", "9": "0"}
    kind = kinds.get(entry.order_type.value)
    if kind is None or entry.symbol != position.stock_id or entry.side.value != "SELL":
        raise RuntimeError("FORCE_FLAT_SHORT_BUCKET_UNPROVEN")
    result = adapter.reconcile(timeout=timeout, strict_positions=True)
    key = f"{position.stock_id}|{kind}"
    actual = int(result.broker_positions.get(key, 0)) - int(baseline.get(key, 0))
    local = int(store.position_buckets().get(key, 0))
    if actual != local or actual > 0:
        raise RuntimeError("FORCE_FLAT_SHORT_BASELINE_DELTA_MISMATCH")
    return abs(actual)


def _checkpoint_position(store, position, pending_exit_reason) -> None:
    store.save_position_checkpoint(position.entry_order_id, {
        "version": 2,
        "stock_id": position.stock_id,
        "side": position.side,
        "peak_return": position.peak_return,
        "worst_return": position.worst_return,
        "reversal_streak": position.reversal_streak,
        "last_reversal_decision": (
            None if position.last_reversal_decision is None
            else position.last_reversal_decision.isoformat()
        ),
        "pending_exit_reason": pending_exit_reason,
        "initial_stop_price": position.initial_stop_price,
        "mfe_basis_entry_price": position.mfe_basis_entry_price,
        "mfe_basis_quantity": position.mfe_basis_quantity,
        "mfe_price": position.mfe_price,
        "mfe_pnl": position.mfe_pnl,
        "mfe_r": position.mfe_r,
        "mfe_time": None if position.mfe_time is None else position.mfe_time.isoformat(),
        "locked_profit_r": position.locked_profit_r,
        "locked_profit_price": position.locked_profit_price,
        "locked_profit_pnl": position.locked_profit_pnl,
        "mfe_protection_armed": position.mfe_protection_armed,
        "mfe_activation_time": (
            None if position.mfe_activation_time is None
            else position.mfe_activation_time.isoformat()
        ),
        "exit_policy_id": LIVE_EXIT_POLICY["policy_id"],
    })


def _trades_today(store: LiveOrderStore, now: datetime) -> int:
    today = now.astimezone(TAIPEI).date()
    return sum(
        1 for order in store.orders()
        if order.purpose.value == "ENTRY" and _stamp(order.created_at).date() == today
    )


def _recover_runtime_state(store: LiveOrderStore, metadata: dict[str, str], now: datetime, *, exit_only: bool = False) -> dict[str, Any]:
    """Rebuild one-trade/session state after a process restart without resending orders."""
    orders = store.orders()
    open_orders = store.orders(open_only=True)
    today = now.astimezone(TAIPEI).date()
    entries_today = [
        order for order in orders
        if order.purpose.value == "ENTRY" and _stamp(order.created_at).date() == today
    ]
    positions = store.positions()
    if len(positions) > 1:
        store.halt("MULTIPLE_STRATEGY_POSITIONS_ON_RECOVERY")
        raise RuntimeError(f"runtime supports one strategy position, found {positions}")

    state: dict[str, Any] = {
        "entry_order_id": None,
        "entry_signal": None,
        "entry_submitted_at": None,
        "position": None,
        "exit_order_id": None,
        "trade_attempted": bool(entries_today),
        "pending_exit_reason": None,
    }

    if positions:
        symbol, signed_quantity = next(iter(positions.items()))
        entry_candidates = [
            order for order in orders
            if order.purpose.value == "ENTRY" and order.symbol == symbol and order.filled_quantity > 0
        ]
        if not entry_candidates:
            store.halt("POSITION_WITHOUT_ENTRY_ORDER")
            raise RuntimeError(f"cannot recover strategy position without entry order: {symbol}")
        entry = entry_candidates[-1]
        if entry.average_fill_price is None:
            store.halt("POSITION_WITHOUT_ENTRY_AVERAGE_PRICE")
            raise RuntimeError(f"cannot recover strategy position without average fill price: {symbol}")
        direction = "LONG" if entry.side.value == "BUY" else "SHORT"
        entered_at = _stamp(entry.created_at)
        position = ManagedPosition(
            stock_id=symbol,
            stock_name=metadata.get(symbol, symbol),
            side=direction,
            quantity=abs(int(signed_quantity)),
            entry_price=float(entry.average_fill_price),
            entry_order_id=entry.client_order_id,
            entry_time=entered_at,
        )
        state.update({
            "entry_order_id": entry.client_order_id,
            "entry_signal": SimpleNamespace(
                stock_id=symbol, stock_name=metadata.get(symbol, symbol), side=direction,
                decision_time=entered_at, entry_price=float(entry.price or entry.average_fill_price),
                quantity=entry.quantity,
            ),
            "entry_submitted_at": entered_at,
            "position": position,
            "trade_attempted": True,
        })
        checkpoint = store.position_checkpoint(entry.client_order_id)
        if checkpoint is None:
            # Legacy/first-fill crash: do not invent the missing extrema.
            state["pending_exit_reason"] = "MISSING_POSITION_CHECKPOINT"
        else:
            try:
                version = int(checkpoint["version"])
                if (version not in {1, 2} or checkpoint["stock_id"] != symbol
                        or checkpoint["side"] != direction):
                    raise ValueError("checkpoint identity mismatch")
                peak = float(checkpoint["peak_return"])
                worst = float(checkpoint["worst_return"])
                if not (Decimal(str(peak)).is_finite() and Decimal(str(worst)).is_finite()
                        and peak >= 0 and worst <= 0):
                    raise ValueError("invalid checkpoint extrema")
                position.peak_return = peak
                position.worst_return = worst
                position.reversal_streak = max(0, int(checkpoint["reversal_streak"]))
                stamp = checkpoint["last_reversal_decision"]
                position.last_reversal_decision = None if stamp is None else _stamp(stamp)
                state["pending_exit_reason"] = checkpoint["pending_exit_reason"]
                if version == 1:
                    # A prior runtime never persisted the MFE floor. Do not
                    # invent a lower floor after restart; request a safe exit.
                    state["pending_exit_reason"] = (
                        state["pending_exit_reason"]
                        or "MFE_STATE_UNAVAILABLE_AFTER_UPGRADE"
                    )
                else:
                    if checkpoint.get("exit_policy_id") != LIVE_EXIT_POLICY["policy_id"]:
                        raise ValueError("checkpoint exit policy mismatch")
                    numeric = {
                        key: checkpoint.get(key)
                        for key in (
                            "initial_stop_price", "mfe_basis_entry_price",
                            "mfe_price", "mfe_pnl", "mfe_time",
                            "locked_profit_price", "locked_profit_pnl",
                            "mfe_activation_time",
                        )
                    }
                    position.mfe_basis_quantity = int(checkpoint["mfe_basis_quantity"])
                    position.mfe_r = float(checkpoint["mfe_r"])
                    position.locked_profit_r = float(checkpoint["locked_profit_r"])
                    position.mfe_protection_armed = bool(checkpoint["mfe_protection_armed"])
                    for key in (
                        "initial_stop_price", "mfe_basis_entry_price", "mfe_price",
                        "mfe_pnl", "locked_profit_price", "locked_profit_pnl",
                    ):
                        value = numeric[key]
                        setattr(position, key, None if value is None else float(value))
                    position.mfe_time = (
                        None if numeric["mfe_time"] is None else _stamp(numeric["mfe_time"])
                    )
                    position.mfe_activation_time = (
                        None if numeric["mfe_activation_time"] is None
                        else _stamp(numeric["mfe_activation_time"])
                    )
                    finite = (
                        Decimal(str(value)).is_finite()
                        for value in (
                            position.mfe_r, position.locked_profit_r,
                            *(
                                value for value in (
                                    position.initial_stop_price,
                                    position.mfe_basis_entry_price,
                                    position.mfe_price,
                                    position.mfe_pnl,
                                    position.locked_profit_price,
                                    position.locked_profit_pnl,
                                ) if value is not None
                            ),
                        )
                    )
                    if (
                        not all(finite)
                        or position.mfe_basis_quantity < 0
                        or position.mfe_r < 0
                        or position.locked_profit_r < 0
                        or (
                            position.mfe_protection_armed
                            and (
                                position.locked_profit_price is None
                                or position.mfe_activation_time is None
                            )
                        )
                    ):
                        raise ValueError("invalid MFE checkpoint")
            except (KeyError, ValueError, TypeError):
                store.halt("INVALID_POSITION_CHECKPOINT")
                if not exit_only:
                    raise RuntimeError("position checkpoint is invalid; manual reconciliation required") from None
                # Caller has already reconciled actual broker exposure. A bad
                # strategy checkpoint may not obstruct owned risk reduction:
                # rebuild only fill-derived cost/quantity, never resume MFE or
                # invent extrema, and preserve the persistent HALT.
                position = ManagedPosition(
                    stock_id=symbol, stock_name=metadata.get(symbol, symbol),
                    side=direction, quantity=abs(int(signed_quantity)),
                    entry_price=float(entry.average_fill_price),
                    entry_order_id=entry.client_order_id, entry_time=entered_at,
                )
                state["position"] = position
                state["pending_exit_reason"] = "INVALID_POSITION_CHECKPOINT"
        open_exits = [
            order for order in open_orders
            if order.purpose.value == "EXIT" and order.symbol == symbol
        ]
        if len(open_exits) > 1:
            store.halt("MULTIPLE_OPEN_EXIT_ORDERS_ON_RECOVERY")
            raise RuntimeError("multiple open exit orders found during recovery")
        if open_exits:
            state["exit_order_id"] = open_exits[-1].client_order_id
            position.exit_submitted = True
        if entered_at.date() < today and not open_exits:
            state["pending_exit_reason"] = "CARRYOVER_POSITION"
        return state

    if open_orders:
        open_entries = [order for order in open_orders if order.purpose.value == "ENTRY"]
        open_exits = [order for order in open_orders if order.purpose.value == "EXIT"]
        if open_exits:
            store.halt("OPEN_EXIT_WITHOUT_STRATEGY_POSITION")
            raise RuntimeError("open EXIT order exists but local strategy position is flat")
        if len(open_entries) != 1:
            store.halt("UNEXPECTED_OPEN_ORDER_SET_ON_RECOVERY")
            raise RuntimeError(f"unexpected open orders during recovery: {len(open_orders)}")
        entry = open_entries[0]
        entered_at = _stamp(entry.created_at)
        direction = "LONG" if entry.side.value == "BUY" else "SHORT"
        state.update({
            "entry_order_id": entry.client_order_id,
            "entry_signal": SimpleNamespace(
                stock_id=entry.symbol, stock_name=metadata.get(entry.symbol, entry.symbol), side=direction,
                decision_time=entered_at, entry_price=float(entry.price or 0), quantity=entry.quantity,
            ),
            "entry_submitted_at": entered_at,
            "trade_attempted": True,
        })
    return state


def _order_type(value: str | None) -> StockOrderType | None:
    if value in {None, ""}:
        return None
    return StockOrderType(str(value))


def _as_market_fallback(intent):
    """Convert an approved exposure-reducing intent to the 13:23 fallback.

    Regular-board positions are always whole lots in this strategy.  Refuse a
    mixed/odd quantity rather than silently sending it with the wrong Yuanta
    APCode; the supervisor will keep the incident critical and retry only after
    broker reconciliation proves a valid remaining quantity.
    """
    quantity = int(intent.quantity)
    if intent.purpose.value != "EXIT":
        raise RuntimeError("fallback is restricted to exposure-reducing EXIT intents")
    if quantity % 1000 or intent.ap_code != APCode.REGULAR:
        raise RuntimeError("FORCE_FLAT_ODD_LOT_UNSUPPORTED: no verified market/IOC odd-lot route")
    return replace(
        intent,
        price=None,
        price_type=PriceType.MARKET,
        time_in_force=TimeInForce.IOC,
        ap_code=APCode.REGULAR,
    )


def _as_closing_fallback(intent):
    """Use SPARK's documented L/H price flags, not a guessed daily price.

    Yuanta SendStockOrder documents H=limit-up, L=limit-down, Price=0 for
    non-numeric price flags and Time_in_force=0 for ROD. TWSE/TPEx accept
    limit ROD during the 13:25 closing auction, not MARKET/IOC/FOK.
    Only already approved, regular-board whole-lot EXIT intents are adapted.
    """
    if intent.purpose.value != "EXIT":
        raise RuntimeError("closing fallback is restricted to EXIT intents")
    if intent.quantity % 1000 or intent.ap_code != APCode.REGULAR:
        raise RuntimeError("FORCE_FLAT_ODD_LOT_UNSUPPORTED: closing auction route is not verified")
    return replace(
        intent, price=None,
        price_type=PriceType.LIMIT_DOWN if intent.side.value == "SELL" else PriceType.LIMIT_UP,
        time_in_force=TimeInForce.ROD, ap_code=APCode.REGULAR,
    )


def _fallback_exit_replacement_required(order, phase: str) -> bool:
    """Never re-cancel an already compatible closing ROD every loop.

    The caller must cancel an incompatible ACTIVE order and wait for terminal
    broker reconciliation before another NEW order is permitted.
    """
    if phase == "MARKET":
        return (order.price_type != PriceType.MARKET or order.time_in_force != TimeInForce.IOC
                or order.ap_code != APCode.REGULAR or order.quantity % 1000 != 0)
    if phase == "CLOSING":
        expected = PriceType.LIMIT_DOWN if order.side.value == "SELL" else PriceType.LIMIT_UP
        return (order.price_type != expected or order.time_in_force != TimeInForce.ROD
                or order.ap_code != APCode.REGULAR or order.quantity % 1000 != 0)
    return False


def _cancel_incompatible_fallback_exit(adapter, store, order, phase: str) -> bool:
    """Cancel once and keep the old order authoritative until reconciliation.

    A CANCEL_PENDING status itself is a barrier even if a request cache is
    unavailable. Missing broker identity never permits a guessed cancel/new.
    """
    if (order.status not in ACTIVE_EXIT_STATUSES
            or order.status == BrokerOrderStatus.CANCEL_PENDING
            or not _fallback_exit_replacement_required(order, phase)
            or store.pending_mutation(order.client_order_id) is not None):
        return False
    if not order.broker_order_no:
        raise RuntimeError("FALLBACK_ACTIVE_ORDER_IDENTITY_UNPROVEN")
    adapter.cancel(
        order.client_order_id,
        "13:25 closing auction fallback" if phase == "CLOSING" else "13:23 market fallback",
        emergency=True,
    )
    return True


def _decision_floor(now: datetime) -> datetime:
    step = 30
    second = (now.second // step) * step
    return now.replace(second=second, microsecond=0)


def _store_archive_counters(store: LiveOrderStore) -> dict[str, int]:
    """Return non-sensitive durable execution counters for archive metadata."""
    with store._lock:
        new_requests = int(
            store.connection.execute(
                "SELECT COUNT(*) FROM broker_requests WHERE operation='NEW'"
            ).fetchone()[0]
        )
        fills = int(
            store.connection.execute(
                "SELECT COUNT(*) FROM live_fills"
            ).fetchone()[0]
        )
        requests = int(
            store.connection.execute(
                "SELECT COUNT(*) FROM broker_requests"
            ).fetchone()[0]
        )
    return {
        "new_requests": new_requests,
        "fills": fills,
        "requests": requests,
    }


def _archive_counter_delta(
    before: dict[str, int],
    after: dict[str, int],
) -> dict[str, int]:
    return {
        key: max(
            0,
            int(after.get(key, 0))
            - int(before.get(key, 0)),
        )
        for key in (
            "new_requests",
            "fills",
            "requests",
        )
    }


def _post_close_action(
    *,
    emergency: bool,
    graceful_stop: bool,
    scheduled_force_flat: bool = False,
) -> str:
    """Decide whether a flat runtime stops or continues quote archiving."""
    if emergency:
        return "STOP_EMERGENCY"
    if scheduled_force_flat:
        return "STOP_FORCE_FLAT"
    if graceful_stop:
        return "STOP_GRACEFUL"
    return "CONTINUE_ARCHIVE"


def _signal_identifier(signal_date: str, candidate) -> str:
    decision = candidate.decision_time.astimezone(
        TAIPEI
    ).replace(microsecond=0)

    return (
        f"{signal_date}:"
        f"{decision.isoformat()}:"
        f"{candidate.stock_id}:"
        f"{candidate.side}"
    )


def _run_realtime(args, *, environment: str, submit_live: bool) -> int:
    _validate_runtime_intervals(args)
    runtime_dir = args.runtime_dir.resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = runtime_dir / "session.jsonl"
    signal_ledger_path = runtime_dir / "signal-ledger.jsonl"
    kill_path = runtime_dir / "EMERGENCY_STOP"
    stop_path = runtime_dir / "STOP_REQUEST"
    force_flat_path = runtime_dir / "FORCE_FLAT_REQUEST"
    baseline_path = args.baseline.resolve()
    db_path = runtime_dir / "live-orders.sqlite"
    notifier = RuntimeNotifier(runtime_dir)
    trading_notifier = None
    notification_initialization_failed = False
    try:
        trading_notifier = AsyncTradingNotifier(runtime_dir)
    except Exception as exc:
        # An observer must not prevent an already-owned position from reaching
        # the risk-reduction controller. New entries remain disabled.
        notification_initialization_failed = True
        try:
            notifier.critical("TRADING_NOTIFICATION_INITIALIZATION_FAILED",
                              "Trading notification outbox unavailable; running exit-only recovery",
                              error_type=type(exc).__name__)
        except Exception:
            pass
    heartbeat = Heartbeat(runtime_dir)
    gate = LiveTradingGate.from_environment(cli_live=bool(args.live))
    if submit_live and not gate.authorized:
        raise RuntimeError("LIVE start requested but broker gate is not authorized")

    observer_failures: set[str] = set()

    def observer_failure(code: str, exc: Exception) -> None:
        nonlocal operational_exit_only
        operational_exit_only = True
        # Core order-store failures are never ignored by the adapter. This
        # best-effort HALT concerns isolated, nonessential observer failures.
        try:
            store.halt(code)
        except Exception:
            pass
        if code not in observer_failures:
            observer_failures.add(code)
            try:
                notifier.critical(code, "Observer unavailable; entries disabled, broker-backed owned-exposure recovery remains active",
                                  error_type=type(exc).__name__)
            except Exception:
                pass

    def beat(state: str, **details: Any) -> None:
        try:
            heartbeat.beat(state, **details)
        except Exception as exc:
            observer_failure("RUNTIME_HEARTBEAT_WRITE_FAILED", exc)

    def archive_decision_evidence(*values: Any, **details: Any) -> None:
        try:
            session.archive_decision_evidence(*values, **details)
        except Exception as exc:
            observer_failure("RUNTIME_ARCHIVE_OBSERVER_FAILED", exc)

    def log(event: str, **payload: Any) -> None:
        row = {"at": utc_now(), "event": event, **payload}
        try:
            _append_jsonl(log_path, row)
            if event in {"SIGNAL_DETECTED", "SIGNAL_SKIPPED", "ENTRY_GATE_DIAGNOSTICS"}:
                _append_jsonl(signal_ledger_path, row)
        except Exception as exc:
            observer_failure("RUNTIME_LOG_WRITE_FAILED", exc)
        try:
            print(json.dumps(row, ensure_ascii=False, default=str), flush=True)
        except Exception as exc:
            observer_failure("RUNTIME_STDOUT_FAILED", exc)
        if trading_notifier is not None:
            try:
                trading_notifier.emit(event, row)
            except Exception as exc:
                observer_failure("TRADING_NOTIFICATION_PERSISTENCE_FAILED", exc)

    if kill_path.exists() and not args.recover_emergency:
        raise RuntimeError(
            f"persistent emergency stop is active: {kill_path}; "
            "use start-uat/start-prod --recover-emergency --live to run exit-only recovery"
        )

    recovery_only = bool(submit_live and (
        (getattr(args, "recover_emergency", False) and kill_path.exists())
        or (getattr(args, "recover_force_flat", False) and force_flat_path.exists())
    ))
    if submit_live and (getattr(args, "recover_emergency", False) or getattr(args, "recover_force_flat", False)) and not recovery_only:
        raise RuntimeError("exit-only recovery requires its durable stop/force-flat marker")
    if stop_path.exists() and not recovery_only:
        raise RuntimeError(
            f"persistent graceful stop request is active: {stop_path}; "
            "refusing realtime startup until the stop request is resolved"
        )

    if recovery_only:
        # Risk reduction uses durable owned orders plus the reviewed baseline.
        # A missing/stale research seal must never prevent that recovery.
        seal, items, provenance = (
            {"signal_date": datetime.now(TAIPEI).strftime("%Y%m%d")}, [],
            {"mode": "EXIT_ONLY_RECOVERY_NO_RESEARCH_PREREQUISITE"},
        )
    else:
        seal, items, provenance = load_stage_a_watchlist()
        _validate_watchlist_day(str(seal["signal_date"]))
    metadata = {item.stock_id: item.stock_name for item in items}
    quote_items = _quote_universe(items)
    benchmark_symbol = str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"])
    strategy_symbols = {str(item.stock_id) for item in items if str(item.stock_id) != benchmark_symbol}
    engine = _strategy_engine(items, capital_twd=args.capital)
    risk = RiskManager(RiskLimits(
        max_daily_loss=Decimal(str(args.max_daily_loss)),
        max_order_value=Decimal(str(args.max_order_value)),
        max_position_per_stock=(
            None if int(args.max_position_per_stock) <= 0
            else int(args.max_position_per_stock)
        ),
        max_concurrent_positions=int(args.max_concurrent_positions),
        max_trades_per_day=int(args.max_trades_per_day),
        stale_quote_seconds=Decimal(str(args.entry_quote_staleness)),
    ))
    credentials = load_credentials()
    api_types = _extend_quote_types(load_api_types(args.vendor_dir.resolve()))
    session = _Session(
        api_types=api_types,
        environment=environment,
        credentials=credentials,
        engine=engine,
        logger=log,
        non_archive_symbols={benchmark_symbol},
        strategy_symbols=strategy_symbols,
    )
    short_entry = _order_type(args.short_entry_order_type)
    short_cover = _order_type(args.short_cover_order_type)
    allow_short = short_entry is not None and short_cover is not None
    baseline = _load_baseline(baseline_path)
    stop_event = threading.Event()

    def stop_handler(_signum, _frame):
        stop_event.set()
        try:
            kill_path.write_text("SIGNAL_STOP\n", encoding="utf-8")
        except Exception:
            pass

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    adapter = None
    store = LiveOrderStore(db_path)
    entry_order_id: str | None = None
    entry_signal = None
    entry_submitted_at: datetime | None = None
    entry_cancel_requested = False
    pending_exit_reason: str | None = None
    position: ManagedPosition | None = None
    exit_order_id: str | None = None
    last_exit_reprice: datetime | None = None
    trade_attempted = False
    quote_stale_triggered = False
    held_quote_stale_triggered = False
    last_reconnect_at: datetime | None = None
    exit_attempt = 0
    next_exit_retry_at: datetime | None = None
    next_exit_reconcile_at: datetime | None = None
    next_entry_reconcile_at: datetime | None = None
    next_entry_cancel_at: datetime | None = None
    operational_exit_only = recovery_only or notification_initialization_failed
    runtime_instance_id = uuid.uuid4().hex
    recovery_error_count = 0
    exit_quote_alerted = False
    market_fallback_cancel_requested = False
    market_cutoff_alerted = False
    clean_shutdown = False
    archive = None
    archive_started_at = ""
    archive_error_type = ""
    broker_flat_confirmed_at: str | None = None
    failure_code = ""
    startup_stage = "LOCAL_SETUP"
    archive_counter_baseline = _store_archive_counters(store)

    # Acquire after local setup but before session.connect(), so a second
    # start/observe process is rejected before it can touch the broker.
    try:
        runtime_lock = _acquire_runtime_instance_lock(runtime_dir)
    except Exception:
        credentials.update({"pfx_password": "", "trading_password": ""})
        try:
            trading_notifier.close(timeout=3.0)
        except Exception:
            pass
        try:
            session.close()
        except Exception:
            pass
        store.close()
        raise

    try:
        _claim_account_lock(session, args, environment)
        beat(
            "STARTING",
            environment=environment,
            submit_live=submit_live,
            signal_date=seal["signal_date"],
            watchlist_count=len(items),
            entry_start=SPEC["entry_start"],
            gate=gate.public_snapshot(),
            startup_stage="BROKER_CONNECT",
            runtime_instance_id=runtime_instance_id,
            trading_date=datetime.now(TAIPEI).date().isoformat(),
            account_lock_path=str(session._execution_account_lock.name),
            account_lock_instance=getattr(session._execution_account_lock, "instance_id", ""),
        )
        archive_started_at = utc_now()
        archive = None if recovery_only else AppendOnlyRun(
            args.archive_runtime_dir.resolve(),
            seal,
            items,
            provenance,
            compress=True,
            mode=(
                "LIVE_TRADING_QUOTES"
                if submit_live
                else "OBSERVE_ONLY_QUOTES"
            ),
        )
        session.archive = archive
        session.archive_signal_date = str(
            seal["signal_date"]
        )
        session.archive_items = {
            item.stock_id: item
            for item in items
        }
        strategy_identity_payload = {
            "entry_spec": SPEC,
            "market_regime_policy": LONG_MARKET_REGIME_POLICY,
            "anti_chase_policy": ANTI_CHASE_ENTRY_POLICY,
            "exit_policy": LIVE_EXIT_POLICY,
        }
        session.archive_strategy_identity = {
            "entry_policy_id": LONG_MARKET_REGIME_POLICY["policy_id"],
            "anti_chase_policy_id": ANTI_CHASE_ENTRY_POLICY["policy_id"],
            "exit_policy_id": LIVE_EXIT_POLICY["policy_id"],
            "config_hash": hashlib.sha256(
                canonical_bytes(strategy_identity_payload)
            ).hexdigest(),
        }

        if archive is not None:
            log(
                "ARCHIVE_STARTED",
                run_id=archive.run_id,
                mode=archive.mode,
                run_dir=str(archive.run_dir),
            )

        startup_stage = "BROKER_CONNECT"
        session.connect()
        startup_stage = "BROKER_CONNECTED"
        assert session.api is not None
        baseline_current = _baseline_is_current(
            baseline_path,
            account=session.account,
            now=datetime.now(TAIPEI),
        ) if submit_live else True
        if submit_live and not baseline_current and not recovery_only:
            failure_code = "DAILY_BASELINE_NOT_READY"
            raise RuntimeError(
                "LIVE runtime requires today's account-scoped position baseline"
            )
        if recovery_only and not baseline_current:
            meta = _load_baseline_metadata(baseline_path)
            expected = hashlib.sha256(session.account.encode("utf-8")).hexdigest()[:12]
            if meta is None or meta.get("account_fingerprint") != expected:
                raise RuntimeError("exit-only recovery requires a reviewed account-scoped baseline")
        adapter = YuantaSparkExecutionAdapter(
            api=session.api,
            api_types=api_types,
            account=session.account,
            store=store,
            live_gate=gate,
            position_baseline=baseline,
        )
        startup_stage = "BROKER_RECONCILIATION"
        reconciliation = adapter.reconcile(timeout=args.reconcile_timeout, strict_positions=True)
        startup_stage = "BROKER_RECONCILED"
        log("RECONCILIATION_PASSED", result=asdict(reconciliation), gate=gate.public_snapshot())
        recovery_authorized = submit_live and (
            (args.recover_emergency and kill_path.exists())
            or (args.recover_force_flat and force_flat_path.exists())
        )
        if store.control_state()["halted"] and not (recovery_authorized or operational_exit_only):
            failure_code = "BROKER_EXECUTION_HALTED"
            raise RuntimeError(f"broker execution store is halted: {store.control_state().get('reason')}")
        startup_stage = "LOCAL_STATE_RECOVERY"
        recovered = _recover_runtime_state(store, metadata, datetime.now(TAIPEI), exit_only=recovery_only or operational_exit_only)
        if store.control_state()["halted"]:
            operational_exit_only = True
        entry_order_id = recovered["entry_order_id"]
        entry_signal = recovered["entry_signal"]
        entry_submitted_at = recovered["entry_submitted_at"]
        position = recovered["position"]
        exit_order_id = recovered["exit_order_id"]
        trade_attempted = recovered["trade_attempted"]
        pending_exit_reason = recovered["pending_exit_reason"]
        quote_items = _recovery_quote_items(runtime_dir, store, quote_items)
        for item in quote_items:
            engine.add_monitor_symbol(str(item.stock_id), str(item.stock_name))
        if not recovery_only:
            metadata_path = runtime_dir / "quote_market_metadata.json"
            metadata_path.write_text(json.dumps({str(item.stock_id): {
                "stock_name": str(item.stock_name), "market": str(item.market),
            } for item in quote_items}, sort_keys=True) + "\n", encoding="utf-8")
        if position is not None and not any(str(item.stock_id) == position.stock_id for item in quote_items):
            notifier.critical("RECOVERED_POSITION_MARKET_METADATA_MISSING",
                              "Owned exposure lacks quote market metadata; entry is disabled, quote-independent forced-flat recovery remains available",
                              stock_id=position.stock_id)
            operational_exit_only = True
        if any(value is not None and value is not False for key, value in recovered.items() if key not in {"trade_attempted"}):
            log("RUNTIME_STATE_RECOVERED", state={
                "entry_order_id": entry_order_id,
                "position": None if position is None else {"stock_id": position.stock_id, "side": position.side, "quantity": position.quantity, "entry_price": position.entry_price},
                "exit_order_id": exit_order_id,
                "trade_attempted": trade_attempted,
                "pending_exit_reason": pending_exit_reason,
            })
        startup_stage = "QUOTE_SUBSCRIPTION"
        try:
            session.subscribe(quote_items)
        except Exception:
            if not (recovery_only or operational_exit_only or position is not None or entry_order_id is not None or exit_order_id is not None):
                raise
            operational_exit_only = True
            notifier.critical("EXIT_ONLY_QUOTE_SUBSCRIPTION_FAILED",
                              "Quote subscription failed during owned-exposure recovery; broker reconciliation and scheduled market fallback remain active")
        if not recovery_only and position is None and entry_order_id is None:
            _wait_for_quote_readiness(
                engine, quote_items,
                timeout_seconds=getattr(args, "quote_readiness_timeout", 20.0),
                max_age_seconds=args.entry_quote_staleness,
            )
        startup_stage = "RUNNING"
        log(
            "RUNTIME_STARTED",
            environment=environment,
            submit_live=submit_live,
            signal_date=seal["signal_date"],
            capital=args.capital,
            short_enabled=allow_short,
            entry_policy=LONG_MARKET_REGIME_POLICY,
            entry_location_policy=ANTI_CHASE_ENTRY_POLICY,
            exit_policy=LIVE_EXIT_POLICY,
            gate=gate.public_snapshot(),
            runtime_instance_id=runtime_instance_id,
            trading_date=datetime.now(TAIPEI).date().isoformat(),
            account_lock_path=str(session._execution_account_lock.name),
            account_lock_instance=getattr(session._execution_account_lock, "instance_id", ""),
        )
        beat(
            "EXIT_ONLY_RECOVERY" if operational_exit_only else "RUNNING",
            environment=environment,
            submit_live=submit_live,
            signal_date=seal["signal_date"],
            watchlist_count=len(items),
            entry_start=SPEC["entry_start"],
            entry_policy=LONG_MARKET_REGIME_POLICY,
            entry_location_policy=ANTI_CHASE_ENTRY_POLICY,
            exit_policy=LIVE_EXIT_POLICY,
            trade_attempted=trade_attempted,
            last_quote_at=session.last_quote_at,
            gate=gate.public_snapshot(),
            runtime_instance_id=runtime_instance_id,
            trading_date=datetime.now(TAIPEI).date().isoformat(),
            account_lock_path=str(session._execution_account_lock.name),
            account_lock_instance=getattr(session._execution_account_lock, "instance_id", ""),
        )

        while True:
            try:
                now = datetime.now(TAIPEI)
                emergency = kill_path.exists() or stop_event.is_set()
                graceful_stop = stop_path.exists()
                scheduled_force_flat = force_flat_path.exists()
                clock_force_flat = now.time().replace(tzinfo=None) >= time_from_text(SPEC["hard_exit_time"])
                exit_only = emergency or scheduled_force_flat or operational_exit_only or clock_force_flat
                force_flat_phase = _force_flat_market_phase(now)
                market_fallback_due = force_flat_phase == "MARKET"
                closing_fallback_due = force_flat_phase == "CLOSING"
                fallback_due = market_fallback_due or closing_fallback_due
                market_cutoff_reached = force_flat_phase == "CLOSED"

                # Emergency always takes precedence over a graceful stop request.
                if exit_only and graceful_stop:
                    # Exit-only recovery preserves the human stop marker.
                    graceful_stop = False

                runtime_state = (
                    "EMERGENCY_EXIT"
                    if emergency
                    else (
                        "FORCE_FLAT_EXIT"
                        if scheduled_force_flat
                        else ("STOPPING" if graceful_stop else (
                            "EXIT_ONLY_RECOVERY" if operational_exit_only or clock_force_flat else "RUNNING"
                        ))
                    )
                )
                beat(
                    runtime_state,
                    environment=environment,
                    submit_live=submit_live,
                    signal_date=seal["signal_date"],
                    watchlist_count=len(items),
                    entry_start=SPEC["entry_start"],
                    trade_attempted=trade_attempted,
                    last_quote_at=session.last_quote_at,
                    gate=gate.public_snapshot(),
                    position=None if position is None else {
                        "stock_id": position.stock_id,
                        "side": position.side,
                        "quantity": position.quantity,
                        "entry_price": position.entry_price,
                    },
                    entry_order_id=entry_order_id,
                    exit_order_id=exit_order_id,
                    runtime_instance_id=runtime_instance_id,
                    trading_date=now.date().isoformat(),
                    account_lock_path=str(session._execution_account_lock.name),
                    account_lock_instance=getattr(session._execution_account_lock, "instance_id", ""),
                    broker_flat_confirmed_at=broker_flat_confirmed_at,
                    broker_flat_confirmed=broker_flat_confirmed_at is not None,
                )
                decision = _decision_floor(now)
                decision_changed = engine.last_decision is None or decision > engine.last_decision

                if emergency and pending_exit_reason is None:
                    pending_exit_reason = "EMERGENCY_STOP"
                    log("EMERGENCY_STOP_REQUESTED")

                if scheduled_force_flat and pending_exit_reason is None:
                    pending_exit_reason = "SCHEDULED_FORCE_FLAT_1320"
                    log("SCHEDULED_FORCE_FLAT_REQUESTED")

                if clock_force_flat and position is not None and pending_exit_reason is None:
                    pending_exit_reason = "HARD_EXIT"
                    log("CLOCK_FORCE_FLAT_REQUESTED", stock_id=position.stock_id)

                if market_cutoff_reached and (position is not None or entry_order_id is not None or exit_order_id is not None):
                    if not market_cutoff_alerted:
                        market_cutoff_alerted = True
                        store.halt("FORCE_FLAT_MARKET_CLOSED_WITH_EXPOSURE")
                        notifier.critical("FORCE_FLAT_MARKET_CLOSED_WITH_EXPOSURE",
                                          "Market cutoff reached with owned exposure or unresolved order; broker reconciliation continues, no new after-close order will be sent")
                        log("FORCE_FLAT_MARKET_CLOSED_WITH_EXPOSURE", exit_order_id=exit_order_id)

                if graceful_stop and not emergency and pending_exit_reason is None:
                    pending_exit_reason = "GRACEFUL_STOP"
                    log("GRACEFUL_STOP_REQUESTED")

                if emergency and entry_order_id is None and position is None and exit_order_id is None:
                    _confirm_strategy_flat(adapter, store, args.reconcile_timeout)
                    broker_flat_confirmed_at = utc_now()
                    if submit_live:
                        store.halt("MANUAL_EMERGENCY_STOP_NO_EXPOSURE")
                    log("EMERGENCY_STOP_COMPLETE", exposure="NONE")
                    clean_shutdown = True
                    return 0

                if operational_exit_only and not emergency and not scheduled_force_flat and entry_order_id is None and position is None and exit_order_id is None:
                    _confirm_strategy_flat(adapter, store, args.reconcile_timeout)
                    broker_flat_confirmed_at = utc_now()
                    log("EXIT_ONLY_RECOVERY_COMPLETE", exposure="BASELINE_ONLY")
                    clean_shutdown = True
                    return 0

                if (
                    scheduled_force_flat
                    and entry_order_id is None
                    and position is None
                    and exit_order_id is None
                ):
                    _confirm_strategy_flat(adapter, store, args.reconcile_timeout)
                    broker_flat_confirmed_at = utc_now()
                    force_flat_path.unlink(missing_ok=True)
                    log("SCHEDULED_FORCE_FLAT_COMPLETE", exposure="BASELINE_ONLY")
                    clean_shutdown = True
                    return 0

                if (
                    graceful_stop
                    and not emergency
                    and entry_order_id is None
                    and position is None
                    and exit_order_id is None
                ):
                    _confirm_strategy_flat(adapter, store, args.reconcile_timeout)
                    broker_flat_confirmed_at = utc_now()
                    stop_path.unlink(missing_ok=True)
                    log("GRACEFUL_STOP_COMPLETE", exposure="NONE")
                    clean_shutdown = True
                    return 0

                # Entry lifecycle and actual fill state.
                if entry_order_id is not None:
                    if store.get(entry_order_id).status not in TERMINAL:
                        next_entry_reconcile_at = _reconcile_order_deadline(
                            adapter=adapter, store=store, client_order_id=entry_order_id,
                            now=now, next_reconcile_at=next_entry_reconcile_at,
                            timeout=args.reconcile_timeout,
                            interval=getattr(args, "order_reconcile_seconds", 5.0),
                        )
                    order = store.get(entry_order_id)
                    if order.filled_quantity > 0:
                        avg = float(order.average_fill_price or Decimal(str(entry_signal.entry_price)))
                        if position is None:
                            position = ManagedPosition(
                                stock_id=entry_signal.stock_id,
                                stock_name=entry_signal.stock_name,
                                side=entry_signal.side,
                                quantity=order.filled_quantity,
                                entry_price=avg,
                                entry_order_id=entry_order_id,
                                entry_time=entry_submitted_at or now,
                            )
                            _checkpoint_position(store, position, pending_exit_reason)
                            log("POSITION_OPENED", stock_id=position.stock_id, side=position.side, quantity=position.quantity, average_fill_price=avg)
                        elif not position.exit_submitted:
                            net_quantity = abs(int(store.positions().get(position.stock_id, order.filled_quantity)))
                            if net_quantity > 0:
                                position.quantity = net_quantity
                            position.entry_price = avg

                    if order.status not in TERMINAL and entry_submitted_at is not None:
                        expired = (now - entry_submitted_at).total_seconds() >= args.entry_timeout
                        mutation = store.pending_mutation(entry_order_id)
                        entry_cancel_requested = mutation is not None
                        if (exit_only or pending_exit_reason is not None or expired) and not entry_cancel_requested:
                            if order.broker_order_no and (next_entry_cancel_at is None or now >= next_entry_cancel_at):
                                adapter.cancel(entry_order_id, "runtime entry protection", emergency=exit_only or store.control_state()["halted"])
                                entry_cancel_requested = True
                                next_entry_cancel_at = now + timedelta(seconds=args.exit_retry_base_seconds)
                                log("ENTRY_CANCEL_SENT", client_order_id=entry_order_id, reason="emergency_or_timeout")
                            elif expired:
                                # Resolve ACK ambiguity before any further action; never resend.
                                adapter.reconcile(timeout=args.reconcile_timeout, strict_positions=True)
                                log("ENTRY_RECONCILED_AFTER_ACK_DELAY", client_order_id=entry_order_id)
                    elif order.status in TERMINAL and order.filled_quantity == 0 and position is None:
                        log(
                            "ENTRY_NOT_FILLED",
                            client_order_id=entry_order_id,
                            stock_id=order.symbol,
                            status=order.status.value,
                            last_error=order.last_error,
                        )
                        entry_order_id = None
                        if emergency:
                            _confirm_strategy_flat(adapter, store, args.reconcile_timeout)
                            broker_flat_confirmed_at = utc_now()
                            if submit_live:
                                store.halt("MANUAL_EMERGENCY_STOP_NO_EXPOSURE")
                            log("EMERGENCY_STOP_COMPLETE", exposure="NONE")
                            clean_shutdown = True
                            return 0
                        if scheduled_force_flat:
                            _confirm_strategy_flat(adapter, store, args.reconcile_timeout)
                            broker_flat_confirmed_at = utc_now()
                            force_flat_path.unlink(missing_ok=True)
                            log("SCHEDULED_FORCE_FLAT_COMPLETE", exposure="BASELINE_ONLY")
                            clean_shutdown = True
                            return 0
                        if graceful_stop:
                            _confirm_strategy_flat(adapter, store, args.reconcile_timeout)
                            broker_flat_confirmed_at = utc_now()
                            stop_path.unlink(missing_ok=True)
                            log("GRACEFUL_STOP_COMPLETE", exposure="NONE")
                            clean_shutdown = True
                            return 0

                # Strategy decision clock.
                #
                # Once the single LIVE trade allowance has been consumed, flat
                # candidates are still evaluated by the same production signal
                # engine for research/notification purposes, but they can never
                # create a second broker order.
                reversal = False
                if decision_changed:
                    if position is not None:
                        reversal = engine.opposite_signal(
                            position,
                            decision,
                        )
                        engine.last_decision = decision
                        archive_decision_evidence(
                            decision,
                            diagnostics={
                                "decision": "POSITION_MANAGEMENT",
                                "opposite_signal": reversal,
                            },
                            candidate=None,
                        )

                    elif (
                        not exit_only
                        and pending_exit_reason is None
                        and entry_order_id is None
                    ):
                        candidate = engine.choose_entry(
                            decision,
                            allow_short=allow_short,
                        )

                        if engine.last_entry_diagnostics:
                            log(
                                "ENTRY_GATE_DIAGNOSTICS",
                                diagnostics=engine.last_entry_diagnostics,
                            )

                        archive_decision_evidence(
                            decision,
                            diagnostics=engine.last_entry_diagnostics,
                            candidate=candidate,
                        )

                        if candidate is not None:
                            signal_id = _signal_identifier(
                                str(seal["signal_date"]),
                                candidate,
                            )

                            log(
                                "SIGNAL_DETECTED",
                                signal_id=signal_id,
                                candidate=asdict(candidate),
                                live_trade_already_attempted=
                                    trade_attempted,
                            )

                            if trade_attempted:
                                log(
                                    "SIGNAL_SKIPPED",
                                    signal_id=signal_id,
                                    candidate=asdict(candidate),
                                    reason=
                                        "LIVE_TRADE_LIMIT_CONSUMED",
                                )
                                continue

                            age = engine.quote_age_seconds(
                                candidate.stock_id,
                                now,
                            )

                            decision_result = risk.evaluate_entry(
                                signal=candidate,
                                quote_age_seconds=(
                                    float("inf")
                                    if age is None
                                    else age
                                ),
                                broker_positions=
                                    store.positions(),
                                open_orders=
                                    store.orders(
                                        open_only=True
                                    ),
                                trades_today=
                                    _trades_today(
                                        store,
                                        now,
                                    ),
                                realized=
                                    _realized_pnl_today(
                                        store,
                                        engine,
                                        now,
                                    ),
                                halted=bool(
                                    store.control_state()[
                                        "halted"
                                    ]
                                ),
                            )

                            if not decision_result.approved:
                                log(
                                    "RISK_REJECTED_CANDIDATE",
                                    signal_id=signal_id,
                                    candidate=asdict(candidate),
                                    reasons=
                                        decision_result.reasons,
                                )

                                log(
                                    "SIGNAL_SKIPPED",
                                    signal_id=signal_id,
                                    candidate=asdict(candidate),
                                    reason="RISK_REJECTED",
                                    reasons=
                                        decision_result.reasons,
                                )
                                continue

                            log(
                                "RISK_APPROVED_CANDIDATE",
                                signal_id=signal_id,
                                candidate=asdict(candidate),
                            )

                            if not submit_live:
                                log(
                                    "SIGNAL_SKIPPED",
                                    signal_id=signal_id,
                                    candidate=asdict(candidate),
                                    reason="OBSERVE_ONLY",
                                )
                                continue

                            assert (
                                decision_result.intent
                                is not None
                            )

                            intent = bridge_strategy_intent(
                                decision_result.intent,
                                short_entry_order_type=
                                    short_entry,
                                short_cover_order_type=
                                    short_cover,
                            )

                            order = adapter.submit(
                                intent,
                                pre_send_guard=lambda _intent: _entry_pre_send_guard(
                                    candidate=candidate, engine=engine, store=store,
                                    runtime_dir=runtime_dir,
                                    max_age_seconds=args.entry_quote_staleness,
                                ),
                            )

                            entry_order_id = (
                                order.client_order_id
                            )
                            entry_signal = candidate
                            entry_submitted_at = now
                            trade_attempted = True

                            log(
                                "ENTRY_PRE_SEND_REJECTED" if (
                                    order.status == BrokerOrderStatus.REJECTED
                                    and str(order.last_error or "").startswith("UNSENT_")
                                ) else "ENTRY_SUBMITTED",
                                signal_id=signal_id,
                                client_order_id=
                                    entry_order_id,
                                intent_id=
                                    intent.intent_id,
                                stock_id=
                                    intent.symbol,
                                side=
                                    intent.side.value,
                                quantity=
                                    intent.quantity,
                                price=
                                    str(intent.price),
                            )

                    else:
                        engine.last_decision = decision
                        archive_decision_evidence(
                            decision,
                            diagnostics={
                                "decision": "ENTRY_NOT_EVALUATED",
                                "reason": "ENTRY_OR_EXIT_LIFECYCLE_ACTIVE",
                            },
                            candidate=None,
                        )

                # The daily loss guard must keep running while an EXIT is partial/pending.
                if position is not None:
                    healthy_mark = engine.safe_exit_quote(position, now, max_age_seconds=args.max_quote_staleness)
                    if healthy_mark is None and (now - position.entry_time).total_seconds() > args.max_quote_staleness:
                        if not held_quote_stale_triggered:
                            held_quote_stale_triggered = True
                            pending_exit_reason = pending_exit_reason or "HELD_SYMBOL_QUOTE_STALE"
                            operational_exit_only = True
                            store.halt("HELD_SYMBOL_QUOTE_STALE")
                            notifier.critical("HELD_SYMBOL_QUOTE_STALE",
                                              "Owned position has no fresh executable quote, independently of other market streams; new entries are blocked and exit recovery continues",
                                              stock_id=position.stock_id)
                            log("HELD_SYMBOL_QUOTE_STALE", stock_id=position.stock_id)
                    mark = engine.safe_exit_quote(position, now, max_age_seconds=args.exit_quote_staleness)
                    if mark is not None:
                        pnl = execution_pnl(store, now, SPEC)
                        if risk.loss_kill_required(
                            realized=pnl.realized,
                            unrealized=pnl.unrealized({position.stock_id: Decimal(str(mark.price))}, SPEC),
                        ) and pending_exit_reason != "MAX_DAILY_LOSS":
                            pending_exit_reason = "MAX_DAILY_LOSS"
                            kill_path.write_text(f"{utc_now()} MAX_DAILY_LOSS\n", encoding="utf-8")
                            emergency = True
                            _checkpoint_position(store, position, pending_exit_reason)
                            notifier.critical("MAX_DAILY_LOSS", "Execution-based daily loss limit reached")

                # Exit rule may trigger only after an actual fill exists. If entry still has a live remainder,
                # cancel it first so the exit quantity cannot race against later entry fills.
                if (
                    position is not None
                    and not position.exit_submitted
                    and (next_exit_retry_at is None or now >= next_exit_retry_at)
                ):
                    if market_cutoff_reached:
                        if not market_cutoff_alerted:
                            market_cutoff_alerted = True
                            store.halt("FORCE_FLAT_MARKET_CLOSED_WITH_EXPOSURE")
                            notifier.critical(
                                "FORCE_FLAT_MARKET_CLOSED_WITH_EXPOSURE",
                                "Market cutoff reached with broker exposure; no new after-close order was created",
                                stock_id=position.stock_id,
                            )
                            log(
                                "FORCE_FLAT_MARKET_CLOSED_WITH_EXPOSURE",
                                stock_id=position.stock_id,
                                quantity=position.quantity,
                            )
                        time.sleep(0.10)
                        continue
                    if fallback_due:
                        remaining_delta = _authoritative_fallback_delta(
                            adapter,
                            store,
                            baseline=baseline,
                            position=position,
                            timeout=args.reconcile_timeout,
                        )
                        if remaining_delta <= 0:
                            _confirm_strategy_flat(adapter, store, args.reconcile_timeout)
                            broker_flat_confirmed_at = utc_now()
                            if scheduled_force_flat:
                                force_flat_path.unlink(missing_ok=True)
                            log(
                                "HARD_EXIT_MARKET_FALLBACK_ALREADY_FLAT",
                                stock_id=position.stock_id,
                            )
                            clean_shutdown = True
                            return 0
                        position.quantity = remaining_delta
                    safe_quote = engine.safe_exit_quote(
                        position, now, max_age_seconds=args.exit_quote_staleness
                    )
                    if safe_quote is not None:
                        exit_quote_alerted = False
                    exit_decision = engine.evaluate_exit(
                        position,
                        now,
                        reversal=reversal,
                        max_quote_age_seconds=args.exit_quote_staleness,
                    )
                    if fallback_due and exit_decision is None:
                        exit_decision = ExitDecision(
                            "HARD_EXIT_CLOSING_FALLBACK" if closing_fallback_due else "HARD_EXIT_MARKET_FALLBACK",
                            position.entry_price,
                            0.0,
                            0.0,
                        )
                    _checkpoint_position(store, position, pending_exit_reason)
                    if (exit_only or graceful_stop) and exit_decision is None:
                        if safe_quote is not None:
                            projected = engine.projected_net(position, safe_quote.price)
                            exit_decision = ExitDecision(
                                pending_exit_reason
                                or (
                                    "EMERGENCY_STOP"
                                    if emergency
                                    else (
                                        "SCHEDULED_FORCE_FLAT_1320"
                                        if scheduled_force_flat
                                        else "GRACEFUL_STOP"
                                    )
                                ),
                                safe_quote.price,
                                projected,
                                projected / (position.entry_price * position.quantity),
                            )
                        elif not fallback_due and not exit_quote_alerted:
                            exit_quote_alerted = True
                            notifier.critical(
                                "EXIT_QUOTE_UNAVAILABLE",
                                "Exposure exists but no fresh bounded quote is available; no stale-price order was sent",
                                stock_id=position.stock_id,
                            )
                    if exit_decision is not None:
                        pending_exit_reason = pending_exit_reason or exit_decision.reason
                        _checkpoint_position(store, position, pending_exit_reason)
                        entry_terminal = entry_order_id is None or store.get(entry_order_id).status in TERMINAL
                        if not entry_terminal:
                            if not entry_cancel_requested and store.get(entry_order_id).broker_order_no:
                                adapter.cancel(
                                    entry_order_id,
                                    f"exit:{exit_decision.reason}",
                                    emergency=exit_only,
                                )
                                entry_cancel_requested = True
                                log("ENTRY_CANCEL_SENT", client_order_id=entry_order_id, reason=exit_decision.reason)
                        elif submit_live:
                            exit_attempt += 1
                            raw = risk.approve_exit(
                                position=position,
                                quantity=position.quantity,
                                price=exit_decision.price,
                                reason=exit_decision.reason,
                                attempt=exit_attempt,
                            )
                            intent = bridge_strategy_intent(
                                raw,
                                short_entry_order_type=short_entry,
                                short_cover_order_type=short_cover,
                            )
                            if market_fallback_due:
                                intent = _as_market_fallback(intent)
                            elif closing_fallback_due:
                                intent = _as_closing_fallback(intent)
                            exit_guard = lambda actual_intent: _exit_pre_send_guard(
                                actual_intent, position=position, engine=engine,
                                quote_time=None if safe_quote is None else safe_quote.received_at,
                                max_age_seconds=args.exit_quote_staleness,
                            )
                            order = (
                                adapter.submit_rescue(intent, pre_send_guard=exit_guard)
                                if exit_only or store.control_state()["halted"] or exit_attempt > 1
                                else adapter.submit(intent, pre_send_guard=exit_guard)
                            )
                            exit_order_id = order.client_order_id
                            position.exit_submitted = True
                            next_exit_retry_at = None
                            last_exit_reprice = now
                            log(
                                "EXIT_SUBMITTED",
                                client_order_id=exit_order_id,
                                reason=exit_decision.reason,
                                quantity=intent.quantity,
                                price=str(intent.price),
                                price_type=intent.price_type.value,
                                projected_net_pnl=exit_decision.projected_net_pnl,
                            )

                # If an exit trigger was waiting for the entry cancel to settle, submit it as soon as terminal.
                if (
                    submit_live
                    and pending_exit_reason is not None
                    and position is not None
                    and not position.exit_submitted
                    and (next_exit_retry_at is None or now >= next_exit_retry_at)
                    and entry_order_id is not None
                    and store.get(entry_order_id).status in TERMINAL
                    and not market_cutoff_reached
                ):
                    safe_quote = engine.safe_exit_quote(
                        position, now, max_age_seconds=args.exit_quote_staleness
                    )
                    if safe_quote is not None or fallback_due:
                        if fallback_due:
                            remaining = _authoritative_fallback_delta(
                                adapter, store, baseline=baseline,
                                position=position, timeout=args.reconcile_timeout,
                            )
                            if remaining <= 0:
                                _confirm_strategy_flat(adapter, store, args.reconcile_timeout)
                                broker_flat_confirmed_at = utc_now()
                                clean_shutdown = True
                                return 0
                            position.quantity = remaining
                        exit_attempt += 1
                        raw = risk.approve_exit(
                            position=position,
                            quantity=position.quantity,
                            price=safe_quote.price if safe_quote is not None else position.entry_price,
                            reason=pending_exit_reason,
                            attempt=exit_attempt,
                        )
                        intent = bridge_strategy_intent(raw, short_entry_order_type=short_entry, short_cover_order_type=short_cover)
                        if market_fallback_due:
                            intent = _as_market_fallback(intent)
                        elif closing_fallback_due:
                            intent = _as_closing_fallback(intent)
                        exit_guard = lambda actual_intent: _exit_pre_send_guard(
                            actual_intent, position=position, engine=engine,
                            quote_time=None if safe_quote is None else safe_quote.received_at,
                            max_age_seconds=args.exit_quote_staleness,
                        )
                        order = (
                            adapter.submit_rescue(intent, pre_send_guard=exit_guard)
                            if exit_only or store.control_state()["halted"] or exit_attempt > 1
                            else adapter.submit(intent, pre_send_guard=exit_guard)
                        )
                        exit_order_id = order.client_order_id
                        position.exit_submitted = True
                        next_exit_retry_at = None
                        last_exit_reprice = now
                        log("EXIT_SUBMITTED", client_order_id=exit_order_id, reason=pending_exit_reason, quantity=intent.quantity, price=str(intent.price))

                if exit_order_id is not None:
                    if store.get(exit_order_id).status in ACTIVE_EXIT_STATUSES:
                        next_exit_reconcile_at = _reconcile_order_deadline(
                            adapter=adapter, store=store, client_order_id=exit_order_id,
                            now=now, next_reconcile_at=next_exit_reconcile_at,
                            timeout=args.reconcile_timeout,
                            interval=getattr(args, "order_reconcile_seconds", 5.0),
                        )
                    exit_order = store.get(exit_order_id)
                    if fallback_due and _cancel_incompatible_fallback_exit(
                        adapter, store, exit_order, force_flat_phase,
                    ):
                        market_fallback_cancel_requested = True
                        log(
                            "EXIT_CANCEL_FOR_CLOSING_FALLBACK_SENT" if closing_fallback_due else "EXIT_CANCEL_FOR_MARKET_FALLBACK_SENT",
                            client_order_id=exit_order_id,
                        )
                        continue
                    if exit_order.status == BrokerOrderStatus.FILLED:
                        recovery_action, _status, remaining = _reconcile_failed_exit(
                            adapter=adapter, store=store, exit_order_id=exit_order_id,
                            position=position, reconcile_timeout=args.reconcile_timeout,
                        )
                        if recovery_action != "CLOSED":
                            if position is None or recovery_action != "RETRY_RESCUE":
                                raise RuntimeError("FILLED exit has unresolved authoritative remaining exposure")
                            position.quantity = remaining
                            position.exit_submitted = False
                            exit_order_id = None
                            pending_exit_reason = pending_exit_reason or "FILLED_EXIT_REMAINING_EXPOSURE"
                            operational_exit_only = True
                            next_exit_retry_at = now + timedelta(seconds=args.exit_retry_base_seconds)
                            log("FILLED_EXIT_REMAINING_EXPOSURE", remaining_quantity=remaining)
                            continue
                        _confirm_strategy_flat(adapter, store, args.reconcile_timeout)
                        broker_flat_confirmed_at = utc_now()
                        completed_exit_order_id = exit_order_id

                        log(
                            "POSITION_CLOSED",
                            client_order_id=completed_exit_order_id,
                            average_fill_price=str(
                                exit_order.average_fill_price
                            ),
                            quantity=exit_order.filled_quantity,
                        )

                        close_action = _post_close_action(
                            emergency=emergency,
                            graceful_stop=graceful_stop,
                            scheduled_force_flat=scheduled_force_flat,
                        )

                        if close_action == "STOP_EMERGENCY":
                            store.halt(
                                "MANUAL_EMERGENCY_STOP_FLAT"
                            )
                            clean_shutdown = True
                            return 0

                        if close_action == "STOP_GRACEFUL":
                            stop_path.unlink(
                                missing_ok=True
                            )
                            log(
                                "GRACEFUL_STOP_COMPLETE",
                                exposure="FLAT",
                            )
                            clean_shutdown = True
                            return 0

                        if close_action == "STOP_FORCE_FLAT":
                            force_flat_path.unlink(missing_ok=True)
                            log("SCHEDULED_FORCE_FLAT_COMPLETE", exposure="BASELINE_ONLY")
                            clean_shutdown = True
                            return 0

                        # Normal strategy completion is no longer the end of the
                        # realtime process. The single LIVE trade allowance has
                        # already been consumed (trade_attempted stays True), so
                        # clear only transient position/order tracking and keep
                        # the same Yuanta quote session alive through 13:25.
                        entry_order_id = None
                        entry_signal = None
                        entry_submitted_at = None
                        entry_cancel_requested = False
                        next_entry_reconcile_at = None
                        next_entry_cancel_at = None

                        position = None

                        exit_order_id = None
                        pending_exit_reason = None
                        last_exit_reprice = None
                        exit_attempt = 0
                        next_exit_retry_at = None
                        next_exit_reconcile_at = None
                        exit_quote_alerted = False
                        market_fallback_cancel_requested = False
                        market_cutoff_alerted = False

                        log(
                            "LIVE_TRADE_COMPLETE_CONTINUE_ARCHIVE",
                            completed_exit_order_id=
                                completed_exit_order_id,
                            live_trade_limit_consumed=
                                trade_attempted,
                            archive_until="13:25",
                        )

                        continue
                    if exit_order.status in {BrokerOrderStatus.CANCELED, BrokerOrderStatus.REJECTED, BrokerOrderStatus.EXPIRED, BrokerOrderStatus.UNKNOWN}:
                        if (
                            exit_order.status == BrokerOrderStatus.UNKNOWN
                            and next_exit_reconcile_at is not None
                            and now < next_exit_reconcile_at
                        ):
                            time.sleep(0.10)
                            continue

                        reason = f"EXIT_NOT_FILLED:{exit_order.status.value}"
                        planned_market_cancel = (
                            market_fallback_cancel_requested
                            and exit_order.status == BrokerOrderStatus.CANCELED
                        )
                        if not planned_market_cancel:
                            store.halt(reason)
                            kill_path.write_text(
                                f"{utc_now()} EXIT_NOT_FILLED:{exit_order.status.value}\n",
                                encoding="utf-8",
                            )
                        notifier.critical(
                            "EXIT_RESCUE_REQUIRED",
                            "Exit order did not fully close the position; reconciling and retrying remaining exposure",
                            status=exit_order.status.value,
                            client_order_id=exit_order_id,
                        )
                        # The original SendStockOrder may have timed out locally
                        # even though the broker accepted it. Reconcile and re-read
                        # the SAME exit order before another NEW EXIT is allowed.
                        recovery_action, reconciled_status, remaining = (
                            _reconcile_failed_exit(
                                adapter=adapter,
                                store=store,
                                exit_order_id=exit_order_id,
                                position=position,
                                reconcile_timeout=args.reconcile_timeout,
                            )
                        )

                        if recovery_action == "CLOSED":
                            _confirm_strategy_flat(adapter, store, args.reconcile_timeout)
                            broker_flat_confirmed_at = utc_now()
                            log(
                                "POSITION_CLOSED_AFTER_RECONCILIATION",
                                client_order_id=exit_order_id,
                                reconciled_status=reconciled_status.value,
                            )
                            clean_shutdown = True
                            return 0

                        if recovery_action == "TRACK_EXISTING":
                            next_exit_reconcile_at = None
                            if position is not None:
                                position.quantity = remaining
                                position.exit_submitted = True

                            log(
                                "EXIT_RECOVERED_ACTIVE_AFTER_RECONCILIATION",
                                client_order_id=exit_order_id,
                                status=reconciled_status.value,
                                remaining_quantity=remaining,
                            )

                            # Keep exit_order_id intact. The existing broker order
                            # is still responsible for this exposure.
                            continue

                        if recovery_action == "UNKNOWN":
                            store.halt("EXIT_RECONCILIATION_UNRESOLVED")

                            reconcile_delay = max(
                                1.0,
                                min(
                                    float(args.exit_retry_max_seconds),
                                    float(args.exit_retry_base_seconds),
                                ),
                            )
                            next_exit_reconcile_at = now + timedelta(
                                seconds=reconcile_delay
                            )

                            notifier.critical(
                                "EXIT_RECONCILIATION_UNRESOLVED",
                                "Exit state remained unresolved after broker reconciliation; no retry order was sent",
                                client_order_id=exit_order_id,
                            )

                            log(
                                "EXIT_RECONCILIATION_UNRESOLVED",
                                client_order_id=exit_order_id,
                                status=reconciled_status.value,
                                remaining_quantity=remaining,
                            )

                            # Fail closed. Never invent a second EXIT while the
                            # original broker state is still ambiguous.
                            continue

                        # Only a terminal old EXIT plus real remaining exposure may
                        # create a brand-new rescue order.
                        next_exit_reconcile_at = None
                        if position is not None:
                            position.quantity = remaining
                            position.exit_submitted = False

                        exit_order_id = None
                        market_fallback_cancel_requested = False
                        last_exit_reprice = None
                        pending_exit_reason = pending_exit_reason or reason

                        delay = min(
                            args.exit_retry_max_seconds,
                            args.exit_retry_base_seconds
                            * (2 ** max(0, exit_attempt - 1)),
                        )

                        next_exit_retry_at = now + timedelta(seconds=delay)

                        log(
                            "EXIT_RETRY_SCHEDULED",
                            delay_seconds=delay,
                            attempt=exit_attempt + 1,
                            reconciled_status=reconciled_status.value,
                            remaining_quantity=remaining,
                        )
                        continue
                    if (
                        not market_cutoff_reached
                        and exit_order.price_type == PriceType.LIMIT
                        and exit_order.status in {BrokerOrderStatus.ACKNOWLEDGED, BrokerOrderStatus.PARTIALLY_FILLED}
                        and exit_order.broker_order_no
                        and last_exit_reprice is not None
                        and (now - last_exit_reprice).total_seconds() >= args.exit_reprice_seconds
                        and store.pending_mutation(exit_order_id) is None
                    ):
                        safe_quote = (
                            engine.safe_exit_quote(position, now, max_age_seconds=args.exit_quote_staleness)
                            if position is not None else None
                        )
                        new_price = None if safe_quote is None else safe_quote.price
                        if new_price is not None and (exit_order.price is None or Decimal(str(new_price)) != exit_order.price):
                            _exit_pre_send_guard(
                                exit_order, position=position, engine=engine,
                                quote_time=safe_quote.received_at,
                                max_age_seconds=args.exit_quote_staleness,
                            )
                            adapter.modify_price(
                                exit_order_id,
                                Decimal(str(new_price)),
                                emergency=exit_only or store.control_state()["halted"],
                                pre_send_guard=lambda proposed: _exit_pre_send_guard(
                                    proposed, position=position, engine=engine,
                                    quote_time=safe_quote.received_at,
                                    max_age_seconds=args.exit_quote_staleness,
                                ),
                            )
                            last_exit_reprice = now
                            log("EXIT_REPRICE_SENT", client_order_id=exit_order_id, price=new_price)

                # Keep the single broker/quote session alive through 13:25,
                # whether the day had no trade or one completed LIVE trade.
                if (
                    now.time()
                    >= time_from_text("13:25")
                    and entry_order_id is None
                    and position is None
                ):
                    _confirm_strategy_flat(adapter, store, args.reconcile_timeout)
                    broker_flat_confirmed_at = utc_now()
                    log(
                        "SESSION_COMPLETE",
                        trade_attempted=trade_attempted,
                    )
                    clean_shutdown = True
                    return 0

                # Quote outage: with no exposure it is safe to rebuild the quote/broker
                # session and reconcile before doing anything else. With exposure, keep
                # the existing broker session alive and turn the outage into an exit trigger.
                quote_reference = session.last_quote_at or session.quote_started_at
                if quote_reference is not None and time_from_text("09:05") <= now.time() <= time_from_text("13:25"):
                    stale = (now - quote_reference).total_seconds()
                    if stale > args.max_quote_staleness:
                        exposed = entry_order_id is not None or position is not None
                        if exposed:
                            if not quote_stale_triggered:
                                quote_stale_triggered = True
                                pending_exit_reason = pending_exit_reason or "QUOTE_STALE"
                                log("LIVE_QUOTE_STALE_WITH_EXPOSURE", stale_seconds=round(stale, 1))
                                notifier.critical(
                                    "LIVE_QUOTE_STALE_WITH_EXPOSURE",
                                    "Market data is stale while exposure exists; stale-price submissions are blocked",
                                    stale_seconds=round(stale, 1),
                                )
                        elif last_reconnect_at is None or (now - last_reconnect_at).total_seconds() >= args.reconnect_cooldown:
                            last_reconnect_at = now
                            log("QUOTE_RECONNECT_BEGIN", stale_seconds=round(stale, 1))
                            try:
                                adapter.close()
                                session.close()
                                session.connect()
                                assert session.api is not None
                                adapter = YuantaSparkExecutionAdapter(
                                    api=session.api,
                                    api_types=api_types,
                                    account=session.account,
                                    store=store,
                                    live_gate=gate,
                                    position_baseline=baseline,
                                )
                                reconnection = adapter.reconcile(timeout=args.reconcile_timeout, strict_positions=True)
                                if store.control_state()["halted"]:
                                    raise RuntimeError(
                                        f"broker execution store halted during reconnect: {store.control_state().get('reason')}"
                                    )
                                session.subscribe(quote_items)
                                quote_stale_triggered = False
                                log("QUOTE_RECONNECT_PASSED", reconciliation=asdict(reconnection))
                            except Exception as exc:
                                log("QUOTE_RECONNECT_FAILED", error=f"{type(exc).__name__}: {exc}")
                    else:
                        quote_stale_triggered = False

                if position is not None:
                    _checkpoint_position(store, position, pending_exit_reason)
                time.sleep(0.10)
            except Exception as exc:
                # Keep the controller and owned broker session alive. An error
                # never proves the account flat and never authorizes a resend.
                operational_exit_only = True
                recovery_error_count += 1
                pending_exit_reason = pending_exit_reason or "RUNTIME_OPERATIONAL_RECOVERY"
                try:
                    store.halt("RUNTIME_OPERATIONAL_RECOVERY:" + type(exc).__name__)
                except Exception:
                    pass
                try:
                    if str(exc).startswith("FORCE_FLAT_ODD_LOT_UNSUPPORTED"):
                        notifier.critical(
                            "FORCE_FLAT_ODD_LOT_UNSUPPORTED",
                            "Owned odd/mixed-lot remainder cannot use the verified fallback route; no market/IOC or guessed-price order was sent",
                        )
                    notifier.critical(
                        "RUNTIME_OPERATIONAL_RECOVERY",
                        "Runtime operation failed; new entries are disabled, broker-backed owned-exposure recovery continues",
                        error_type=type(exc).__name__,
                        error=str(exc),
                        attempt=recovery_error_count,
                    )
                except Exception:
                    pass
                try:
                    log("RUNTIME_OPERATIONAL_RECOVERY", error_type=type(exc).__name__,
                        error=str(exc), attempt=recovery_error_count)
                except Exception:
                    pass
                try:
                    if getattr(adapter, "_query_uncertain", False) is True:
                        # A timed-out vendor query can deliver a late snapshot.
                        # A fresh connection/adapter fences out that old stream;
                        # durable intent IDs and the account lock remain intact.
                        adapter.close()
                        session.close()
                        session.connect()
                        adapter = YuantaSparkExecutionAdapter(
                            api=session.api, api_types=api_types,
                            account=session.account, store=store,
                            live_gate=gate, position_baseline=baseline,
                        )
                        try:
                            session.subscribe(quote_items)
                        except Exception:
                            notifier.critical("RECOVERY_QUOTES_UNAVAILABLE",
                                              "Broker was reconnected but market-data recovery is unavailable; entries remain disabled")
                    adapter.reconcile(timeout=args.reconcile_timeout, strict_positions=True)
                    recovered = _recover_runtime_state(store, metadata, datetime.now(TAIPEI), exit_only=True)
                    entry_order_id = recovered["entry_order_id"]
                    entry_signal = recovered["entry_signal"]
                    entry_submitted_at = recovered["entry_submitted_at"]
                    position = recovered["position"]
                    exit_order_id = recovered["exit_order_id"]
                    trade_attempted = trade_attempted or recovered["trade_attempted"]
                    pending_exit_reason = recovered["pending_exit_reason"] or pending_exit_reason
                    entry_cancel_requested = (
                        entry_order_id is not None
                        and store.pending_mutation(entry_order_id) is not None
                    )
                    if position is not None:
                        engine.add_monitor_symbol(position.stock_id, position.stock_name)
                    next_entry_reconcile_at = None
                    next_exit_reconcile_at = None
                except Exception as recovery_exc:
                    try:
                        notifier.critical(
                            "RUNTIME_RECOVERY_BROKER_UNCERTAIN",
                            "Broker reconciliation remains uncertain; no exposure or order state was guessed, new entries remain disabled",
                            error_type=type(recovery_exc).__name__,
                            error=str(recovery_exc),
                        )
                    except Exception:
                        pass
                time.sleep(min(float(args.exit_retry_max_seconds),
                               float(args.exit_retry_base_seconds)
                               * (2 ** min(recovery_error_count - 1, 6))))
    except Exception as exc:
        archive_error_type = type(exc).__name__
        if not failure_code:
            if startup_stage == "QUOTE_SUBSCRIPTION":
                failure_code = "QUOTE_SUBSCRIPTION_FAILED"
            elif startup_stage in {"BROKER_CONNECT", "BROKER_CONNECTED"}:
                failure_code = "BROKER_CONNECTION_FAILED"
            elif startup_stage in {"BROKER_RECONCILIATION", "BROKER_RECONCILED"}:
                failure_code = "BROKER_RECONCILIATION_FAILED"
            elif startup_stage == "LOCAL_STATE_RECOVERY":
                failure_code = "LOCAL_STATE_RECOVERY_FAILED"
            else:
                failure_code = "RUNTIME_FAILED"
        try:
            log(
                "RUNTIME_FATAL_ERROR",
                failure_code=failure_code,
                failure_stage=startup_stage,
                error_type=archive_error_type,
                error=str(exc),
                traceback=traceback.format_exc(),
            )
        except Exception:
            pass
        raise
    finally:
        try:
            heartbeat.stopped(
                clean_shutdown,
                environment=environment,
                submit_live=submit_live,
                failure_code=("" if clean_shutdown else failure_code),
                failure_stage=("" if clean_shutdown else startup_stage),
                broker_flat_confirmed_at=broker_flat_confirmed_at,
                broker_flat_confirmed=broker_flat_confirmed_at is not None,
                runtime_instance_id=runtime_instance_id,
                trading_date=datetime.now(TAIPEI).date().isoformat(),
                account_lock_path=str(getattr(getattr(session, "_execution_account_lock", None), "name", "")),
                account_lock_instance=getattr(getattr(session, "_execution_account_lock", None), "instance_id", ""),
            )
        except Exception:
            pass
        if not clean_shutdown:
            try:
                notifier.critical(
                    "RUNTIME_STOPPED_UNSAFE",
                    "Live runtime stopped without a confirmed clean terminal state",
                    environment=environment,
                    submit_live=submit_live,
                    failure_code=failure_code,
                    failure_stage=startup_stage,
                )
            except Exception:
                pass
        credentials.update({"pfx_password": "", "trading_password": ""})
        if adapter is not None:
            try:
                adapter.close()
            except Exception as exc:
                try:
                    log("ADAPTER_CLOSE_ERROR", error=f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass

        session.close()
        _release_account_session_lock(session)

        if archive is not None:
            try:
                counters_now = _store_archive_counters(
                    store
                )
                counter_delta = _archive_counter_delta(
                    archive_counter_baseline,
                    counters_now,
                )

                now_taipei = datetime.now(TAIPEI)

                if not clean_shutdown:
                    archive_status = "FAILED"
                elif kill_path.exists():
                    archive_status = "STOPPED_EMERGENCY"
                elif (
                    now_taipei.time()
                    >= time_from_text("13:25")
                ):
                    archive_status = "COMPLETE"
                else:
                    archive_status = "STOPPED_CLEAN"

                manifest = archive.finalize(
                    status=archive_status,
                    started_at=archive_started_at,
                    ended_at=utc_now(),
                    error_type=archive_error_type,
                    actual_orders=counter_delta[
                        "new_requests"
                    ],
                    actual_fills=counter_delta[
                        "fills"
                    ],
                    broker_order_calls=counter_delta[
                        "requests"
                    ],
                    terminal_flat_confirmed_at=broker_flat_confirmed_at,
                )

                log(
                    "ARCHIVE_FINALIZED",
                    run_id=archive.run_id,
                    status=archive_status,
                    event_counts=manifest[
                        "event_counts"
                    ],
                    actual_orders=manifest[
                        "actual_orders"
                    ],
                    actual_fills=manifest[
                        "actual_fills"
                    ],
                    broker_order_calls=manifest[
                        "broker_order_calls"
                    ],
                )

            except Exception as exc:
                log(
                    "ARCHIVE_FINALIZE_ERROR",
                    error=f"{type(exc).__name__}: {exc}",
                )

        try:
            trading_notifier.close(timeout=3.0)
        except Exception:
            pass

        try:
            notifier.close(timeout=12.0)
        except Exception:
            pass

        store.close()
        _release_runtime_instance_lock(runtime_lock)


def time_from_text(text: str):
    from datetime import time as time_cls
    hour, minute = map(int, text.split(":"))
    return time_cls(hour, minute)


def _connect_for_control(args, environment: str, *, baseline: dict[str, int], cli_live: bool):
    _validate_runtime_intervals(args)
    credentials = load_credentials()
    api_types = _extend_quote_types(load_api_types(args.vendor_dir.resolve()))
    logger = lambda event, **payload: print(json.dumps({"at": utc_now(), "event": event, **payload}, ensure_ascii=False, default=str))
    session = _Session(api_types=api_types, environment=environment, credentials=credentials, engine=None, logger=logger)
    store = LiveOrderStore(args.runtime_dir.resolve() / "live-orders.sqlite")
    try:
        _claim_account_lock(session, args, environment)
        session.connect()
        assert session.api is not None
        adapter = YuantaSparkExecutionAdapter(
            api=session.api,
            api_types=api_types,
            account=session.account,
            store=store,
            live_gate=LiveTradingGate.from_environment(cli_live=cli_live),
            position_baseline=baseline,
        )
        return credentials, session, store, adapter
    except Exception:
        session.close()
        _release_account_session_lock(session)
        store.close()
        credentials.update({"pfx_password": "", "trading_password": ""})
        raise


def _preflight(args, environment: str) -> int:
    _validate_runtime_intervals(args)
    runtime_dir = args.runtime_dir.resolve()

    # Preflight opens an independent broker connection. It must therefore own
    # the same kernel singleton as realtime/clear-halt before credentials are
    # loaded or any broker connection is attempted.
    runtime_lock = _acquire_runtime_instance_lock(runtime_dir)

    credentials = None
    session = None
    store = None
    adapter = None

    try:
        baseline = _load_baseline(args.baseline.resolve())
        seal, items, _provenance = load_stage_a_watchlist()
        _validate_watchlist_day(str(seal["signal_date"]))

        quote_items = _quote_universe(items)
        benchmark_symbol = str(
            LONG_MARKET_REGIME_POLICY["benchmark_symbol"]
        )
        strategy_symbols = {
            str(item.stock_id)
            for item in items
            if str(item.stock_id) != benchmark_symbol
        }
        engine = _strategy_engine(items)

        credentials = load_credentials()
        api_types = _extend_quote_types(
            load_api_types(args.vendor_dir.resolve())
        )

        logger = lambda event, **payload: print(
            json.dumps(
                {
                    "at": utc_now(),
                    "event": event,
                    **payload,
                },
                ensure_ascii=False,
                default=str,
            )
        )

        session = _Session(
            api_types=api_types,
            environment=environment,
            credentials=credentials,
            engine=engine,
            logger=logger,
            non_archive_symbols={benchmark_symbol},
            strategy_symbols=strategy_symbols,
        )

        store = LiveOrderStore(
            runtime_dir / "live-orders.sqlite"
        )

        control = store.control_state()
        if control["halted"]:
            print(
                json.dumps(
                    {
                        "status": "BLOCKED",
                        "reason": "BROKER_EXECUTION_HALTED",
                    },
                    ensure_ascii=False,
                )
            )
            return 2

        _claim_account_lock(session, args, environment)
        session.connect()
        assert session.api is not None
        if not _baseline_is_current(
            args.baseline.resolve(),
            account=session.account,
            now=datetime.now(TAIPEI),
        ):
            raise RuntimeError(
                "preflight requires today's account-scoped position baseline"
            )

        adapter = YuantaSparkExecutionAdapter(
            api=session.api,
            api_types=api_types,
            account=session.account,
            store=store,
            live_gate=LiveTradingGate.from_environment(
                cli_live=False
            ),
            position_baseline=baseline,
        )

        result = adapter.reconcile(
            timeout=args.reconcile_timeout,
            strict_positions=True,
        )

        session.subscribe(quote_items)
        readiness = _wait_for_quote_readiness(
            engine, quote_items,
            timeout_seconds=getattr(args, "quote_readiness_timeout", 20.0),
        )

        print(
            json.dumps(
                {
                    "status": "READY",
                    "environment": environment,
                    "signal_date": seal["signal_date"],
                    "quote_subscription": "ACCEPTED",
                    "quote_symbols": len(quote_items),
                    "quote_readiness": readiness,
                    "entry_policy": LONG_MARKET_REGIME_POLICY,
                    "reconciliation": asdict(result),
                    "gate": adapter.live_gate.public_snapshot(),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )

        return 0

    finally:
        if credentials is not None:
            credentials.update(
                {
                    "pfx_password": "",
                    "trading_password": "",
                }
            )

        if adapter is not None:
            adapter.close()

        if session is not None:
            session.close()
            _release_account_session_lock(session)

        if store is not None:
            store.close()

        _release_runtime_instance_lock(
            runtime_lock
        )


def _capture_baseline(args, environment: str) -> int:
    automatic = bool(getattr(args, "for_live_start", False))
    if not (args.accept_existing_positions or automatic):
        raise RuntimeError(
            "baseline capture requires --accept-existing-positions or --for-live-start"
        )
    runtime_dir = args.runtime_dir.resolve()
    if (runtime_dir / "STOP_REQUEST").exists():
        raise RuntimeError("baseline capture refused while STOP_REQUEST is active")
    if (runtime_dir / "EMERGENCY_STOP").exists():
        raise RuntimeError("baseline capture refused while EMERGENCY_STOP is active")

    runtime_lock = _acquire_runtime_instance_lock(runtime_dir)
    credentials = None
    session = None
    store = None
    adapter = None
    try:
        # Recheck after owning the singleton so a concurrent stop/kill request
        # cannot race the earlier friendly checks.
        if (runtime_dir / "STOP_REQUEST").exists():
            raise RuntimeError("baseline capture refused while STOP_REQUEST is active")
        if (runtime_dir / "EMERGENCY_STOP").exists():
            raise RuntimeError("baseline capture refused while EMERGENCY_STOP is active")
        credentials, session, store, adapter = _connect_for_control(
            args,
            environment,
            baseline={},
            cli_live=False,
        )
        baseline_path = args.baseline.resolve()
        now = datetime.now(TAIPEI)
        if automatic and _baseline_is_current(
            baseline_path,
            account=session.account,
            now=now,
        ):
            print(json.dumps({
                "status": "BASELINE_REUSED",
                "path": str(baseline_path),
                "trading_date": now.date().isoformat(),
            }, ensure_ascii=False, indent=2))
            return 0

        snapshot = adapter.inspect_broker_state(timeout=args.reconcile_timeout)
        if snapshot.open_orders:
            raise RuntimeError("baseline capture refused while broker has open orders")
        if store.position_buckets():
            raise RuntimeError("baseline capture refused while local strategy positions exist")
        active_local_orders = [
            order
            for order in store.orders()
            if order.status not in TERMINAL
        ]
        if active_local_orders:
            raise RuntimeError("baseline capture refused while local active orders exist")
        if automatic:
            today = now.date()
            entries_today = [
                order
                for order in store.orders()
                if order.purpose.value == "ENTRY"
                and _stamp(order.created_at).date() == today
            ]
            if entries_today:
                raise RuntimeError(
                    "automatic baseline capture refused after today's first entry order"
                )
        positions = dict(snapshot.positions)
        captured_at = utc_now()
        _write_baseline(baseline_path, positions)
        _write_baseline_metadata(
            baseline_path,
            account=session.account,
            captured_at=captured_at,
        )
        print(json.dumps({
            "status": "BASELINE_CAPTURED",
            "path": str(baseline_path),
            "trading_date": now.date().isoformat(),
            "positions": positions,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        if credentials is not None:
            credentials.update({"pfx_password": "", "trading_password": ""})
        if adapter is not None:
            adapter.close()
        if session is not None:
            session.close()
            _release_account_session_lock(session)
        if store is not None:
            store.close()
        _release_runtime_instance_lock(runtime_lock)


def _stop(args) -> int:
    runtime = args.runtime_dir.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    marker = runtime / "STOP_REQUEST"
    heartbeat_path = runtime / "heartbeat.json"

    if not heartbeat_path.is_file():
        marker.unlink(missing_ok=True)
        print(json.dumps({"status": "RUNTIME_NOT_RUNNING"}, ensure_ascii=False, indent=2))
        return 0

    try:
        payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        pid = int(payload.get("pid", 0))
        stamp = datetime.fromisoformat(str(payload["at"]).replace("Z", "+00:00"))
        heartbeat_age = (
            datetime.now(TAIPEI) - stamp.astimezone(TAIPEI)
        ).total_seconds()
        if pid <= 0:
            raise ValueError("invalid runtime pid")
        os.kill(pid, 0)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        marker.unlink(missing_ok=True)
        print(json.dumps({"status": "RUNTIME_NOT_RUNNING"}, ensure_ascii=False, indent=2))
        return 0

    if heartbeat_age > 5.0:
        raise RuntimeError(
            "runtime PID is alive but heartbeat is stale; refusing graceful stop; "
            "review runtime state before using kill"
        )

    marker.write_text(f"{utc_now()} {args.reason}\n", encoding="utf-8")
    print(json.dumps({"status": "GRACEFUL_STOP_REQUESTED"}, ensure_ascii=False, indent=2))
    return 0


def _kill(args) -> int:
    runtime = args.runtime_dir.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    marker = runtime / "EMERGENCY_STOP"
    marker.write_text(f"{utc_now()} {args.reason}\n", encoding="utf-8")
    print(json.dumps({"status": "EMERGENCY_STOP_REQUESTED", "marker": str(marker)}, ensure_ascii=False, indent=2))
    if not args.live:
        return 0
    heartbeat_path = runtime / "heartbeat.json"
    if heartbeat_path.is_file():
        try:
            payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
            pid = int(payload.get("pid", 0))
            stamp = datetime.fromisoformat(str(payload["at"]).replace("Z", "+00:00"))
            heartbeat_age = (
                datetime.now(TAIPEI) - stamp.astimezone(TAIPEI)
            ).total_seconds()
            if pid > 0:
                os.kill(pid, 0)
                if heartbeat_age <= 5.0:
                    print(json.dumps({
                        "status": "RUNNING_RUNTIME_WILL_EXECUTE_EMERGENCY_EXIT",
                        "pid": pid,
                    }, ensure_ascii=False, indent=2))
                    return 0
                RuntimeNotifier(runtime).critical(
                    "STALE_RUNTIME_DURING_KILL",
                    "Runtime process exists but heartbeat is stale; refusing a second broker controller",
                    pid=pid,
                    heartbeat_age_seconds=round(heartbeat_age, 3),
                )
                raise RuntimeError(
                    "runtime PID is still alive but heartbeat is stale; stop that process before independent recovery"
                )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            pass
    args.recover_emergency = True
    return _run_realtime(args, environment=args.environment, submit_live=True)


def _clear_halt(args) -> int:
    runtime = args.runtime_dir.resolve()

    # clear-halt opens an independent broker control connection, so it must
    # never run alongside the realtime controller. Acquire the same kernel
    # singleton lock before loading credentials or connecting to the broker.
    runtime_lock = _acquire_runtime_instance_lock(runtime)

    credentials = None
    session = None
    store = None
    adapter = None
    try:
        baseline = _load_baseline(args.baseline.resolve())
        credentials, session, store, adapter = _connect_for_control(
            args, args.environment, baseline=baseline, cli_live=False
        )

        snapshot = adapter.inspect_broker_state(timeout=args.reconcile_timeout)
        if snapshot.open_orders:
            raise RuntimeError("cannot clear halt while actual broker orders are open")
        if snapshot.positions != baseline:
            raise RuntimeError("cannot clear halt while actual broker positions differ from reviewed baseline")
        if store.orders(open_only=True):
            raise RuntimeError("cannot clear halt while local broker orders are open")
        if store.positions():
            raise RuntimeError("cannot clear halt while strategy positions are non-flat")

        store.clear_halt(args.reason)

        marker = runtime / "EMERGENCY_STOP"
        marker.unlink(missing_ok=True)

        print(json.dumps({
            "status": "HALT_CLEARED",
            "reason": args.reason,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        if credentials is not None:
            credentials.update({
                "pfx_password": "",
                "trading_password": "",
            })
        if adapter is not None:
            adapter.close()
        if session is not None:
            session.close()
            _release_account_session_lock(session)
        if store is not None:
            store.close()
        _release_runtime_instance_lock(runtime_lock)


def _status(args) -> int:
    runtime = args.runtime_dir.resolve()
    db = runtime / "live-orders.sqlite"
    result: dict[str, Any] = {
        "credentials": credential_status(),
        "runtime_dir": str(runtime),
        "emergency_stop_marker": (runtime / "EMERGENCY_STOP").exists(),
        "database_exists": db.exists(),
        "entry_policy": LONG_MARKET_REGIME_POLICY,
        "exit_policy": LIVE_EXIT_POLICY,
    }
    if db.exists():
        store = LiveOrderStore(db)
        try:
            result["broker_store"] = store.snapshot()
        finally:
            store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vendor-dir", type=Path, default=DEFAULT_VENDOR_DIR)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_RUNTIME_DIR / "position_baseline.json")
    parser.add_argument("--reconcile-timeout", type=float, default=20.0)
    parser.add_argument("--quote-readiness-timeout", type=float, default=20.0)


def _start_options(parser: argparse.ArgumentParser) -> None:
    _common(parser)
    parser.add_argument(
        "--archive-runtime-dir",
        type=Path,
        default=DEFAULT_ARCHIVE_RUNTIME_DIR,
        help=(
            "append-only quote archive root; defaults to the existing "
            "yuanta_intraday_shadow_v01 runtime so historical replay paths "
            "remain compatible"
        ),
    )
    parser.add_argument("--live", action="store_true", help="required together with EXECUTION_MODE=LIVE and ENABLE_LIVE_TRADING=YES")
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--entry-timeout", type=float, default=15.0)
    parser.add_argument("--exit-reprice-seconds", type=float, default=5.0)
    parser.add_argument("--exit-retry-base-seconds", type=float, default=2.0)
    parser.add_argument("--exit-retry-max-seconds", type=float, default=30.0)
    parser.add_argument("--order-reconcile-seconds", type=float, default=5.0)
    parser.add_argument("--max-quote-staleness", type=float, default=30.0)
    parser.add_argument("--entry-quote-staleness", type=float, default=5.0)
    parser.add_argument("--exit-quote-staleness", type=float, default=3.0)
    parser.add_argument("--reconnect-cooldown", type=float, default=60.0)
    parser.add_argument("--max-daily-loss", type=int, default=5_000)
    parser.add_argument("--max-order-value", type=int, default=190_000)
    parser.add_argument(
        "--max-position-per-stock",
        type=int,
        default=0,
        help="share cap per stock; 0 disables this cap while MAX_ORDER_VALUE still applies",
    )
    parser.add_argument("--max-concurrent-positions", type=int, default=1)
    parser.add_argument("--max-trades-per-day", type=int, default=1)
    parser.add_argument("--recover-emergency", action="store_true", help="allow exit-only restart while the persistent emergency marker exists")
    parser.add_argument(
        "--recover-force-flat",
        action="store_true",
        help="allow an exit-only restart for an independent scheduled force-flat request",
    )
    parser.add_argument("--short-entry-order-type", choices=["4", "5", "6", "9"], default=None)
    parser.add_argument("--short-cover-order-type", choices=["4", "5", "6", "9"], default=None)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="WarrantScope Yuanta guarded realtime execution runtime")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status")
    _common(status)

    for name in ("preflight-uat", "preflight-prod"):
        cmd = sub.add_parser(name)
        _common(cmd)

    for name in ("baseline-uat", "baseline-prod"):
        cmd = sub.add_parser(name)
        _common(cmd)
        cmd.add_argument("--accept-existing-positions", action="store_true")
        cmd.add_argument("--for-live-start", action="store_true")

    for name in ("observe-uat", "observe-prod", "start-uat", "start-prod"):
        cmd = sub.add_parser(name)
        _start_options(cmd)

    stop = sub.add_parser("stop")
    _common(stop)
    stop.add_argument("--reason", required=True)

    kill = sub.add_parser("kill")
    _start_options(kill)
    kill.add_argument("--reason", required=True)
    kill.add_argument("--environment", choices=["UAT", "PROD"], default="PROD")

    clear = sub.add_parser("clear-halt")
    _common(clear)
    clear.add_argument("--reason", required=True)
    clear.add_argument("--environment", choices=["UAT", "PROD"], default="PROD")

    watchdog = sub.add_parser("watchdog")
    watchdog.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    watchdog.add_argument("--stale-seconds", type=float, default=15.0)
    watchdog.add_argument("--interval-seconds", type=float, default=5.0)

    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "status":
            return _status(args)
        if args.command == "stop":
            return _stop(args)
        if args.command == "kill":
            return _kill(args)
        if args.command == "clear-halt":
            return _clear_halt(args)
        if args.command == "watchdog":
            return watchdog_monitor(
                args.runtime_dir,
                stale_seconds=args.stale_seconds,
                interval_seconds=args.interval_seconds,
            )
        if args.command.startswith("preflight-"):
            return _preflight(args, args.command.rsplit("-", 1)[1].upper())
        if args.command.startswith("baseline-"):
            return _capture_baseline(args, args.command.rsplit("-", 1)[1].upper())
        if args.command in {"observe-uat", "observe-prod", "start-uat", "start-prod"}:
            environment = "UAT" if args.command.endswith("uat") else "PROD"
            submit_live = args.command.startswith("start-")
            if submit_live and not args.live:
                raise RuntimeError("start requires explicit --live")
            return _run_realtime(args, environment=environment, submit_live=submit_live)
        raise RuntimeError(f"unsupported command: {args.command}")
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
