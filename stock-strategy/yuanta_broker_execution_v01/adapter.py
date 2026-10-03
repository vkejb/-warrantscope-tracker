"""Live Yuanta SPARK broker execution adapter.

The adapter is deliberately broker-only: it does not change signal generation,
position sizing, risk rules, or market-data logic.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import hashlib
import inspect
import json
import math
from queue import Empty, Queue
import re
import threading
import time
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from .models import (
    BrokerOrderStatus,
    ExecutionIntent,
    PRICE_FLAG,
    TIF_CODE,
    StoredOrder,
    IntentPurpose,
    TERMINAL_STATUSES,
)
from .gate import LiveTradingGate
from .store import LiveOrderStore


TAIPEI = ZoneInfo("Asia/Taipei")


class BrokerAdapterError(RuntimeError):
    pass


class LiveExecutionDisabled(BrokerAdapterError):
    pass


class ReconciliationMismatch(BrokerAdapterError):
    pass


class ReconciliationRequired(BrokerAdapterError):
    pass


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    status: str
    local_positions: dict[str, int]
    position_baseline: dict[str, int]
    expected_broker_positions: dict[str, int]
    broker_positions: dict[str, int]
    order_mismatches: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class BrokerStateSnapshot:
    orders: list[dict[str, Any]]
    open_orders: list[dict[str, Any]]
    positions: dict[str, int]


def _safe(value: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(value, name)
    except Exception:
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _as_str(value: Any, default: str = "") -> str:
    try:
        text = str(value)
    except Exception:
        return default
    return text.strip()


def _collection(value: Any) -> list[Any]:
    if value is None:
        return []
    try:
        return list(value)
    except Exception:
        pass
    count = _as_int(_safe(value, "Count", 0), 0)
    result = []
    for index in range(count):
        try:
            result.append(value[index])
        except Exception:
            break
    return result


def _required_attribute(value: Any, name: str) -> Any:
    try:
        result = getattr(value, name)
    except Exception as exc:
        raise BrokerAdapterError(f"missing/unreadable broker field {name}") from exc
    if result is None:
        raise BrokerAdapterError(f"null broker field {name}")
    return result


def _strict_integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise BrokerAdapterError(f"invalid broker integer {name}")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number != number.to_integral_value() or number < minimum:
            raise ValueError(name)
        return int(number)
    except (ValueError, ArithmeticError, TypeError) as exc:
        raise BrokerAdapterError(f"invalid broker integer {name}") from exc


def _optional_integer(value: Any, name: str, default: int = 0) -> int:
    try:
        raw = getattr(value, name)
    except AttributeError:
        return default
    return _strict_integer(raw, name)


def _required_collection(value: Any, name: str) -> list[Any]:
    """Read an explicit complete Python/.NET list, never truncate to flat."""
    collection = _required_attribute(value, name)
    if isinstance(collection, (str, bytes, bytearray, Mapping)):
        raise BrokerAdapterError(f"invalid broker collection {name}")
    if isinstance(collection, (list, tuple)):
        return list(collection)
    # SPARK exposes .NET List<T>. Count + indexed access proves the whole
    # collection was read; falling back to a partial iterator is not safe.
    count = _strict_integer(_required_attribute(collection, "Count"), f"{name}.Count")
    try:
        return [collection[index] for index in range(count)]
    except Exception as exc:
        raise BrokerAdapterError(f"incomplete broker collection {name}") from exc


def _validate_snapshot_order(value: Any, *, merge: bool, account: str) -> None:
    report_account = str(_required_attribute(value, "Account")).strip().upper()
    if report_account != account:
        raise BrokerAdapterError("broker snapshot account mismatch")
    if not str(_required_attribute(value, "OrderNo")).strip() or not str(_required_attribute(value, "CompanyNo")).strip():
        raise BrokerAdapterError("broker snapshot missing order/symbol identity")
    if str(_required_attribute(value, "BS")).strip().upper() not in {"B", "S", "BUY", "SELL"}:
        raise BrokerAdapterError("invalid broker snapshot side")
    _strict_integer(_required_attribute(value, "RptType"), "RptType")
    _strict_integer(_required_attribute(value, "OrderStatus"), "OrderStatus")
    quantity = _strict_integer(_required_attribute(value, "OrderQty"), "OrderQty")
    if merge:
        filled = _strict_integer(_required_attribute(value, "OkQty"), "OkQty")
        _strict_integer(_required_attribute(value, "LastOrderStatus"), "LastOrderStatus")
        if filled > quantity:
            raise BrokerAdapterError("broker snapshot fill quantity exceeds order quantity")
    # Any provided amount/price must also be valid; absent nonessential fields
    # stay backward compatible with the official sparse report variants.
    for field in ("BeforeQty", "APCode", "TradeKind"):
        try:
            raw = getattr(value, field)
        except AttributeError:
            continue
        _strict_integer(raw, field)
    for field in ("Price", "LastDealPrice", "AvgDealPrice"):
        try:
            raw = getattr(value, field)
        except AttributeError:
            continue
        try:
            number = Decimal(str(raw))
            if not number.is_finite() or number < 0:
                raise ValueError(field)
        except (ValueError, ArithmeticError, TypeError) as exc:
            raise BrokerAdapterError(f"invalid broker snapshot price {field}") from exc


def _normalise_order_result(value: Any) -> list[dict[str, Any]]:
    result = []
    for item in _collection(_safe(value, "ResultList")):
        result.append(
            {
                "identify": _as_int(_safe(item, "Identify", _safe(item, "Identity", 0))),
                "reply_code": _as_int(_safe(item, "ReplyCode", -1), -1),
                "order_no": _as_str(_safe(item, "OrderNO", _safe(item, "OrderNo", ""))),
                "err_type": _as_str(_safe(item, "ErrType", "")),
                "err_no": _as_str(_safe(item, "ErrNO", "")),
                "advisory": _as_str(_safe(item, "Advisory", "")),
            }
        )
    return result


def _optional_temporal_attribute(value: Any, name: str) -> Any:
    """Absent legacy fields are allowed; unreadable provided fields are not."""
    try:
        return getattr(value, name)
    except AttributeError as exc:
        missing = object()
        if inspect.getattr_static(value, name, missing) is not missing:
            raise BrokerAdapterError(f"unreadable broker field {name}") from exc
        return None
    except Exception as exc:
        raise BrokerAdapterError(f"unreadable broker field {name}") from exc


def _validated_report_day(year: int, month: int, day: int, field: str) -> str:
    try:
        # SPARK documents Gregorian dates. Do not guess ROC years or replace
        # unknown report dates with today's/query/receipt date.
        if year < 1900:
            raise ValueError(field)
        stamp = datetime(year, month, day)
    except (ValueError, OverflowError) as exc:
        raise BrokerAdapterError(f"invalid broker date {field}") from exc
    return stamp.strftime("%Y%m%d")


def _normalise_report_temporal_fields(value: Any) -> dict[str, str]:
    """Read the documented report OrderDate/OrderTime without a clock fallback.

    YSendOrder.py uses OrderDate.ushtYear/bytMon/bytDay and
    OrderTime.bytHour/bytMin/bytSec/ushtMSec for both report families.
    Existing explicit TradeDate strings remain unchanged after validation.
    """
    raw_trade_date = _optional_temporal_attribute(value, "TradeDate")
    trade_date = ""
    explicit_day = ""
    if raw_trade_date is not None:
        try:
            trade_date = str(raw_trade_date).strip()
        except Exception as exc:
            raise BrokerAdapterError("unreadable broker date TradeDate") from exc
        if trade_date:
            if not re.fullmatch(
                r"(?:[0-9]{8}|[0-9]{4}/[0-9]{2}/[0-9]{2}|[0-9]{4}-[0-9]{2}-[0-9]{2})",
                trade_date,
            ):
                raise BrokerAdapterError("invalid broker date TradeDate")
            digits = trade_date.replace("/", "").replace("-", "")
            explicit_day = _validated_report_day(
                int(digits[:4]), int(digits[4:6]), int(digits[6:]), "TradeDate"
            )

    native_date = _optional_temporal_attribute(value, "OrderDate")
    native_day = ""
    if native_date is not None and not (
        isinstance(native_date, str) and not native_date.strip()
    ):
        components = [
            _strict_integer(_required_attribute(native_date, component), f"OrderDate.{component}")
            for component in ("ushtYear", "bytMon", "bytDay")
        ]
        native_day = _validated_report_day(*components, "OrderDate")
    if explicit_day and native_day and explicit_day != native_day:
        raise BrokerAdapterError("conflicting broker dates TradeDate/OrderDate")

    native_time = _optional_temporal_attribute(value, "OrderTime")
    order_time = ""
    if native_time is not None and not (
        isinstance(native_time, str) and not native_time.strip()
    ):
        hour, minute, second, milliseconds = [
            _strict_integer(_required_attribute(native_time, component), f"OrderTime.{component}")
            for component in ("bytHour", "bytMin", "bytSec", "ushtMSec")
        ]
        try:
            if milliseconds > 999:
                raise ValueError("milliseconds")
            datetime(2000, 1, 1, hour, minute, second, milliseconds * 1000)
        except (ValueError, OverflowError) as exc:
            raise BrokerAdapterError("invalid broker time OrderTime") from exc
        order_time = f"{hour:02d}:{minute:02d}:{second:02d}.{milliseconds:03d}"

    source = (
        "TradeDate+OrderDate" if explicit_day and native_day
        else "TradeDate" if explicit_day
        else "OrderDate" if native_day
        else ""
    )
    return {
        "trade_date": trade_date if explicit_day else native_day,
        "trade_date_source": source,
        "order_time": order_time,
    }


def _normalise_real_report(value: Any) -> dict[str, Any]:
    return {
        "account": _as_str(_safe(value, "Account", "")),
        "rpt_type": _strict_integer(_required_attribute(value, "RptType"), "RptType"),
        "order_no": _as_str(_safe(value, "OrderNo", "")),
        "symbol": _as_str(_safe(value, "CompanyNo", "")),
        "side": _as_str(_safe(value, "BS", "")),
        "order_type": _as_str(_safe(value, "OrderType", "") or ""),
        "price": _as_str(_safe(value, "Price", "0")),
        "before_qty": _optional_integer(value, "BeforeQty"),
        "order_qty": _strict_integer(_required_attribute(value, "OrderQty"), "OrderQty"),
        "trade_kind": _optional_integer(value, "TradeKind"),
        "ap_code": _optional_integer(value, "APCode"),
        "basket_no": _as_str(_safe(value, "BasketNo", "")),
        "order_status": _strict_integer(_required_attribute(value, "OrderStatus"), "OrderStatus"),
        "seq_no": _as_str(_safe(value, "SeqNo", "")),
        "stk_error_no": _as_str(_safe(value, "StkErrorNo", "")),
        "order_error_no": _as_str(_safe(value, "OrderErrorNo", "")),
        **_normalise_report_temporal_fields(value),
    }


def _normalise_merge_report(value: Any) -> dict[str, Any]:
    return {
        "account": _as_str(_safe(value, "Account", "")),
        "rpt_type": _strict_integer(_required_attribute(value, "RptType"), "RptType"),
        "order_no": _as_str(_safe(value, "OrderNo", "")),
        "symbol": _as_str(_safe(value, "CompanyNo", "")),
        "side": _as_str(_safe(value, "BS", "")),
        "order_type": _as_str(_safe(value, "OrderType", "") or ""),
        "price": _as_str(_safe(value, "Price", "0")),
        "last_deal_price": _as_str(_safe(value, "LastDealPrice", "0")),
        "avg_deal_price": _as_str(_safe(value, "AvgDealPrice", "0")),
        "before_qty": _optional_integer(value, "BeforeQty"),
        "order_qty": _strict_integer(_required_attribute(value, "OrderQty"), "OrderQty"),
        "ok_qty": _strict_integer(_required_attribute(value, "OkQty"), "OkQty"),
        "ap_code": _optional_integer(value, "APCode"),
        "order_status": _strict_integer(_required_attribute(value, "OrderStatus"), "OrderStatus"),
        "last_order_status": _strict_integer(_required_attribute(value, "LastOrderStatus"), "LastOrderStatus"),
        "basket_no": _as_str(_safe(value, "BasketNo", "")),
        "stk_error_no": _as_str(_safe(value, "StkErrorNo", "")),
        **_normalise_report_temporal_fields(value),
    }


def _normalise_positions(value: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in _required_collection(value, "StkStoreList"):
        symbol = str(_required_attribute(item, "StkCode")).strip().upper()
        if not symbol:
            raise BrokerAdapterError("broker inventory missing symbol")
        quantity = _strict_integer(_required_attribute(item, "StockQty"), "StockQty")
        trade_kind = _strict_integer(_required_attribute(item, "TradeKind"), "TradeKind")
        if trade_kind not in {0, 3, 4, 6}:
            raise BrokerAdapterError("unsupported broker inventory financing category")
        # GetStoreSummary reports StockQty as a positive magnitude.  Financing
        # buys are long, while short/borrow inventory is negative exposure.
        # Keep this conversion here so reconciliation never guesses from side.
        if trade_kind in {4, 6}:
            quantity = -abs(quantity)
        if quantity:
            key = f"{symbol}|{trade_kind}"
            result[key] = result.get(key, 0) + quantity
    return result


def _remote_status(row: Mapping[str, Any]) -> BrokerOrderStatus:
    order_status = int(row.get("order_status", -1))
    last = int(row.get("last_order_status", -1))
    order_qty = int(row.get("order_qty", 0))
    ok_qty = int(row.get("ok_qty", 0))

    if order_status == 30 or last == 2:
        return BrokerOrderStatus.CANCELED
    if order_status in {24, 25} or last in {24, 25}:
        return BrokerOrderStatus.EXPIRED
    if order_status == 10 or last == 1:
        return BrokerOrderStatus.REJECTED
    if order_qty > 0 and ok_qty >= order_qty:
        return BrokerOrderStatus.FILLED
    if ok_qty > 0:
        return BrokerOrderStatus.PARTIALLY_FILLED
    if order_status in {0, 5}:
        return BrokerOrderStatus.SEND_PENDING
    return BrokerOrderStatus.ACKNOWLEDGED


class YuantaSparkExecutionAdapter:
    """Execution adapter around an already-open and already-logged-in SPARK API.

    The immutable ``live_gate`` is fail-closed by default.  The caller owns
    login, credentials and session lifecycle; this object owns order/report
    translation and requires a successful reconciliation before every process
    may begin sending.
    """

    def __init__(
        self,
        *,
        api: Any,
        api_types: Mapping[str, Any],
        account: str,
        store: LiveOrderStore,
        live_gate: LiveTradingGate | None = None,
        language: Any | None = None,
        position_baseline: Mapping[str, int] | None = None,
        pre_order_reconcile_timeout: float = 20.0,
    ):
        clean_account = account.strip().upper()
        if not clean_account.startswith("S") or len(clean_account) != 12:
            raise ValueError("account must be Yuanta securities format S + 11 digits")
        self.api = api
        self.api_types = dict(api_types)
        self.account = clean_account
        self.store = store
        self.live_gate = live_gate or LiveTradingGate.from_environment(cli_live=False)
        self._reconciled = False
        self.pre_order_reconcile_timeout = float(pre_order_reconcile_timeout)
        if not math.isfinite(self.pre_order_reconcile_timeout) or self.pre_order_reconcile_timeout <= 0:
            raise ValueError("pre_order_reconcile_timeout must be positive")
        self.position_baseline = {}
        for raw_key, raw_quantity in dict(position_baseline or {}).items():
            key = str(raw_key).strip().upper()
            if "|" not in key:
                key = f"{key}|0"
            symbol, separator, trade_kind = key.partition("|")
            if not symbol or not separator or trade_kind not in {"0", "3", "4", "6"}:
                raise ValueError("position baseline keys must be SYMBOL or SYMBOL|0/3/4/6")
            quantity = int(raw_quantity)
            if quantity and trade_kind in {"0", "3"} and quantity < 0:
                raise ValueError("cash/margin baseline quantities must be positive")
            if quantity and trade_kind in {"4", "6"} and quantity > 0:
                raise ValueError("short/borrow baseline quantities must be negative")
            if quantity:
                self.position_baseline[key] = quantity
        self.language = (
            language
            if language is not None
            else getattr(self.api_types["Language"], "UTF8")
        )
        self._queue: Queue[tuple[str, Any] | None] = Queue()
        self._closed = False
        self._callback_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._event_loop,
            name="yuanta-execution-events",
            daemon=True,
        )
        self._reconcile_lock = threading.Lock()
        self._submission_lock = threading.RLock()
        self._detail_event = threading.Event()
        self._merge_event = threading.Event()
        self._position_event = threading.Event()
        self._latest_merge: list[dict[str, Any]] | None = None
        self._latest_positions: dict[str, int] | None = None
        self._query_worker: threading.Thread | None = None
        self._query_error: Exception | None = None
        self._query_uncertain = False
        self._snapshot_complete = True
        self.api.OnResponse += self._on_response
        self._worker.start()

    def close(self) -> None:
        with self._callback_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.api.OnResponse -= self._on_response
            except Exception:
                pass
        # The callback lock guarantees all callbacks that started before close
        # have enqueued their normalized payload before this FIFO sentinel.
        self._queue.put(None)
        self._worker.join(timeout=10)
        if self._worker.is_alive():
            self.store.halt("CALLBACK_QUEUE_DRAIN_TIMEOUT")
            raise BrokerAdapterError("callback queue did not drain during close")

    def __enter__(self) -> "YuantaSparkExecutionAdapter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _require_live(self, *, allow_halted: bool = False) -> None:
        if self._closed:
            raise BrokerAdapterError("adapter is closed")
        if not self.live_gate.authorized:
            raise LiveExecutionDisabled(
                "live broker sends require EXECUTION_MODE=LIVE, "
                "ENABLE_LIVE_TRADING=YES and CLI --live"
            )
        if not self._reconciled:
            raise ReconciliationRequired(
                "startup reconciliation has not passed; call reconcile() before broker sends"
            )
        if not allow_halted:
            self.store.assert_not_halted()

    def _stock_list(self, order: Any) -> Any:
        factory = self.api_types["List"][self.api_types["StockOrder"]]
        result = factory()
        result.Add(order)
        return result

    @staticmethod
    def _set_identity(stock_order: Any, identify: int) -> None:
        # Yuanta's current Python sample uses Identify while one table labels it Identity.
        if hasattr(stock_order, "Identify"):
            stock_order.Identify = int(identify)
        elif hasattr(stock_order, "Identity"):
            stock_order.Identity = int(identify)
        else:
            raise AttributeError("StockOrder exposes neither Identify nor Identity")

    @staticmethod
    def _broker_order_quantity(
        *,
        ap_code: int,
        trade_kind: int,
        quantity_shares: int,
    ) -> int:
        quantity = int(quantity_shares)
        if quantity <= 0:
            raise ValueError("quantity_shares must be positive")

        # Canonical runtime/store quantities stay in shares.
        # Yuanta regular-board-lot NEW orders (APCode=0, TradeKind=0)
        # require OrderQty in lots, so convert only at this broker boundary.
        #
        # Do NOT apply this conversion to cancel / modify-price /
        # reduce-quantity until their SDK quantity contract is verified.
        if int(trade_kind) == 0 and int(ap_code) == 0:
            if quantity % 1000 != 0:
                raise BrokerAdapterError(
                    "regular-board-lot NEW quantity must be a multiple of 1000 shares"
                )
            return quantity // 1000

        return quantity

    def _build_stock_order(
        self,
        order: StoredOrder,
        *,
        identify: int,
        trade_kind: int,
        order_no: str = "",
        quantity: int | None = None,
        price: Decimal | None = None,
    ) -> Any:
        try:
            return self._construct_stock_order(order, identify=identify, trade_kind=trade_kind, order_no=order_no, quantity=quantity, price=price)
        except Exception as exc:
            operation = {0: "NEW", 4: "CANCEL", 7: "MODIFY_PRICE", 3: "REDUCE"}.get(trade_kind, "NEW")
            self._persist_unsent_failure(order, operation, identify, f"UNSENT_ORDER_BUILD:{type(exc).__name__}:{exc}")
            raise

    def _persist_unsent_failure(self, order: StoredOrder, operation: str, identify: int, reason: str) -> None:
        if identify and not self.store.reject_unsent_request(identify, reason):
            self.store.halt(f"UNSENT_REQUEST_IDENTITY_CONFLICT:{identify}")
            return
        if operation == "NEW":
            self.store.reject(order.client_order_id, reason)
        elif operation == "CANCEL":
            self.store.cancel_failed(order.client_order_id, reason)
        else:
            self.store.modification_result(order.client_order_id, kind="price" if operation == "MODIFY_PRICE" else "reduce", success=False, reason=reason)

    def _construct_stock_order(
        self, order: StoredOrder, *, identify: int, trade_kind: int,
        order_no: str = "", quantity: int | None = None, price: Decimal | None = None,
    ) -> Any:
        stock = self.api_types["StockOrder"]()
        self._set_identity(stock, identify)
        stock.Account = self.account
        stock.OrderNo = order_no
        stock.TradeDate = datetime.now(TAIPEI).strftime("%Y/%m/%d")
        stock.APCode = int(order.ap_code)
        stock.TradeKind = int(trade_kind)
        stock.OrderType = order.order_type.value
        stock.StkCode = order.symbol
        stock.BuySell = "B" if order.side.value == "BUY" else "S"
        stock.PriceFlag = PRICE_FLAG[order.price_type]
        effective_price = order.price if price is None else price
        stock.Price = float(effective_price or 0)
        stock.BasketNo = order.basket_no
        quantity_shares = int(order.quantity if quantity is None else quantity)
        stock.OrderQty = self._broker_order_quantity(
            ap_code=int(order.ap_code),
            trade_kind=int(trade_kind),
            quantity_shares=quantity_shares,
        )
        stock.Time_in_force = TIF_CODE[order.time_in_force]
        return stock

    def _send(
        self,
        stored: StoredOrder,
        stock_order: Any,
        *,
        operation: str,
        pre_send_guard: Callable[[ExecutionIntent | StoredOrder], Any] | None = None,
        guard_subject: ExecutionIntent | StoredOrder | None = None,
    ) -> None:
        operation_name = {"submit": "NEW", "rescue-submit": "NEW", "cancel": "CANCEL", "modify_price": "MODIFY_PRICE", "reduce_quantity": "REDUCE"}[operation]
        try:
            payload = self._stock_list(stock_order)
        except Exception as exc:
            request = self.store.latest_request(stored.client_order_id, operation_name)
            self._persist_unsent_failure(stored, operation_name, int(request["identify"]) if request else 0, f"UNSENT_PAYLOAD_BUILD:{type(exc).__name__}:{exc}")
            raise
        # Request persistence, mark_send_pending, and SDK payload construction
        # may be slow. Revalidate time/quote/safety at the actual send boundary,
        # not before these steps. No broker invocation has occurred on failure.
        try:
            if pre_send_guard is not None and pre_send_guard(guard_subject or stored) is False:
                raise BrokerAdapterError("pre_send_guard returned False")
            if operation == "submit" and stored.purpose == IntentPurpose.ENTRY:
                self.store.assert_not_halted()
        except Exception as exc:
            reason = f"UNSENT_PRE_SEND_GUARD:{type(exc).__name__}:{exc}"
            request = self.store.latest_request(stored.client_order_id, operation_name)
            self._persist_unsent_failure(stored, operation_name, int(request["identify"]) if request else 0, reason)
            if operation_name == "NEW":
                return
            raise BrokerAdapterError(reason) from exc
        try:
            accepted = self.api.SendStockOrder(self.account, payload, self.language)
        except Exception as exc:
            self.store.mark_unknown(
                stored.client_order_id,
                f"{operation} exception: {type(exc).__name__}: {exc}",
            )
            raise
        if accepted is False:
            self.store.mark_unknown(
                stored.client_order_id,
                f"{operation} SendStockOrder returned False",
            )
            raise BrokerAdapterError(f"{operation} was not accepted by SendStockOrder")

    def submit(
        self, intent: ExecutionIntent, *, pre_send_guard: Callable[[ExecutionIntent], Any] | None = None
    ) -> StoredOrder:
        with self._submission_lock:
            return self._submit(intent, pre_send_guard=pre_send_guard)

    def _submit(
        self, intent: ExecutionIntent, *, pre_send_guard: Callable[[ExecutionIntent], Any] | None = None
    ) -> StoredOrder:
        self._require_live()
        if not isinstance(intent, ExecutionIntent):
            raise TypeError(
                "submit accepts only canonical ExecutionIntent; use the reviewed intent bridge"
            )
        # Validate broker NEW quantity before mutating the durable order store.
        self._broker_order_quantity(
            ap_code=int(intent.ap_code),
            trade_kind=0,
            quantity_shares=int(intent.quantity),
        )

        stored, created = self.store.reserve(intent)
        if not created:
            # Never resend a persisted intent automatically. A detailed broker
            # query/reconciliation resolves SEND_PENDING/UNKNOWN after restart.
            return stored

        # A newly-created NEW order must pass a fresh broker snapshot immediately
        # before any broker request is created. The RESERVED local order is
        # intentionally ignored when no remote order exists yet.
        try:
            self.reconcile(
                timeout=self.pre_order_reconcile_timeout,
                strict_positions=True,
            )
        except Exception as exc:
            self.store.reject(stored.client_order_id, f"UNSENT_PREORDER_RECONCILE:{type(exc).__name__}:{exc}")
            raise
        if intent.purpose == IntentPurpose.EXIT:
            try:
                self._assert_exit_reduces(intent)
            except Exception as exc:
                self.store.reject(stored.client_order_id, f"UNSENT_EXIT_VALIDATION:{exc}")
                raise
        stock = self._build_stock_order(
            stored,
            identify=0,
            trade_kind=0,
        )
        identify = self.store.create_request(stored.client_order_id, "NEW")
        try:
            self._set_identity(stock, identify)
        except Exception as exc:
            self._persist_unsent_failure(stored, "NEW", identify, f"UNSENT_IDENTITY_BUILD:{type(exc).__name__}:{exc}")
            raise
        self.store.mark_send_pending(stored.client_order_id)
        self._send(stored, stock, operation="submit", pre_send_guard=pre_send_guard, guard_subject=intent)
        return self.store.get(stored.client_order_id)

    def _assert_exit_reduces(self, intent: ExecutionIntent) -> None:
        category = {"0": 0, "9": 0, "3": 3, "4": 4, "5": 6, "6": 6}[intent.order_type.value]
        key = f"{intent.symbol}|{category}"
        local = int(self.store.position_buckets().get(key, 0))
        actual = int((self._latest_positions or {}).get(key, 0)) - int(self.position_baseline.get(key, 0))
        desired_sign = 1 if intent.side.value == "SELL" else -1
        if local * desired_sign <= 0 or actual * desired_sign <= 0:
            raise BrokerAdapterError("EXIT must reduce an existing reconciled strategy position")
        if intent.quantity > min(abs(local), abs(actual)):
            raise BrokerAdapterError("EXIT exceeds reconciled remaining strategy position")
        if any(
            row.get("symbol") == intent.symbol and _remote_status(row) not in TERMINAL_STATUSES
            for row in (self._latest_merge or [])
        ):
            raise BrokerAdapterError("EXIT blocked: existing open broker order for symbol")

    def cancel(
        self,
        client_order_id: str,
        reason: str = "strategy cancel",
        *,
        emergency: bool = False,
    ) -> StoredOrder:
        self._require_live(allow_halted=emergency)
        stored = self.store.get(client_order_id)
        if stored.status in {
            BrokerOrderStatus.FILLED,
            BrokerOrderStatus.CANCELED,
            BrokerOrderStatus.EXPIRED,
            BrokerOrderStatus.REJECTED,
        }:
            return stored
        inflight = self.store.pending_mutation(client_order_id)
        if inflight is not None:
            raise BrokerAdapterError(
                f"broker mutation already in flight: {inflight['operation']}"
            )
        if not stored.broker_order_no:
            raise BrokerAdapterError("cannot cancel before broker OrderNo is known")

        pending = self.store.request_cancel(client_order_id, reason)
        identify = self.store.create_request(
            client_order_id,
            "CANCEL",
            {"reason": reason},
        )
        stock = self._build_stock_order(
            pending,
            identify=identify,
            trade_kind=4,
            order_no=pending.broker_order_no or "",
            quantity=pending.remaining_quantity or pending.quantity,
        )
        self._send(pending, stock, operation="cancel")
        return self.store.get(client_order_id)

    def submit_rescue(
        self, intent: ExecutionIntent, *, pre_send_guard: Callable[[ExecutionIntent], Any] | None = None
    ) -> StoredOrder:
        with self._submission_lock:
            return self._submit_rescue(intent, pre_send_guard=pre_send_guard)

    def _submit_rescue(
        self, intent: ExecutionIntent, *, pre_send_guard: Callable[[ExecutionIntent], Any] | None = None
    ) -> StoredOrder:
        """Submit only an exposure-reducing EXIT while the persistent halt is active."""
        if not isinstance(intent, ExecutionIntent) or intent.purpose.value != "EXIT":
            raise BrokerAdapterError("rescue submission accepts EXIT intents only")
        # Rescue is also a NEW broker order, so validate its board-lot
        # quantity before reserving any durable local request.
        self._broker_order_quantity(
            ap_code=int(intent.ap_code),
            trade_kind=0,
            quantity_shares=int(intent.quantity),
        )

        self._require_live(allow_halted=True)

        # Rescue EXIT must re-confirm the broker's current authoritative state.
        # This protects against:
        #   - a previous EXIT that actually reached the broker,
        #   - partial fills that changed remaining exposure,
        #   - manual/external inventory changes,
        #   - another live order for the same symbol.
        #
        # Reconciliation is intentionally allowed while the store is halted.
        self.reconcile(
            timeout=self.pre_order_reconcile_timeout,
            strict_positions=True,
        )
        self._assert_exit_reduces(intent)

        stored, created = self.store.reserve(intent, allow_halted=True)
        if not created:
            return stored
        stock = self._build_stock_order(stored, identify=0, trade_kind=0)
        identify = self.store.create_request(stored.client_order_id, "NEW")
        try:
            self._set_identity(stock, identify)
        except Exception as exc:
            self._persist_unsent_failure(stored, "NEW", identify, f"UNSENT_IDENTITY_BUILD:{type(exc).__name__}:{exc}")
            raise
        self.store.mark_send_pending(stored.client_order_id)
        stored = self.store.get(stored.client_order_id)
        self._send(stored, stock, operation="rescue-submit", pre_send_guard=pre_send_guard, guard_subject=intent)
        return self.store.get(stored.client_order_id)

    def modify_price(
        self, client_order_id: str, new_price: Any, *, emergency: bool = False,
        pre_send_guard: Callable[[StoredOrder], Any] | None = None,
    ) -> StoredOrder:
        self._require_live(allow_halted=emergency)
        stored = self.store.get(client_order_id)
        if stored.status in TERMINAL_STATUSES:
            return stored
        inflight = self.store.pending_mutation(client_order_id)
        if inflight is not None:
            raise BrokerAdapterError(
                f"broker mutation already in flight: {inflight['operation']}"
            )
        if not stored.broker_order_no:
            raise BrokerAdapterError("cannot modify price before broker OrderNo is known")
        parsed = Decimal(str(new_price))
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError("new_price must be positive")

        identify = self.store.create_request(
            client_order_id,
            "MODIFY_PRICE",
            {"new_price": str(parsed)},
        )
        stock = self._build_stock_order(
            stored,
            identify=identify,
            trade_kind=7,
            order_no=stored.broker_order_no,
            quantity=stored.remaining_quantity or stored.quantity,
            price=parsed,
        )
        self._send(stored, stock, operation="modify_price", pre_send_guard=pre_send_guard, guard_subject=replace(stored, price=parsed))
        return self.store.get(client_order_id)

    def reduce_quantity(self, client_order_id: str, reduce_by: int) -> StoredOrder:
        """Request a reduction by broker share quantity.

        Yuanta exposes TradeKind=03 as 改量; this method intentionally exposes
        reduction semantics (`reduce_by`) rather than replacement semantics.
        """
        self._require_live()
        stored = self.store.get(client_order_id)
        if stored.status in TERMINAL_STATUSES:
            return stored
        inflight = self.store.pending_mutation(client_order_id)
        if inflight is not None:
            raise BrokerAdapterError(
                f"broker mutation already in flight: {inflight['operation']}"
            )
        if not stored.broker_order_no:
            raise BrokerAdapterError("cannot reduce before broker OrderNo is known")
        if not isinstance(reduce_by, int) or isinstance(reduce_by, bool) or reduce_by <= 0:
            raise ValueError("reduce_by must be a positive share quantity")
        if reduce_by >= stored.remaining_quantity:
            raise ValueError("reduce_by must be smaller than remaining quantity; use cancel otherwise")

        identify = self.store.create_request(
            client_order_id,
            "REDUCE",
            {"reduce_by": int(reduce_by), "before_quantity": stored.quantity, "expected_quantity": stored.quantity - reduce_by},
        )
        stock = self._build_stock_order(
            stored,
            identify=identify,
            trade_kind=3,
            order_no=stored.broker_order_no,
            quantity=reduce_by,
        )
        self._send(stored, stock, operation="reduce_quantity")
        return self.store.get(client_order_id)

    def request_reconciliation(self) -> None:
        if self._closed:
            raise BrokerAdapterError("adapter is closed")
        if self._query_uncertain:
            raise BrokerAdapterError("snapshot query outcome is uncertain; reconnect with a fresh broker session")
        self._reconciled = False
        self._detail_event.clear()
        self._merge_event.clear()
        self._position_event.clear()
        self._latest_merge = None
        self._latest_positions = None
        self._query_error = None

        def dispatch_queries() -> None:
            try:
                for query in (self.api.GetRealReport, self.api.GetRealReportMerge, self.api.GetStoreSummary):
                    if self._closed:
                        raise BrokerAdapterError("adapter closed during snapshot query")
                    if query(self.account, self.language) is False:
                        raise BrokerAdapterError("broker rejected reconciliation query")
            except Exception as exc:
                self._query_error = exc
                # Wake the deadline waiter; it rejects these events rather
                # than accepting fabricated empty snapshots on query failure.
                self._detail_event.set()
                self._merge_event.set()
                self._position_event.set()

        # Vendor calls are expected to return promptly, but a blocked call
        # must not suspend the main exit timer. A timeout poisons this adapter
        # because SPARK snapshots have no trustworthy per-request identifier.
        self._query_worker = threading.Thread(target=dispatch_queries, name="yuanta-reconciliation-query", daemon=True)
        self._query_worker.start()

    @contextmanager
    def _snapshot_deadline(self, timeout: float):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("reconciliation timeout must be positive")
        deadline = time.monotonic() + timeout
        if not self._reconcile_lock.acquire(timeout=timeout):
            self.store.halt("RECONCILIATION_LOCK_TIMEOUT")
            raise BrokerAdapterError("reconciliation lock timed out")
        self._snapshot_complete = False
        try:
            yield deadline
        except Exception:
            if not self._snapshot_complete:
                self._query_uncertain = True
            self._reconciled = False
            raise
        finally:
            self._reconcile_lock.release()

    def _wait_snapshot(self, deadline: float, *, inspection: bool = False) -> None:
        prefix = "BROKER_INSPECTION" if inspection else "RECONCILIATION"
        for event, suffix, query in (
            (self._detail_event, "DETAIL", "GetRealReport"),
            (self._merge_event, "ORDER", "GetRealReportMerge"),
            (self._position_event, "POSITION", "GetStoreSummary"),
        ):
            while not event.is_set():
                if self._query_error is not None:
                    self.store.halt("RECONCILIATION_QUERY_FAILED")
                    raise BrokerAdapterError(f"broker reconciliation query failed: {self._query_error}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.store.halt(f"{prefix}_{suffix}_TIMEOUT")
                    raise BrokerAdapterError(f"{query} timed out")
                event.wait(min(remaining, 0.02))
            if self._query_error is not None:
                self.store.halt("RECONCILIATION_QUERY_FAILED")
                raise BrokerAdapterError(f"broker reconciliation query failed: {self._query_error}")
        # Queue.join has no timeout and could otherwise suspend the exit timer
        # indefinitely during a stuck callback or continuous event inflow.
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.store.halt(f"{prefix}_CALLBACK_DRAIN_TIMEOUT")
                    raise BrokerAdapterError("callback queue drain timed out")
                self._queue.all_tasks_done.wait(remaining)
        if self._query_worker is not None:
            self._query_worker.join(max(0.0, deadline - time.monotonic()))
            if self._query_worker.is_alive():
                self.store.halt(f"{prefix}_QUERY_DISPATCH_TIMEOUT")
                raise BrokerAdapterError("broker query dispatch timed out")
        if self._query_error is not None:
            self.store.halt("RECONCILIATION_QUERY_FAILED")
            raise BrokerAdapterError(f"broker reconciliation query failed: {self._query_error}")
        if time.monotonic() > deadline:
            self.store.halt(f"{prefix}_DEADLINE_EXCEEDED")
            raise BrokerAdapterError("reconciliation total deadline exceeded")
        self._snapshot_complete = True

    def reconcile(
        self,
        *,
        timeout: float = 20.0,
        strict_positions: bool = True,
    ) -> ReconciliationResult:
        """Recover missed detailed reports, then compare aggregate orders and inventory.

        The default is deliberately strict: any manual/external stock position in
        the same account creates a mismatch. Set strict_positions=False only if
        the caller has an independently reviewed ownership model for unmanaged
        positions.
        """
        with self._snapshot_deadline(timeout) as deadline:
            self.request_reconciliation()
            self._wait_snapshot(deadline)

            merge = list(self._latest_merge or [])
            broker_positions = dict(self._latest_positions or {})
            local_positions = self.store.position_buckets()
            order_mismatches = self._compare_orders(merge)
            expected_positions = dict(self.position_baseline)
            for symbol, quantity in local_positions.items():
                expected_positions[symbol] = expected_positions.get(symbol, 0) + quantity
                if expected_positions[symbol] == 0:
                    expected_positions.pop(symbol)
            position_mismatch = strict_positions and expected_positions != broker_positions

            if order_mismatches or position_mismatch:
                self.store.halt("RECONCILIATION_MISMATCH")
                raise ReconciliationMismatch(
                    f"orders={order_mismatches!r}; "
                    f"local_positions={local_positions!r}; baseline={self.position_baseline!r}; "
                    f"expected={expected_positions!r}; broker_positions={broker_positions!r}"
                )
            self._reconciled = True
            return ReconciliationResult(
                status="MATCH",
                local_positions=local_positions,
                position_baseline=dict(self.position_baseline),
                expected_broker_positions=expected_positions,
                broker_positions=broker_positions,
                order_mismatches=[],
            )

    def inspect_broker_state(self, *, timeout: float = 20.0) -> BrokerStateSnapshot:
        """Query actual broker orders and inventory without trusting local SQLite."""
        with self._snapshot_deadline(timeout) as deadline:
            self.request_reconciliation()
            self._wait_snapshot(deadline, inspection=True)
            orders = list(self._latest_merge or [])
            terminal = {
                BrokerOrderStatus.FILLED,
                BrokerOrderStatus.CANCELED,
                BrokerOrderStatus.EXPIRED,
                BrokerOrderStatus.REJECTED,
            }
            open_orders = [row for row in orders if _remote_status(row) not in terminal]
            return BrokerStateSnapshot(
                orders=orders,
                open_orders=open_orders,
                positions=dict(self._latest_positions or {}),
            )

    def _compare_orders(self, merge: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_basket = {row.get("basket_no"): row for row in merge if row.get("basket_no")}
        by_order = {row.get("order_no"): row for row in merge if row.get("order_no")}
        mismatches: list[dict[str, Any]] = []
        today = datetime.now(TAIPEI).date()

        for local in self.store.orders():
            # GetRealReportMerge is a current-session broker view. Historical
            # terminal orders are durable audit history and must not make the
            # next trading day fail reconciliation merely because the broker no
            # longer returns yesterday's completed order. Non-terminal/UNKNOWN
            # history is intentionally *not* ignored.
            try:
                created_day = datetime.fromisoformat(local.created_at.replace("Z", "+00:00")).astimezone(TAIPEI).date()
            except (TypeError, ValueError):
                created_day = today
            if created_day < today and local.status in TERMINAL_STATUSES:
                continue
            remote = by_basket.get(local.basket_no)
            if remote is None and local.broker_order_no:
                candidate = by_order.get(local.broker_order_no)
                if candidate is not None and not candidate.get("basket_no") and self._is_current_order(local):
                    remote = candidate

            if remote is None:
                # A failed local build/guard is not a missing broker order.
                # Only durable proof that NEW never reached SendStockOrder is
                # sufficient; UNKNOWN and ordinary API rejection remain unsafe.
                if self.store.is_proven_unsent_rejection(local.client_order_id):
                    continue
                if local.status not in {BrokerOrderStatus.RESERVED}:
                    mismatches.append(
                        {"client_order_id": local.client_order_id, "reason": "missing_remote_order"}
                    )
                continue

            if not self._identity_matches(local, remote):
                mismatches.append({"client_order_id": local.client_order_id, "reason": "broker_identity"})
                continue

            remote_order_no = str(remote.get("order_no", "") or "")
            if not local.broker_order_no and remote_order_no:
                local = self.store.bind_broker_order(local.client_order_id, remote_order_no)

            remote_filled = int(remote.get("ok_qty", 0))
            if remote_filled != local.filled_quantity:
                mismatches.append(
                    {
                        "client_order_id": local.client_order_id,
                        "reason": "filled_quantity",
                        "local": local.filled_quantity,
                        "remote": remote_filled,
                    }
                )

            remote_quantity = int(remote.get("order_qty", 0))
            if remote_quantity > 0 and remote_quantity != local.quantity:
                # A successful reduction changes effective broker quantity. It is
                # safe to adopt only after the detailed fill count already matches.
                if remote_filled == local.filled_quantity and remote_quantity < local.quantity:
                    local = self.store.apply_authoritative_order_quantity(
                        local.client_order_id,
                        remote_quantity,
                    )
                else:
                    mismatches.append(
                        {
                            "client_order_id": local.client_order_id,
                            "reason": "order_quantity",
                            "local": local.quantity,
                            "remote": remote_quantity,
                        }
                    )

            if local.broker_order_no and remote_order_no and local.broker_order_no != remote_order_no:
                mismatches.append(
                    {
                        "client_order_id": local.client_order_id,
                        "reason": "broker_order_no",
                        "local": local.broker_order_no,
                        "remote": remote_order_no,
                    }
                )

            remote_status = _remote_status(remote)
            self.store.finalize_latest_request(local.client_order_id, "NEW", success=remote_status != BrokerOrderStatus.REJECTED)
            self._resolve_mutation_from_remote(local, remote)
            # A terminal broker snapshot is authoritative even when the
            # cancel/final callback was lost. No fill quantity is fabricated.
            if remote_filled == local.filled_quantity:
                if remote_status == BrokerOrderStatus.CANCELED:
                    self.store.canceled(local.client_order_id)
                elif remote_status == BrokerOrderStatus.EXPIRED:
                    self.store.expired(local.client_order_id, "recovered from merge")
                elif remote_status == BrokerOrderStatus.REJECTED:
                    self.store.reject(local.client_order_id, "recovered from merge")
            local = self.store.get(local.client_order_id)
            comparable_local = local.status
            # A detailed replay may leave UNKNOWN/SEND_PENDING but the aggregate
            # report proves the broker state. Only auto-recover if fill counts agree.
            if comparable_local in {BrokerOrderStatus.UNKNOWN, BrokerOrderStatus.SEND_PENDING}:
                if remote_filled == local.filled_quantity:
                    if remote_status == BrokerOrderStatus.ACKNOWLEDGED:
                        self.store.acknowledge(local.client_order_id)
                        comparable_local = self.store.get(local.client_order_id).status
                    elif remote_status == BrokerOrderStatus.CANCELED:
                        self.store.canceled(local.client_order_id)
                        comparable_local = BrokerOrderStatus.CANCELED
                    elif remote_status == BrokerOrderStatus.EXPIRED:
                        self.store.expired(local.client_order_id, "recovered from merge")
                        comparable_local = BrokerOrderStatus.EXPIRED
                    elif remote_status == BrokerOrderStatus.REJECTED:
                        self.store.reject(local.client_order_id, "recovered from merge")
                        comparable_local = BrokerOrderStatus.REJECTED
                    elif remote_status == BrokerOrderStatus.FILLED and local.filled_quantity == local.quantity:
                        comparable_local = BrokerOrderStatus.FILLED

            if remote_status == BrokerOrderStatus.PARTIALLY_FILLED:
                accepted = {
                    BrokerOrderStatus.PARTIALLY_FILLED,
                    BrokerOrderStatus.CANCEL_PENDING,
                }
            elif remote_status == BrokerOrderStatus.ACKNOWLEDGED:
                accepted = {
                    BrokerOrderStatus.ACKNOWLEDGED,
                    BrokerOrderStatus.CANCEL_PENDING,
                }
            elif remote_status == BrokerOrderStatus.SEND_PENDING:
                accepted = {
                    BrokerOrderStatus.SEND_PENDING,
                    BrokerOrderStatus.ACKNOWLEDGED,
                }
            else:
                accepted = {remote_status}

            if comparable_local not in accepted:
                mismatches.append(
                    {
                        "client_order_id": local.client_order_id,
                        "reason": "status",
                        "local": comparable_local.value,
                        "remote": remote_status.value,
                    }
                )

        # Reverse reconciliation: an OPEN broker order that cannot be
        # matched to a locally-owned basket/order number is external/manual
        # intervention. Terminal broker history is intentionally ignored.
        local_orders = self.store.orders()
        for remote in merge:
            remote_status = _remote_status(remote)
            if remote_status in TERMINAL_STATUSES:
                continue

            basket_no = str(remote.get("basket_no", "") or "")
            order_no = str(remote.get("order_no", "") or "")

            owned = next((
                order for order in local_orders
                if self._is_current_order(order) and self._identity_matches(order, remote)
                and (
                    (basket_no and basket_no == order.basket_no)
                    or (not basket_no and order_no and order_no == order.broker_order_no)
                )
            ), None)
            if owned is not None:
                continue

            mismatches.append(
                {
                    "reason": "unexpected_remote_open_order",
                    "symbol": str(remote.get("symbol", "") or ""),
                    "side": str(remote.get("side", "") or ""),
                    "order_no": order_no,
                    "basket_no": basket_no,
                    "remote_status": remote_status.value,
                }
            )

        return mismatches

    @staticmethod
    def _order_day(local: StoredOrder):
        try:
            return datetime.fromisoformat(local.created_at.replace("Z", "+00:00")).astimezone(TAIPEI).date()
        except (TypeError, ValueError):
            return None

    def _is_current_order(self, local: StoredOrder) -> bool:
        return self._order_day(local) == datetime.now(TAIPEI).date()

    def _identity_matches(self, local: StoredOrder, report: Mapping[str, Any]) -> bool:
        if str(report.get("account", "")).strip().upper() != self.account:
            return False
        if str(report.get("symbol", "")).strip().upper() != local.symbol:
            return False
        if str(report.get("side", "")).strip().upper() not in {local.side.value, "B" if local.side.value == "BUY" else "S"}:
            return False
        basket = str(report.get("basket_no", "") or "")
        if basket and basket != local.basket_no:
            return False
        order_no = str(report.get("order_no", "") or "")
        if local.broker_order_no and order_no and order_no != local.broker_order_no:
            return False
        raw_day = str(report.get("trade_date", "") or "").strip()
        if raw_day:
            try:
                report_day = datetime.strptime(raw_day.replace("/", "").replace("-", "")[:8], "%Y%m%d").date()
            except ValueError:
                return False
            if report_day != self._order_day(local):
                return False
        return True

    def _resolve_mutation_from_remote(self, local: StoredOrder, remote: Mapping[str, Any]) -> None:
        pending = self.store.pending_mutation(local.client_order_id)
        if pending is None:
            return
        status = _remote_status(remote)
        operation = pending["operation"]
        if status in TERMINAL_STATUSES:
            self.store.finalize_latest_request(local.client_order_id, operation, success=(operation == "CANCEL" and status == BrokerOrderStatus.CANCELED))
            return
        if operation == "MODIFY_PRICE":
            try:
                matches = Decimal(str(remote.get("price", "0"))) == Decimal(str(pending["payload"]["new_price"]))
            except (KeyError, ValueError, ArithmeticError):
                matches = False
            if matches:
                self.store.finalize_latest_request(local.client_order_id, operation, success=True)
        elif operation == "REDUCE":
            expected = pending["payload"].get("expected_quantity")
            quantity = int(remote.get("order_qty", 0))
            if expected is not None and quantity > 0 and quantity == int(expected):
                self.store.finalize_latest_request(local.client_order_id, operation, success=True)

    def _on_response(
        self,
        int_mark: Any,
        _index: Any,
        response_name: Any,
        _handle: Any,
        value: Any,
    ) -> None:
        """Normalize .NET objects immediately, then hand pure Python to the worker."""
        with self._callback_lock:
            if self._closed:
                return
            name = _as_str(response_name)
            try:
                mark = int(int_mark)
                if mark == 1 and name == "SendStockOrder":
                    self._queue.put(("order_result", _normalise_order_result(value)))
                elif mark == 1 and name == "GetRealReport":
                    raw_rows = _required_collection(value, "RealReportList")
                    for row in raw_rows:
                        _validate_snapshot_order(row, merge=False, account=self.account)
                    rows = [_normalise_real_report(x) for x in raw_rows]
                    self._queue.put(("detail_snapshot", rows))
                elif mark == 1 and name == "GetRealReportMerge":
                    raw_rows = _required_collection(value, "RealReportMergeList")
                    for row in raw_rows:
                        _validate_snapshot_order(row, merge=True, account=self.account)
                    rows = [_normalise_merge_report(x) for x in raw_rows]
                    self._queue.put(("merge_snapshot", rows))
                elif mark == 1 and name == "GetStoreSummary":
                    self._queue.put(("position_snapshot", _normalise_positions(value)))
                elif mark == 2 and name == "RR_RealReport":
                    self._queue.put(("real_report", _normalise_real_report(value)))
                elif mark == 2 and name == "RR_RealReportMerge":
                    self._queue.put(("merge_live", _normalise_merge_report(value)))
            except Exception as exc:
                # No completion event for a malformed snapshot. An explicit
                # failure wakes the bounded waiter; reconnect must construct a
                # fresh adapter so a late callback cannot certify false flat.
                if name in {"GetRealReport", "GetRealReportMerge", "GetStoreSummary"}:
                    self._query_error = exc
                    self._query_uncertain = True
                    self._reconciled = False
                self._queue.put(
                    ("callback_error", f"{type(exc).__name__}: {exc}")
                )

    def _event_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except Empty:
                continue
            if item is None:
                self._queue.task_done()
                break
            kind, payload = item
            try:
                if kind == "order_result":
                    self._apply_order_result(payload)
                elif kind == "real_report":
                    self._apply_real_report(payload)
                elif kind == "detail_snapshot":
                    for report in payload:
                        self._apply_real_report(report)
                    self._detail_event.set()
                elif kind == "merge_snapshot":
                    self._latest_merge = payload
                    self._merge_event.set()
                elif kind == "position_snapshot":
                    self._latest_positions = payload
                    self._position_event.set()
                elif kind == "merge_live":
                    self._apply_merge_live(payload)
                elif kind == "callback_error":
                    self.store.halt(f"CALLBACK_NORMALIZATION_ERROR:{payload}")
            except Exception as exc:
                self.store.halt(f"EVENT_PROCESSING_ERROR:{type(exc).__name__}")
            finally:
                self._queue.task_done()

    def _lookup_report_order(self, report: Mapping[str, Any]) -> StoredOrder | None:
        # BasketNo is generated by this runtime and is stable across days.
        # Yuanta order numbers may be reused, so an order-number-first lookup
        # can attach today's fill to historical local state.
        basket = str(report.get("basket_no", "") or "")
        if basket:
            local = self.store.get_by_basket_no(basket)
            if local is not None and self._identity_matches(local, report):
                return local
            # A nonempty unknown/conflicting basket is not permission to
            # attach this report to some other (possibly historical) order.
            return None
        order_no = str(report.get("order_no", "") or "")
        if order_no:
            local = self.store.get_by_broker_order_no(order_no)
            if local is not None and self._is_current_order(local) and self._identity_matches(local, report):
                return local
        return None

    def _resolve_order_result_request(
        self, broker_identify: int, *, order_no: str = ""
    ) -> dict[str, Any] | None:
        """Use proven order identity; never let a sole pending request steal an ACK.

        Reused Identify plus an unknown OrderNo is genuinely ambiguous. A later
        BasketNo report/query can bind the actual order; this function cannot
        infer ownership from a count of pending requests alone.
        """
        pending = self.store.pending_requests()
        today = datetime.now(TAIPEI).date()

        def is_today(request: Mapping[str, Any]) -> bool:
            try:
                return (
                    datetime.fromisoformat(
                        str(request["order_created_at"]).replace("Z", "+00:00")
                    )
                    .astimezone(TAIPEI)
                    .date()
                    == today
                )
            except (KeyError, TypeError, ValueError):
                return False

        current = [request for request in pending if is_today(request)]
        exact = self.store.get_request(broker_identify)
        owned = self.store.get_by_broker_order_no(order_no) if order_no else None
        if owned is not None and self._is_current_order(owned):
            if exact is not None and exact["client_order_id"] == owned.client_order_id:
                return exact
            same_order = [item for item in current if item["client_order_id"] == owned.client_order_id]
            if len(same_order) == 1:
                request = same_order[0]
                self.store.record_broker_result_correlation(
                    broker_identify=broker_identify,
                    request_identify=int(request["identify"]),
                    client_order_id=owned.client_order_id,
                )
                return request
            # A basket-backed report can finalize NEW before its API ACK.
            latest = self.store.latest_request(owned.client_order_id, "NEW")
            if latest is not None and not same_order:
                return latest
        elif exact is not None:
            local = self.store.get(exact["client_order_id"])
            if self._is_current_order(local):
                if exact["request_status"] != "SEND_PENDING":
                    if not order_no or not local.broker_order_no or local.broker_order_no == order_no:
                        return exact
                elif len(current) == 1 and (not local.broker_order_no or local.broker_order_no == order_no):
                    return exact

        self.store.halt("AMBIGUOUS_BROKER_ORDER_RESULT")
        return None

    def _apply_order_result(self, rows: Iterable[Mapping[str, Any]]) -> None:
        for row in rows:
            broker_identify = int(row.get("identify", 0))
            result_payload = {key: row.get(key, "") for key in ("identify", "reply_code", "order_no", "err_type", "err_no", "advisory")}
            result_key = hashlib.sha256(json.dumps(
                {"day": datetime.now(TAIPEI).date().isoformat(), "result": result_payload},
                sort_keys=True, default=str,
            ).encode()).hexdigest()
            if self.store.broker_result_receipt(result_key) is not None:
                continue
            request = self._resolve_order_result_request(broker_identify, order_no=str(row.get("order_no", "") or ""))
            if request is None:
                continue
            identify = int(request["identify"])
            client_order_id = request["client_order_id"]
            operation = request["operation"]
            success = int(row.get("reply_code", -1)) == 0
            detail = {
                "order_no": str(row.get("order_no", "") or ""),
                "err_type": str(row.get("err_type", "") or ""),
                "err_no": str(row.get("err_no", "") or ""),
                "advisory": str(row.get("advisory", "") or ""),
            }
            completed = self.store.complete_request(identify, success=success, payload=detail)
            accepted_outcomes = {"ACCEPTED", "CONFIRMED"} if success else {"REJECTED", "FAILED"}
            if completed is None or completed["request_status"] not in accepted_outcomes:
                # Contradictory API results halt, but must not mark a live
                # broker order rejected or reopen a failed/unsent request.
                self.store.record_broker_result_receipt(result_key, identify, result_payload)
                continue

            if success:
                order_no = detail["order_no"]
                if order_no:
                    self.store.bind_broker_order(client_order_id, order_no)
                if operation == "NEW":
                    if self.store.get(client_order_id).broker_order_no:
                        self.store.acknowledge(client_order_id)
                    else:
                        self.store.mark_unknown(client_order_id, "ACK_WITHOUT_BROKER_ORDER_NO")
                # CANCEL/MODIFY/REDUCE are only request-accepted here; terminal
                # state is driven by RR_RealReport / RR_RealReportMerge.
                self.store.record_broker_result_receipt(result_key, identify, result_payload)
                continue

            reason = " ".join(
                detail[key] for key in ("err_type", "err_no", "advisory") if detail[key]
            ).strip() or f"{operation} rejected"
            if operation == "NEW":
                self.store.reject(client_order_id, reason)
            elif operation == "CANCEL":
                self.store.cancel_failed(client_order_id, reason)
            else:
                self.store.modification_result(
                    client_order_id,
                    kind="price" if operation == "MODIFY_PRICE" else "reduce",
                    success=False,
                    reason=reason,
                )
            self.store.record_broker_result_receipt(result_key, identify, result_payload)

    def _apply_real_report(self, report: Mapping[str, Any]) -> None:
        local = self._lookup_report_order(report)
        if local is None:
            return
        order_no = str(report.get("order_no", "") or "")
        if order_no and not local.broker_order_no:
            local = self.store.bind_broker_order(local.client_order_id, order_no)

        rpt_type = int(report.get("rpt_type", 0))
        status = int(report.get("order_status", -1))
        if rpt_type == 51:
            quantity = int(report.get("order_qty", 0))
            price = Decimal(str(report.get("price", "0")))
            seq_no = str(report.get("seq_no", "") or "")
            if quantity <= 0 or not price.is_finite() or price <= 0 or seq_no in {"", "0"}:
                self.store.halt(f"INVALID_FILL_REPORT:{local.client_order_id}")
                return
            self.store.finalize_latest_request(local.client_order_id, "NEW", success=True)
            fill_id = f"{local.client_order_id}:{seq_no}"
            self.store.record_fill(
                local.client_order_id,
                fill_id=fill_id,
                quantity=quantity,
                price=price,
                broker_order_no=order_no or local.broker_order_no,
                seq_no=seq_no,
                legacy_fill_id=f"{order_no}:{seq_no}" if order_no else None,
            )
            return

        if rpt_type != 50:
            return

        error = str(
            report.get("stk_error_no", "")
            or report.get("order_error_no", "")
            or ""
        )
        if status in {0, 18}:
            self.store.finalize_latest_request(local.client_order_id, "NEW", success=True)
            self.store.acknowledge(local.client_order_id)
        elif status == 1:
            self.store.finalize_latest_request(local.client_order_id, "NEW", success=False)
            self.store.reject(local.client_order_id, error or "broker order failed")
        elif status == 2:
            self.store.finalize_latest_request(local.client_order_id, "CANCEL", success=True)
            self.store.canceled(local.client_order_id)
        elif status == 3:
            # RR has order identity, not mutation identity. A delayed failure
            # from an older cancel must not release a newer request's latch.
            self.store.audit_event("MUTATION_FAILURE_REQUIRES_RECONCILIATION", {"client_order_id": local.client_order_id, "operation": "CANCEL", "error": error})
        elif status == 4:
            # Accept the quantity only if it exactly proves the known request
            # target in our canonical share unit. Never blindly interpret an
            # arbitrary RR quantity as a reduction delta or broker lot count.
            effective = int(report.get("order_qty", 0))
            request = self.store.latest_request(local.client_order_id, "REDUCE")
            expected = None if request is None else request["payload"].get("expected_quantity")
            if expected is not None and effective == int(expected) and 0 < effective <= local.quantity:
                self.store.apply_authoritative_order_quantity(local.client_order_id, effective)
            self._resolve_mutation_from_remote(local, report)
            self.store.modification_result(local.client_order_id, kind="reduce", success=True)
        elif status == 5:
            self.store.audit_event("MUTATION_FAILURE_REQUIRES_RECONCILIATION", {"client_order_id": local.client_order_id, "operation": "REDUCE", "error": error})
        elif status == 8:
            # Do not fabricate a fill from an order-status report. The RptType=51
            # fill can arrive before or after this event. Reconciliation catches
            # any genuinely missing fill after the replay window completes.
            pass
        elif status == 20:
            self._resolve_mutation_from_remote(local, report)
            self.store.modification_result(
                local.client_order_id, kind="price", success=True
            )
        elif status == 21:
            self.store.audit_event("MUTATION_FAILURE_REQUIRES_RECONCILIATION", {"client_order_id": local.client_order_id, "operation": "MODIFY_PRICE", "error": error})
        elif status in {24, 25}:
            self.store.expired(
                local.client_order_id,
                error or f"broker status {status}",
            )

    def _latest_request(
        self, client_order_id: str, operation: str
    ) -> dict[str, Any] | None:
        return self.store.latest_request(client_order_id, operation)

    def _apply_merge_live(self, report: Mapping[str, Any]) -> None:
        local = self._lookup_report_order(report)
        if local is None:
            return
        order_no = str(report.get("order_no", "") or "")
        if order_no and not local.broker_order_no:
            local = self.store.bind_broker_order(local.client_order_id, order_no)

        remote_qty = int(report.get("order_qty", 0))
        remote_filled = int(report.get("ok_qty", 0))
        if (
            remote_qty > 0
            and remote_filled == local.filled_quantity
            and remote_qty < local.quantity
        ):
            local = self.store.apply_authoritative_order_quantity(
                local.client_order_id,
                remote_qty,
            )
        self._resolve_mutation_from_remote(local, report)

        order_status = int(report.get("order_status", -1))
        last = int(report.get("last_order_status", -1))
        if order_status == 30 or last == 2:
            self.store.canceled(local.client_order_id)
        elif order_status in {24, 25} or last in {24, 25}:
            self.store.expired(
                local.client_order_id,
                f"merge status {order_status}/{last}",
            )
        elif order_status == 10 or last == 1:
            self.store.reject(local.client_order_id, "merge report rejected")
        elif order_status == 20 or last in {0, 18, 4, 20}:
            self.store.acknowledge(local.client_order_id)
