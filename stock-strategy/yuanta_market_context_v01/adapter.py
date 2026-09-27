"""Normalize Yuanta .NET callback objects without importing the vendor SDK."""

from __future__ import annotations

from datetime import datetime
import math
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .models import QuoteContext, SecurityContext


TAIPEI = ZoneInfo("Asia/Taipei")


def _text(value: Any) -> str:
    try:
        return str(value or "").strip()
    except Exception:
        return ""


def _number(value: Any, cast=float):
    try:
        result = cast(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(result, float) and not math.isfinite(result):
        return None
    return result


def _items(value: Any) -> Iterable[Any]:
    if value is None:
        return ()
    try:
        return tuple(value)
    except TypeError:
        count = _number(getattr(value, "Count", 0), int) or 0
        return tuple(value[index] for index in range(count))


def _warnings(value: Any) -> tuple[str, ...]:
    return tuple(item for item in (_text(row) for row in _items(value)) if item)


def _market(value: Any) -> str:
    rendered = _text(value).upper()
    aliases = {"1": "TWSE", "2": "TWOTC", "TWOTC": "TWOTC", "TPEX": "TWOTC"}
    return aliases.get(rendered, rendered)


def _quote_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=TAIPEI) if value.tzinfo is None else value
    components = tuple(
        _number(getattr(value, name, None), int)
        for name in ("Year", "Month", "Day", "Hour", "Minute", "Second", "Millisecond")
    )
    if all(component is not None for component in components):
        year, month, day, hour, minute, second, millisecond = components
        return datetime(
            year, month, day, hour, minute, second,
            microsecond=millisecond * 1000,
            tzinfo=TAIPEI,
        )
    rendered = _text(value)
    if not rendered:
        raise ValueError("quote time is missing")
    parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=TAIPEI) if parsed.tzinfo is None else parsed


def normalize_stock_information_result(result: Any) -> tuple[SecurityContext, ...]:
    """Convert ``GetStockInformation`` output to immutable internal rows."""
    normalized = []
    for row in _items(getattr(result, "StockInformationList", None)):
        symbol = _text(getattr(row, "StockCode", ""))
        if not symbol:
            continue
        normalized.append(SecurityContext(
            symbol=symbol,
            market=_market(getattr(row, "MarketNo", "")),
            day_trade_code=_text(getattr(row, "Dayoffmark", "")).upper(),
            credit_percent=_number(getattr(row, "Creditpercent", None), int),
            lend_percent=_number(getattr(row, "Lendpercent", None), int),
            credit_remnants=_number(getattr(row, "Creditremnants", None), int),
            lend_remnants=_number(getattr(row, "Lendremnants", None), int),
            lend_sell_mark=_text(getattr(row, "LendSellMark", "")).upper(),
            lend_qty=_number(getattr(row, "LendQty", None), int),
            warnings=_warnings(getattr(row, "StockWarning", None)),
            update_date=_text(getattr(row, "UpdateDate", "")),
        ))
    return tuple(normalized)


def normalize_quote_result(result: Any) -> tuple[QuoteContext, ...]:
    """Convert ``GetWatchListAll`` output to immutable internal rows."""
    normalized = []
    for row in _items(getattr(result, "QueryWatchList", None)):
        symbol = _text(getattr(row, "StkCode", ""))
        if not symbol:
            continue

        def f(name: str) -> float:
            return float(_number(getattr(row, name, None), float) or 0.0)

        def i(name: str) -> int:
            return int(_number(getattr(row, name, None), int) or 0)

        normalized.append(QuoteContext(
            symbol=symbol,
            market=_market(getattr(row, "MarketNo", "")),
            stock_name=_text(getattr(row, "StkName", "")),
            quote_time=_quote_time(getattr(row, "Time", None)),
            previous_close=f("YstPrice"),
            open_reference=f("OpenRefPrice"),
            limit_up=f("UpStopPrice"),
            limit_down=f("DownStopPrice"),
            open_price=f("OpenPrice"),
            high_price=f("HighPrice"),
            low_price=f("LowPrice"),
            bid=f("BuyPrice"),
            ask=f("SellPrice"),
            last=f("DealPrice"),
            total_volume=i("TotalVol"),
            total_amount=i("TotalDealAmt"),
            total_out_volume=i("TotalOutVol"),
            total_in_volume=i("TotalInVol"),
            order_buy_count=_number(getattr(row, "OrderBuyCount", None), int),
            order_buy_qty=_number(getattr(row, "OrderBuyQty", None), int),
            order_sell_count=_number(getattr(row, "OrderSellCount", None), int),
            order_sell_qty=_number(getattr(row, "OrderSellQty", None), int),
        ))
    return tuple(normalized)


class YuantaReadOnlyContextAdapter:
    """Issue the two P0 read-only queries and retain normalized callbacks.

    The vendor types are injected so importing this package never starts the SDK.
    The adapter deliberately exposes no generic ``send`` method and no order API.
    """

    RESPONSE_NAMES = frozenset({"GetStockInformation", "GetWatchListAll"})

    def __init__(self, api: Any, api_types: Mapping[str, Any]):
        required = {"List", "StkInfo", "Quote", "Market", "Language"}
        missing = required.difference(api_types)
        if missing:
            raise ValueError(f"missing read-only API types: {sorted(missing)}")
        self._api = api
        self._types = api_types
        self._lock = RLock()
        self.security: dict[str, SecurityContext] = {}
        self.quotes: dict[str, QuoteContext] = {}
        self.callback_errors = 0

    def _market(self, name: str):
        key = str(name).upper()
        aliases = {"TWSE": "TWSE", "TPEX": "TWOTC", "TWOTC": "TWOTC"}
        if key not in aliases:
            raise ValueError(f"unsupported market: {name}")
        return getattr(self._types["Market"], aliases[key])

    def request(self, account: str, watch_items: Sequence[Any]) -> dict[str, bool]:
        """Request eligibility and snapshot quote data for a fixed watchlist."""
        stock_list = self._types["List"][self._types["StkInfo"]]()
        quote_list = self._types["List"][self._types["Quote"]]()
        for watch in watch_items:
            market = self._market(getattr(watch, "market"))
            symbol = str(getattr(watch, "stock_id"))
            stock = self._types["StkInfo"]()
            stock.MarketType, stock.StockCode = market, symbol
            stock_list.Add(stock)
            quote = self._types["Quote"]()
            quote.MarketType, quote.StockCode = market, symbol
            quote_list.Add(quote)
        language = self._types["Language"].UTF8
        return {
            "stock_information_requested": bool(
                self._api.GetStockInformation(account, stock_list, language)
            ),
            "watchlist_snapshot_requested": bool(
                self._api.GetWatchListAll(account, quote_list, language)
            ),
        }

    def handle_response(self, response_name: object, value: Any) -> bool:
        name = _text(response_name)
        if name not in self.RESPONSE_NAMES:
            return False
        try:
            if name == "GetStockInformation":
                rows = normalize_stock_information_result(value)
                with self._lock:
                    self.security.update({row.symbol: row for row in rows})
            else:
                rows = normalize_quote_result(value)
                with self._lock:
                    self.quotes.update({row.symbol: row for row in rows})
        except Exception:
            with self._lock:
                self.callback_errors += 1
            return False
        return True

    def snapshot(self) -> tuple[dict[str, SecurityContext], dict[str, QuoteContext], int]:
        with self._lock:
            return dict(self.security), dict(self.quotes), self.callback_errors
