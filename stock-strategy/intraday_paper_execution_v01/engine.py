"""Crash-safe paper execution engine.

This module intentionally contains no Yuanta SDK import and no live-order adapter.
It models the dangerous parts of execution locally so restart, duplicate, partial-
fill, cancellation, flattening, and kill-switch behaviour can be tested first.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Iterable, Mapping
import uuid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid price: {value!r}") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError("price must be finite and positive")
    return result


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExecutionMode(str, Enum):
    DISABLED = "DISABLED"
    PAPER_ONLY = "PAPER_ONLY"


class OrderStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}
OPEN = {
    OrderStatus.SUBMITTED,
    OrderStatus.ACKNOWLEDGED,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.CANCEL_PENDING,
}


class ExecutionError(RuntimeError):
    pass


class InvalidTransition(ExecutionError):
    pass


class DuplicateOrderConflict(ExecutionError):
    pass


class EmergencyStopActive(ExecutionError):
    pass


class ExecutionDisabled(ExecutionError):
    pass


class ReconciliationError(ExecutionError):
    pass


@dataclass(frozen=True)
class Order:
    order_id: str
    idempotency_key: str
    fingerprint: str
    symbol: str
    side: Side
    quantity: int
    limit_price: Decimal
    intent: str
    status: OrderStatus
    filled_quantity: int
    average_fill_price: Decimal | None
    created_at: str
    updated_at: str

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["side"] = self.side.value
        data["status"] = self.status.value
        data["limit_price"] = str(self.limit_price)
        data["average_fill_price"] = (
            None if self.average_fill_price is None else str(self.average_fill_price)
        )
        data["remaining_quantity"] = self.remaining_quantity
        return data


class PaperExecutionEngine:
    """SQLite-backed, single-process paper execution state machine."""

    def __init__(self, database: str | Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.database), timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "PaperExecutionEngine":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    limit_price TEXT NOT NULL,
                    intent TEXT NOT NULL CHECK(intent IN ('ENTRY','EXIT')),
                    status TEXT NOT NULL,
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    average_fill_price TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL REFERENCES orders(order_id),
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    price TEXT NOT NULL,
                    filled_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    order_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    emergency_stop INTEGER NOT NULL,
                    reason TEXT,
                    generation INTEGER NOT NULL,
                    execution_mode TEXT NOT NULL DEFAULT 'DISABLED',
                    updated_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO control
                    (singleton, emergency_stop, reason, generation, updated_at)
                    VALUES (1, 0, NULL, 0, 'INITIAL');
                """
            )
            columns = {
                row[1] for row in self.connection.execute("PRAGMA table_info(control)")
            }
            if "execution_mode" not in columns:
                self.connection.execute(
                    "ALTER TABLE control ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'DISABLED'"
                )

    def _event(
        self, event_type: str, order_id: str | None, payload: Mapping[str, object]
    ) -> None:
        self.connection.execute(
            "INSERT INTO events(event_type, order_id, payload, created_at) VALUES(?,?,?,?)",
            (event_type, order_id, json.dumps(payload, sort_keys=True), _now()),
        )

    @staticmethod
    def _fingerprint(
        symbol: str, side: Side, quantity: int, limit_price: Decimal, intent: str
    ) -> str:
        canonical = json.dumps(
            {
                "symbol": symbol,
                "side": side.value,
                "quantity": quantity,
                "limit_price": str(limit_price.normalize()),
                "intent": intent,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _row_to_order(row: sqlite3.Row) -> Order:
        average = row["average_fill_price"]
        return Order(
            order_id=row["order_id"],
            idempotency_key=row["idempotency_key"],
            fingerprint=row["fingerprint"],
            symbol=row["symbol"],
            side=Side(row["side"]),
            quantity=int(row["quantity"]),
            limit_price=Decimal(row["limit_price"]),
            intent=row["intent"],
            status=OrderStatus(row["status"]),
            filled_quantity=int(row["filled_quantity"]),
            average_fill_price=None if average is None else Decimal(average),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_order(self, order_id: str) -> Order:
        row = self.connection.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise KeyError(order_id)
        return self._row_to_order(row)

    def get_by_key(self, idempotency_key: str) -> Order | None:
        row = self.connection.execute(
            "SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return None if row is None else self._row_to_order(row)

    def orders(self, *, open_only: bool = False) -> list[Order]:
        rows = self.connection.execute("SELECT * FROM orders ORDER BY created_at, order_id")
        result = [self._row_to_order(row) for row in rows]
        return [order for order in result if order.status in OPEN] if open_only else result

    def control_state(self) -> dict[str, object]:
        row = self.connection.execute("SELECT * FROM control WHERE singleton = 1").fetchone()
        assert row is not None
        return {
            "emergency_stop": bool(row["emergency_stop"]),
            "reason": row["reason"],
            "generation": int(row["generation"]),
            "execution_mode": ExecutionMode(row["execution_mode"]).value,
            "updated_at": row["updated_at"],
        }

    def set_execution_mode(self, mode: ExecutionMode | str, reason: str) -> list[Order]:
        """Persistently enable paper execution or disable all new entries.

        No LIVE enum exists by design.  Disabling also requests cancellation of
        every open paper order; risk-reducing EXIT orders may still be created.
        """
        clean_reason = reason.strip()
        if not clean_reason:
            raise ValueError("mode-change reason is required")
        try:
            parsed = ExecutionMode(mode)
        except ValueError as exc:
            raise ValueError("only DISABLED and PAPER_ONLY are supported") from exc
        current = self.control_state()["execution_mode"]
        if current != parsed.value:
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE control SET execution_mode = ?, reason = ?, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (parsed.value, clean_reason, _now()),
                )
                self._event(
                    "EXECUTION_MODE_CHANGED",
                    None,
                    {"from": current, "to": parsed.value, "reason": clean_reason},
                )
        if parsed == ExecutionMode.DISABLED:
            return self.cancel_all(f"EXECUTION_DISABLED: {clean_reason}")
        return []

    def submit_order(
        self,
        *,
        idempotency_key: str,
        symbol: str,
        side: Side | str,
        quantity: int,
        limit_price: object,
        intent: str = "ENTRY",
    ) -> Order:
        key = idempotency_key.strip()
        clean_symbol = symbol.strip().upper()
        parsed_side = Side(side)
        parsed_price = _decimal(limit_price)
        parsed_intent = intent.strip().upper()
        if not key or not clean_symbol:
            raise ValueError("idempotency_key and symbol are required")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        if parsed_intent not in {"ENTRY", "EXIT"}:
            raise ValueError("intent must be ENTRY or EXIT")
        fingerprint = self._fingerprint(
            clean_symbol, parsed_side, quantity, parsed_price, parsed_intent
        )
        existing = self.get_by_key(key)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise DuplicateOrderConflict(
                    "idempotency key already belongs to a different order payload"
                )
            return existing
        control = self.control_state()
        if parsed_intent == "ENTRY":
            if control["execution_mode"] != ExecutionMode.PAPER_ONLY.value:
                raise ExecutionDisabled("new entry blocked because execution is disabled")
            if control["emergency_stop"]:
                raise EmergencyStopActive("new entry blocked by persistent emergency stop")
        order_id = uuid.uuid4().hex
        stamp = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO orders(
                    order_id,idempotency_key,fingerprint,symbol,side,quantity,
                    limit_price,intent,status,filled_quantity,average_fill_price,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    order_id,
                    key,
                    fingerprint,
                    clean_symbol,
                    parsed_side.value,
                    quantity,
                    str(parsed_price),
                    parsed_intent,
                    OrderStatus.SUBMITTED.value,
                    0,
                    None,
                    stamp,
                    stamp,
                ),
            )
            self._event(
                "ORDER_SUBMITTED",
                order_id,
                {
                    "idempotency_key": key,
                    "fingerprint": fingerprint,
                    "mode": "PAPER_ONLY",
                },
            )
        return self.get_order(order_id)

    def _set_status(
        self,
        order_id: str,
        allowed: Iterable[OrderStatus],
        target: OrderStatus,
        event_type: str,
        payload: Mapping[str, object] | None = None,
    ) -> Order:
        current = self.get_order(order_id)
        if current.status == target:
            return current
        if current.status not in set(allowed):
            raise InvalidTransition(f"{current.status.value} -> {target.value} is invalid")
        with self.connection:
            self.connection.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?",
                (target.value, _now(), order_id),
            )
            self._event(event_type, order_id, payload or {})
        return self.get_order(order_id)

    def acknowledge(self, order_id: str) -> Order:
        return self._set_status(
            order_id,
            {OrderStatus.SUBMITTED},
            OrderStatus.ACKNOWLEDGED,
            "ORDER_ACKNOWLEDGED",
        )

    def reject(self, order_id: str, reason: str) -> Order:
        if not reason.strip():
            raise ValueError("rejection reason is required")
        return self._set_status(
            order_id,
            {OrderStatus.SUBMITTED, OrderStatus.ACKNOWLEDGED},
            OrderStatus.REJECTED,
            "ORDER_REJECTED",
            {"reason": reason.strip()},
        )

    def record_fill(
        self, order_id: str, *, fill_id: str, quantity: int, price: object
    ) -> Order:
        if not fill_id.strip():
            raise ValueError("fill_id is required")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValueError("fill quantity must be a positive integer")
        fill_price = _decimal(price)
        existing = self.connection.execute(
            "SELECT order_id, quantity, price FROM fills WHERE fill_id = ?", (fill_id,)
        ).fetchone()
        if existing is not None:
            if (
                existing["order_id"] != order_id
                or int(existing["quantity"]) != quantity
                or Decimal(existing["price"]) != fill_price
            ):
                raise DuplicateOrderConflict("fill_id already has a different payload")
            return self.get_order(order_id)
        order = self.get_order(order_id)
        if order.status not in {
            OrderStatus.SUBMITTED,
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCEL_PENDING,
        }:
            raise InvalidTransition(f"cannot fill an order in {order.status.value}")
        new_filled = order.filled_quantity + quantity
        if new_filled > order.quantity:
            raise InvalidTransition("fill would exceed order quantity")
        previous_value = (order.average_fill_price or Decimal("0")) * order.filled_quantity
        average = (previous_value + fill_price * quantity) / new_filled
        if new_filled == order.quantity:
            next_status = OrderStatus.FILLED
        elif order.status == OrderStatus.CANCEL_PENDING:
            next_status = OrderStatus.CANCEL_PENDING
        else:
            next_status = OrderStatus.PARTIALLY_FILLED
        with self.connection:
            self.connection.execute(
                "INSERT INTO fills(fill_id,order_id,quantity,price,filled_at) VALUES(?,?,?,?,?)",
                (fill_id, order_id, quantity, str(fill_price), _now()),
            )
            self.connection.execute(
                """
                UPDATE orders
                SET filled_quantity = ?, average_fill_price = ?, status = ?, updated_at = ?
                WHERE order_id = ?
                """,
                (new_filled, str(average), next_status.value, _now(), order_id),
            )
            self._event(
                "FILL_RECORDED",
                order_id,
                {
                    "fill_id": fill_id,
                    "quantity": quantity,
                    "price": str(fill_price),
                    "cumulative_quantity": new_filled,
                    "status": next_status.value,
                },
            )
        return self.get_order(order_id)

    def request_cancel(self, order_id: str, reason: str) -> Order:
        if not reason.strip():
            raise ValueError("cancel reason is required")
        return self._set_status(
            order_id,
            {
                OrderStatus.SUBMITTED,
                OrderStatus.ACKNOWLEDGED,
                OrderStatus.PARTIALLY_FILLED,
            },
            OrderStatus.CANCEL_PENDING,
            "CANCEL_REQUESTED",
            {"reason": reason.strip()},
        )

    def confirm_cancel(self, order_id: str) -> Order:
        return self._set_status(
            order_id,
            {OrderStatus.CANCEL_PENDING},
            OrderStatus.CANCELED,
            "CANCEL_CONFIRMED",
        )

    def cancel_all(self, reason: str) -> list[Order]:
        result = []
        for order in self.orders(open_only=True):
            if order.status == OrderStatus.CANCEL_PENDING:
                result.append(order)
            else:
                result.append(self.request_cancel(order.order_id, reason))
        return result

    def position_quantities(self) -> dict[str, int]:
        positions: dict[str, int] = {}
        rows = self.connection.execute(
            """
            SELECT o.symbol, o.side, f.quantity
            FROM fills f JOIN orders o ON o.order_id = f.order_id
            ORDER BY f.filled_at, f.fill_id
            """
        )
        for row in rows:
            sign = 1 if row["side"] == Side.BUY.value else -1
            positions[row["symbol"]] = positions.get(row["symbol"], 0) + sign * int(
                row["quantity"]
            )
        return {symbol: quantity for symbol, quantity in positions.items() if quantity}

    def emergency_stop(self, reason: str) -> list[Order]:
        clean_reason = reason.strip()
        if not clean_reason:
            raise ValueError("emergency-stop reason is required")
        with self.connection:
            current = self.control_state()
            if not current["emergency_stop"]:
                self.connection.execute(
                    """
                    UPDATE control
                    SET emergency_stop = 1, reason = ?, generation = generation + 1,
                        updated_at = ? WHERE singleton = 1
                    """,
                    (clean_reason, _now()),
                )
                self._event("EMERGENCY_STOP_ENABLED", None, {"reason": clean_reason})
        return self.cancel_all(f"EMERGENCY_STOP: {clean_reason}")

    def force_flatten(self, mark_prices: Mapping[str, object]) -> list[Order]:
        control = self.control_state()
        if not control["emergency_stop"]:
            raise EmergencyStopActive("force_flatten requires emergency stop first")
        unsafe = [order for order in self.orders(open_only=True) if order.intent == "ENTRY"]
        if unsafe:
            raise InvalidTransition("confirm all entry-order cancellations before flattening")
        positions = self.position_quantities()
        open_exits = [order for order in self.orders(open_only=True) if order.intent == "EXIT"]
        if open_exits:
            coverage: dict[str, int] = {}
            for order in open_exits:
                sign = -1 if order.side == Side.SELL else 1
                coverage[order.symbol] = coverage.get(order.symbol, 0) + (
                    sign * order.remaining_quantity
                )
            expected = {symbol: -quantity for symbol, quantity in positions.items()}
            coverage = {symbol: quantity for symbol, quantity in coverage.items() if quantity}
            if coverage != expected:
                raise ReconciliationError(
                    "open exit quantity does not exactly offset the current paper position"
                )
            return open_exits
        generation = int(control["generation"])
        result = []
        for symbol, quantity in sorted(positions.items()):
            if symbol not in mark_prices:
                raise ValueError(f"missing mark price for {symbol}")
            side = Side.SELL if quantity > 0 else Side.BUY
            key = f"emergency-flatten:{generation}:{symbol}:{quantity}"
            result.append(
                self.submit_order(
                    idempotency_key=key,
                    symbol=symbol,
                    side=side,
                    quantity=abs(quantity),
                    limit_price=mark_prices[symbol],
                    intent="EXIT",
                )
            )
        return result

    def reset_emergency_stop(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("reset reason is required")
        if self.orders(open_only=True) or self.position_quantities():
            raise InvalidTransition("cannot reset until all orders are terminal and positions are flat")
        with self.connection:
            self.connection.execute(
                """
                UPDATE control SET emergency_stop = 0, reason = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (reason.strip(), _now()),
            )
            self._event("EMERGENCY_STOP_RESET", None, {"reason": reason.strip()})

    def reconcile(
        self,
        *,
        authoritative_orders: Mapping[str, tuple[str, int]],
        authoritative_positions: Mapping[str, int],
    ) -> dict[str, object]:
        """Fail closed when a restart snapshot disagrees with local persisted state.

        authoritative_orders maps idempotency key to (status, filled quantity).  A
        future reviewed adapter may construct this snapshot, but this module never
        connects to a broker itself.
        """
        local_orders = {
            order.idempotency_key: (order.status.value, order.filled_quantity)
            for order in self.orders()
        }
        local_positions = self.position_quantities()
        remote_orders = {
            key: (OrderStatus(status).value, int(filled))
            for key, (status, filled) in authoritative_orders.items()
        }
        remote_positions = {
            symbol: int(quantity)
            for symbol, quantity in authoritative_positions.items()
            if int(quantity)
        }
        mismatches: dict[str, object] = {}
        if local_orders != remote_orders:
            mismatches["orders"] = {"local": local_orders, "authoritative": remote_orders}
        if local_positions != remote_positions:
            mismatches["positions"] = {
                "local": local_positions,
                "authoritative": remote_positions,
            }
        if mismatches:
            self.emergency_stop("RECONCILIATION_MISMATCH")
            with self.connection:
                self._event("RECONCILIATION_FAILED", None, mismatches)
            raise ReconciliationError(json.dumps(mismatches, sort_keys=True))
        with self.connection:
            self._event(
                "RECONCILIATION_PASSED",
                None,
                {"order_count": len(local_orders), "positions": local_positions},
            )
        return {"status": "MATCH", "order_count": len(local_orders), "positions": local_positions}

    def snapshot(self) -> dict[str, object]:
        return {
            "mode": self.control_state()["execution_mode"],
            "live_send_available": False,
            "control": self.control_state(),
            "orders": [order.to_dict() for order in self.orders()],
            "positions": self.position_quantities(),
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
        }
