"""Crash-safe local state for Yuanta broker execution."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Mapping
import uuid

from .models import (
    APCode,
    BrokerOrderStatus,
    ExecutionIntent,
    IntentPurpose,
    PriceType,
    Side,
    StockOrderType,
    StoredOrder,
    TERMINAL_STATUSES,
    TimeInForce,
    basket_no_for,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StoreError(RuntimeError):
    pass


class DuplicateIntentConflict(StoreError):
    pass


class BrokerStateHalted(StoreError):
    pass


class LiveOrderStore:
    """Single-process durable state store.

    One SQLite connection is protected by an RLock because SPARK callbacks are
    processed on a worker thread while strategy calls originate elsewhere.
    """

    def __init__(self, database: str | Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(str(self.database), timeout=10, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def __enter__(self) -> "LiveOrderStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        with self._lock, self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS live_orders (
                    client_order_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    basket_no TEXT NOT NULL UNIQUE,
                    broker_order_no TEXT UNIQUE,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    price TEXT,
                    price_type TEXT NOT NULL,
                    time_in_force TEXT NOT NULL,
                    ap_code INTEGER NOT NULL,
                    order_type TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    status TEXT NOT NULL,
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    average_fill_price TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_requests (
                    identify INTEGER PRIMARY KEY,
                    client_order_id TEXT NOT NULL REFERENCES live_orders(client_order_id),
                    operation TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    request_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_fills (
                    fill_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL REFERENCES live_orders(client_order_id),
                    broker_order_no TEXT,
                    seq_no TEXT,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    price TEXT NOT NULL,
                    filled_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    client_order_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_control (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    halted INTEGER NOT NULL,
                    reason TEXT,
                    next_identify INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO live_control(singleton,halted,reason,next_identify,updated_at)
                VALUES(1,0,NULL,1,'INITIAL');
                """
            )

    @staticmethod
    def _fingerprint(intent: ExecutionIntent) -> str:
        payload = {
            "intent_id": intent.intent_id,
            "symbol": intent.symbol,
            "side": intent.side.value,
            "quantity": intent.quantity,
            "price": None if intent.price is None else str(intent.price.normalize()),
            "price_type": intent.price_type.value,
            "time_in_force": intent.time_in_force.value,
            "ap_code": int(intent.ap_code),
            "order_type": intent.order_type.value,
            "purpose": intent.purpose.value,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _event(self, event_type: str, client_order_id: str | None, payload: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO live_events(event_type,client_order_id,payload,created_at) VALUES(?,?,?,?)",
            (event_type, client_order_id, json.dumps(payload, sort_keys=True, default=str), utc_now()),
        )

    def control_state(self) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT halted,reason,next_identify,updated_at FROM live_control WHERE singleton=1"
            ).fetchone()
            assert row is not None
            return {
                "halted": bool(row["halted"]),
                "reason": row["reason"],
                "next_identify": int(row["next_identify"]),
                "updated_at": row["updated_at"],
            }

    def halt(self, reason: str) -> None:
        clean = reason.strip()
        if not clean:
            raise ValueError("halt reason is required")
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE live_control SET halted=1, reason=?, updated_at=? WHERE singleton=1",
                (clean, utc_now()),
            )
            self._event("BROKER_EXECUTION_HALTED", None, {"reason": clean})

    def clear_halt(self, reason: str) -> None:
        clean = reason.strip()
        if not clean:
            raise ValueError("clear-halt reason is required")
        with self._lock:
            unknown = self.connection.execute(
                "SELECT COUNT(*) FROM live_orders WHERE status=?",
                (BrokerOrderStatus.UNKNOWN.value,),
            ).fetchone()[0]
            if unknown:
                raise BrokerStateHalted("cannot clear halt while UNKNOWN orders exist")
            with self.connection:
                self.connection.execute(
                    "UPDATE live_control SET halted=0, reason=?, updated_at=? WHERE singleton=1",
                    (clean, utc_now()),
                )
                self._event("BROKER_EXECUTION_RESUMED", None, {"reason": clean})

    def assert_not_halted(self) -> None:
        state = self.control_state()
        if state["halted"]:
            raise BrokerStateHalted(str(state["reason"] or "broker execution halted"))

    def _next_identify_locked(self) -> int:
        row = self.connection.execute(
            "SELECT next_identify FROM live_control WHERE singleton=1"
        ).fetchone()
        assert row is not None
        identify = int(row[0])
        next_value = 1 if identify >= 2_000_000_000 else identify + 1
        self.connection.execute(
            "UPDATE live_control SET next_identify=?, updated_at=? WHERE singleton=1",
            (next_value, utc_now()),
        )
        return identify

    @staticmethod
    def _row_to_order(row: sqlite3.Row) -> StoredOrder:
        price = row["price"]
        avg = row["average_fill_price"]
        # StoredOrder.identify is the original NEW request identify. Resolve it
        # from broker_requests for compatibility with callers/tests.
        identify = int(row["new_identify"]) if "new_identify" in row.keys() else 0
        return StoredOrder(
            client_order_id=row["client_order_id"],
            intent_id=row["intent_id"],
            basket_no=row["basket_no"],
            identify=identify,
            broker_order_no=row["broker_order_no"],
            symbol=row["symbol"],
            side=Side(row["side"]),
            quantity=int(row["quantity"]),
            price=None if price is None else Decimal(price),
            price_type=PriceType(row["price_type"]),
            time_in_force=TimeInForce(row["time_in_force"]),
            ap_code=APCode(int(row["ap_code"])),
            order_type=StockOrderType(row["order_type"]),
            purpose=IntentPurpose(row["purpose"]),
            status=BrokerOrderStatus(row["status"]),
            filled_quantity=int(row["filled_quantity"]),
            average_fill_price=None if avg is None else Decimal(avg),
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _order_select(self) -> str:
        return """
            SELECT o.*,
                   COALESCE((
                       SELECT r.identify FROM broker_requests r
                       WHERE r.client_order_id=o.client_order_id AND r.operation='NEW'
                       ORDER BY r.created_at,r.identify LIMIT 1
                   ),0) AS new_identify
            FROM live_orders o
        """

    def get(self, client_order_id: str) -> StoredOrder:
        with self._lock:
            row = self.connection.execute(
                self._order_select() + " WHERE o.client_order_id=?", (client_order_id,)
            ).fetchone()
            if row is None:
                raise KeyError(client_order_id)
            return self._row_to_order(row)

    def get_by_intent(self, intent_id: str) -> StoredOrder | None:
        with self._lock:
            row = self.connection.execute(
                self._order_select() + " WHERE o.intent_id=?", (intent_id,)
            ).fetchone()
            return None if row is None else self._row_to_order(row)

    def get_by_broker_order_no(self, order_no: str) -> StoredOrder | None:
        with self._lock:
            row = self.connection.execute(
                self._order_select() + " WHERE o.broker_order_no=?", (order_no,)
            ).fetchone()
            return None if row is None else self._row_to_order(row)

    def get_by_basket_no(self, basket_no: str) -> StoredOrder | None:
        with self._lock:
            row = self.connection.execute(
                self._order_select() + " WHERE o.basket_no=?", (basket_no,)
            ).fetchone()
            return None if row is None else self._row_to_order(row)

    def orders(self, *, open_only: bool = False) -> list[StoredOrder]:
        with self._lock:
            rows = list(
                self.connection.execute(
                    self._order_select() + " ORDER BY o.created_at,o.client_order_id"
                )
            )
            orders = [self._row_to_order(row) for row in rows]
            if open_only:
                return [x for x in orders if x.status not in TERMINAL_STATUSES]
            return orders

    def reserve(self, intent: ExecutionIntent) -> tuple[StoredOrder, bool]:
        fingerprint = self._fingerprint(intent)
        with self._lock:
            existing_row = self.connection.execute(
                "SELECT fingerprint,client_order_id FROM live_orders WHERE intent_id=?",
                (intent.intent_id,),
            ).fetchone()
            if existing_row is not None:
                if existing_row["fingerprint"] != fingerprint:
                    raise DuplicateIntentConflict(
                        "intent_id already belongs to a different execution payload"
                    )
                return self.get(existing_row["client_order_id"]), False

            self.assert_not_halted()
            client_order_id = uuid.uuid4().hex
            basket = basket_no_for(intent.intent_id)
            stamp = utc_now()
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO live_orders(
                        client_order_id,intent_id,fingerprint,basket_no,broker_order_no,
                        symbol,side,quantity,price,price_type,time_in_force,ap_code,order_type,
                        purpose,status,filled_quantity,average_fill_price,last_error,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        client_order_id,
                        intent.intent_id,
                        fingerprint,
                        basket,
                        None,
                        intent.symbol,
                        intent.side.value,
                        intent.quantity,
                        None if intent.price is None else str(intent.price),
                        intent.price_type.value,
                        intent.time_in_force.value,
                        int(intent.ap_code),
                        intent.order_type.value,
                        intent.purpose.value,
                        BrokerOrderStatus.RESERVED.value,
                        0,
                        None,
                        None,
                        stamp,
                        stamp,
                    ),
                )
                self._event(
                    "ORDER_RESERVED",
                    client_order_id,
                    {"intent_id": intent.intent_id, "basket_no": basket},
                )
            return self.get(client_order_id), True

    def create_request(
        self,
        client_order_id: str,
        operation: str,
        payload: Mapping[str, Any] | None = None,
    ) -> int:
        clean_op = operation.strip().upper()
        if clean_op not in {"NEW", "CANCEL", "MODIFY_PRICE", "REDUCE"}:
            raise ValueError(f"unsupported broker operation: {operation}")
        with self._lock, self.connection:
            self.get(client_order_id)
            identify = self._next_identify_locked()
            stamp = utc_now()
            self.connection.execute(
                """
                INSERT INTO broker_requests(
                    identify,client_order_id,operation,payload,request_status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    identify,
                    client_order_id,
                    clean_op,
                    json.dumps(dict(payload or {}), sort_keys=True, default=str),
                    "SEND_PENDING",
                    stamp,
                    stamp,
                ),
            )
            self._event(
                "BROKER_REQUEST_CREATED",
                client_order_id,
                {"identify": identify, "operation": clean_op, "payload": dict(payload or {})},
            )
            return identify

    def get_request(self, identify: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM broker_requests WHERE identify=?", (int(identify),)
            ).fetchone()
            if row is None:
                return None
            return {
                "identify": int(row["identify"]),
                "client_order_id": row["client_order_id"],
                "operation": row["operation"],
                "payload": json.loads(row["payload"]),
                "request_status": row["request_status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    def latest_request(
        self, client_order_id: str, operation: str
    ) -> dict[str, Any] | None:
        clean_op = operation.strip().upper()
        with self._lock:
            row = self.connection.execute(
                """
                SELECT identify FROM broker_requests
                WHERE client_order_id=? AND operation=?
                ORDER BY created_at DESC,identify DESC LIMIT 1
                """,
                (client_order_id, clean_op),
            ).fetchone()
        return None if row is None else self.get_request(int(row["identify"]))

    def pending_mutation(self, client_order_id: str) -> dict[str, Any] | None:
        """Return an unresolved cancel/modify request for this broker order."""
        with self._lock:
            row = self.connection.execute(
                """
                SELECT identify FROM broker_requests
                WHERE client_order_id=?
                  AND operation IN ('CANCEL','MODIFY_PRICE','REDUCE')
                  AND request_status IN ('SEND_PENDING','ACCEPTED')
                ORDER BY created_at DESC,identify DESC LIMIT 1
                """,
                (client_order_id,),
            ).fetchone()
        return None if row is None else self.get_request(int(row["identify"]))

    def finalize_latest_request(
        self, client_order_id: str, operation: str, *, success: bool
    ) -> dict[str, Any] | None:
        request = self.latest_request(client_order_id, operation)
        if request is None:
            return None
        if request["request_status"] not in {"SEND_PENDING", "ACCEPTED"}:
            return request
        with self._lock, self.connection:
            status = "CONFIRMED" if success else "FAILED"
            self.connection.execute(
                "UPDATE broker_requests SET request_status=?,updated_at=? WHERE identify=?",
                (status, utc_now(), int(request["identify"])),
            )
            self._event(
                "BROKER_REQUEST_FINALIZED",
                client_order_id,
                {
                    "identify": int(request["identify"]),
                    "operation": request["operation"],
                    "success": bool(success),
                },
            )
        request["request_status"] = status
        return request

    def complete_request(
        self,
        identify: int,
        *,
        success: bool,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            request = self.get_request(identify)
            if request is None:
                return None
            with self.connection:
                self.connection.execute(
                    "UPDATE broker_requests SET request_status=?,updated_at=? WHERE identify=?",
                    ("ACCEPTED" if success else "REJECTED", utc_now(), int(identify)),
                )
                self._event(
                    "BROKER_REQUEST_RESULT",
                    request["client_order_id"],
                    {
                        "identify": int(identify),
                        "operation": request["operation"],
                        "success": bool(success),
                        **dict(payload or {}),
                    },
                )
            request["request_status"] = "ACCEPTED" if success else "REJECTED"
            return request

    def mark_send_pending(self, client_order_id: str) -> StoredOrder:
        return self._set_status(
            client_order_id,
            BrokerOrderStatus.SEND_PENDING,
            "ORDER_SEND_REQUESTED",
        )

    def bind_broker_order(self, client_order_id: str, broker_order_no: str) -> StoredOrder:
        clean = str(broker_order_no).strip()
        if not clean:
            raise ValueError("broker_order_no is required")
        with self._lock:
            current = self.get(client_order_id)
            if current.broker_order_no and current.broker_order_no != clean:
                raise StoreError("broker order number changed for existing order")
            with self.connection:
                self.connection.execute(
                    "UPDATE live_orders SET broker_order_no=?, updated_at=? WHERE client_order_id=?",
                    (clean, utc_now(), client_order_id),
                )
                self._event("BROKER_ORDER_BOUND", client_order_id, {"broker_order_no": clean})
            return self.get(client_order_id)

    def _set_status(
        self,
        client_order_id: str,
        status: BrokerOrderStatus,
        event_type: str,
        *,
        error: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> StoredOrder:
        with self._lock:
            current = self.get(client_order_id)
            if current.status == BrokerOrderStatus.FILLED and status != BrokerOrderStatus.FILLED:
                return current
            with self.connection:
                self.connection.execute(
                    "UPDATE live_orders SET status=?, last_error=?, updated_at=? WHERE client_order_id=?",
                    (status.value, error, utc_now(), client_order_id),
                )
                event_payload = dict(payload or {})
                if error:
                    event_payload["error"] = error
                self._event(event_type, client_order_id, event_payload)
            return self.get(client_order_id)

    def acknowledge(self, client_order_id: str) -> StoredOrder:
        with self._lock:
            current = self.get(client_order_id)
            if current.status == BrokerOrderStatus.CANCEL_PENDING:
                return current
            target = (
                BrokerOrderStatus.PARTIALLY_FILLED
                if current.filled_quantity
                else BrokerOrderStatus.ACKNOWLEDGED
            )
            return self._set_status(client_order_id, target, "ORDER_ACKNOWLEDGED")

    def reject(self, client_order_id: str, reason: str) -> StoredOrder:
        return self._set_status(
            client_order_id,
            BrokerOrderStatus.REJECTED,
            "ORDER_REJECTED",
            error=reason.strip() or "broker rejected order",
        )

    def mark_unknown(self, client_order_id: str, reason: str) -> StoredOrder:
        result = self._set_status(
            client_order_id,
            BrokerOrderStatus.UNKNOWN,
            "ORDER_STATE_UNKNOWN",
            error=reason.strip() or "unknown broker state",
        )
        self.halt(f"UNKNOWN_ORDER_STATE:{client_order_id}")
        return result

    def request_cancel(self, client_order_id: str, reason: str) -> StoredOrder:
        with self._lock:
            current = self.get(client_order_id)
            if current.status in TERMINAL_STATUSES:
                return current
            return self._set_status(
                client_order_id,
                BrokerOrderStatus.CANCEL_PENDING,
                "CANCEL_REQUESTED",
                payload={"reason": reason},
            )

    def cancel_failed(self, client_order_id: str, reason: str) -> StoredOrder:
        with self._lock:
            current = self.get(client_order_id)
            target = (
                BrokerOrderStatus.PARTIALLY_FILLED
                if current.filled_quantity
                else BrokerOrderStatus.ACKNOWLEDGED
            )
            return self._set_status(
                client_order_id,
                target,
                "CANCEL_FAILED",
                error=reason,
            )

    def canceled(self, client_order_id: str) -> StoredOrder:
        return self._set_status(
            client_order_id,
            BrokerOrderStatus.CANCELED,
            "CANCEL_CONFIRMED",
        )

    def expired(self, client_order_id: str, reason: str) -> StoredOrder:
        return self._set_status(
            client_order_id,
            BrokerOrderStatus.EXPIRED,
            "ORDER_EXPIRED",
            error=reason,
        )

    def modification_result(
        self, client_order_id: str, *, kind: str, success: bool, reason: str = ""
    ) -> StoredOrder:
        with self._lock:
            current = self.get(client_order_id)
            if current.status == BrokerOrderStatus.CANCEL_PENDING:
                target = BrokerOrderStatus.CANCEL_PENDING
            else:
                target = (
                    BrokerOrderStatus.PARTIALLY_FILLED
                    if current.filled_quantity
                    else BrokerOrderStatus.ACKNOWLEDGED
                )
            return self._set_status(
                client_order_id,
                target,
                f"{kind.upper()}_{'CONFIRMED' if success else 'FAILED'}",
                error=None if success else (reason or f"{kind} failed"),
            )

    def confirm_reduction(self, client_order_id: str, reduce_by: int) -> StoredOrder:
        with self._lock:
            current = self.get(client_order_id)
            new_quantity = current.quantity - int(reduce_by)
            if new_quantity < current.filled_quantity or new_quantity <= 0:
                self.halt(f"INVALID_REDUCTION_CONFIRMATION:{client_order_id}")
                raise StoreError("confirmed reduction conflicts with filled quantity")
            with self.connection:
                self.connection.execute(
                    "UPDATE live_orders SET quantity=?,updated_at=? WHERE client_order_id=?",
                    (new_quantity, utc_now(), client_order_id),
                )
                self._event(
                    "REDUCTION_APPLIED",
                    client_order_id,
                    {"reduce_by": int(reduce_by), "new_quantity": new_quantity},
                )
            return self.modification_result(client_order_id, kind="reduce", success=True)

    def apply_authoritative_order_quantity(self, client_order_id: str, quantity: int) -> StoredOrder:
        """Apply broker merge OrderQty, which is documented as effective quantity including fills."""
        quantity = int(quantity)
        with self._lock:
            current = self.get(client_order_id)
            if quantity <= 0 or quantity < current.filled_quantity:
                raise StoreError("authoritative quantity is invalid")
            if quantity == current.quantity:
                return current
            with self.connection:
                self.connection.execute(
                    "UPDATE live_orders SET quantity=?,updated_at=? WHERE client_order_id=?",
                    (quantity, utc_now(), client_order_id),
                )
                self._event(
                    "AUTHORITATIVE_ORDER_QUANTITY_APPLIED",
                    client_order_id,
                    {"old_quantity": current.quantity, "new_quantity": quantity},
                )
            return self.get(client_order_id)

    def record_fill(
        self,
        client_order_id: str,
        *,
        fill_id: str,
        quantity: int,
        price: Any,
        broker_order_no: str | None = None,
        seq_no: str | None = None,
    ) -> StoredOrder:
        if not fill_id.strip():
            raise ValueError("fill_id is required")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValueError("fill quantity must be positive")
        fill_price = Decimal(str(price))
        if not fill_price.is_finite() or fill_price <= 0:
            raise ValueError("fill price must be positive")

        with self._lock:
            existing = self.connection.execute(
                "SELECT client_order_id,quantity,price FROM live_fills WHERE fill_id=?",
                (fill_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["client_order_id"] != client_order_id
                    or int(existing["quantity"]) != quantity
                    or Decimal(existing["price"]) != fill_price
                ):
                    raise DuplicateIntentConflict("fill_id already has a different payload")
                return self.get(client_order_id)

            order = self.get(client_order_id)
            new_filled = order.filled_quantity + quantity
            if new_filled > order.quantity:
                self.halt(f"OVERFILL:{client_order_id}")
                raise StoreError("broker fill would exceed local order quantity")
            prior_value = (order.average_fill_price or Decimal("0")) * order.filled_quantity
            average = (prior_value + fill_price * quantity) / new_filled
            status = (
                BrokerOrderStatus.FILLED
                if new_filled == order.quantity
                else (
                    BrokerOrderStatus.CANCEL_PENDING
                    if order.status == BrokerOrderStatus.CANCEL_PENDING
                    else BrokerOrderStatus.PARTIALLY_FILLED
                )
            )
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO live_fills(
                        fill_id,client_order_id,broker_order_no,seq_no,quantity,price,filled_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        fill_id,
                        client_order_id,
                        broker_order_no,
                        seq_no,
                        quantity,
                        str(fill_price),
                        utc_now(),
                    ),
                )
                self.connection.execute(
                    """
                    UPDATE live_orders SET filled_quantity=?,average_fill_price=?,status=?,updated_at=?
                    WHERE client_order_id=?
                    """,
                    (new_filled, str(average), status.value, utc_now(), client_order_id),
                )
                self._event(
                    "FILL_RECORDED",
                    client_order_id,
                    {
                        "fill_id": fill_id,
                        "quantity": quantity,
                        "price": str(fill_price),
                        "cumulative_quantity": new_filled,
                        "status": status.value,
                    },
                )
            return self.get(client_order_id)

    def positions(self) -> dict[str, int]:
        with self._lock:
            result: dict[str, int] = {}
            rows = self.connection.execute(
                """
                SELECT o.symbol,o.side,f.quantity
                FROM live_fills f JOIN live_orders o ON o.client_order_id=f.client_order_id
                ORDER BY f.filled_at,f.fill_id
                """
            )
            for row in rows:
                sign = 1 if row["side"] == Side.BUY.value else -1
                result[row["symbol"]] = result.get(row["symbol"], 0) + sign * int(row["quantity"])
            return {symbol: qty for symbol, qty in result.items() if qty}

    def position_buckets(self) -> dict[str, int]:
        """Return signed fills without netting different financing categories."""

        trade_kind_by_order_type = {
            StockOrderType.CASH.value: 0,
            StockOrderType.DAY_TRADE_CONTROL.value: 0,
            StockOrderType.MARGIN_BUY.value: 3,
            StockOrderType.SHORT_SELL.value: 4,
            StockOrderType.BORROW_SELL_STRATEGY.value: 6,
            StockOrderType.BORROW_SELL_HEDGE.value: 6,
        }
        with self._lock:
            result: dict[str, int] = {}
            rows = self.connection.execute(
                """
                SELECT o.symbol,o.side,o.order_type,f.quantity
                FROM live_fills f JOIN live_orders o ON o.client_order_id=f.client_order_id
                ORDER BY f.filled_at,f.fill_id
                """
            )
            for row in rows:
                trade_kind = trade_kind_by_order_type[str(row["order_type"])]
                key = f"{row['symbol']}|{trade_kind}"
                sign = 1 if row["side"] == Side.BUY.value else -1
                result[key] = result.get(key, 0) + sign * int(row["quantity"])
            return {key: quantity for key, quantity in result.items() if quantity}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "control": self.control_state(),
                "orders": [
                    {
                        **asdict(order),
                        "side": order.side.value,
                        "price": None if order.price is None else str(order.price),
                        "price_type": order.price_type.value,
                        "time_in_force": order.time_in_force.value,
                        "ap_code": int(order.ap_code),
                        "order_type": order.order_type.value,
                        "purpose": order.purpose.value,
                        "status": order.status.value,
                        "average_fill_price": (
                            None if order.average_fill_price is None else str(order.average_fill_price)
                        ),
                    }
                    for order in self.orders()
                ],
                "positions": self.positions(),
            }
