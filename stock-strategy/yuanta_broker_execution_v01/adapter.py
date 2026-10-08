"""Live Yuanta SPARK broker execution adapter.

The adapter is deliberately broker-only: it does not change signal generation,
position sizing, risk rules, or market-data logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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

# Actual SPARK PROD evidence on 2026-10-06 showed a 32-character broker
# BasketNo made from a five-character broker prefix plus the first 27
# characters of the submitted 32-character BasketNo. Keep exact matching for
# SDK variants that echo the value, and accept only this narrowly evidenced
# transformation together with the existing account/symbol/side/day checks.
_BROKER_BASKET_LENGTH = 32
_BROKER_BASKET_PREFIX_LENGTH = 5


def _basket_identity_matches(local_basket: Any, broker_basket: Any) -> bool:
    local = str(local_basket or "").strip()
    broker = str(broker_basket or "").strip()
    if not local or not broker:
        return False
    if broker == local:
        return True
    retained = _BROKER_BASKET_LENGTH - _BROKER_BASKET_PREFIX_LENGTH
    return bool(
        local.startswith("WS")
        and len(local) == _BROKER_BASKET_LENGTH
        and len(broker) == _BROKER_BASKET_LENGTH
        and broker[_BROKER_BASKET_PREFIX_LENGTH:] == local[:retained]
    )


class BrokerAdapterError(RuntimeError):
    pass


class LiveExecutionDisabled(BrokerAdapterError):
    pass


class ReconciliationMismatch(BrokerAdapterError):
    pass


class ReconciliationRequired(BrokerAdapterError):
    pass


class ExternalManualOrderConflict(BrokerAdapterError):
    """A manual order overlaps a not-yet-filled strategy entry candidate."""


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    status: str
    local_positions: dict[str, int]
    position_baseline: dict[str, int]
    expected_broker_positions: dict[str, int]
    broker_positions: dict[str, int]
    order_mismatches: list[dict[str, Any]]
    external_position_adjustments: dict[str, int] = field(default_factory=dict)
    external_orders_adopted: list[dict[str, Any]] = field(default_factory=list)


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


def _optional_code_text(value: Any) -> str:
    """Preserve numeric zero while keeping absent optional codes unknown."""
    if value is None or value == "":
        return ""
    return _as_str(value)


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
        "order_type": _optional_code_text(_safe(value, "OrderType", "")),
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
        "order_type": _optional_code_text(_safe(value, "OrderType", "")),
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


def _history_text(value: Any, name: str) -> str:
    raw = _optional_temporal_attribute(value, name)
    if raw is None:
        return ""
    try:
        return str(raw).strip()
    except Exception as exc:
        raise BrokerAdapterError(f"unreadable broker history field {name}") from exc


def _history_integer(value: Any, name: str) -> int | None:
    raw = _optional_temporal_attribute(value, name)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    return _strict_integer(raw, name)


def _history_amount(value: Any, name: str, *, signed: bool = False) -> str:
    raw = _optional_temporal_attribute(value, name)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    try:
        number = Decimal(str(raw))
        if isinstance(raw, bool) or not number.is_finite() or (not signed and number < 0):
            raise ValueError(name)
    except (ValueError, ArithmeticError, TypeError) as exc:
        raise BrokerAdapterError(f"invalid broker history amount {name}") from exc
    return str(number)


def _history_date(value: Any, name: str) -> str:
    raw = _optional_temporal_attribute(value, name)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    if isinstance(raw, str):
        text = raw.strip()
        if not re.fullmatch(r"(?:[0-9]{8}|[0-9]{4}/[0-9]{2}/[0-9]{2}|[0-9]{4}-[0-9]{2}-[0-9]{2})", text):
            raise BrokerAdapterError(f"invalid broker history date {name}")
        digits = text.replace("/", "").replace("-", "")
        return _validated_report_day(int(digits[:4]), int(digits[4:6]), int(digits[6:]), name)
    parts = [_strict_integer(_required_attribute(raw, key), f"{name}.{key}")
             for key in ("ushtYear", "bytMon", "bytDay")]
    return _validated_report_day(*parts, name)


def _history_time(value: Any, name: str) -> str:
    raw = _optional_temporal_attribute(value, name)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    hour, minute, second, millisecond = [
        _strict_integer(_required_attribute(raw, key), f"{name}.{key}")
        for key in ("bytHour", "bytMin", "bytSec", "ushtMSec")
    ]
    try:
        if millisecond > 999:
            raise ValueError(name)
        datetime(2000, 1, 1, hour, minute, second, millisecond * 1000)
    except (ValueError, OverflowError) as exc:
        raise BrokerAdapterError(f"invalid broker history time {name}") from exc
    return f"{hour:02d}:{minute:02d}:{second:02d}.{millisecond:03d}"


def _history_identity(value: Any, *, account: str) -> dict[str, Any]:
    # Verify ownership of the *account*, not ownership of an order by a bot.
    # No account identifier is included in the returned audit DTO.
    if str(_required_attribute(value, "Account")).strip() != account:
        raise BrokerAdapterError("broker history account mismatch")
    order_no = str(_required_attribute(value, "OrderNo")).strip()
    symbol = str(_required_attribute(value, "CompanyNo")).strip()
    side = str(_required_attribute(value, "BS")).strip()
    if not order_no or not symbol or side not in {"B", "S"}:
        raise BrokerAdapterError("invalid broker history order identity")
    order_type = _history_integer(value, "OrderType")
    if order_type is not None and order_type not in {0, 3, 4, 5, 6, 7, 8}:
        raise BrokerAdapterError("unsupported broker history OrderType")
    return {
        "account_verified": True, "order_no": order_no, "symbol": symbol,
        "side": side, "order_type": "" if order_type is None else str(order_type),
        "market_no": _history_text(value, "MarketNo"),
        "market_name": _history_text(value, "MarketName"),
        "stock_name": _history_text(value, "StkName"),
    }


def _history_price_type(value: Any, name: str) -> tuple[str, str]:
    flag = _history_text(value, name)
    kinds = {"1": "MARKET", "2": "LIMIT", "H": "LIMIT_UP", "L": "LIMIT_DOWN", "-": "FLAT"}
    if flag and flag not in kinds:
        # History uses 1/2, NOT SendStockOrder's M/space encoding.
        raise BrokerAdapterError(f"unsupported broker history {name}")
    return flag, kinds.get(flag, "")


def _normalise_history_order(value: Any, *, account: str) -> dict[str, Any]:
    result = _history_identity(value, account=account)
    status = _strict_integer(_required_attribute(value, "OrderStatus"), "OrderStatus")
    price_flag, price_type = _history_price_type(value, "PriceFlag")
    tif_code = _history_text(value, "Time_in_Force")
    tifs = {"0": "ROD", "3": "IOC", "4": "FOK"}
    if tif_code and tif_code not in tifs:
        raise BrokerAdapterError("unsupported broker history Time_in_Force")
    result.update({
        "source": "GetOrderTradeReport.StkOrderList",
        "price": _history_amount(value, "Price"),
        "price_flag": price_flag, "price_type": price_type,
        "time_in_force_code": tif_code, "time_in_force": tifs.get(tif_code, ""),
        "ap_code": _history_integer(value, "APCode"),
        "order_status": status,
        # Status 20 is ACK, not proof of FILLED, even for IOC.
        "terminal_status": {10: "REJECTED", 24: "EXPIRED", 25: "EXPIRED", 30: "CANCELED"}.get(status, "UNKNOWN"),
        "before_qty": _history_integer(value, "BeforeQty"),
        "after_qty": _strict_integer(_required_attribute(value, "AfterQty"), "AfterQty"),
        "ok_qty": _strict_integer(_required_attribute(value, "OkQty"), "OkQty"),
        "cancel_qty": _history_integer(value, "CancelQty"),
        "original_qty": _history_integer(value, "OR_QTY"),
        "basket_no": _history_text(value, "BasketNo"),
        "channel": _history_text(value, "Channel"),
        "error_no": _history_text(value, "ErrorNo"),
        "tax": _history_amount(value, "OTax"),
        "fees": _history_amount(value, "OCharge"),
        "due_amount": _history_amount(value, "ODueAmt", signed=True),
    })
    for source, target in (("TradeDate", "trade_date"), ("AcceptDate", "accept_date"), ("UpdateDate", "update_date")):
        result[target] = _history_date(value, source)
        result[f"{target}_source"] = source if result[target] else ""
    for source, target in (("AcceptTime", "accept_time"), ("UpdateTime", "update_time")):
        result[target] = _history_time(value, source)
        result[f"{target}_source"] = source if result[target] else ""
    for source, target in (
        ("CancelFlag", "cancel_flag"), ("ReduceFlag", "reduce_flag"),
        ("TraditionFlag", "tradition_flag"), ("TradeCurrency", "trade_currency"),
        ("Order_Success", "order_success_flag"), ("Reduce_Flag", "reduced_flag"),
        ("Chg_Prz_Flag", "repriced_flag"), ("TSE_Cancel", "exchange_cancel_flag"),
    ):
        result[target] = _history_text(value, source)
    return result


def _normalise_history_trade(value: Any, *, account: str) -> dict[str, Any]:
    result = _history_identity(value, account=account)
    price_flag, price_type = _history_price_type(value, "Price_Flag")
    result.update({
        "source": "GetOrderTradeReport.StkTradeList",
        "ok_qty": _strict_integer(_required_attribute(value, "OkQty"), "OkQty", minimum=1),
        "order_price": _history_amount(value, "OPrice"),
        "fill_price": _history_amount(value, "SPrice"),
        "price_flag": price_flag, "price_type": price_type,
        "exchange_code": _history_integer(value, "Exchange_Code"),
        "trade_currency": _history_text(value, "TradeCurrency"),
        # StkTrade has no SeqNo, BasketNo, TIF, or APCode. Never manufacture them
        # or auto-join it to StkOrder using OrderNo alone across dates.
        "trade_date": "", "trade_date_source": "", "fill_time": "",
        "fill_time_source": "",
    })
    raw = _optional_temporal_attribute(value, "DateTime")
    if raw is not None:
        year, month, day, hour, minute, second, millisecond = [
            _strict_integer(_required_attribute(raw, key), f"DateTime.{key}")
            for key in ("Year", "Month", "Day", "Hour", "Minute", "Second", "Millisecond")
        ]
        result["trade_date"] = _validated_report_day(year, month, day, "DateTime")
        try:
            if millisecond > 999:
                raise ValueError("DateTime")
            datetime(year, month, day, hour, minute, second, millisecond * 1000)
        except (ValueError, OverflowError) as exc:
            raise BrokerAdapterError("invalid broker history DateTime") from exc
        result["trade_date_source"] = result["fill_time_source"] = "DateTime"
        result["fill_time"] = f"{hour:02d}:{minute:02d}:{second:02d}.{millisecond:03d}"
    return result


def _normalise_order_trade_report(value: Any, *, account: str) -> dict[str, Any]:
    """Pure read-only DTO translation; never attaches callbacks or writes a store.

    Caller must capture GetOrderTradeReport(False, account, language) on its
    independently controlled session. This report is evidence for review, not
    permission to adopt a fill, loosen BasketNo ownership, or authorize orders.
    Missing fields remain unknown; AcceptDate never substitutes for TradeDate.
    Channel is retained verbatim: the vendor specifies no channel-code meaning.
    """
    if not isinstance(account, str) or not re.fullmatch(r"S[0-9]{11}", account):
        raise ValueError("expected Yuanta securities account is required")
    return {
        "source": "GetOrderTradeReport", "account_verified": True,
        "orders": [_normalise_history_order(row, account=account)
                   for row in _required_collection(value, "StkOrderList")],
        "trades": [_normalise_history_trade(row, account=account)
                   for row in _required_collection(value, "StkTradeList")],
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
        position_baseline_captured_at: str | datetime | None = None,
        external_inventory_listener: Callable[[dict[str, Any]], Any] | None = None,
        pre_order_reconcile_timeout: float = 20.0,
        ambiguous_result_grace_seconds: float = 2.0,
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
        self.ambiguous_result_grace_seconds = float(ambiguous_result_grace_seconds)
        if (
            not math.isfinite(self.ambiguous_result_grace_seconds)
            or self.ambiguous_result_grace_seconds <= 0
        ):
            raise ValueError("ambiguous_result_grace_seconds must be positive")
        self._frozen_position_baseline: dict[str, int] = {}
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
                self._frozen_position_baseline[key] = quantity
        self.position_baseline_captured_at = self._parse_baseline_captured_at(
            position_baseline_captured_at
        )
        self.external_inventory_listener = external_inventory_listener
        self.position_baseline: dict[str, int] = dict(self._frozen_position_baseline)
        self._refresh_effective_position_baseline()
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
        self._snapshot_mutation_requests: dict[str, int] = {}
        # SPARK may return a reused Identify in SendStockOrder before the
        # basket-backed RR_RealReport arrives.  Keep the unproven result only
        # for a short bounded grace period.  A later report may prove the
        # current order through account/symbol/side/day/BasketNo/OrderNo; if
        # it does not, expiry preserves the original fail-closed halt.
        self._deferred_order_results: dict[str, tuple[float, dict[str, Any]]] = {}
        self._query_worker: threading.Thread | None = None
        self._query_error: Exception | None = None
        self._query_uncertain = False
        self._snapshot_complete = True
        self.api.OnResponse += self._on_response
        self._worker.start()

    @staticmethod
    def _parse_baseline_captured_at(
        value: str | datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        try:
            stamp = (
                value
                if isinstance(value, datetime)
                else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("position baseline captured_at is invalid") from exc
        if stamp.tzinfo is None:
            raise ValueError("position baseline captured_at must be timezone-aware")
        return stamp.astimezone(TAIPEI)

    def _external_adjustment_day(self) -> str | None:
        captured = self.position_baseline_captured_at
        today = datetime.now(TAIPEI).date()
        if captured is None or captured.date() != today:
            return None
        return today.strftime("%Y%m%d")

    @staticmethod
    def _combine_positions(*parts: Mapping[str, int]) -> dict[str, int]:
        result: dict[str, int] = {}
        for part in parts:
            for key, raw_quantity in part.items():
                quantity = int(raw_quantity)
                if quantity:
                    result[str(key)] = result.get(str(key), 0) + quantity
                    if result[str(key)] == 0:
                        result.pop(str(key))
        return result

    def _refresh_effective_position_baseline(self) -> dict[str, int]:
        day = self._external_adjustment_day()
        if day is None:
            # Preserve the original public attribute behavior for UAT/tests and
            # callers without reviewed baseline provenance. External adoption
            # is disabled in this compatibility mode.
            return {}
        adjustments = self.store.external_position_adjustments(day)
        self.position_baseline = self._combine_positions(
            self._frozen_position_baseline,
            adjustments,
        )
        return adjustments

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
        # Fence target recovery to requests that already existed when this
        # query began. A completed older snapshot cannot finalize a mutation
        # created while the query was in flight.
        self._snapshot_mutation_requests = {}
        for order in self.store.orders(open_only=True):
            mutation = self.store.pending_mutation(order.client_order_id)
            if mutation is not None:
                self._snapshot_mutation_requests[order.client_order_id] = int(mutation["identify"])

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
        except ExternalManualOrderConflict:
            # The snapshot itself completed and proved a candidate-specific
            # manual-order collision.  The current candidate is rejected, but
            # a later symbol may run its own mandatory fresh reconciliation.
            raise
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

    def _remote_is_locally_owned(
        self,
        remote: Mapping[str, Any],
        local_orders: Iterable[StoredOrder],
    ) -> bool:
        basket_no = str(remote.get("basket_no", "") or "")
        order_no = str(remote.get("order_no", "") or "")
        return any(
            self._is_current_order(order)
            and self._identity_matches(order, remote)
            and (
                (basket_no and _basket_identity_matches(order.basket_no, basket_no))
                or (
                    not basket_no
                    and order_no
                    and order_no == order.broker_order_no
                )
            )
            for order in local_orders
        )

    @staticmethod
    def _remote_order_stamp(remote: Mapping[str, Any]) -> datetime | None:
        raw_day = str(remote.get("trade_date", "") or "").strip()
        raw_time = str(remote.get("order_time", "") or "").strip()
        day = raw_day.replace("/", "").replace("-", "")
        if not re.fullmatch(r"[0-9]{8}", day):
            return None
        if not re.fullmatch(r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?", raw_time):
            return None
        try:
            return datetime.fromisoformat(
                f"{day[:4]}-{day[4:6]}-{day[6:]}T{raw_time}"
            ).replace(tzinfo=TAIPEI)
        except ValueError:
            return None

    @staticmethod
    def _external_trade_kind(remote: Mapping[str, Any]) -> int | None:
        raw = str(remote.get("order_type", "") or "").strip()
        return {"0": 0, "9": 0, "3": 3, "4": 4, "5": 6, "6": 6}.get(raw)

    def _external_inventory_candidates(
        self,
        merge: list[dict[str, Any]],
        local_positions: Mapping[str, int],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Translate only broker-proven manual fills after the daily baseline.

        Unknown non-empty basket numbers are never treated as manual orders:
        they may be lost strategy ownership.  A manual order may coexist with
        the bot only while its symbol has no strategy-owned position.  A newly
        RESERVED strategy candidate on that symbol is a soft conflict which
        the caller may skip without halting the whole account.
        """
        captured_at = self.position_baseline_captured_at
        day = self._external_adjustment_day()
        if captured_at is None or day is None:
            return [], []

        local_orders = self.store.orders()
        local_open_symbols = {
            order.symbol
            for order in local_orders
            if order.status not in TERMINAL_STATUSES
        }
        local_position_symbols = {
            str(key).partition("|")[0]
            for key, quantity in local_positions.items()
            if int(quantity)
        }
        existing_records = self.store.external_inventory_records(day)
        candidates: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []

        for remote in merge:
            if self._remote_is_locally_owned(remote, local_orders):
                continue
            filled = int(remote.get("ok_qty", 0) or 0)
            if filled <= 0:
                continue
            stamp = self._remote_order_stamp(remote)
            if stamp is None:
                conflicts.append({
                    "reason": "external_fill_missing_timestamp",
                    "symbol": str(remote.get("symbol", "") or ""),
                    "order_no": str(remote.get("order_no", "") or ""),
                })
                continue
            if stamp <= captured_at:
                # This order was already represented by the frozen baseline.
                continue

            symbol = str(remote.get("symbol", "") or "").strip().upper()
            order_no = str(remote.get("order_no", "") or "").strip()
            basket_no = str(remote.get("basket_no", "") or "").strip()
            raw_side = str(remote.get("side", "") or "").strip().upper()
            side = {"BUY": "B", "SELL": "S", "B": "B", "S": "S"}.get(raw_side)
            trade_kind = self._external_trade_kind(remote)
            remote_status = _remote_status(remote)

            if basket_no:
                conflicts.append({
                    "reason": "unknown_remote_basket_with_fill",
                    "symbol": symbol,
                    "order_no": order_no,
                    "basket_no": basket_no,
                })
                continue
            if not order_no or not symbol or side is None or trade_kind is None:
                conflicts.append({
                    "reason": "unsupported_external_fill_identity",
                    "symbol": symbol,
                    "order_no": order_no,
                })
                continue
            signed = filled if side == "B" else -filled
            existing = existing_records.get(order_no)
            previous_filled = 0
            if existing is not None:
                if (
                    str(existing["symbol"]).upper() != symbol
                    or str(existing["side"]).upper() != side
                    or int(existing["trade_kind"]) != trade_kind
                ):
                    conflicts.append({
                        "reason": "external_fill_identity_changed",
                        "symbol": symbol,
                        "order_no": order_no,
                    })
                    continue
                previous_filled = int(existing["cumulative_filled_quantity"])
                if filled < previous_filled:
                    conflicts.append({
                        "reason": "external_fill_quantity_decreased",
                        "symbol": symbol,
                        "order_no": order_no,
                    })
                    continue

            # A previously adopted fill is already part of the effective
            # baseline. Seeing the same cumulative quantity again after the
            # strategy later owns this symbol is harmless; any new manual fill
            # quantity while a strategy position exists is a hard conflict.
            if symbol in local_position_symbols and filled > previous_filled:
                conflicts.append({
                    "reason": "external_fill_conflicts_with_strategy_position",
                    "symbol": symbol,
                    "order_no": order_no,
                })
                continue
            # A completed manual fill can safely become part of the rolling
            # external baseline before a later strategy entry on the same
            # symbol.  Only an in-flight manual order overlaps the strategy's
            # newly RESERVED entry and must make that candidate stand down.
            if (
                symbol in local_open_symbols
                and remote_status not in TERMINAL_STATUSES
            ):
                conflicts.append({
                    "reason": "external_manual_order_conflict",
                    "symbol": symbol,
                    "order_no": order_no,
                })

            fingerprint_payload = {
                "trading_date": day,
                "order_no": order_no,
                "symbol": symbol,
                "trade_kind": trade_kind,
                "side": side,
                "filled": filled,
                "remote_status": remote_status.value,
                "order_time": str(remote.get("order_time", "") or ""),
            }
            candidates.append({
                "trading_date": day,
                "order_no": order_no,
                "symbol": symbol,
                "trade_kind": trade_kind,
                "side": side,
                "cumulative_filled_quantity": filled,
                "signed_quantity": signed,
                "remote_status": remote_status.value,
                "row_fingerprint": hashlib.sha256(
                    json.dumps(
                        fingerprint_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            })
        return candidates, conflicts

    def _candidate_external_baseline(
        self,
        day: str,
        candidates: Iterable[Mapping[str, Any]],
    ) -> tuple[dict[str, int], dict[str, int]]:
        records = self.store.external_inventory_records(day)
        for row in candidates:
            records[str(row["order_no"])] = dict(row)
        adjustments: dict[str, int] = {}
        for row in records.values():
            key = f"{str(row['symbol']).upper()}|{int(row['trade_kind'])}"
            adjustments[key] = adjustments.get(key, 0) + int(row["signed_quantity"])
        adjustments = {key: value for key, value in adjustments.items() if value}
        return self._combine_positions(self._frozen_position_baseline, adjustments), adjustments

    def reconcile(
        self,
        *,
        timeout: float = 20.0,
        strict_positions: bool = True,
    ) -> ReconciliationResult:
        """Recover missed detailed reports, then compare aggregate orders and inventory.

        The default remains strict for unexplained inventory.  When an
        account-scoped baseline timestamp is supplied, complete current-session
        broker reports may explain unrelated manual fills after that timestamp.
        Those fills are durably separated from strategy-owned fills and become a
        rolling external baseline; unexplained transfers and same-symbol overlap
        with a strategy position still halt fail-closed.
        """
        with self._snapshot_deadline(timeout) as deadline:
            self.request_reconciliation()
            self._wait_snapshot(deadline)

            merge = list(self._latest_merge or [])
            broker_positions = dict(self._latest_positions or {})
            local_positions = self.store.position_buckets()
            order_mismatches = self._compare_orders(merge)
            external_adjustments = self._refresh_effective_position_baseline()
            adopted: list[dict[str, Any]] = []

            candidates, external_conflicts = self._external_inventory_candidates(
                merge,
                local_positions,
            )
            order_mismatches.extend(external_conflicts)

            manual_conflicts = [
                row for row in order_mismatches
                if row.get("reason") == "external_manual_order_conflict"
            ]
            hard_order_mismatches = [
                row for row in order_mismatches
                if row.get("reason") != "external_manual_order_conflict"
            ]

            day = self._external_adjustment_day()
            candidate_baseline = dict(self.position_baseline)
            if day is not None and candidates:
                candidate_baseline, _ = self._candidate_external_baseline(
                    day,
                    candidates,
                )

            expected_positions = self._combine_positions(
                candidate_baseline,
                local_positions,
            )
            position_mismatch = strict_positions and expected_positions != broker_positions

            if hard_order_mismatches or position_mismatch:
                self.store.halt("RECONCILIATION_MISMATCH")
                raise ReconciliationMismatch(
                    f"orders={order_mismatches!r}; "
                    f"local_positions={local_positions!r}; baseline={candidate_baseline!r}; "
                    f"expected={expected_positions!r}; broker_positions={broker_positions!r}"
                )

            if day is not None and candidates:
                adopted = self.store.adopt_external_inventory_rows(day, candidates)
                external_adjustments = self._refresh_effective_position_baseline()
                expected_positions = self._combine_positions(
                    self.position_baseline,
                    local_positions,
                )
                if expected_positions != broker_positions:
                    self.store.halt("EXTERNAL_INVENTORY_COMMIT_MISMATCH")
                    raise ReconciliationMismatch(
                        "external inventory changed during durable adoption"
                    )
                if adopted and self.external_inventory_listener is not None:
                    for event in adopted:
                        try:
                            self.external_inventory_listener(dict(event))
                        except Exception as exc:
                            self.store.audit_event(
                                "EXTERNAL_INVENTORY_LISTENER_FAILED",
                                {
                                    "error_type": type(exc).__name__,
                                    "symbol": event.get("symbol"),
                                },
                            )
            if manual_conflicts:
                # Broker-proven partial fills were first adopted into the
                # rolling external baseline, but no broker send can occur from
                # the overlapping strategy candidate. A later different symbol
                # still runs its own fresh reconciliation before submission.
                self._reconciled = True
                raise ExternalManualOrderConflict(
                    "manual broker order overlaps the strategy entry candidate; "
                    "candidate was skipped without changing strategy inventory"
                )
            self._reconciled = True
            return ReconciliationResult(
                status="MATCH",
                local_positions=local_positions,
                position_baseline=dict(self.position_baseline),
                expected_broker_positions=expected_positions,
                broker_positions=broker_positions,
                order_mismatches=[],
                external_position_adjustments=dict(external_adjustments),
                external_orders_adopted=adopted,
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
            if remote is None:
                transformed = [
                    row for row in merge
                    if _basket_identity_matches(local.basket_no, row.get("basket_no"))
                ]
                if len(transformed) == 1:
                    remote = transformed[0]
                elif len(transformed) > 1:
                    mismatches.append({
                        "client_order_id": local.client_order_id,
                        "reason": "ambiguous_transformed_basket",
                    })
                    continue
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
            self._resolve_mutation_from_remote(local, remote, authoritative_snapshot=True)
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

            pending = self.store.pending_mutation(local.client_order_id)
            if pending is not None and pending["payload"].get("result_identity_uncertain"):
                mismatches.append({"client_order_id": local.client_order_id,
                                   "reason": "unresolved_broker_mutation",
                                   "operation": pending["operation"],
                                   "identify": int(pending["identify"])})

        # Reverse reconciliation: unrelated manual orders may coexist with
        # the bot.  An external order on a strategy-owned position remains a
        # hard conflict; an order on a newly RESERVED candidate is a soft
        # conflict so that candidate can be skipped without halting all other
        # symbols. Terminal fills are validated separately against inventory.
        local_orders = self.store.orders()
        local_position_symbols = {
            key.partition("|")[0]
            for key, quantity in self.store.position_buckets().items()
            if int(quantity)
        }
        local_open_symbols = {
            order.symbol
            for order in local_orders
            if order.status not in TERMINAL_STATUSES
        }
        for remote in merge:
            remote_status = _remote_status(remote)
            if remote_status in TERMINAL_STATUSES:
                continue
            if self._remote_is_locally_owned(remote, local_orders):
                continue

            symbol = str(remote.get("symbol", "") or "").strip().upper()
            order_no = str(remote.get("order_no", "") or "").strip()
            basket_no = str(remote.get("basket_no", "") or "").strip()
            raw_side = str(remote.get("side", "") or "").strip().upper()
            side = {"BUY": "B", "SELL": "S", "B": "B", "S": "S"}.get(raw_side)
            stamp = self._remote_order_stamp(remote)
            conflict_reason = None
            if basket_no:
                # Preserve the established audit/alert reason while still
                # refusing to classify an unknown basket as a manual order.
                conflict_reason = "unexpected_remote_open_order"
            elif (
                not order_no
                or not symbol
                or side is None
                or self._external_trade_kind(remote) is None
            ):
                conflict_reason = "unsupported_external_open_order_identity"
            elif stamp is None:
                conflict_reason = "external_open_order_missing_timestamp"
            elif self._external_adjustment_day() is None:
                conflict_reason = "unexpected_remote_open_order"
            elif stamp <= self.position_baseline_captured_at:
                conflict_reason = "external_open_order_predates_baseline"
            elif symbol in local_position_symbols:
                conflict_reason = "unexpected_remote_open_order"
            elif symbol in local_open_symbols:
                conflict_reason = "external_manual_order_conflict"
            if conflict_reason is not None:
                mismatches.append(
                    {
                        "reason": conflict_reason,
                        "symbol": symbol,
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
        if basket and not _basket_identity_matches(local.basket_no, basket):
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

    def _resolve_mutation_from_remote(
        self, local: StoredOrder, remote: Mapping[str, Any], *, authoritative_snapshot: bool = False
    ) -> None:
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
            # Prices can revisit an earlier target (A -> B -> A). A matching
            # live/detail-history RR is therefore not current mutation proof.
            # Only the fully drained current merge query, fenced to this
            # request, may establish the actual broker price.
            if (matches and authoritative_snapshot and self._snapshot_complete
                    and self._snapshot_mutation_requests.get(local.client_order_id) == int(pending["identify"])):
                self.store.finalize_latest_request(local.client_order_id, operation, success=True)
        elif operation == "REDUCE":
            expected = pending["payload"].get("expected_quantity")
            quantity = int(remote.get("order_qty", 0))
            if expected is not None and quantity > 0 and quantity == int(expected):
                self.store.finalize_latest_request(local.client_order_id, operation, success=True)
        # A failure status identifies an operation, not its request. It may be
        # a delayed manual/earlier mutation. Neither an empty detail snapshot,
        # FIRST local operation, OrderTime nor fill SeqNo proves attribution.
        # Keep the durable barrier until an exact result or terminal/target
        # state proves the current mutation's outcome.
        remaining = self.store.pending_mutation(local.client_order_id)
        failure_status = {"CANCEL": 3, "REDUCE": 5, "MODIFY_PRICE": 21}[operation]
        if remaining is not None and int(remote.get("last_order_status", -1)) == failure_status:
            self.store.mark_request_result_uncertain(int(remaining["identify"]), "UNATTRIBUTED_BROKER_MUTATION_FAILURE")
            self.store.halt("BLOCKED_BROKER_MUTATION_AMBIGUOUS")

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
                self._retry_deferred_order_results()
                continue
            if item is None:
                self._retry_deferred_order_results(force_expire=True)
                self._queue.task_done()
                break
            kind, payload = item
            try:
                if kind == "order_result":
                    self._apply_order_result(payload)
                elif kind == "real_report":
                    self._apply_real_report(payload)
                    self._retry_deferred_order_results()
                elif kind == "detail_snapshot":
                    for report in payload:
                        self._apply_real_report(report)
                    self._retry_deferred_order_results()
                    self._detail_event.set()
                elif kind == "merge_snapshot":
                    self._latest_merge = payload
                    self._retry_deferred_order_results()
                    self._merge_event.set()
                elif kind == "position_snapshot":
                    self._latest_positions = payload
                    self._position_event.set()
                elif kind == "merge_live":
                    self._apply_merge_live(payload)
                    self._retry_deferred_order_results()
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
            transformed = [
                order for order in self.store.orders()
                if self._is_current_order(order)
                and _basket_identity_matches(order.basket_no, basket)
                and self._identity_matches(order, report)
            ]
            if len(transformed) == 1:
                return transformed[0]
            if len(transformed) > 1:
                self.store.halt("AMBIGUOUS_TRANSFORMED_BASKET")
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
            mutation = self.store.pending_mutation(owned.client_order_id)
            if mutation is not None and (exact is None or int(exact["identify"]) != int(mutation["identify"])):
                # OrderNo proves the order, not which operation produced an API
                # result. A reused NEW Identify can equally be a late NEW ACK
                # or a new cancel/modify result; preserve both request histories.
                self.store.mark_request_result_uncertain(int(mutation["identify"]), "REUSED_OR_UNPROVEN_RESULT_IDENTIFY")
                self.store.halt("BLOCKED_BROKER_MUTATION_AMBIGUOUS")
                return None
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

        return None

    @staticmethod
    def _order_result_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: row.get(key, "")
            for key in (
                "identify",
                "reply_code",
                "order_no",
                "err_type",
                "err_no",
                "advisory",
            )
        }

    @staticmethod
    def _order_result_key(result_payload: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "day": datetime.now(TAIPEI).date().isoformat(),
                    "result": dict(result_payload),
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()

    def _process_order_result(
        self,
        row: Mapping[str, Any],
        *,
        result_key: str,
        result_payload: Mapping[str, Any],
    ) -> bool:
        if self.store.broker_result_receipt(result_key) is not None:
            return True
        broker_identify = int(row.get("identify", 0))
        request = self._resolve_order_result_request(
            broker_identify,
            order_no=str(row.get("order_no", "") or ""),
        )
        if request is None:
            return False

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
        completed = self.store.complete_request(
            identify,
            success=success,
            payload=detail,
        )
        accepted_outcomes = {"ACCEPTED", "CONFIRMED"} if success else {"REJECTED", "FAILED"}
        if completed is None or completed["request_status"] not in accepted_outcomes:
            # Contradictory API results halt, but must not mark a live broker
            # order rejected or reopen a failed/unsent request.
            self.store.record_broker_result_receipt(
                result_key,
                identify,
                result_payload,
            )
            return True

        if success:
            order_no = detail["order_no"]
            if order_no:
                self.store.bind_broker_order(client_order_id, order_no)
            if operation == "NEW":
                if self.store.get(client_order_id).broker_order_no:
                    self.store.acknowledge(client_order_id)
                else:
                    self.store.mark_unknown(
                        client_order_id,
                        "ACK_WITHOUT_BROKER_ORDER_NO",
                    )
            # CANCEL/MODIFY/REDUCE are only request-accepted here; terminal
            # state is driven by RR_RealReport / RR_RealReportMerge.
            self.store.record_broker_result_receipt(
                result_key,
                identify,
                result_payload,
            )
            return True

        reason = " ".join(
            detail[key]
            for key in ("err_type", "err_no", "advisory")
            if detail[key]
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
        self.store.record_broker_result_receipt(
            result_key,
            identify,
            result_payload,
        )
        return True

    def _defer_order_result(
        self,
        row: Mapping[str, Any],
        *,
        result_key: str,
    ) -> None:
        if result_key in self._deferred_order_results:
            return
        self._deferred_order_results[result_key] = (
            time.monotonic() + self.ambiguous_result_grace_seconds,
            dict(row),
        )
        self.store.audit_event(
            "BROKER_ORDER_RESULT_DEFERRED",
            {
                "broker_identify": int(row.get("identify", 0)),
                "order_no": str(row.get("order_no", "") or ""),
                "grace_seconds": self.ambiguous_result_grace_seconds,
                "result_key": result_key,
            },
        )

    def _retry_deferred_order_results(self, *, force_expire: bool = False) -> None:
        now = time.monotonic()
        for result_key, (deadline, row) in list(
            self._deferred_order_results.items()
        ):
            result_payload = self._order_result_payload(row)
            if self._process_order_result(
                row,
                result_key=result_key,
                result_payload=result_payload,
            ):
                self._deferred_order_results.pop(result_key, None)
                self.store.audit_event(
                    "BROKER_ORDER_RESULT_DEFERRED_RESOLVED",
                    {
                        "broker_identify": int(row.get("identify", 0)),
                        "order_no": str(row.get("order_no", "") or ""),
                        "result_key": result_key,
                    },
                )
                continue
            if not force_expire and now < deadline:
                continue
            self._deferred_order_results.pop(result_key, None)
            broker_identify = int(row.get("identify", 0))
            self.store.audit_event(
                "BROKER_ORDER_RESULT_DEFERRED_EXPIRED",
                {
                    "broker_identify": broker_identify,
                    "order_no": str(row.get("order_no", "") or ""),
                    "result_key": result_key,
                },
            )
            self.store.halt("AMBIGUOUS_BROKER_ORDER_RESULT")

    def _apply_order_result(self, rows: Iterable[Mapping[str, Any]]) -> None:
        for row in rows:
            result_payload = self._order_result_payload(row)
            result_key = self._order_result_key(result_payload)
            if self._process_order_result(
                row,
                result_key=result_key,
                result_payload=result_payload,
            ):
                self._deferred_order_results.pop(result_key, None)
                continue
            self._defer_order_result(
                row,
                result_key=result_key,
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
        if status in {3, 5, 21}:
            operation = {3: "CANCEL", 5: "REDUCE", 21: "MODIFY_PRICE"}[status]
            pending = self.store.pending_mutation(local.client_order_id)
            if pending is not None and pending["operation"] == operation:
                self.store.mark_request_result_uncertain(int(pending["identify"]), "UNATTRIBUTED_BROKER_MUTATION_FAILURE")
                self.store.halt("BLOCKED_BROKER_MUTATION_AMBIGUOUS")
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
