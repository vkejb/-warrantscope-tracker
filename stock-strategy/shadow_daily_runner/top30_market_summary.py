from __future__ import annotations

import argparse
import csv
from datetime import datetime
import gzip
import hashlib
import json
import math
from pathlib import Path
from statistics import fmean
from zoneinfo import ZoneInfo

from prospective_shadow_v01.market_data_provider import ExistingDailyDataProvider

from .config import CFG, STOCK_STRATEGY_DIR, RunnerConfig
from .io_utils import sha256_file, write_immutable


SCHEMA_VERSION = "1"
TAIPEI = ZoneInfo("Asia/Taipei")
OUTPUT_FIELDS = (
    "rank",
    "stock_id",
    "stock_name",
    "stage_a_score",
    "open",
    "high",
    "low",
    "close",
    "total_volume",
    "average_volume_20",
    "vwap",
    "close_vs_vwap_pct",
    "intraday_range_pct",
    "high_first_time",
    "low_first_time",
    "tick_count",
    "tick_source",
    "quote_coverage_start",
    "quote_coverage_end",
    "intraday_data_status",
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _compact_date(value: str) -> str:
    compact = str(value).strip().replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError("date must be YYYYMMDD")
    datetime.strptime(compact, "%Y%m%d")
    return compact


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _load_stage_seal(path: Path, signal_date: str) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    fields = (
        "schema_version",
        "signal_date",
        "setup",
        "mode",
        "stocks",
        "model_hash",
        "model_spec_hash",
        "config_hash",
        "input_hash",
        "eligible_stock_count",
    )
    content = {key: value[key] for key in fields}
    digest = hashlib.sha256(_canonical(content)).hexdigest()
    if value.get("seal_hash") != digest:
        raise RuntimeError("Stage A seal hash mismatch")
    if value.get("signal_date") != signal_date:
        raise RuntimeError("Stage A seal date mismatch")
    if len(value.get("stocks", [])) != 30:
        raise RuntimeError("Stage A seal must contain exactly 30 stocks")
    return value


def _run_local_date(manifest: dict) -> str | None:
    raw = str(manifest.get("started_at", ""))
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(TAIPEI).strftime("%Y%m%d")


def _valid_run_manifests(runs_root: Path, signal_date: str) -> list[tuple[Path, dict]]:
    results: list[tuple[Path, dict]] = []
    if not runs_root.is_dir():
        return results
    for path in sorted(runs_root.glob("*/run_manifest.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("status") != "COMPLETE" or _run_local_date(value) != signal_date:
            continue
        without_hash = {key: item for key, item in value.items() if key != "manifest_hash"}
        if hashlib.sha256(_canonical(without_hash)).hexdigest() != value.get("manifest_hash"):
            continue
        results.append((path.parent, value))
    return results


def _read_jsonl(path: Path):
    opener = gzip.open if path.suffix == ".gz" else Path.open
    with opener(path, mode="rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"malformed quote JSONL {path}:{line_number}") from exc
            if isinstance(value, dict):
                yield value


def _stream_name(manifest: dict, stem: str) -> str | None:
    artifacts = manifest.get("artifacts", {})
    for name in (f"{stem}.jsonl.gz", f"{stem}.jsonl"):
        if name in artifacts:
            return name
    return None


def _quote_time(value: object) -> str | None:
    text = str(value or "").strip()
    if len(text) < 8:
        return None
    try:
        datetime.strptime(text[:8], "%H:%M:%S")
    except ValueError:
        return None
    return text


def _collect_ticks(
    runs: list[tuple[Path, dict]],
    symbols: set[str],
) -> tuple[dict[str, list[dict]], list[dict], list[str]]:
    by_role: dict[str, dict[str, list[dict]]] = {
        "STAGE_A": {},
        "EXPANDED": {},
    }
    sources: list[dict] = []
    warnings: list[str] = []
    for run_dir, manifest in runs:
        run_id = str(manifest.get("run_id", run_dir.name))
        source_record = {
            "run_id": run_id,
            "manifest_hash": manifest.get("manifest_hash"),
            "started_at": manifest.get("started_at"),
            "ended_at": manifest.get("ended_at"),
        }
        sources.append(source_record)
        for role, stem in (("STAGE_A", "ticks"), ("EXPANDED", "expanded_ticks")):
            name = _stream_name(manifest, stem)
            if name is None:
                continue
            path = run_dir / name
            expected = manifest["artifacts"][name]
            if not path.is_file() or sha256_file(path) != expected:
                warnings.append(f"{run_id}:{name}:HASH_OR_FILE_INVALID")
                continue
            for row in _read_jsonl(path):
                stock_id = str(row.get("stock_id", "")).strip()
                if stock_id not in symbols:
                    continue
                quote_time = _quote_time(row.get("quote_time"))
                price = _number(row.get("deal_price"))
                volume = _number(row.get("deal_volume"))
                if (
                    quote_time is None
                    or quote_time[:8] < "09:00:00"
                    or quote_time[:8] > "13:30:00"
                    or price is None
                    or price <= 0
                    or volume is None
                    or volume <= 0
                ):
                    continue
                by_role[role].setdefault(stock_id, []).append(
                    {
                        "quote_time": quote_time,
                        "received_at": str(row.get("received_at", "")),
                        "serial_no": row.get("serial_no"),
                        "price": price,
                        "volume": volume,
                        "run_id": run_id,
                    }
                )

    selected: dict[str, list[dict]] = {}
    for stock_id in symbols:
        # A symbol may be subscribed through both lists.  Prefer the dedicated
        # Top30 stream so a duplicated callback is never counted twice.
        rows = by_role["STAGE_A"].get(stock_id) or by_role["EXPANDED"].get(stock_id) or []
        unique: dict[tuple[object, ...], dict] = {}
        for row in rows:
            key = (
                row["run_id"],
                row["serial_no"],
                row["quote_time"],
                row["price"],
                row["volume"],
            )
            unique.setdefault(key, row)
        selected[stock_id] = sorted(
            unique.values(),
            key=lambda item: (item["quote_time"], item["received_at"], str(item["serial_no"])),
        )
        if selected[stock_id]:
            selected[stock_id][0]["selected_role"] = (
                "STAGE_A_TOP30" if by_role["STAGE_A"].get(stock_id) else "EXPANDED_SHADOW"
            )
    return selected, sources, warnings


def _first_time_at_price(rows: list[dict], target: float) -> str | None:
    tolerance = max(1e-9, abs(target) * 1e-9)
    return next(
        (row["quote_time"] for row in rows if abs(row["price"] - target) <= tolerance),
        None,
    )


def calculate_row(
    stage_row: dict,
    bars: list,
    signal_date: str,
    ticks: list[dict],
) -> dict:
    positions = [index for index, bar in enumerate(bars) if bar.date == signal_date]
    if len(positions) != 1:
        raise RuntimeError(f"daily OHLCV missing or duplicated for {stage_row['stock_id']}")
    index = positions[0]
    bar = bars[index]
    previous_close = bars[index - 1].close if index else None
    volumes = [item.volume for item in bars[max(0, index - 19) : index + 1]]
    average_volume_20 = fmean(volumes) if len(volumes) == 20 else None
    intraday_range_pct = (
        (bar.high - bar.low) / previous_close * 100
        if previous_close is not None and previous_close > 0
        else None
    )

    tick_volume = sum(row["volume"] for row in ticks)
    vwap = (
        sum(row["price"] * row["volume"] for row in ticks) / tick_volume
        if tick_volume > 0
        else None
    )
    close_vs_vwap_pct = (bar.close / vwap - 1) * 100 if vwap else None
    high_time = _first_time_at_price(ticks, bar.high) if ticks else None
    low_time = _first_time_at_price(ticks, bar.low) if ticks else None
    if not ticks:
        intraday_status = "MISSING_TICK_DATA"
        tick_source = None
    elif high_time is None or low_time is None:
        intraday_status = "PARTIAL_EXTREME_COVERAGE"
        tick_source = ticks[0].get("selected_role")
    else:
        intraday_status = "COMPLETE_CAPTURED_TICK_METRICS"
        tick_source = ticks[0].get("selected_role")

    def rounded(value: float | None, digits: int = 6):
        return round(value, digits) if value is not None else None

    return {
        "rank": int(stage_row["rank"]),
        "stock_id": str(stage_row["stock_id"]),
        "stock_name": str(stage_row.get("stock_name", bar.name)),
        "stage_a_score": float(stage_row["score"]),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "total_volume": int(bar.volume),
        "average_volume_20": rounded(average_volume_20, 2),
        "vwap": rounded(vwap),
        "close_vs_vwap_pct": rounded(close_vs_vwap_pct),
        "intraday_range_pct": rounded(intraday_range_pct),
        "high_first_time": high_time,
        "low_first_time": low_time,
        "tick_count": len(ticks),
        "tick_source": tick_source,
        "quote_coverage_start": ticks[0]["quote_time"] if ticks else None,
        "quote_coverage_end": ticks[-1]["quote_time"] if ticks else None,
        "intraday_data_status": intraday_status,
    }


def build_top30_market_summary(
    signal_date: str,
    *,
    stage_seal_path: Path,
    archives: list[Path],
    calendar_path: Path,
    yuanta_runs_root: Path,
    output_root: Path,
) -> dict:
    target = _compact_date(signal_date)
    stage = _load_stage_seal(stage_seal_path, target)
    provider = ExistingDailyDataProvider(
        archive_paths=[Path(path) for path in archives],
        trading_calendar_path=calendar_path,
    )
    snapshot = provider.load_through(target)
    stocks = {stock.code: stock for stock in snapshot.prepared_stocks}
    symbols = {str(row["stock_id"]) for row in stage["stocks"]}
    runs = _valid_run_manifests(yuanta_runs_root, target)
    ticks_by_symbol, source_runs, warnings = _collect_ticks(runs, symbols)

    rows = []
    for stage_row in sorted(stage["stocks"], key=lambda item: int(item["rank"])):
        stock_id = str(stage_row["stock_id"])
        stock = stocks.get(stock_id)
        if stock is None:
            raise RuntimeError(f"Stage A stock missing from official daily inputs: {stock_id}")
        rows.append(
            calculate_row(
                stage_row,
                stock.bars,
                target,
                ticks_by_symbol.get(stock_id, []),
            )
        )

    missing_intraday = [row["stock_id"] for row in rows if row["intraday_data_status"] != "COMPLETE_CAPTURED_TICK_METRICS"]
    if missing_intraday:
        warnings.append("INTRADAY_FIELDS_INCOMPLETE:" + ",".join(missing_intraday))
    content = {
        "schema_version": SCHEMA_VERSION,
        "signal_date": target,
        "status": "COMPLETE" if not warnings else "COMPLETE_WITH_WARNINGS",
        "stage_a_seal_hash": stage["seal_hash"],
        "stage_a_created_at": stage.get("created_at"),
        "daily_input_manifest_hash": snapshot.input_manifest_hash,
        "definitions": {
            "average_volume_20": "mean official total volume over the latest 20 trading observations, including signal_date",
            "close_vs_vwap_pct": "(official close / Yuanta captured-trade VWAP - 1) * 100",
            "intraday_range_pct": "(official high - official low) / previous official close * 100",
            "high_first_time": "first captured regular-session trade at the official daily high",
            "low_first_time": "first captured regular-session trade at the official daily low",
            "missing_policy": "null; never imputed",
        },
        "source_runs": source_runs,
        "rows": rows,
        "warnings": sorted(set(warnings)),
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    content["summary_hash"] = hashlib.sha256(_canonical(content)).hexdigest()

    output_root.mkdir(parents=True, exist_ok=True)
    json_path = output_root / f"{target}.json"
    csv_path = output_root / f"{target}.csv"
    json_payload = (
        json.dumps(content, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    write_immutable(json_path, json_payload)

    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows({key: row.get(key) for key in OUTPUT_FIELDS} for row in rows)
    write_immutable(csv_path, buffer.getvalue().encode("utf-8"))
    return {
        **content,
        "artifacts": {
            "json": str(json_path),
            "json_sha256": sha256_file(json_path),
            "csv": str(csv_path),
            "csv_sha256": sha256_file(csv_path),
        },
    }


def _archives_from_active_inputs(cfg: RunnerConfig) -> list[Path]:
    value = json.loads(cfg.active_inputs_path.read_text(encoding="utf-8"))
    archives = []
    for item in value.get("archive_hashes", []):
        path = Path(item["path"])
        if not path.is_file() or sha256_file(path) != item.get("sha256"):
            raise RuntimeError(f"active archive hash mismatch: {path}")
        archives.append(path)
    if not archives:
        raise RuntimeError("active inputs contain no archives")
    return archives


def build_from_runtime(signal_date: str, cfg: RunnerConfig = CFG) -> dict:
    target = _compact_date(signal_date)
    return build_top30_market_summary(
        target,
        stage_seal_path=cfg.stage_a_runtime_dir / "seals" / f"{target}.json",
        archives=_archives_from_active_inputs(cfg),
        calendar_path=cfg.calendar_path,
        yuanta_runs_root=STOCK_STRATEGY_DIR / "yuanta_intraday_shadow_v01" / "runtime" / "runs",
        output_root=cfg.runtime_dir / "top30_market_summary",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build/read the read-only daily Top30 market summary")
    subcommands = parser.add_subparsers(dest="command", required=True)
    build = subcommands.add_parser("build")
    build.add_argument("--date", required=True)
    status = subcommands.add_parser("status")
    status.add_argument("--date", required=True)
    args = parser.parse_args(argv)
    target = _compact_date(args.date)
    if args.command == "build":
        result = build_from_runtime(target)
    else:
        path = CFG.runtime_dir / "top30_market_summary" / f"{target}.json"
        if not path.is_file():
            raise FileNotFoundError(path)
        result = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
