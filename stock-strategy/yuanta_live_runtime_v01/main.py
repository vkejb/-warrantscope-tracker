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
from yuanta_intraday_shadow_v01.collector import load_stage_a_watchlist, utc_now
from yuanta_intraday_shadow_v01.collector_main import _book_payload
from yuanta_intraday_shadow_v01.main import DEFAULT_VENDOR_DIR as SHADOW_DEFAULT_VENDOR_DIR, _safe_text
from yuanta_intraday_shadow_v01.yuanta_keychain import load_credentials, status as credential_status

from .strategy import LiveDirectionEngine, ManagedPosition

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
    ):
        self.api_types = api_types
        self.environment = environment
        self.credentials = credentials
        self.engine = engine
        self.logger = logger
        self.login_event = threading.Event()
        self.login_ok = False
        self.login_code = ""
        self.api = None
        self.account = credentials["account"]
        self.stock_list = None
        self.book_list = None
        self.subscribed = False
        self.last_quote_at: datetime | None = None

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
        self.api.SubscribeStockTick(self.account, self.stock_list, self.api_types["Language"].UTF8)
        self.api.SubscribeFiveTickA(self.account, self.book_list, self.api_types["Language"].UTF8)
        self.subscribed = True
        self.last_quote_at = datetime.now(TAIPEI)  # grace period until real callbacks arrive
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



def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(TAIPEI)


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


def _strategy_raw(*, signal, purpose: str, quantity: int, price: float, reason: str = "") -> dict[str, Any]:
    if purpose == "ENTRY":
        side = "BUY" if signal.side == "LONG" else "SELL"
        stamp = signal.decision_time.strftime("%Y%m%d-%H%M%S")
        intent_id = f"realtime-{stamp}-{signal.stock_id}-{signal.side.lower()}-entry"
    else:
        side = "SELL" if signal.side == "LONG" else "BUY"
        stamp = datetime.now(TAIPEI).strftime("%Y%m%d-%H%M%S")
        intent_id = f"realtime-{stamp}-{signal.stock_id}-{signal.side.lower()}-exit-{reason.lower()}"
    return {
        "status": "APPROVED",
        "intent_id": intent_id,
        "stock_id": signal.stock_id,
        "side": side,
        "intent_type": purpose,
        "quantity_lots": str(Decimal(quantity) / Decimal(1000)),
        "suggested_limit_price": str(Decimal(str(price))),
    }


def _decision_floor(now: datetime) -> datetime:
    step = 30
    second = (now.second // step) * step
    return now.replace(second=second, microsecond=0)


def _run_realtime(args, *, environment: str, submit_live: bool) -> int:
    runtime_dir = args.runtime_dir.resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = runtime_dir / "session.jsonl"
    kill_path = runtime_dir / "EMERGENCY_STOP"
    baseline_path = args.baseline.resolve()
    db_path = runtime_dir / "live-orders.sqlite"

    def log(event: str, **payload: Any) -> None:
        row = {"at": utc_now(), "event": event, **payload}
        _append_jsonl(log_path, row)
        print(json.dumps(row, ensure_ascii=False, default=str), flush=True)

    if kill_path.exists() and not args.recover_emergency:
        raise RuntimeError(
            f"persistent emergency stop is active: {kill_path}; "
            "use start-uat/start-prod --recover-emergency --live to run exit-only recovery"
        )

    seal, items, _provenance = load_stage_a_watchlist()
    _validate_watchlist_day(str(seal["signal_date"]))
    metadata = {item.stock_id: item.stock_name for item in items}
    engine = LiveDirectionEngine(metadata, capital_twd=args.capital)
    credentials = load_credentials()
    api_types = _extend_quote_types(load_api_types(args.vendor_dir.resolve()))
    session = _Session(api_types=api_types, environment=environment, credentials=credentials, engine=engine, logger=log)
    short_entry = _order_type(args.short_entry_order_type)
    short_cover = _order_type(args.short_cover_order_type)
    allow_short = short_entry is not None and short_cover is not None
    baseline = _load_baseline(baseline_path)
    gate = LiveTradingGate.from_environment(cli_live=bool(args.live))
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
    try:
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
        if store.control_state()["halted"]:
            raise RuntimeError(f"broker execution store is halted: {store.control_state().get('reason')}")
        if submit_live and not gate.authorized:
            raise RuntimeError("LIVE start requested but broker gate is not authorized")
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

        while True:
            now = datetime.now(TAIPEI)
            emergency = kill_path.exists() or stop_event.is_set()
            decision = _decision_floor(now)
            decision_changed = engine.last_decision is None or decision > engine.last_decision

            if emergency and pending_exit_reason is None:
                pending_exit_reason = "EMERGENCY_STOP"
                log("EMERGENCY_STOP_REQUESTED")
            if emergency and entry_order_id is None and position is None and exit_order_id is None:
                if submit_live:
                    store.halt("MANUAL_EMERGENCY_STOP_NO_EXPOSURE")
                log("EMERGENCY_STOP_COMPLETE", exposure="NONE")
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
                    log("ENTRY_NOT_FILLED", client_order_id=entry_order_id, status=order.status.value)
                    entry_order_id = None
                    if emergency:
                        if submit_live:
                            store.halt("MANUAL_EMERGENCY_STOP_NO_EXPOSURE")
                        log("EMERGENCY_STOP_COMPLETE", exposure="NONE")
                        return 0

            # Strategy decision clock. Observe mode uses the exact same live state but never submits.
            reversal = False
            if decision_changed:
                if position is not None:
                    reversal = engine.opposite_signal(position, decision)
                    engine.last_decision = decision
                elif not trade_attempted and not emergency and entry_order_id is None:
                    candidate = engine.choose_entry(decision, allow_short=allow_short)
                    if candidate is not None:
                        log("APPROVED_CANDIDATE", candidate=asdict(candidate))
                        if submit_live:
                            raw = _strategy_raw(signal=candidate, purpose="ENTRY", quantity=candidate.quantity, price=candidate.entry_price)
                            intent = bridge_strategy_intent(
                                raw,
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
            if position is not None and not position.exit_submitted:
                exit_decision = engine.evaluate_exit(position, now, reversal=reversal)
                if emergency and exit_decision is None:
                    price = engine.latest_exit_price(position)
                    if price is not None:
                        from .strategy import ExitDecision
                        exit_decision = ExitDecision("EMERGENCY_STOP", price, 0.0, 0.0)
                if exit_decision is not None:
                    pending_exit_reason = exit_decision.reason
                    entry_terminal = entry_order_id is None or store.get(entry_order_id).status in TERMINAL
                    if not entry_terminal:
                        if not entry_cancel_requested and store.get(entry_order_id).broker_order_no:
                            adapter.cancel(entry_order_id, f"exit:{exit_decision.reason}")
                            entry_cancel_requested = True
                            log("ENTRY_CANCEL_SENT", client_order_id=entry_order_id, reason=exit_decision.reason)
                    elif submit_live:
                        raw = _strategy_raw(
                            signal=position,
                            purpose="EXIT",
                            quantity=position.quantity,
                            price=exit_decision.price,
                            reason=exit_decision.reason,
                        )
                        intent = bridge_strategy_intent(
                            raw,
                            short_entry_order_type=short_entry,
                            short_cover_order_type=short_cover,
                        )
                        order = adapter.submit(intent)
                        exit_order_id = order.client_order_id
                        position.exit_submitted = True
                        last_exit_reprice = now
                        log("EXIT_SUBMITTED", client_order_id=exit_order_id, reason=exit_decision.reason, quantity=intent.quantity, price=str(intent.price), projected_net_pnl=exit_decision.projected_net_pnl)

            # If an exit trigger was waiting for the entry cancel to settle, submit it as soon as terminal.
            if (
                submit_live
                and pending_exit_reason is not None
                and position is not None
                and not position.exit_submitted
                and entry_order_id is not None
                and store.get(entry_order_id).status in TERMINAL
            ):
                price = engine.latest_exit_price(position)
                if price is not None:
                    raw = _strategy_raw(signal=position, purpose="EXIT", quantity=position.quantity, price=price, reason=pending_exit_reason)
                    intent = bridge_strategy_intent(raw, short_entry_order_type=short_entry, short_cover_order_type=short_cover)
                    order = adapter.submit(intent)
                    exit_order_id = order.client_order_id
                    position.exit_submitted = True
                    last_exit_reprice = now
                    log("EXIT_SUBMITTED", client_order_id=exit_order_id, reason=pending_exit_reason, quantity=intent.quantity, price=str(intent.price))

            if exit_order_id is not None:
                exit_order = store.get(exit_order_id)
                if exit_order.status == BrokerOrderStatus.FILLED:
                    log("POSITION_CLOSED", client_order_id=exit_order_id, average_fill_price=str(exit_order.average_fill_price), quantity=exit_order.filled_quantity)
                    if emergency:
                        store.halt("MANUAL_EMERGENCY_STOP_FLAT")
                    return 0
                if exit_order.status in {BrokerOrderStatus.CANCELED, BrokerOrderStatus.REJECTED, BrokerOrderStatus.EXPIRED, BrokerOrderStatus.UNKNOWN}:
                    store.halt(f"EXIT_NOT_FILLED:{exit_order.status.value}")
                    kill_path.write_text(f"{utc_now()} EXIT_NOT_FILLED:{exit_order.status.value}\n", encoding="utf-8")
                    raise RuntimeError(f"exit order entered terminal unsafe state: {exit_order.status.value}")
                if (
                    exit_order.broker_order_no
                    and last_exit_reprice is not None
                    and (now - last_exit_reprice).total_seconds() >= args.exit_reprice_seconds
                    and store.pending_mutation(exit_order_id) is None
                ):
                    new_price = engine.latest_exit_price(position) if position is not None else None
                    if new_price is not None and (exit_order.price is None or Decimal(str(new_price)) != exit_order.price):
                        adapter.modify_price(exit_order_id, Decimal(str(new_price)))
                        last_exit_reprice = now
                        log("EXIT_REPRICE_SENT", client_order_id=exit_order_id, price=new_price)

            # End normally after hard-exit window if no trade was taken.
            if now.time() >= time_from_text("13:25") and entry_order_id is None and position is None:
                log("NO_TRADE_SESSION_COMPLETE")
                return 0

            # Quote outage: with no exposure it is safe to rebuild the quote/broker
            # session and reconcile before doing anything else. With exposure, keep
            # the existing broker session alive and turn the outage into an exit trigger.
            if session.last_quote_at is not None and time_from_text("09:05") <= now.time() <= time_from_text("13:25"):
                stale = (now - session.last_quote_at).total_seconds()
                if stale > args.max_quote_staleness:
                    exposed = entry_order_id is not None or position is not None
                    if exposed:
                        if not quote_stale_triggered:
                            quote_stale_triggered = True
                            pending_exit_reason = pending_exit_reason or "QUOTE_STALE"
                            log("LIVE_QUOTE_STALE_WITH_EXPOSURE", stale_seconds=round(stale, 1))
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
    finally:
        credentials.update({"pfx_password": "", "trading_password": ""})
        if adapter is not None:
            try:
                adapter.close()
            except Exception as exc:
                log("ADAPTER_CLOSE_ERROR", error=f"{type(exc).__name__}: {exc}")
        session.close()
        store.close()


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
    credentials, session, store, adapter = _connect_for_control(args, environment, baseline=baseline, cli_live=False)
    try:
        result = adapter.reconcile(timeout=args.reconcile_timeout, strict_positions=True)
        print(json.dumps({"status": "READY", "environment": environment, "reconciliation": asdict(result), "gate": adapter.live_gate.public_snapshot()}, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        credentials.update({"pfx_password": "", "trading_password": ""})
        adapter.close(); session.close(); store.close()


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


def _kill(args) -> int:
    runtime = args.runtime_dir.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    marker = runtime / "EMERGENCY_STOP"
    marker.write_text(f"{utc_now()} {args.reason}\n", encoding="utf-8")
    print(json.dumps({"status": "EMERGENCY_STOP_REQUESTED", "marker": str(marker)}, ensure_ascii=False, indent=2))
    return 0


def _clear_halt(args) -> int:
    runtime = args.runtime_dir.resolve()
    store = LiveOrderStore(runtime / "live-orders.sqlite")
    try:
        if store.orders(open_only=True):
            raise RuntimeError("cannot clear halt while broker orders are open")
        if store.positions():
            raise RuntimeError("cannot clear halt while strategy positions are non-flat")
        store.clear_halt(args.reason)
        marker = runtime / "EMERGENCY_STOP"
        marker.unlink(missing_ok=True)
        print(json.dumps({"status": "HALT_CLEARED", "reason": args.reason}, ensure_ascii=False, indent=2))
        return 0
    finally:
        store.close()


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
    parser.add_argument("--live", action="store_true", help="required together with EXECUTION_MODE=LIVE and ENABLE_LIVE_TRADING=YES")
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--entry-timeout", type=float, default=15.0)
    parser.add_argument("--exit-reprice-seconds", type=float, default=5.0)
    parser.add_argument("--max-quote-staleness", type=float, default=30.0)
    parser.add_argument("--reconnect-cooldown", type=float, default=60.0)
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

    kill = sub.add_parser("kill")
    kill.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    kill.add_argument("--reason", required=True)

    clear = sub.add_parser("clear-halt")
    clear.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    clear.add_argument("--reason", required=True)

    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "status":
            return _status(args)
        if args.command == "kill":
            return _kill(args)
        if args.command == "clear-halt":
            return _clear_halt(args)
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
