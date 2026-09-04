from __future__ import annotations

import csv
import math
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path


ORDINARY_STOCK = re.compile(r"^[1-9][0-9]{3}$")


@dataclass(frozen=True)
class Bar:
    date: str
    stock_id: str
    name: str
    volume: int  # shares, not lots
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class TaiexBar:
    date: str
    open: float
    high: float
    low: float
    close: float


def _number(value: str) -> float:
    return float(str(value).replace(",", "").strip())


def load_ohlcv_archives(paths: list[Path]) -> tuple[dict[str, list[Bar]], dict]:
    rows: dict[tuple[str, str], Bar] = {}
    invalid_rows = 0
    archives_used = []
    supplement_dates = set()

    def ingest(reader, is_supplement: bool = False):
        nonlocal invalid_rows
        for row in reader:
            if is_supplement:
                supplement_dates.add(row["date"].strip())
            code = row["code"].strip()
            if not ORDINARY_STOCK.fullmatch(code):
                continue
            try:
                values = [_number(row[k]) for k in ("open", "high", "low", "close")]
                volume = int(_number(row["volume"]))
            except (ValueError, TypeError, KeyError):
                invalid_rows += 1
                continue
            if volume < 0 or any(not math.isfinite(v) or v <= 0 for v in values):
                invalid_rows += 1
                continue
            bar = Bar(row["date"].strip(), code, row["name"].strip(), volume, *values)
            rows[(code, bar.date)] = bar

    for path in paths:
        if path.suffix.lower() == ".csv":
            archives_used.append(path.name)
            with path.open(encoding="utf-8-sig") as handle:
                ingest(csv.DictReader(handle), is_supplement=True)
            continue
        try:
            archive = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            continue
        archives_used.append(path.name)
        with archive:
            for csv_name in (n for n in archive.namelist() if n.lower().endswith(".csv")):
                with archive.open(csv_name) as raw:
                    ingest(csv.DictReader(line.decode("utf-8-sig") for line in raw))
    stocks: dict[str, list[Bar]] = {}
    for (_, _), bar in rows.items():
        stocks.setdefault(bar.stock_id, []).append(bar)
    for bars in stocks.values():
        bars.sort(key=lambda b: b.date)
    dates = [bar.date for bars in stocks.values() for bar in bars]
    daily_counts = {}
    for date in dates:
        daily_counts[date] = daily_counts.get(date, 0) + 1
    latest=max(dates) if dates else None
    audit = {
        "archives_used": archives_used,
        "stock_count": len(stocks),
        "row_count": len(rows),
        "invalid_or_missing_ohlcv_rows": invalid_rows,
        "invalid_ohlcv_rows_skipped": invalid_rows,
        "first_date": min(dates) if dates else None,
        "last_date": latest,
        "trading_date_count": len(daily_counts),
        "minimum_daily_ordinary_stock_rows": (
            {"date": min(daily_counts, key=daily_counts.get), "rows": min(daily_counts.values())}
            if daily_counts
            else None
        ),
        "csv_supplement_dates": sorted(supplement_dates),
        "stocks_ending_before_latest_date": sum(bool(bars) and bars[-1].date < latest for bars in stocks.values()) if latest else 0,
        "ohlc_unit": "New Taiwan dollars per share; source values used without synthetic fills",
        "volume_unit": "shares; divided by 1000 only when compared with the 2000-lot threshold",
    }
    return stocks, audit


def load_taiex(path: Path) -> tuple[list[TaiexBar], dict]:
    bars = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            bars.append(TaiexBar(row["date"], *[float(row[k]) for k in ("open", "high", "low", "close")]))
    bars.sort(key=lambda b: b.date)
    return bars, {
        "row_count": len(bars),
        "first_date": bars[0].date if bars else None,
        "last_date": bars[-1].date if bars else None,
        "duplicate_dates": len(bars) - len({b.date for b in bars}),
    }


def load_institutional(path: Path) -> tuple[dict[tuple[str, str], tuple[int, int]], dict]:
    values = {}
    sources = {}
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            values[(row["stock_id"], row["date"])] = (int(row["foreign_netbuy"]), int(row["investment_trust_netbuy"]))
            source = row.get("market", "unspecified")
            sources[source] = sources.get(source, 0) + 1
    return values, {"row_count": len(values), "date_count": len({d for _, d in values}), "stock_count": len({c for c, _ in values}), "source_rows": sources}


def load_stock_info(path: Path) -> tuple[set[str], dict]:
    financial_codes = set()
    ordinary_codes = set()
    financial_categories = {}
    rows = 0
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows += 1
            code = row["stock_id"].strip()
            category = row["industry_category"].strip()
            if ORDINARY_STOCK.fullmatch(code):
                ordinary_codes.add(code)
            if ORDINARY_STOCK.fullmatch(code) and any(
                word in category for word in ("金融", "證券", "銀行", "保險", "期貨")
            ):
                financial_codes.add(code)
                financial_categories[category] = financial_categories.get(category, 0) + 1
    return financial_codes, {
        "row_count": rows,
        "ordinary_stock_codes": len(ordinary_codes),
        "financial_codes": len(financial_codes),
        "financial_category_rows": financial_categories,
        "classification_timing": "Latest/historical security-master labels, not a complete point-in-time daily industry history",
    }
