#!/usr/bin/env python3
"""Guarded realtime runner for the Yuanta SPARK broker adapter.

This is the missing runtime layer between the sealed Stage A Top30 realtime quotes,
the frozen direction-following rule, and ``yuanta_broker_execution_v01``.
Production sends are impossible unless the broker adapter's existing three-way LIVE
gate is authorized and startup reconciliation has passed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import errno
import fcntl
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from yuanta_broker_execution_v01 import (
    BrokerOrderStatus,
    LiveOrderStore,
    LiveTradingGate,
    StockOrderType,
    YuantaSparkExecutionAdapter,
    bridge_strategy_intent,
    load_api_types,
)
from yuanta_intraday_shadow_v01.collector import (
    AppendOnlyRun,
    DEFAULT_RUNTIME_DIR as DEFAULT_ARCHIVE_RUNTIME_DIR,
    load_stage_a_watchlist,
    utc_now,
)
from yuanta_intraday_shadow_v01.collector_main import _book_payload, _quote_time
from yuanta_intraday_shadow_v01.main import DEFAULT_VENDOR_DIR as SHADOW_DEFAULT_VENDOR_DIR, _safe_text
from yuanta_intraday_shadow_v01.yuanta_keychain import load_credentials, status as credential_status

from .notifications import RuntimeNotifier
from .trading_bot_notifier import AsyncTradingNotifier
from .risk_manager import RiskLimits, RiskManager
from .strategy import LiveDirectionEngine, ManagedPosition, SPEC
from .watchdog import Heartbeat, monitor as watchdog_monitor

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
    ):
        self.api_types = api_types
        self.environment = environment
        self.credentials = credentials
        self.engine = engine
        self.logger = logger
        self.archive = archive
        self.archive_signal_date = str(archive_signal_date)
        self.archive_items = dict(archive_items or {})
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

    def _archive_quote(
        self,
        *,
        kind: str,
        symbol: str,
        value,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Mirror one already-received quote callback into the raw archive.

        Archiving shares the same Yuanta session/callback as the live strategy.
        An archive failure is recorded but must not prevent the strategy engine
        from consuming subsequent market data.
        """
        if self.archive is None:
            return

        item = self.archive_items.get(symbol)
        if item is None:
            try:
                self.archive.callback_error()
            except Exception:
                pass
            self.logger(
                "ARCHIVE_CALLBACK_ERROR",
                stock_id=symbol,
                error="UNKNOWN_WATCHLIST_SYMBOL",
            )
            return

        base = {
            "received_at": utc_now(),
            "signal_date": self.archive_signal_date,
            "stock_id": symbol,
            "stock_name": item.stock_name,
            "market": item.market,
            "stage_a_rank": item.rank,
            "stage_a_score": item.score,
        }

        try:
            if kind == "ticks":
                self.archive.append(
                    "ticks",
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
                    "books",
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
                self.archive.callback_error()
            except Exception:
                pass
            self.logger(
                "ARCHIVE_CALLBACK_ERROR",
                stock_id=symbol,
                quote_kind=kind,
                error=f"{type(exc).__name__}: {exc}",
            )

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
                self.engine.record_tick(
                    symbol,
                    at=now,
                    price=getattr(value, "DealPrice", ""),
                    volume=getattr(value, "DealVol", ""),
                    bid=getattr(value, "BuyPrice", ""),
                    ask=getattr(value, "SellPrice", ""),
                    flag=getattr(value, "InOutFlag", ""),
                    serial=getattr(value, "SerialNo", 0),
                )
                self._archive_quote(
                    kind="ticks",
                    symbol=symbol,
                    value=value,
                )
                self.last_quote_at = now
                return
            if name == "SubscribeFiveTickA":
                symbol = _safe_text(getattr(value, "StkCode", ""))
                payload = _book_payload(value)
                if all(key in payload for key in ("buy_prices", "buy_volumes", "sell_prices", "sell_volumes")):
                    self.engine.record_book_combined(
                        symbol,
                        at=now,
                        buy_prices=payload["buy_prices"],
                        buy_volumes=payload["buy_volumes"],
                        sell_prices=payload["sell_prices"],
                        sell_volumes=payload["sell_volumes"],
                    )
                elif "prices" in payload and "volumes" in payload:
                    flag = str(payload.get("index_flag", ""))
                    side = "BUY" if "20" in flag else "SELL" if "21" in flag else ""
                    if side:
                        self.engine.record_book_side(
                            symbol,
                            at=now,
                            side=side,
                            prices=payload["prices"],
                            volumes=payload["volumes"],
                        )
                self._archive_quote(
                    kind="books",
                    symbol=symbol,
                    value=value,
                    payload=payload,
                )
                self.last_quote_at = now
        except Exception as exc:
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
        self.stock_list = self.api_types["List"][self.api_types["StockTick"]]()
        self.book_list = self.api_types["List"][self.api_types["FiveTickA"]]()
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
            raise RuntimeError("quote subscription was rejected by broker API")
        self.subscribed = True
        self.last_quote_at = None
        self.quote_started_at = datetime.now(TAIPEI)
        self.logger("QUOTES_SUBSCRIBED", count=len(items))

    def close(self) -> None:
        api = self.api
        if api is None:
            return
        if self.subscribed:
            try:
                api.UnSubscribeStockTick(self.account, self.stock_list, self.api_types["Language"].UTF8)
            except Exception:
                pass
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
    today = now.astimezone(TAIPEI).date()
    entries = [
        order for order in store.orders()
        if order.purpose.value == "ENTRY"
        and order.status == BrokerOrderStatus.FILLED
        and order.average_fill_price is not None
        and _stamp(order.created_at).date() == today
    ]
    exits = [
        order for order in store.orders()
        if order.purpose.value == "EXIT"
        and order.status == BrokerOrderStatus.FILLED
        and order.average_fill_price is not None
        and _stamp(order.created_at).date() == today
    ]
    total = Decimal("0")
    unused = list(exits)
    for entry in entries:
        match = next(
            (
                order for order in unused
                if order.symbol == entry.symbol
                and order.side != entry.side
                and _stamp(order.created_at) >= _stamp(entry.created_at)
            ),
            None,
        )
        if match is None:
            continue
        unused.remove(match)
        quantity = min(entry.filled_quantity, match.filled_quantity)
        side = "LONG" if entry.side.value == "BUY" else "SHORT"
        total += Decimal(str(engine._projected_net(
            side,
            float(entry.average_fill_price),
            float(match.average_fill_price),
            quantity,
        )))
    return total


def _trades_today(store: LiveOrderStore, now: datetime) -> int:
    today = now.astimezone(TAIPEI).date()
    return sum(
        1 for order in store.orders()
        if order.purpose.value == "ENTRY" and _stamp(order.created_at).date() == today
    )


def _recover_runtime_state(store: LiveOrderStore, metadata: dict[str, str], now: datetime) -> dict[str, Any]:
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
) -> str:
    """Decide whether a flat runtime stops or continues quote archiving."""
    if emergency:
        return "STOP_EMERGENCY"
    if graceful_stop:
        return "STOP_GRACEFUL"
    return "CONTINUE_ARCHIVE"


def _run_realtime(args, *, environment: str, submit_live: bool) -> int:
    runtime_dir = args.runtime_dir.resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = runtime_dir / "session.jsonl"
    kill_path = runtime_dir / "EMERGENCY_STOP"
    stop_path = runtime_dir / "STOP_REQUEST"
    baseline_path = args.baseline.resolve()
    db_path = runtime_dir / "live-orders.sqlite"
    notifier = RuntimeNotifier(runtime_dir)
    trading_notifier = AsyncTradingNotifier(runtime_dir)
    heartbeat = Heartbeat(runtime_dir)
    gate = LiveTradingGate.from_environment(cli_live=bool(args.live))
    if submit_live and not gate.authorized:
        raise RuntimeError("LIVE start requested but broker gate is not authorized")

    def log(event: str, **payload: Any) -> None:
        row = {"at": utc_now(), "event": event, **payload}
        _append_jsonl(log_path, row)
        print(json.dumps(row, ensure_ascii=False, default=str), flush=True)
        trading_notifier.emit(event, row)

    if kill_path.exists() and not args.recover_emergency:
        raise RuntimeError(
            f"persistent emergency stop is active: {kill_path}; "
            "use start-uat/start-prod --recover-emergency --live to run exit-only recovery"
        )

    if stop_path.exists():
        raise RuntimeError(
            f"persistent graceful stop request is active: {stop_path}; "
            "refusing realtime startup until the stop request is resolved"
        )

    seal, items, provenance = load_stage_a_watchlist()
    _validate_watchlist_day(str(seal["signal_date"]))
    metadata = {item.stock_id: item.stock_name for item in items}
    engine = LiveDirectionEngine(metadata, capital_twd=args.capital)
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
    session = _Session(api_types=api_types, environment=environment, credentials=credentials, engine=engine, logger=log)
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
    last_reconnect_at: datetime | None = None
    exit_attempt = 0
    next_exit_retry_at: datetime | None = None
    next_exit_reconcile_at: datetime | None = None
    exit_quote_alerted = False
    clean_shutdown = False
    archive = None
    archive_started_at = ""
    archive_error_type = ""
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
        archive_started_at = utc_now()
        archive = AppendOnlyRun(
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

        log(
            "ARCHIVE_STARTED",
            run_id=archive.run_id,
            mode=archive.mode,
            run_dir=str(archive.run_dir),
        )

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
        reconciliation = adapter.reconcile(timeout=args.reconcile_timeout, strict_positions=True)
        log("RECONCILIATION_PASSED", result=asdict(reconciliation), gate=gate.public_snapshot())
        if store.control_state()["halted"] and not (
            submit_live and args.recover_emergency and kill_path.exists()
        ):
            raise RuntimeError(f"broker execution store is halted: {store.control_state().get('reason')}")
        recovered = _recover_runtime_state(store, metadata, datetime.now(TAIPEI))
        entry_order_id = recovered["entry_order_id"]
        entry_signal = recovered["entry_signal"]
        entry_submitted_at = recovered["entry_submitted_at"]
        position = recovered["position"]
        exit_order_id = recovered["exit_order_id"]
        trade_attempted = recovered["trade_attempted"]
        pending_exit_reason = recovered["pending_exit_reason"]
        if any(value is not None and value is not False for key, value in recovered.items() if key not in {"trade_attempted"}):
            log("RUNTIME_STATE_RECOVERED", state={
                "entry_order_id": entry_order_id,
                "position": None if position is None else {"stock_id": position.stock_id, "side": position.side, "quantity": position.quantity, "entry_price": position.entry_price},
                "exit_order_id": exit_order_id,
                "trade_attempted": trade_attempted,
                "pending_exit_reason": pending_exit_reason,
            })
        session.subscribe(items)
        log(
            "RUNTIME_STARTED",
            environment=environment,
            submit_live=submit_live,
            signal_date=seal["signal_date"],
            capital=args.capital,
            short_enabled=allow_short,
            gate=gate.public_snapshot(),
        )
        heartbeat.beat(
            "RUNNING",
            environment=environment,
            submit_live=submit_live,
            signal_date=seal["signal_date"],
            watchlist_count=len(items),
            entry_start=SPEC["entry_start"],
            trade_attempted=trade_attempted,
            last_quote_at=session.last_quote_at,
            gate=gate.public_snapshot(),
        )

        while True:
            now = datetime.now(TAIPEI)
            emergency = kill_path.exists() or stop_event.is_set()
            graceful_stop = stop_path.exists()

            # Emergency always takes precedence over a graceful stop request.
            if emergency and graceful_stop:
                stop_path.unlink(missing_ok=True)
                graceful_stop = False

            runtime_state = (
                "EMERGENCY_EXIT"
                if emergency
                else ("STOPPING" if graceful_stop else "RUNNING")
            )
            heartbeat.beat(
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
            )
            decision = _decision_floor(now)
            decision_changed = engine.last_decision is None or decision > engine.last_decision

            if emergency and pending_exit_reason is None:
                pending_exit_reason = "EMERGENCY_STOP"
                log("EMERGENCY_STOP_REQUESTED")

            if graceful_stop and not emergency and pending_exit_reason is None:
                pending_exit_reason = "GRACEFUL_STOP"
                log("GRACEFUL_STOP_REQUESTED")

            if emergency and entry_order_id is None and position is None and exit_order_id is None:
                if submit_live:
                    store.halt("MANUAL_EMERGENCY_STOP_NO_EXPOSURE")
                log("EMERGENCY_STOP_COMPLETE", exposure="NONE")
                clean_shutdown = True
                return 0

            if (
                graceful_stop
                and not emergency
                and entry_order_id is None
                and position is None
                and exit_order_id is None
            ):
                stop_path.unlink(missing_ok=True)
                log("GRACEFUL_STOP_COMPLETE", exposure="NONE")
                clean_shutdown = True
                return 0

            # Entry lifecycle and actual fill state.
            if entry_order_id is not None:
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
                        log("POSITION_OPENED", stock_id=position.stock_id, side=position.side, quantity=position.quantity, average_fill_price=avg)
                    elif not position.exit_submitted:
                        net_quantity = abs(int(store.positions().get(position.stock_id, order.filled_quantity)))
                        if net_quantity > 0:
                            position.quantity = net_quantity
                        position.entry_price = avg

                if order.status not in TERMINAL and entry_submitted_at is not None:
                    expired = (now - entry_submitted_at).total_seconds() >= args.entry_timeout
                    if (emergency or pending_exit_reason is not None or expired) and not entry_cancel_requested:
                        if order.broker_order_no:
                            adapter.cancel(entry_order_id, "runtime entry protection")
                            entry_cancel_requested = True
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
                        if submit_live:
                            store.halt("MANUAL_EMERGENCY_STOP_NO_EXPOSURE")
                        log("EMERGENCY_STOP_COMPLETE", exposure="NONE")
                        return 0
                    if graceful_stop:
                        stop_path.unlink(missing_ok=True)
                        log("GRACEFUL_STOP_COMPLETE", exposure="NONE")
                        clean_shutdown = True
                        return 0

            # Strategy decision clock. Observe mode uses the exact same live state but never submits.
            reversal = False
            if decision_changed:
                if position is not None:
                    reversal = engine.opposite_signal(position, decision)
                    engine.last_decision = decision
                elif (
                    not trade_attempted
                    and not emergency
                    and pending_exit_reason is None
                    and entry_order_id is None
                ):
                    candidate = engine.choose_entry(decision, allow_short=allow_short)
                    if candidate is not None:
                        age = engine.quote_age_seconds(candidate.stock_id, now)
                        decision_result = risk.evaluate_entry(
                            signal=candidate,
                            quote_age_seconds=float("inf") if age is None else age,
                            broker_positions=store.positions(),
                            open_orders=store.orders(open_only=True),
                            trades_today=_trades_today(store, now),
                            realized=_realized_pnl_today(store, engine, now),
                            halted=bool(store.control_state()["halted"]),
                        )
                        if not decision_result.approved:
                            log(
                                "RISK_REJECTED_CANDIDATE",
                                candidate=asdict(candidate),
                                reasons=decision_result.reasons,
                            )
                            continue
                        log("RISK_APPROVED_CANDIDATE", candidate=asdict(candidate))
                        if submit_live:
                            assert decision_result.intent is not None
                            intent = bridge_strategy_intent(
                                decision_result.intent,
                                short_entry_order_type=short_entry,
                                short_cover_order_type=short_cover,
                            )
                            order = adapter.submit(intent)
                            entry_order_id = order.client_order_id
                            entry_signal = candidate
                            entry_submitted_at = now
                            trade_attempted = True
                            log("ENTRY_SUBMITTED", client_order_id=entry_order_id, intent_id=intent.intent_id, stock_id=intent.symbol, side=intent.side.value, quantity=intent.quantity, price=str(intent.price))
                else:
                    engine.last_decision = decision

            # Exit rule may trigger only after an actual fill exists. If entry still has a live remainder,
            # cancel it first so the exit quantity cannot race against later entry fills.
            if (
                position is not None
                and not position.exit_submitted
                and (next_exit_retry_at is None or now >= next_exit_retry_at)
            ):
                safe_quote = engine.safe_exit_quote(
                    position, now, max_age_seconds=args.exit_quote_staleness
                )
                if safe_quote is not None:
                    exit_quote_alerted = False
                if safe_quote is not None and risk.loss_kill_required(
                    realized=_realized_pnl_today(store, engine, now),
                    unrealized=Decimal(str(engine.projected_net(position, safe_quote.price))),
                ):
                    pending_exit_reason = "MAX_DAILY_LOSS"
                    kill_path.write_text(f"{utc_now()} MAX_DAILY_LOSS\n", encoding="utf-8")
                    emergency = True
                    notifier.critical(
                        "MAX_DAILY_LOSS",
                        "Daily loss boundary reached; entry is disabled and exit recovery is active",
                        stock_id=position.stock_id,
                    )
                exit_decision = engine.evaluate_exit(
                    position,
                    now,
                    reversal=reversal,
                    max_quote_age_seconds=args.exit_quote_staleness,
                )
                if (emergency or graceful_stop) and exit_decision is None:
                    if safe_quote is not None:
                        from .strategy import ExitDecision
                        projected = engine.projected_net(position, safe_quote.price)
                        exit_decision = ExitDecision(
                            pending_exit_reason
                            or ("EMERGENCY_STOP" if emergency else "GRACEFUL_STOP"),
                            safe_quote.price,
                            projected,
                            projected / (position.entry_price * position.quantity),
                        )
                    elif not exit_quote_alerted:
                        exit_quote_alerted = True
                        notifier.critical(
                            "EXIT_QUOTE_UNAVAILABLE",
                            "Exposure exists but no fresh bounded quote is available; no stale-price order was sent",
                            stock_id=position.stock_id,
                        )
                if exit_decision is not None:
                    pending_exit_reason = exit_decision.reason
                    entry_terminal = entry_order_id is None or store.get(entry_order_id).status in TERMINAL
                    if not entry_terminal:
                        if not entry_cancel_requested and store.get(entry_order_id).broker_order_no:
                            adapter.cancel(
                                entry_order_id,
                                f"exit:{exit_decision.reason}",
                                emergency=emergency,
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
                        order = (
                            adapter.submit_rescue(intent)
                            if emergency or store.control_state()["halted"] or exit_attempt > 1
                            else adapter.submit(intent)
                        )
                        exit_order_id = order.client_order_id
                        position.exit_submitted = True
                        next_exit_retry_at = None
                        last_exit_reprice = now
                        log("EXIT_SUBMITTED", client_order_id=exit_order_id, reason=exit_decision.reason, quantity=intent.quantity, price=str(intent.price), projected_net_pnl=exit_decision.projected_net_pnl)

            # If an exit trigger was waiting for the entry cancel to settle, submit it as soon as terminal.
            if (
                submit_live
                and pending_exit_reason is not None
                and position is not None
                and not position.exit_submitted
                and (next_exit_retry_at is None or now >= next_exit_retry_at)
                and entry_order_id is not None
                and store.get(entry_order_id).status in TERMINAL
            ):
                safe_quote = engine.safe_exit_quote(
                    position, now, max_age_seconds=args.exit_quote_staleness
                )
                if safe_quote is not None:
                    exit_attempt += 1
                    raw = risk.approve_exit(
                        position=position,
                        quantity=position.quantity,
                        price=safe_quote.price,
                        reason=pending_exit_reason,
                        attempt=exit_attempt,
                    )
                    intent = bridge_strategy_intent(raw, short_entry_order_type=short_entry, short_cover_order_type=short_cover)
                    order = (
                        adapter.submit_rescue(intent)
                        if emergency or store.control_state()["halted"] or exit_attempt > 1
                        else adapter.submit(intent)
                    )
                    exit_order_id = order.client_order_id
                    position.exit_submitted = True
                    next_exit_retry_at = None
                    last_exit_reprice = now
                    log("EXIT_SUBMITTED", client_order_id=exit_order_id, reason=pending_exit_reason, quantity=intent.quantity, price=str(intent.price))

            if exit_order_id is not None:
                exit_order = store.get(exit_order_id)
                if exit_order.status == BrokerOrderStatus.FILLED:
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

                    # Normal strategy completion is no longer the end of the
                    # realtime process. The single LIVE trade allowance has
                    # already been consumed (trade_attempted stays True), so
                    # clear only transient position/order tracking and keep
                    # the same Yuanta quote session alive through 13:25.
                    entry_order_id = None
                    entry_signal = None
                    entry_submitted_at = None
                    entry_cancel_requested = False

                    position = None

                    exit_order_id = None
                    pending_exit_reason = None
                    last_exit_reprice = None
                    exit_attempt = 0
                    next_exit_retry_at = None
                    next_exit_reconcile_at = None
                    exit_quote_alerted = False

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
                    store.halt(reason)
                    kill_path.write_text(f"{utc_now()} EXIT_NOT_FILLED:{exit_order.status.value}\n", encoding="utf-8")
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
                    exit_order.broker_order_no
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
                        adapter.modify_price(
                            exit_order_id,
                            Decimal(str(new_price)),
                            emergency=emergency or store.control_state()["halted"],
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
                            session.subscribe(items)
                            quote_stale_triggered = False
                            log("QUOTE_RECONNECT_PASSED", reconciliation=asdict(reconnection))
                        except Exception as exc:
                            log("QUOTE_RECONNECT_FAILED", error=f"{type(exc).__name__}: {exc}")
                else:
                    quote_stale_triggered = False

            time.sleep(0.10)
    except Exception as exc:
        archive_error_type = type(exc).__name__
        raise
    finally:
        try:
            heartbeat.stopped(
                clean_shutdown,
                environment=environment,
                submit_live=submit_live,
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
                )
            except Exception:
                pass
        credentials.update({"pfx_password": "", "trading_password": ""})
        if adapter is not None:
            try:
                adapter.close()
            except Exception as exc:
                log(
                    "ADAPTER_CLOSE_ERROR",
                    error=f"{type(exc).__name__}: {exc}",
                )

        session.close()

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

        store.close()
        _release_runtime_instance_lock(runtime_lock)


def time_from_text(text: str):
    from datetime import time as time_cls
    hour, minute = map(int, text.split(":"))
    return time_cls(hour, minute)


def _connect_for_control(args, environment: str, *, baseline: dict[str, int], cli_live: bool):
    credentials = load_credentials()
    api_types = _extend_quote_types(load_api_types(args.vendor_dir.resolve()))
    logger = lambda event, **payload: print(json.dumps({"at": utc_now(), "event": event, **payload}, ensure_ascii=False, default=str))
    session = _Session(api_types=api_types, environment=environment, credentials=credentials, engine=None, logger=logger)
    store = LiveOrderStore(args.runtime_dir.resolve() / "live-orders.sqlite")
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


def _preflight(args, environment: str) -> int:
    baseline = _load_baseline(args.baseline.resolve())
    seal, items, _provenance = load_stage_a_watchlist()
    _validate_watchlist_day(str(seal["signal_date"]))
    metadata = {item.stock_id: item.stock_name for item in items}
    engine = LiveDirectionEngine(metadata)
    credentials = load_credentials()
    api_types = _extend_quote_types(load_api_types(args.vendor_dir.resolve()))
    logger = lambda event, **payload: print(json.dumps(
        {"at": utc_now(), "event": event, **payload}, ensure_ascii=False, default=str
    ))
    session = _Session(
        api_types=api_types,
        environment=environment,
        credentials=credentials,
        engine=engine,
        logger=logger,
    )
    store = LiveOrderStore(args.runtime_dir.resolve() / "live-orders.sqlite")
    adapter = None
    try:
        session.connect()
        assert session.api is not None
        adapter = YuantaSparkExecutionAdapter(
            api=session.api,
            api_types=api_types,
            account=session.account,
            store=store,
            live_gate=LiveTradingGate.from_environment(cli_live=False),
            position_baseline=baseline,
        )
        result = adapter.reconcile(timeout=args.reconcile_timeout, strict_positions=True)
        session.subscribe(items)
        print(json.dumps({
            "status": "READY",
            "environment": environment,
            "signal_date": seal["signal_date"],
            "quote_subscription": "ACCEPTED",
            "reconciliation": asdict(result),
            "gate": adapter.live_gate.public_snapshot(),
        }, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        credentials.update({"pfx_password": "", "trading_password": ""})
        if adapter is not None:
            adapter.close()
        session.close(); store.close()


def _capture_baseline(args, environment: str) -> int:
    if not args.accept_existing_positions:
        raise RuntimeError("baseline capture requires --accept-existing-positions")
    credentials, session, store, adapter = _connect_for_control(args, environment, baseline={}, cli_live=False)
    try:
        result = adapter.reconcile(timeout=args.reconcile_timeout, strict_positions=False)
        positions = dict(result.broker_positions)
        _write_baseline(args.baseline.resolve(), positions)
        print(json.dumps({"status": "BASELINE_CAPTURED", "path": str(args.baseline.resolve()), "positions": positions}, ensure_ascii=False, indent=2))
        return 0
    finally:
        credentials.update({"pfx_password": "", "trading_password": ""})
        adapter.close(); session.close(); store.close()


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
