"""Live Yuanta SPARK broker execution adapter.

The adapter is deliberately broker-only: it does not change signal generation,
position sizing, risk rules, or market-data logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from queue import Empty, Queue
import threading
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .models import (
    BrokerOrderStatus,
    ExecutionIntent,
    PRICE_FLAG,
    TIF_CODE,
    StoredOrder,
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


def _normalise_real_report(value: Any) -> dict[str, Any]:
    return {
        "account": _as_str(_safe(value, "Account", "")),
        "rpt_type": _as_int(_safe(value, "RptType", 0)),
        "order_no": _as_str(_safe(value, "OrderNo", "")),
        "symbol": _as_str(_safe(value, "CompanyNo", "")),
        "side": _as_str(_safe(value, "BS", "")),
        "price": _as_str(_safe(value, "Price", "0")),
        "before_qty": _as_int(_safe(value, "BeforeQty", 0)),
        "order_qty": _as_int(_safe(value, "OrderQty", 0)),
        "trade_kind": _as_int(_safe(value, "TradeKind", 0)),
        "ap_code": _as_int(_safe(value, "APCode", 0)),
        "basket_no": _as_str(_safe(value, "BasketNo", "")),
        "order_status": _as_int(_safe(value, "OrderStatus", -1), -1),
        "seq_no": _as_str(_safe(value, "SeqNo", "")),
        "stk_error_no": _as_str(_safe(value, "StkErrorNo", "")),
        "order_error_no": _as_str(_safe(value, "OrderErrorNo", "")),
    }


def _normalise_merge_report(value: Any) -> dict[str, Any]:
    return {
        "account": _as_str(_safe(value, "Account", "")),
        "rpt_type": _as_int(_safe(value, "RptType", 0)),
        "order_no": _as_str(_safe(value, "OrderNo", "")),
        "symbol": _as_str(_safe(value, "CompanyNo", "")),
        "side": _as_str(_safe(value, "BS", "")),
        "price": _as_str(_safe(value, "Price", "0")),
        "last_deal_price": _as_str(_safe(value, "LastDealPrice", "0")),
        "avg_deal_price": _as_str(_safe(value, "AvgDealPrice", "0")),
        "before_qty": _as_int(_safe(value, "BeforeQty", 0)),
        "order_qty": _as_int(_safe(value, "OrderQty", 0)),
        "ok_qty": _as_int(_safe(value, "OkQty", 0)),
        "ap_code": _as_int(_safe(value, "APCode", 0)),
        "order_status": _as_int(_safe(value, "OrderStatus", -1), -1),
        "last_order_status": _as_int(_safe(value, "LastOrderStatus", -1), -1),
        "basket_no": _as_str(_safe(value, "BasketNo", "")),
        "stk_error_no": _as_str(_safe(value, "StkErrorNo", "")),
    }


def _normalise_positions(value: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in _collection(_safe(value, "StkStoreList")):
        symbol = _as_str(_safe(item, "StkCode", ""))
        if not symbol:
            continue
        quantity = _as_int(_safe(item, "StockQty", 0))
        trade_kind = _as_int(_safe(item, "TradeKind", 0))
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
        self._detail_event = threading.Event()
        self._merge_event = threading.Event()
        self._position_event = threading.Event()
        self._latest_merge: list[dict[str, Any]] | None = None
        self._latest_positions: dict[str, int] | None = None
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

    def _require_live(self) -> None:
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
        stock.OrderQty = int(order.quantity if quantity is None else quantity)
        stock.Time_in_force = TIF_CODE[order.time_in_force]
        return stock

    def _send(
        self,
        stored: StoredOrder,
        stock_order: Any,
        *,
        operation: str,
    ) -> None:
        payload = self._stock_list(stock_order)
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

    def submit(self, intent: ExecutionIntent) -> StoredOrder:
        self._require_live()
        if not isinstance(intent, ExecutionIntent):
            raise TypeError(
                "submit accepts only canonical ExecutionIntent; use the reviewed intent bridge"
            )
        stored, created = self.store.reserve(intent)
        if not created:
            # Never resend a persisted intent automatically. A detailed broker
            # query/reconciliation resolves SEND_PENDING/UNKNOWN after restart.
            return stored

        identify = self.store.create_request(stored.client_order_id, "NEW")
        self.store.mark_send_pending(stored.client_order_id)
        stored = self.store.get(stored.client_order_id)
        stock = self._build_stock_order(
            stored,
            identify=identify,
            trade_kind=0,
        )
        self._send(stored, stock, operation="submit")
        return self.store.get(stored.client_order_id)

    def cancel(self, client_order_id: str, reason: str = "strategy cancel") -> StoredOrder:
        self._require_live()
        stored = self.store.get(client_order_id)
        inflight = self.store.pending_mutation(client_order_id)
        if inflight is not None:
            raise BrokerAdapterError(
                f"broker mutation already in flight: {inflight['operation']}"
            )
        if stored.status in {
            BrokerOrderStatus.FILLED,
            BrokerOrderStatus.CANCELED,
            BrokerOrderStatus.EXPIRED,
            BrokerOrderStatus.REJECTED,
        }:
            return stored
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

    def modify_price(self, client_order_id: str, new_price: Any) -> StoredOrder:
        self._require_live()
        stored = self.store.get(client_order_id)
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
        self._send(stored, stock, operation="modify_price")
        return self.store.get(client_order_id)

    def reduce_quantity(self, client_order_id: str, reduce_by: int) -> StoredOrder:
        """Request a reduction by broker share quantity.

        Yuanta exposes TradeKind=03 as 改量; this method intentionally exposes
        reduction semantics (`reduce_by`) rather than replacement semantics.
        """
        self._require_live()
        stored = self.store.get(client_order_id)
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
            {"reduce_by": int(reduce_by)},
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
        self._reconciled = False
        self._detail_event.clear()
        self._merge_event.clear()
        self._position_event.clear()
        self._latest_merge = None
        self._latest_positions = None

        detail = self.api.GetRealReport(self.account, self.language)
        merge = self.api.GetRealReportMerge(self.account, self.language)
        positions = self.api.GetStoreSummary(self.account, self.language)
        if detail is False or merge is False or positions is False:
            self.store.halt("RECONCILIATION_QUERY_REJECTED")
            raise BrokerAdapterError("broker rejected reconciliation query")

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
        with self._reconcile_lock:
            self.request_reconciliation()
            if not self._detail_event.wait(timeout):
                self.store.halt("RECONCILIATION_DETAIL_TIMEOUT")
                raise BrokerAdapterError("GetRealReport timed out")
            if not self._merge_event.wait(timeout):
                self.store.halt("RECONCILIATION_ORDER_TIMEOUT")
                raise BrokerAdapterError("GetRealReportMerge timed out")
            if not self._position_event.wait(timeout):
                self.store.halt("RECONCILIATION_POSITION_TIMEOUT")
                raise BrokerAdapterError("GetStoreSummary timed out")

            # Ensure all detailed snapshot events have been applied before compare.
            self._queue.join()

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
                remote = by_order.get(local.broker_order_no)

            if remote is None:
                if local.status not in {BrokerOrderStatus.RESERVED}:
                    mismatches.append(
                        {"client_order_id": local.client_order_id, "reason": "missing_remote_order"}
                    )
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
                if remote_filled == local.filled_quantity:
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

        return mismatches

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
            try:
                mark = int(int_mark)
                name = str(response_name)
                if mark == 1 and name == "SendStockOrder":
                    self._queue.put(("order_result", _normalise_order_result(value)))
                elif mark == 1 and name == "GetRealReport":
                    rows = [
                        _normalise_real_report(x)
                        for x in _collection(_safe(value, "RealReportList"))
                    ]
                    self._queue.put(("detail_snapshot", rows))
                elif mark == 1 and name == "GetRealReportMerge":
                    rows = [
                        _normalise_merge_report(x)
                        for x in _collection(_safe(value, "RealReportMergeList"))
                    ]
                    self._queue.put(("merge_snapshot", rows))
                elif mark == 1 and name == "GetStoreSummary":
                    self._queue.put(("position_snapshot", _normalise_positions(value)))
                elif mark == 2 and name == "RR_RealReport":
                    self._queue.put(("real_report", _normalise_real_report(value)))
                elif mark == 2 and name == "RR_RealReportMerge":
                    self._queue.put(("merge_live", _normalise_merge_report(value)))
            except Exception as exc:
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
        order_no = str(report.get("order_no", "") or "")
        if order_no:
            local = self.store.get_by_broker_order_no(order_no)
            if local is not None:
                return local
        basket = str(report.get("basket_no", "") or "")
        if basket:
            return self.store.get_by_basket_no(basket)
        return None

    def _apply_order_result(self, rows: Iterable[Mapping[str, Any]]) -> None:
        for row in rows:
            identify = int(row.get("identify", 0))
            request = self.store.get_request(identify)
            if request is None:
                continue
            client_order_id = request["client_order_id"]
            operation = request["operation"]
            success = int(row.get("reply_code", -1)) == 0
            detail = {
                "order_no": str(row.get("order_no", "") or ""),
                "err_type": str(row.get("err_type", "") or ""),
                "err_no": str(row.get("err_no", "") or ""),
                "advisory": str(row.get("advisory", "") or ""),
            }
            self.store.complete_request(identify, success=success, payload=detail)

            if success:
                order_no = detail["order_no"]
                if order_no:
                    self.store.bind_broker_order(client_order_id, order_no)
                if operation == "NEW":
                    self.store.acknowledge(client_order_id)
                # CANCEL/MODIFY/REDUCE are only request-accepted here; terminal
                # state is driven by RR_RealReport / RR_RealReportMerge.
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
            seq_no = str(report.get("seq_no", "") or "0")
            if quantity <= 0 or price <= 0:
                self.store.halt(f"INVALID_FILL_REPORT:{local.client_order_id}")
                return
            fill_id = f"{order_no or local.client_order_id}:{seq_no}"
            self.store.record_fill(
                local.client_order_id,
                fill_id=fill_id,
                quantity=quantity,
                price=price,
                broker_order_no=order_no or local.broker_order_no,
                seq_no=seq_no,
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
            self.store.acknowledge(local.client_order_id)
        elif status == 1:
            self.store.reject(local.client_order_id, error or "broker order failed")
        elif status == 2:
            self.store.finalize_latest_request(local.client_order_id, "CANCEL", success=True)
            self.store.canceled(local.client_order_id)
        elif status == 3:
            self.store.finalize_latest_request(local.client_order_id, "CANCEL", success=False)
            self.store.cancel_failed(local.client_order_id, error or "cancel failed")
        elif status == 4:
            self.store.finalize_latest_request(local.client_order_id, "REDUCE", success=True)
            # If the matching REDUCE request is available, apply the requested
            # reduction immediately. Merge reconciliation later validates it.
            reduce_request = self._latest_request(local.client_order_id, "REDUCE")
            if reduce_request and reduce_request["request_status"] in {"ACCEPTED", "CONFIRMED"}:
                reduce_by = int(reduce_request["payload"].get("reduce_by", 0))
                if reduce_by > 0:
                    self.store.confirm_reduction(local.client_order_id, reduce_by)
                else:
                    self.store.modification_result(
                        local.client_order_id, kind="reduce", success=True
                    )
            else:
                self.store.modification_result(
                    local.client_order_id, kind="reduce", success=True
                )
        elif status == 5:
            self.store.finalize_latest_request(local.client_order_id, "REDUCE", success=False)
            self.store.modification_result(
                local.client_order_id,
                kind="reduce",
                success=False,
                reason=error,
            )
        elif status == 8:
            # Do not fabricate a fill from an order-status report. The RptType=51
            # fill can arrive before or after this event. Reconciliation catches
            # any genuinely missing fill after the replay window completes.
            pass
        elif status == 20:
            self.store.finalize_latest_request(local.client_order_id, "MODIFY_PRICE", success=True)
            self.store.modification_result(
                local.client_order_id, kind="price", success=True
            )
        elif status == 21:
            self.store.finalize_latest_request(local.client_order_id, "MODIFY_PRICE", success=False)
            self.store.modification_result(
                local.client_order_id,
                kind="price",
                success=False,
                reason=error,
            )
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
            and remote_qty != local.quantity
        ):
            local = self.store.apply_authoritative_order_quantity(
                local.client_order_id,
                remote_qty,
            )

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
