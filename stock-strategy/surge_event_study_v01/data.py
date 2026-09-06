from __future__ import annotations

from collections import defaultdict
import csv
import hashlib
import math
from pathlib import Path
import re
import zipfile

from .config import CFG, Config
from .models import Bar, PreparedBenchmark, PreparedStock


ORDINARY_STOCK = re.compile(r"^[1-9][0-9]{3}$")
REQUIRED_COLUMNS = {"date", "code", "name", "volume", "open", "high", "low", "close"}


class DataValidationError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _date(value: str) -> str:
    result = str(value).strip().replace("-", "")
    if len(result) != 8 or not result.isdigit():
        raise DataValidationError(f"invalid date: {value!r}")
    return result


def _number(value: str, field: str, *, positive: bool = False) -> float:
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise DataValidationError(f"invalid {field}: {value!r}") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise DataValidationError(f"invalid {field}: {value!r}")
    return result


def _bar(row: dict[str, str], cfg: Config) -> Bar | None:
    code = str(row.get("code", "")).strip()
    if code != cfg.benchmark_symbol and not ORDINARY_STOCK.fullmatch(code):
        return None
    day = _date(row["date"])
    if day < cfg.warmup_start or day > cfg.maximum_input_date:
        return None
    volume_float = _number(row["volume"], "volume")
    if volume_float < 0 or not volume_float.is_integer():
        raise DataValidationError(f"invalid volume: {row['volume']!r}")
    values = [_number(row[key], key, positive=True) for key in ("open", "high", "low", "close")]
    open_, high, low, close = values
    if high < max(open_, close) or low > min(open_, close) or high < low:
        raise DataValidationError(f"inconsistent OHLC for {code} on {day}")
    return Bar(day, code, str(row.get("name", "")).strip(), int(volume_float), open_, high, low, close)


def _read_csv_rows(handle, source: str, cfg: Config, on_invalid):
    reader = csv.DictReader(handle)
    missing = REQUIRED_COLUMNS - set(reader.fieldnames or ())
    if missing:
        raise DataValidationError(f"{source}: missing columns: {', '.join(sorted(missing))}")
    for line_number, row in enumerate(reader, start=2):
        try:
            yield _bar(row, cfg)
        except DataValidationError as exc:
            on_invalid(source, line_number, str(exc))


def load_ohlcv(
    archive_paths: list[Path],
    *,
    supplement_paths: list[Path] | None = None,
    cfg: Config = CFG,
) -> tuple[dict[str, list[Bar]], list[Bar], dict]:
    """Load unadjusted OHLCV. Later files override earlier duplicate code-dates."""

    by_code: dict[str, list[Bar]] = defaultdict(list)
    invalid_rows = 0
    invalid_samples: list[dict] = []
    accepted_rows = 0
    skipped_archives: list[dict] = []
    sources: list[str] = []

    def record_invalid(source: str, line_number: int, reason: str) -> None:
        nonlocal invalid_rows
        invalid_rows += 1
        if len(invalid_samples) < 20:
            invalid_samples.append(
                {"source": source, "line": line_number, "reason": reason}
            )

    def accept_rows(rows):
        nonlocal invalid_rows, accepted_rows
        for item in rows:
            if item is None:
                continue
            accepted_rows += 1
            by_code[item.code].append(item)

    for path in archive_paths:
        sources.append(str(path))
        try:
            archive = zipfile.ZipFile(path)
        except (FileNotFoundError, zipfile.BadZipFile) as exc:
            skipped_archives.append({"path": str(path), "reason": type(exc).__name__})
            continue
        with archive:
            csv_names = sorted(name for name in archive.namelist() if name.lower().endswith(".csv"))
            if not csv_names:
                skipped_archives.append({"path": str(path), "reason": "NO_CSV_MEMBER"})
                continue
            for csv_name in csv_names:
                with archive.open(csv_name) as raw:
                    decoded = (line.decode("utf-8-sig") for line in raw)
                    try:
                        accept_rows(
                            _read_csv_rows(
                                decoded, f"{path}:{csv_name}", cfg, record_invalid
                            )
                        )
                    except DataValidationError:
                        raise

    for path in supplement_paths or []:
        sources.append(str(path))
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                accept_rows(_read_csv_rows(handle, str(path), cfg, record_invalid))
        except FileNotFoundError as exc:
            raise DataValidationError(f"missing supplement: {path}") from exc

    duplicate_rows_overridden = 0
    unique: dict[str, list[Bar]] = {}
    for code, bars in by_code.items():
        bars.sort(key=lambda item: item.date)
        ordered: list[Bar] = []
        for item in bars:
            if ordered and ordered[-1].date == item.date:
                ordered[-1] = item
                duplicate_rows_overridden += 1
            else:
                ordered.append(item)
        unique[code] = ordered

    benchmark = unique.pop(cfg.benchmark_symbol, [])
    if not benchmark:
        raise DataValidationError(f"benchmark {cfg.benchmark_symbol} is absent")
    if not unique:
        raise DataValidationError("no four-digit stock rows were loaded")

    daily_counts: dict[str, int] = defaultdict(int)
    for bars in unique.values():
        for item in bars:
            daily_counts[item.date] += 1
    broad_gap_dates: list[dict] = []
    ordered_dates = sorted(day for day in daily_counts if cfg.discovery_start <= day <= cfg.feature_oos_end)
    prior_counts: list[int] = []
    for day in ordered_dates:
        count = daily_counts[day]
        if len(prior_counts) >= 20:
            window = sorted(prior_counts[-20:])
            median = (window[9] + window[10]) / 2
            if median and count < median * 0.70:
                broad_gap_dates.append({"date": day, "rows": count, "prior20_median": median})
        prior_counts.append(count)

    all_stock_dates = [item.date for bars in unique.values() for item in bars]
    audit = {
        "sources": sources,
        "skipped_archives": skipped_archives,
        "accepted_rows_before_deduplication": accepted_rows,
        "duplicate_rows_overridden": duplicate_rows_overridden,
        "ordinary_code_count": len(unique),
        "ordinary_row_count": sum(len(bars) for bars in unique.values()),
        "benchmark_rows": len(benchmark),
        "first_stock_date": min(all_stock_dates) if all_stock_dates else None,
        "last_stock_date": max(all_stock_dates) if all_stock_dates else None,
        "broad_source_gap_dates": broad_gap_dates,
        "invalid_rows": invalid_rows,
        "invalid_row_samples": invalid_samples,
        "security_master_limitation": (
            "Four-digit code syntax is only a proxy for historical common-stock membership; "
            "no complete point-in-time security master is available."
        ),
        "corporate_action_limitation": (
            "Unadjusted OHLCV and the 0.89-1.11 overnight discontinuity rule cannot identify "
            "all ex-rights/ex-dividend events."
        ),
        "turnover_limitation": "close multiplied by volume is a proxy, not reported turnover.",
    }
    return unique, benchmark, audit


def _prefix(values: list[float]) -> list[float]:
    result = [0.0]
    for value in values:
        result.append(result[-1] + value)
    return result


def prepare_benchmark(
    calendar: list[str], bars: list[Bar], cfg: Config = CFG
) -> PreparedBenchmark:
    """Align 0050 without treating its trading suspension as a missing market day."""

    by_date = {bar.date: bar for bar in bars}
    normalized: list[float | None] = []
    segments: list[int | None] = []
    segment = -1
    previous_bar: Bar | None = None
    previous_index: int | None = None
    previous_normalized: float | None = None
    for index, day in enumerate(calendar):
        current = by_date.get(day)
        if current is None:
            normalized.append(None)
            segments.append(None)
            previous_bar = None
            previous_index = None
            previous_normalized = None
            continue
        contiguous = previous_index is not None and previous_index + 1 == index
        continuous_price = bool(
            previous_bar
            and cfg.discontinuity_lower_ratio
            <= current.open / previous_bar.close
            <= cfg.discontinuity_upper_ratio
        )
        if not contiguous or not continuous_price or previous_normalized is None:
            segment += 1
            value = current.close
        else:
            value = previous_normalized * current.close / previous_bar.close
        normalized.append(value)
        segments.append(segment)
        previous_bar = current
        previous_index = index
        previous_normalized = value
    return PreparedBenchmark(calendar, normalized, segments)


def prepare_stocks(
    stocks: dict[str, list[Bar]], benchmark: list[Bar], cfg: Config = CFG
) -> tuple[list[PreparedStock], PreparedBenchmark, dict]:
    # The session calendar is the union of healthy market dates. 0050 was
    # suspended for its 2025 split, so using only 0050 would compress horizons.
    calendar = sorted(
        {
            bar.date
            for bars in stocks.values()
            for bar in bars
        }
        | {bar.date for bar in benchmark}
    )
    calendar_index = {day: index for index, day in enumerate(calendar)}
    prepared_benchmark = prepare_benchmark(calendar, benchmark, cfg)
    prepared: list[PreparedStock] = []
    discontinuities = 0
    missing_calendar_rows = 0

    for code in sorted(stocks):
        bars = [bar for bar in stocks[code] if bar.date in calendar_index]
        missing_calendar_rows += len(stocks[code]) - len(bars)
        if len(bars) <= cfg.feature_lookback_sessions:
            continue
        indices = [calendar_index[bar.date] for bar in bars]
        segments = [0]
        segment = 0
        returns = [0.0]
        true_ranges = [0.0]
        for position in range(1, len(bars)):
            previous, current = bars[position - 1], bars[position]
            contiguous = indices[position] == indices[position - 1] + 1
            overnight_ratio = current.open / previous.close
            discontinuity = not (
                cfg.discontinuity_lower_ratio <= overnight_ratio <= cfg.discontinuity_upper_ratio
            )
            if not contiguous or discontinuity:
                segment += 1
            if discontinuity:
                discontinuities += 1
            segments.append(segment)
            if contiguous and not discontinuity:
                returns.append(current.close / previous.close - 1.0)
                true_ranges.append(
                    max(
                        current.high - current.low,
                        abs(current.high - previous.close),
                        abs(current.low - previous.close),
                    )
                    / previous.close
                )
            else:
                returns.append(0.0)
                true_ranges.append(0.0)
        closes = [bar.close for bar in bars]
        volumes = [float(bar.volume) for bar in bars]
        turnover = [bar.close * bar.volume for bar in bars]
        prepared.append(
            PreparedStock(
                code=code,
                name=bars[-1].name,
                bars=bars,
                calendar_indices=indices,
                segment_ids=segments,
                prefix_close=_prefix(closes),
                prefix_volume=_prefix(volumes),
                prefix_turnover_proxy=_prefix(turnover),
                daily_returns=returns,
                prefix_return=_prefix(returns),
                prefix_return_square=_prefix([value * value for value in returns]),
                true_range_ratios=true_ranges,
                prefix_true_range_ratio=_prefix(true_ranges),
            )
        )

    audit = {
        "prepared_stock_count": len(prepared),
        "calendar_sessions": len(calendar),
        "calendar_first_date": calendar[0] if calendar else None,
        "calendar_last_date": calendar[-1] if calendar else None,
        "stock_rows_not_on_0050_calendar": missing_calendar_rows,
        "detected_overnight_discontinuities": discontinuities,
        "discontinuity_rule": [cfg.discontinuity_lower_ratio, cfg.discontinuity_upper_ratio],
    }
    return prepared, prepared_benchmark, audit
