from __future__ import annotations

import csv
from datetime import date, datetime
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .config import CFG, Config
from .models import (
    DailyBar,
    IndexDailyBar,
    IndexMinuteBar,
    MinuteBar,
    OddLotQuote,
    TradingSession,
)


SYMBOL = re.compile(r"^[0-9]{4}$")
MARKETS = {"TWSE", "TPEX"}

DAILY_COLUMNS = {
    "date",
    "symbol",
    "name",
    "market",
    "security_type",
    "trading_status",
    "close",
    "volume",
    "turnover",
}
TRADING_CALENDAR_COLUMNS = {"date", "market"}
INDEX_DAILY_COLUMNS = {"date", "market", "index_id", "close"}
MINUTE_COLUMNS = {
    "bar_end",
    "symbol",
    "market",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "limit_up",
}
INDEX_MINUTE_COLUMNS = {"bar_end", "market", "index_id", "close"}
ODD_QUOTE_COLUMNS = {
    "exchange_time",
    "received_time",
    "symbol",
    "market",
    "bid1",
    "ask1",
    "ask1_quantity",
    "ask2_quantity",
    "regular_last",
    "limit_up",
    "feed_state",
    "market_status",
}


class DataValidationError(ValueError):
    pass


def _date(value: str) -> date:
    text = value.strip()
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        return date.fromisoformat(text)
    except ValueError as exc:
        raise DataValidationError(f"invalid date: {value!r}") from exc


def _datetime(value: str, cfg: Config = CFG) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DataValidationError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo(cfg.timezone)).replace(tzinfo=None)
    return parsed


def _market(value: str) -> str:
    market = value.strip().upper()
    if market not in MARKETS:
        raise DataValidationError(f"unsupported market: {value!r}")
    return market


def _index_id(value: str, market: str, cfg: Config = CFG) -> str:
    index_id = value.strip().upper()
    expected = dict(cfg.benchmark_index_ids).get(market)
    if not index_id or index_id != expected:
        raise DataValidationError(
            f"index_id for {market} must be canonical {expected!r}, got {value!r}"
        )
    return index_id


def _float(value: str, field: str, *, positive: bool = False) -> float:
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise DataValidationError(f"invalid {field}: {value!r}") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise DataValidationError(f"invalid {field}: {value!r}")
    return result


def _int(value: str, field: str, *, nonnegative: bool = True) -> int:
    number = _float(value, field)
    if not number.is_integer() or (nonnegative and number < 0):
        raise DataValidationError(f"invalid {field}: {value!r}")
    return int(number)


def _rows(paths: list[Path], required: set[str]):
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = required - set(reader.fieldnames or ())
            if missing:
                raise DataValidationError(
                    f"{path}: missing columns: {', '.join(sorted(missing))}"
                )
            for line_number, row in enumerate(reader, start=2):
                try:
                    yield path, line_number, row
                except DataValidationError as exc:
                    raise DataValidationError(f"{path}:{line_number}: {exc}") from exc


def _symbol(value: str) -> str:
    symbol = value.strip()
    if not SYMBOL.fullmatch(symbol):
        raise DataValidationError(f"symbol must be four digits: {value!r}")
    return symbol


def _require_rows(items: list, paths: list[Path], label: str) -> None:
    if not items:
        locations = ", ".join(str(path) for path in paths) or "<no files>"
        raise DataValidationError(f"{label}: no data rows in {locations}")


def load_daily(paths: list[Path]) -> tuple[list[DailyBar], dict]:
    result: list[DailyBar] = []
    seen: set[tuple[str, str, date]] = set()
    for path, line, row in _rows(paths, DAILY_COLUMNS):
        try:
            item = DailyBar(
                date=_date(row["date"]),
                symbol=_symbol(row["symbol"]),
                name=row["name"].strip(),
                market=_market(row["market"]),
                security_type=row["security_type"].strip().upper(),
                trading_status=row["trading_status"].strip().upper(),
                close=_float(row["close"], "close", positive=True),
                volume=_int(row["volume"], "volume"),
                turnover=_float(row["turnover"], "turnover"),
            )
        except DataValidationError as exc:
            raise DataValidationError(f"{path}:{line}: {exc}") from exc
        if item.turnover < 0:
            raise DataValidationError(f"{path}:{line}: turnover must be nonnegative")
        key = (item.market, item.symbol, item.date)
        if key in seen:
            raise DataValidationError(f"{path}:{line}: duplicate daily row {key}")
        seen.add(key)
        result.append(item)
    _require_rows(result, paths, "daily")
    result.sort(key=lambda x: (x.date, x.market, x.symbol))
    return result, _audit(result, paths, lambda x: x.date)


def load_trading_calendar(paths: list[Path]) -> tuple[list[TradingSession], dict]:
    result: list[TradingSession] = []
    seen: set[tuple[str, date]] = set()
    for path, line, row in _rows(paths, TRADING_CALENDAR_COLUMNS):
        try:
            item = TradingSession(
                date=_date(row["date"]),
                market=_market(row["market"]),
            )
        except DataValidationError as exc:
            raise DataValidationError(f"{path}:{line}: {exc}") from exc
        key = (item.market, item.date)
        if key in seen:
            raise DataValidationError(
                f"{path}:{line}: duplicate trading-calendar row {key}"
            )
        seen.add(key)
        result.append(item)
    _require_rows(result, paths, "trading_calendar")
    result.sort(key=lambda x: (x.date, x.market))
    return result, _audit(result, paths, lambda x: x.date)


def load_index_daily(paths: list[Path]) -> tuple[list[IndexDailyBar], dict]:
    result: list[IndexDailyBar] = []
    seen: set[tuple[str, date]] = set()
    for path, line, row in _rows(paths, INDEX_DAILY_COLUMNS):
        try:
            market = _market(row["market"])
            item = IndexDailyBar(
                date=_date(row["date"]),
                market=market,
                index_id=_index_id(row["index_id"], market),
                close=_float(row["close"], "close", positive=True),
            )
        except DataValidationError as exc:
            raise DataValidationError(f"{path}:{line}: {exc}") from exc
        key = (item.market, item.date)
        if key in seen:
            raise DataValidationError(f"{path}:{line}: duplicate index daily row {key}")
        seen.add(key)
        result.append(item)
    _require_rows(result, paths, "index_daily")
    result.sort(key=lambda x: (x.date, x.market))
    return result, _audit(result, paths, lambda x: x.date)


def load_minutes(paths: list[Path]) -> tuple[list[MinuteBar], dict]:
    result: list[MinuteBar] = []
    seen: set[tuple[str, str, datetime]] = set()
    for path, line, row in _rows(paths, MINUTE_COLUMNS):
        try:
            item = MinuteBar(
                bar_end=_datetime(row["bar_end"]),
                symbol=_symbol(row["symbol"]),
                market=_market(row["market"]),
                open=_float(row["open"], "open", positive=True),
                high=_float(row["high"], "high", positive=True),
                low=_float(row["low"], "low", positive=True),
                close=_float(row["close"], "close", positive=True),
                volume=_int(row["volume"], "volume"),
                turnover=_float(row["turnover"], "turnover"),
                limit_up=_float(row["limit_up"], "limit_up", positive=True),
            )
        except DataValidationError as exc:
            raise DataValidationError(f"{path}:{line}: {exc}") from exc
        if item.bar_end.second or item.bar_end.microsecond:
            raise DataValidationError(f"{path}:{line}: bar_end must be a full minute")
        if item.low > min(item.open, item.close) or item.high < max(item.open, item.close):
            raise DataValidationError(f"{path}:{line}: OHLC is inconsistent")
        if item.high < item.low or item.turnover < 0:
            raise DataValidationError(f"{path}:{line}: invalid high/low or turnover")
        if (item.volume == 0) != (item.turnover == 0):
            raise DataValidationError(
                f"{path}:{line}: volume and turnover must both be zero or both be positive"
            )
        if item.volume > 0:
            average_price = item.turnover / item.volume
            if average_price < item.low - 1e-8 or average_price > item.high + 1e-8:
                raise DataValidationError(
                    f"{path}:{line}: turnover/volume lies outside minute low/high; check units"
                )
        if item.limit_up + 1e-8 < item.high:
            raise DataValidationError(f"{path}:{line}: high exceeds limit_up")
        key = (item.market, item.symbol, item.bar_end)
        if key in seen:
            raise DataValidationError(f"{path}:{line}: duplicate minute row {key}")
        seen.add(key)
        result.append(item)
    _require_rows(result, paths, "minutes")
    result.sort(key=lambda x: (x.bar_end, x.market, x.symbol))
    previous_by_stock_day: dict[tuple[str, str, date], MinuteBar] = {}
    for item in result:
        stock_day = (item.market, item.symbol, item.date)
        previous = previous_by_stock_day.get(stock_day)
        if item.volume == 0:
            prices = (item.open, item.high, item.low, item.close)
            if max(prices) - min(prices) > 1e-8:
                raise DataValidationError(
                    f"zero-volume minute must have identical OHLC: {stock_day} {item.bar_end}"
                )
            if previous is not None and abs(item.close - previous.close) > 1e-8:
                raise DataValidationError(
                    f"zero-volume minute must carry previous close: {stock_day} {item.bar_end}"
                )
        previous_by_stock_day[stock_day] = item
    return result, _audit(result, paths, lambda x: x.date)


def load_index_minutes(paths: list[Path]) -> tuple[list[IndexMinuteBar], dict]:
    result: list[IndexMinuteBar] = []
    seen: set[tuple[str, datetime]] = set()
    for path, line, row in _rows(paths, INDEX_MINUTE_COLUMNS):
        try:
            market = _market(row["market"])
            item = IndexMinuteBar(
                bar_end=_datetime(row["bar_end"]),
                market=market,
                index_id=_index_id(row["index_id"], market),
                close=_float(row["close"], "close", positive=True),
            )
        except DataValidationError as exc:
            raise DataValidationError(f"{path}:{line}: {exc}") from exc
        if item.bar_end.second or item.bar_end.microsecond:
            raise DataValidationError(f"{path}:{line}: bar_end must be a full minute")
        key = (item.market, item.bar_end)
        if key in seen:
            raise DataValidationError(f"{path}:{line}: duplicate index minute row {key}")
        seen.add(key)
        result.append(item)
    _require_rows(result, paths, "index_minutes")
    result.sort(key=lambda x: (x.bar_end, x.market))
    return result, _audit(result, paths, lambda x: x.date)


def load_odd_quotes(paths: list[Path]) -> tuple[list[OddLotQuote], dict]:
    result: list[OddLotQuote] = []
    seen: set[tuple[str, str, datetime, datetime]] = set()
    for path, line, row in _rows(paths, ODD_QUOTE_COLUMNS):
        try:
            item = OddLotQuote(
                exchange_time=_datetime(row["exchange_time"]),
                received_time=_datetime(row["received_time"]),
                symbol=_symbol(row["symbol"]),
                market=_market(row["market"]),
                bid1=_float(row["bid1"], "bid1"),
                ask1=_float(row["ask1"], "ask1"),
                ask1_quantity=_int(row["ask1_quantity"], "ask1_quantity"),
                ask2_quantity=_int(row["ask2_quantity"], "ask2_quantity"),
                regular_last=_float(row["regular_last"], "regular_last"),
                limit_up=_float(row["limit_up"], "limit_up"),
                feed_state=row["feed_state"].strip().upper(),
                market_status=row["market_status"].strip().upper(),
            )
        except DataValidationError as exc:
            raise DataValidationError(f"{path}:{line}: {exc}") from exc
        key = (item.market, item.symbol, item.exchange_time, item.received_time)
        if key in seen:
            raise DataValidationError(f"{path}:{line}: duplicate quote row {key}")
        seen.add(key)
        result.append(item)
    _require_rows(result, paths, "odd_quotes")
    result.sort(key=lambda x: (x.received_time, x.market, x.symbol))
    return result, _audit(result, paths, lambda x: x.exchange_time.date())


def _audit(items: list, paths: list[Path], get_date) -> dict:
    dates = [get_date(item) for item in items]
    return {
        "files": [str(path) for path in paths],
        "rows": len(items),
        "first_date": min(dates).isoformat() if dates else None,
        "last_date": max(dates).isoformat() if dates else None,
        "date_count": len(set(dates)),
    }
