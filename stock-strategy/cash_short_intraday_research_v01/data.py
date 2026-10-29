"""Read-only, stdlib-only inventory and quote reader for isolated SHORT research.

No strategy, SDK, broker, production loader or writer is imported. Only the
collector's Top30 and archived 0050/2330 quote streams are read. Native vendor
volumes are never converted to shares or assumed to be certified board lots.
``discover`` is cheap; ``audit_all`` hashes and streams canonical sources, so
call discover first and inspect its size/scan estimate before a full audit.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
CONTEXT = {"0050", "2330"}
UNITS = "UNKNOWN_SDK_NATIVE_NO_SHARE_LOT_CERTIFICATION"
NORMALIZATION_VERSION = "SDK_IN_OUT_FLAG_STATUS_AND_SERIAL_MINUS_ONE_HEADER_V2"
# Source: SDK IO_Doc/SubscribeStocktick_IO_Spec.doc, StockTickResult table.
# Type only defines 0=Normal. These status codes belong to InOutFlag, NOT Type.
_STATUS_FLAGS = {"10": "INDICATIVE", "11": "MATCH_DELAY_DOWN", "12": "MATCH_DELAY_UP",
                 "13": "CLOSE_DELAY", "14": "HALT", "15": "RESUMED"}
_STREAMS = {
    "ticks": ("ticks.jsonl.gz", "ticks.jsonl"),
    "books": ("books.jsonl.gz", "books.jsonl"),
    "context_ticks": ("market_context_ticks.jsonl.gz", "market_context_ticks.jsonl"),
    "context_books": ("market_context_books.jsonl.gz", "market_context_books.jsonl"),
}
_SEAL_FIELDS = (
    "schema_version", "signal_date", "setup", "mode", "stocks", "model_hash",
    "model_spec_hash", "config_hash", "input_hash", "eligible_stock_count",
)
_QUOTE_FIELDS = {
    "received_at", "callback_received_at", "exchange_time", "quote_time",
    "stock_id", "stock_name", "market", "role", "event_type", "event_kind",
    "signal_date", "stage_a_rank", "stage_a_score", "serial_no", "raw_serial_no",
    "deal_price", "deal_volume", "buy_price", "sell_price", "in_out_flag",
    "tick_type", "index_flag", "buy_prices", "buy_volumes", "sell_prices",
    "sell_volumes", "ingest_sequence", "ingest_accepted", "ingest_reason",
    "subscription_generation", "run_id",
}


class DataError(ValueError):
    """A source cannot be safely or unambiguously used for offline research."""

    def __init__(self, message, *, sources=None):
        super().__init__(message)
        self.sources = sources or []


def _root(root) -> Path:
    p = Path(root).resolve()
    if (p / "stock-strategy").is_dir():
        p = p / "stock-strategy"
    return p


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _json(path: Path, *, sources=None) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("not object")
        return value
    except (OSError, ValueError, TypeError) as exc:
        # Do not echo arbitrary payloads or exception messages into reports.
        raise DataError(f"UNREADABLE_JSON:{path.name}:{type(exc).__name__}", sources=sources) from exc


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _stamp(value) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None or d.utcoffset() is None:
        raise DataError("NAIVE_OR_MISSING_RECEIPT_TIME")
    return d.astimezone(TAIPEI)


def _maybe_stamp(value):
    try:
        return _stamp(value)
    except (ValueError, TypeError):
        return None


def _exchange_time(row: dict, received: datetime):
    explicit = row.get("exchange_time")
    if explicit:
        parsed = _maybe_stamp(explicit)
        if parsed is not None:
            return parsed
    text = str(row.get("quote_time") or "").strip()
    if text:
        for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
            try:
                clock = datetime.strptime(text, fmt).time()
                return datetime.combine(received.date(), clock, TAIPEI)
            except ValueError:
                pass
    # Especially for FIVE_LEVEL: unknown exchange time must remain unknown.
    return None


def _number(value) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite")
    return number


def _safe_payload(row: dict) -> dict:
    def safe(v):
        return v is None or isinstance(v, (str, int, float, bool))
    out = {}
    for key in _QUOTE_FIELDS:
        if key not in row:
            continue
        value = row[key]
        if safe(value) or (isinstance(value, list) and all(safe(x) for x in value)):
            out[key] = value
    return out


def _iter_jsonl(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("not object")
                except (ValueError, TypeError) as exc:
                    raise DataError(f"INVALID_JSONL:{path.name}:{line_no}") from exc
                yield line_no, row
    except (OSError, EOFError) as exc:
        raise DataError(f"UNREADABLE_EVENT_STREAM:{path.name}") from exc


def _day(manifest: dict) -> str:
    start = _stamp(manifest["started_at"]).strftime("%Y%m%d")
    end = _stamp(manifest["ended_at"]).strftime("%Y%m%d")
    if start != end:
        raise DataError("CROSS_DAY_SOURCE_RUN")
    return start


def _stream_paths(run: Path, manifest: dict) -> dict:
    artifacts = manifest.get("artifacts", {})
    return {kind: run / name for kind, names in _STREAMS.items()
            for name in names if name in artifacts and not any(
                other in artifacts for other in names[:names.index(name)])}


def discover(root) -> list[dict]:
    """Inventory only run manifests and file stats; never decompress quote data.

    One row per run (including failed/stopped sources). Paths are strings for
    JSON compatibility. Estimates are deliberately broad, not an ETA promise.
    Expanded streams, decision evidence and broker-private directories are not
    traversed or opened. Canonical selection is performed later per day.
    """
    base = _root(root)
    runtime = base / "yuanta_intraday_shadow_v01" / "runtime"
    rows = []
    for path in sorted((runtime / "runs").glob("*/run_manifest.json")):
        row = {"run_id": path.parent.name, "run_dir": str(path.parent),
               "manifest_path": str(path), "strategy_root": str(base)}
        try:
            m = _json(path)
            paths = _stream_paths(path.parent, m)
            sizes = {k: p.stat().st_size if p.is_file() else None for k, p in paths.items()}
            nbytes = sum(v for v in sizes.values() if v is not None)
            session_path = runtime / "session_analyses" / path.parent.name / "session_manifest.json"
            s = _json(session_path) if session_path.is_file() else {}
            row.update(day=_day(m), signal_date=str(m.get("signal_date", "")),
                       status=m.get("status"), mode=m.get("mode", "UNKNOWN"),
                       started_at=m.get("started_at"), ended_at=m.get("ended_at"),
                       stage_a_seal_hash=m.get("stage_a_seal_hash"),
                       paths={k: str(p) for k, p in paths.items()}, sizes=sizes,
                       source_bytes=nbytes, estimated_scan_minutes_low=nbytes/(8*1024**2*60),
                       estimated_scan_minutes_high=nbytes/(0.5*1024**2*60),
                       estimate_basis="0.5_TO_8_COMPRESSED_MB_PER_SECOND_UNBENCHMARKED",
                       event_counts=m.get("event_counts", {}),
                       callback_errors=int(m.get("event_counts", {}).get("callback_errors", 0)),
                       market_context_event_counts=m.get("market_context_event_counts", {}),
                       coverage_status=s.get("coverage_status", "UNRECORDED"),
                       stream_coverage_pass=s.get("stream_coverage_pass"),
                       session_manifest_path=str(session_path) if s else None,
                       recorded_max_market_gap_seconds=s.get("max_market_event_gap_seconds"),
                       actual_orders=int(m.get("actual_orders", 0)),
                       actual_fills=int(m.get("actual_fills", 0)),
                       broker_order_calls=int(m.get("broker_order_calls", 0)))
        except (DataError, KeyError, TypeError, ValueError) as exc:
            row.update(day="UNKNOWN", status="BLOCKED", error=str(exc))
        rows.append(row)
    return rows


def _selection_rank(row: dict):
    counts = row.get("event_counts", {})
    complete = row.get("status") == "COMPLETE"
    clean = row.get("callback_errors") == 0
    full = row.get("coverage_status") == "FULL_SESSION" and row.get("stream_coverage_pass") is True
    active = counts.get("ticks", 0) > 0 and counts.get("books", 0) > 0
    try:
        start, end = _stamp(row["started_at"]), _stamp(row["ended_at"])
        duration = (end-start).total_seconds()
        clock_coverage = start.time() <= time(9) and end.time() >= time(13, 30)
    except (KeyError, ValueError, TypeError):
        duration = 0
        clock_coverage = False
    return (complete and full and clean and active and clock_coverage,
            complete and clean and active and clock_coverage,
            complete, full, clean, clock_coverage,
            row.get("mode") == "SHADOW_ONLY_READ_ONLY_QUOTES", duration, row["run_id"])


def _same_identity(rows):
    if len({r.get("day") for r in rows}) != 1:
        raise DataError("DIFFERENT_SESSION_DATES_CANNOT_BE_COMBINED")
    if len({r.get("stage_a_seal_hash") for r in rows}) != 1:
        raise DataError("DIFFERENT_CANDIDATE_SEALS_CANNOT_BE_COMBINED")


def _inspect(run_dir: Path, base: Path | None = None) -> dict:
    base = base or run_dir.parents[3]
    m = _json(run_dir / "run_manifest.json")
    d = _day(m)
    source_rows = []
    unsigned = {k: v for k, v in m.items() if k != "manifest_hash"}
    calculated = hashlib.sha256(_canonical(unsigned)).hexdigest()
    source_rows.append({"day": d, "run_id": run_dir.name, "role": "run_manifest",
                        "path": str(run_dir / "run_manifest.json"),
                        "sha256": calculated, "expected_sha256": m.get("manifest_hash"),
                        "hash_method": "CANONICAL_JSON_EXCLUDING_MANIFEST_HASH",
                        "hash_ok": calculated == m.get("manifest_hash")})
    paths = _stream_paths(run_dir, m)
    for kind, p in {"watchlist": run_dir / "watchlist.json", **paths}.items():
        expected = m.get("artifacts", {}).get(p.name)
        digest = _hash(p) if p.is_file() else None
        source_rows.append({"day": d, "run_id": run_dir.name, "role": kind,
                            "path": str(p), "bytes": p.stat().st_size if p.is_file() else None,
                            "sha256": digest, "expected_sha256": expected,
                            "hash_method": "FILE_BYTES", "hash_ok": bool(expected and digest == expected)})
    if not all(x["hash_ok"] for x in source_rows):
        raise DataError("SOURCE_HASH_MISMATCH", sources=source_rows)
    session_path = run_dir.parents[1] / "session_analyses" / run_dir.name / "session_manifest.json"
    session = _json(session_path, sources=source_rows) if session_path.is_file() else {}
    if session:
        session_digest = hashlib.sha256(_canonical({k: v for k, v in session.items() if k != "analysis_hash"})).hexdigest()
        session_ok = (session_digest == session.get("analysis_hash") and
                      session.get("source_manifest_hash") == m.get("manifest_hash") and
                      session.get("source_run_id") == run_dir.name and str(session.get("session_date")) == d)
        source_rows.append({"day": d, "run_id": run_dir.name, "role": "session_manifest",
                            "path": str(session_path), "bytes": session_path.stat().st_size,
                            "sha256": session_digest, "expected_sha256": session.get("analysis_hash"),
                            "hash_method": "CANONICAL_JSON_EXCLUDING_ANALYSIS_HASH",
                            "hash_ok": session_ok})
        if not session_ok:
            raise DataError("SESSION_MANIFEST_HASH_OR_SOURCE_IDENTITY_MISMATCH", sources=source_rows)
    w = _json(run_dir / "watchlist.json", sources=source_rows)
    if (w.get("stage_a_seal_hash") != m.get("stage_a_seal_hash") or
            str(w.get("signal_date")) != str(m.get("signal_date"))):
        raise DataError("WATCHLIST_MANIFEST_IDENTITY_MISMATCH", sources=source_rows)
    stocks = w.get("stocks", [])
    ids = [str(x["stock_id"]) for x in stocks]
    if len(ids) != 30 or len(set(ids)) != 30 or sorted(int(x["rank"]) for x in stocks) != list(range(1, 31)):
        raise DataError("SEALED_TOP30_RANK_OR_MEMBERSHIP_INVALID", sources=source_rows)
    seal_path = base / "stage_a_prospective_watchlist_v01" / "runtime" / "seals" / f"{m['signal_date']}.json"
    seal = _json(seal_path, sources=source_rows)
    try:
        seal_digest = hashlib.sha256(_canonical({k: seal[k] for k in _SEAL_FIELDS})).hexdigest()
    except KeyError as exc:
        raise DataError("STAGE_A_SEAL_SCHEMA_INVALID", sources=source_rows) from exc
    seal_ok = seal_digest == seal.get("seal_hash") == m.get("stage_a_seal_hash")
    seal_ok = seal_ok and seal.get("status") == "SEALED"
    sealed = {(str(x["stock_id"]), int(x["rank"]), float(x["score"])) for x in seal.get("stocks", [])}
    archived = {(str(x["stock_id"]), int(x["rank"]), float(x["score"])) for x in stocks}
    seal_ok = seal_ok and len(sealed) == 30 and sealed == archived
    source_rows.append({"day": d, "run_id": run_dir.name, "role": "stage_a_seal",
                        "path": str(seal_path), "bytes": seal_path.stat().st_size,
                        "sha256": _hash(seal_path), "content_sha256": seal_digest,
                        "expected_content_sha256": m.get("stage_a_seal_hash"),
                        "hash_method": "STAGE_A_FROZEN_CONTENT_FIELDS", "hash_ok": seal_ok})
    if not seal_ok:
        raise DataError("STAGE_A_SEAL_HASH_OR_MEMBERSHIP_MISMATCH", sources=source_rows)
    if any(int(m.get(k, 0)) != 0 for k in ("actual_orders", "actual_fills", "broker_order_calls")):
        raise DataError("SOURCE_DECLARES_NONZERO_ACTUAL_ORDER_ACTIVITY", sources=source_rows)
    return {"day": d, "manifest": m, "watchlist": w, "seal": seal,
            "candidates": set(ids), "paths": paths, "sources": source_rows,
            "run_dir": run_dir, "strategy_root": base, "session": session}


def _pairs(prices, volumes):
    if not isinstance(prices, list) or not isinstance(volumes, list) or len(prices) != len(volumes):
        raise ValueError("invalid levels")
    pairs = [(_number(p), _number(v)) for p, v in zip(prices, volumes)]
    if any(p < 0 or v < 0 for p, v in pairs):
        raise ValueError("negative level")
    return pairs


def _events(doc):
    """Stream original file order; sequence gaps are retained, not repaired."""
    fallback_sequence = 0
    prior_receipt, prior_exchange, prior_serial, prior_sequence = {}, {}, {}, {}
    for stream, path in doc["paths"].items():
        for line_no, row in _iter_jsonl(path):
            symbol = str(row.get("stock_id", ""))
            if symbol not in doc["candidates"] | CONTEXT:
                raise DataError(f"QUOTE_OUTSIDE_SEALED_TOP30_AND_CONTEXT:{path.name}:{line_no}")
            if stream.startswith("context") and symbol not in CONTEXT:
                raise DataError("NON_CONTEXT_SYMBOL_IN_CONTEXT_STREAM")
            if not stream.startswith("context") and symbol not in doc["candidates"]:
                raise DataError("NON_CANDIDATE_IN_TOP30_STREAM")
            fallback_sequence += 1
            received = _stamp(row["received_at"])
            if received.strftime("%Y%m%d") != doc["day"]:
                raise DataError("QUOTE_RECEIPT_OUTSIDE_SESSION_DATE")
            exchange = _exchange_time(row, received)
            known_sequence = isinstance(row.get("ingest_sequence"), int) and not isinstance(row.get("ingest_sequence"), bool)
            sequence = row["ingest_sequence"] if known_sequence else fallback_sequence
            flags = []
            if stream in prior_receipt and received < prior_receipt[stream]:
                flags.append("RECEIPT_TIME_REGRESSION")
            prior_receipt[stream] = received
            key = (stream, symbol)
            if known_sequence and stream in prior_sequence and sequence < prior_sequence[stream]:
                flags.append("INGEST_SEQUENCE_REGRESSION")
            if known_sequence:
                prior_sequence[stream] = sequence
            event = {"kind": "tick" if stream.endswith("ticks") else "book", "symbol": symbol,
                     "received_at": received, "exchange_time": exchange, "sequence": sequence,
                     "sequence_known": known_sequence,
                     "sequence_source": "ARCHIVED_INGEST_SEQUENCE" if known_sequence else "SYNTHETIC_FILE_ORDER_TIEBREAK_ONLY",
                     "payload": _safe_payload(row), "source_file": str(path), "source_line": line_no,
                     "out_of_order": bool(flags), "order_flags": flags, "volume_units": UNITS}
            try:
                if event["kind"] == "tick":
                    event.update(price=_number(row["deal_price"]), volume=_number(row["deal_volume"]),
                                 bid=_number(row["buy_price"]), ask=_number(row["sell_price"]),
                                 flag=str(row.get("in_out_flag", "")), serial=int(row.get("serial_no", 0)))
                    if event["price"] <= 0 or event["volume"] < 0:
                        raise ValueError("invalid tick")
                    event["quote_valid"] = event["bid"] > 0 and event["ask"] > 0 and event["bid"] <= event["ask"]
                else:
                    event.update(bids=_pairs(row.get("buy_prices"), row.get("buy_volumes")),
                                 asks=_pairs(row.get("sell_prices"), row.get("sell_volumes")))
                    event["quote_valid"] = bool(event["bids"] and event["asks"] and
                        0 < event["bids"][0][0] <= event["asks"][0][0])
            except (ValueError, TypeError, KeyError):
                event.update(kind="status", status="INVALID_QUOTE_SCHEMA", quote_valid=False)
            event["ingest_accepted"] = row.get("ingest_accepted")
            if row.get("ingest_accepted") is False:
                event.update(kind="status", status="ARCHIVED_INGEST_REJECTED", quote_valid=False)
            raw_type = str(row.get("tick_type", "")).strip()
            raw_flag = str(row.get("in_out_flag", "")).strip()
            if stream.endswith("ticks"):
                event["flag"] = raw_flag
                try:
                    raw_serial = int(row.get("serial_no", 0))
                except (ValueError, TypeError):
                    raw_serial = 0
                event["serial"] = raw_serial
                if raw_serial == -1:
                    # SDK clearing/header record: these three fields are NOT a
                    # trade's bid/ask/last. No observed event clock is claimed.
                    event.update(kind="status", status="HEADER", status_code="SERIAL_MINUS_ONE",
                                 quote_valid=False, exchange_time=None)
                    for field, raw in (("upper_limit", "buy_price"), ("lower_limit", "sell_price"),
                                       ("reference_price", "deal_price")):
                        try:
                            value = _number(row.get(raw))
                            event[field] = value if value > 0 else None
                        except (ValueError, TypeError):
                            event[field] = None
                elif raw_flag in _STATUS_FLAGS:
                    event.update(kind="status", status=_STATUS_FLAGS[raw_flag], status_code=raw_flag,
                                 status_source="SDK_IN_OUT_FLAG", quote_valid=False)
                    if raw_flag in {"10", "11", "12", "13"}:
                        event["exchange_time"] = None  # SDK defines other fields as zero.
                elif raw_flag not in {"0", "1", "2", "3"}:
                    event.update(kind="status", status="UNKNOWN_IN_OUT_FLAG", quote_valid=False)
                elif raw_type not in {"", "0"}:
                    event.update(kind="status", status="UNKNOWN_TICK_TYPE", quote_valid=False)
            # Header/status records never reset or regress a trade's serial or
            # exchange-clock baseline. Late ordinary trades remain flagged.
            if event["kind"] == "tick":
                if key in prior_serial and event["serial"] < prior_serial[key]:
                    event["order_flags"].append("SERIAL_REGRESSION")
                prior_serial[key] = max(event["serial"], prior_serial.get(key, event["serial"]))
                if exchange is not None and key in prior_exchange and exchange < prior_exchange[key]:
                    event["order_flags"].append("EXCHANGE_TIME_REGRESSION")
                if exchange is not None:
                    prior_exchange[key] = max(exchange, prior_exchange.get(key, exchange))
            elif event["kind"] == "book" and exchange is not None:
                if key in prior_exchange and exchange < prior_exchange[key]:
                    event["order_flags"].append("EXCHANGE_TIME_REGRESSION")
                prior_exchange[key] = max(exchange, prior_exchange.get(key, exchange))
            event["out_of_order"] = bool(event["order_flags"])
            yield stream, event


def _identity(event):
    if event["kind"] != "tick" or event["exchange_time"] is None or event["serial"] <= 0:
        return None  # Unknown identity is not evidence of a duplicate trade.
    return (event["symbol"], event["serial"], event["exchange_time"], event["price"], event["volume"])


def load_day(run_dirs) -> dict:
    """Verify sources then materialize ONE canonical Top30+0050/2330 day.

    Multiple supplied runs must share day and seal. Prefer a complete canonical
    run; never merge observations from distinct candidate universes. If legacy
    streams have no global ingest order, equal-receipt cross-file order remains
    explicitly ambiguous rather than being represented as exchange order.
    """
    if isinstance(run_dirs, (str, Path)):
        run_dirs = [run_dirs]
    run_dirs = [Path(x).resolve() for x in run_dirs]
    if not run_dirs:
        raise DataError("NO_SOURCE_RUNS")
    rows = []
    for p in run_dirs:
        found = {r["run_dir"]: r for r in discover(p.parents[3])}
        if str(p) not in found:
            raise DataError("RUN_OUTSIDE_COLLECTOR_RUNS")
        rows.append(found[str(p)])
    _same_identity(rows)
    selected = max(rows, key=_selection_rank)
    doc = _inspect(Path(selected["run_dir"]), Path(selected["strategy_root"]))
    events = [e for _, e in _events(doc)]
    events.sort(key=lambda e: (e["received_at"], e["sequence"], e["source_file"], e["source_line"]))
    seen, filtered, duplicates = set(), [], 0
    receipt_groups = defaultdict(list)
    header_state = {}
    for event in events:
        identity = _identity(event)
        if identity is not None and identity in seen:
            duplicates += 1
            continue
        if identity is not None:
            seen.add(identity)
        if event.get("status") == "HEADER":
            header_state[event["symbol"]] = {
                **{k: event.get(k) for k in ("reference_price", "upper_limit", "lower_limit")},
                "header_received_at": event["received_at"], "header_sequence": event["sequence"],
                "header_sequence_known": event["sequence_known"]}
        elif event["kind"] in {"tick", "book"} and event["symbol"] in header_state:
            header = header_state[event["symbol"]]
            # Equal-clock legacy file ties have no proved causal order.
            if header["header_received_at"] < event["received_at"] or (
                header["header_received_at"] == event["received_at"] and
                header["header_sequence_known"] and event["sequence_known"] and
                header["header_sequence"] < event["sequence"]):
                event.update(header)
        filtered.append(event)
        receipt_groups[event["received_at"]].append(event)
    ambiguous = 0
    for group in receipt_groups.values():
        if len(group) > 1 and len({e["source_file"] for e in group}) > 1 and not all(e["sequence_known"] for e in group):
            ambiguous += 1
            for e in group:
                e["receipt_order_ambiguous"] = True
    metadata = {str(x["stock_id"]): {k: v for k, v in x.items() if k in {
            "stock_id", "stock_name", "rank", "score", "market", "role"}} | {"name": str(x.get("stock_name", x["stock_id"]))}
            for x in doc["watchlist"].get("stocks", []) + doc["watchlist"].get("market_context", [])
            if str(x["stock_id"]) in doc["candidates"] | CONTEXT}
    for event in filtered:
        metadata.setdefault(event["symbol"], {"name": event["symbol"]})
    opened = datetime.strptime(doc["day"], "%Y%m%d").replace(hour=9, tzinfo=TAIPEI)
    seal_at = _maybe_stamp(doc["seal"].get("created_at"))
    watch_at = _maybe_stamp(doc["watchlist"].get("created_at"))
    return {"day": doc["day"], "metadata": metadata,
            "candidates": doc["candidates"], "events": filtered,
            "selected_run_ids": [selected["run_id"]],
            "ignored_run_ids": [x["run_id"] for x in rows if x is not selected],
            "selection_policy": "ONE_CANONICAL_RUN_NEVER_MERGE",
            "stage_a_seal_hash": doc["manifest"]["stage_a_seal_hash"],
            "sources": doc["sources"], "duplicates_dropped": duplicates,
            "ambiguous_receipt_ties": ambiguous, "volume_units": UNITS,
            "source_hash_verified": True, "source_status": selected.get("status"),
            "recorded_coverage_status": selected.get("coverage_status"),
            "recorded_stream_coverage_pass": selected.get("stream_coverage_pass"),
            "callback_errors": selected.get("callback_errors"),
            "point_in_time_pass": bool(seal_at and watch_at and seal_at <= opened and watch_at <= opened and str(doc["manifest"]["signal_date"]) < doc["day"]),
            "point_in_time_evidence": "DECLARED_ARCHIVED_TIMESTAMPS_NOT_INDEPENDENT_TIMESTAMP_ATTESTATION",
            "normalization_version": NORMALIZATION_VERSION}


def _calendar(base: Path):
    path = base / "shadow_daily_runner" / "runtime" / "trading_calendar.csv"
    if not path.is_file():
        return None, None
    dates = {line.strip().replace("-", "") for line in path.read_text(encoding="utf-8-sig").splitlines()
             if len(line.strip()) == 10 and line.strip()[4] == "-"}
    return dates, {"role": "calendar", "path": str(path), "sha256": _hash(path),
                   "bytes": path.stat().st_size, "hash_method": "FILE_BYTES",
                   "hash_ok": None, "as_of_status": "CURRENT_LOCAL_CALENDAR_NOT_PROOF_OF_HISTORICAL_AVAILABILITY"}


def _new_symbol_coverage(symbol, name, candidates):
    return {"name": name, "is_candidate": symbol in candidates,
            "is_context": symbol in CONTEXT, "volume_units_status": UNITS,
            "tick_events": 0, "book_events": 0, "trade_tick_events": 0,
            "regular_tick_events": 0, "regular_book_events": 0, "regular_status_events": 0,
            "first5m_status_count": 0, "native_trade_volume_total": 0,
            "first5m_tick_count": 0, "first5m_trade_tick_count": 0,
            "first5m_book_count": 0, "last5m_tick_count": 0, "last5m_book_count": 0,
            "unknown_in_out_flag_count": 0, "in_out_flag_counts": {}, "raw_tick_type_counts": {},
            "unknown_tick_exchange_time_count": 0, "unknown_book_exchange_time_count": 0,
            "missing_tick_ingest_sequence_count": 0, "missing_book_ingest_sequence_count": 0,
            "invalid_tick_quote_count": 0, "invalid_book_quote_count": 0,
            "out_of_order_tick_count": 0, "out_of_order_book_count": 0,
            "status_14_times": [], "status_15_times": [], "header_records": [],
            "status_code_evidence": "SDK_IN_OUT_FLAG_14_HALT_15_RESUMED_NO_INFERRED_MISSING_INTERVALS",
            "first_regular_tick_received_at": None, "last_regular_tick_received_at": None,
            "first_regular_book_received_at": None, "last_regular_book_received_at": None,
            "first_regular_tick_exchange_time": None, "last_regular_tick_exchange_time": None,
            "first_regular_book_exchange_time": None, "last_regular_book_exchange_time": None,
            "max_tick_gap_seconds": None, "max_book_gap_seconds": None,
            "gap_method": "PER_SYMBOL_RECEIPT_GAPS_INCLUDING_09_00_AND_13_30_EDGES_NO_OUTAGE_INFERENCE"}


def _update_symbol_coverage(stats, event, stream, opened, closed, prior):
    kind = "tick" if stream.endswith("ticks") else "book"
    stats[kind + "_events"] += 1
    stats["trade_tick_events"] += event["kind"] == "tick"
    if event["kind"] == "tick":
        stats["native_trade_volume_total"] += event["volume"]
    stats["unknown_" + kind + "_exchange_time_count"] += event["exchange_time"] is None
    stats["missing_" + kind + "_ingest_sequence_count"] += not event["sequence_known"]
    stats["invalid_" + kind + "_quote_count"] += not event["quote_valid"]
    stats["out_of_order_" + kind + "_count"] += event["out_of_order"]
    if kind == "tick":
        raw_flag = str(event["payload"].get("in_out_flag", ""))
        raw_type = str(event["payload"].get("tick_type", ""))
        for field, code in (("in_out_flag_counts", raw_flag), ("raw_tick_type_counts", raw_type)):
            stats[field][code] = stats[field].get(code, 0) + 1
        stats["unknown_in_out_flag_count"] += raw_flag not in {"0", "1", "2", "3", *_STATUS_FLAGS}
        if raw_flag in {"14", "15"}:
            stats["status_" + raw_flag + "_times"].append({
                "received_at": event["received_at"].isoformat(),
                "exchange_time": event["exchange_time"].isoformat() if event["exchange_time"] else None,
                "sequence": event["sequence"], "sequence_known": event["sequence_known"],
                "serial": event.get("serial"), "source_line": event["source_line"]})
        if event.get("status") == "HEADER":
            stats["header_records"].append({"received_at": event["received_at"].isoformat(),
                **{k: event.get(k) for k in ("reference_price", "upper_limit", "lower_limit")},
                "sequence": event["sequence"], "sequence_known": event["sequence_known"]})
    at = event["received_at"]
    if not opened <= at <= closed:
        return
    if event["kind"] == "status":
        stats["regular_status_events"] += 1
        stats["first5m_status_count"] += (at-opened).total_seconds() < 300
        return  # SDK status/header/invalid records are not trade or book coverage.
    stats["regular_" + kind + "_events"] += 1
    first_key, last_key = "first_regular_" + kind + "_received_at", "last_regular_" + kind + "_received_at"
    stamp = at.isoformat()  # All clocks are normalized to one fixed timezone.
    if stats[first_key] is None or stamp < stats[first_key]:
        stats[first_key] = stamp
    if stats[last_key] is None or stamp > stats[last_key]:
        stats[last_key] = stamp
    if event["exchange_time"] is not None:
        et = event["exchange_time"]
        first_clock, last_clock = "first_regular_" + kind + "_exchange_time", "last_regular_" + kind + "_exchange_time"
        exchange_stamp = et.isoformat()
        if stats[first_clock] is None or exchange_stamp < stats[first_clock]:
            stats[first_clock] = exchange_stamp
        if stats[last_clock] is None or exchange_stamp > stats[last_clock]:
            stats[last_clock] = exchange_stamp
    elapsed = (at-opened).total_seconds()
    if elapsed < 300:
        stats["first5m_" + kind + "_count"] += 1
        stats["first5m_trade_tick_count"] += event["kind"] == "tick"
    if (closed-at).total_seconds() <= 300:
        stats["last5m_" + kind + "_count"] += 1
    key = (event["symbol"], kind)
    previous = prior.get(key, opened)
    if at >= previous:
        gap = (at-previous).total_seconds()
        stats["max_" + kind + "_gap_seconds"] = max(stats["max_" + kind + "_gap_seconds"] or 0, gap)
    prior[key] = max(at, previous)


def _finish_symbol_coverage(stats, opened, closed):
    for kind in ("tick", "book"):
        last = stats["last_regular_" + kind + "_received_at"]
        if last is not None:
            stats["max_" + kind + "_gap_seconds"] = max(
                stats["max_" + kind + "_gap_seconds"] or 0,
                (closed-_stamp(last)).total_seconds())
    stats["has_regular_ticks_and_books"] = bool(stats["regular_tick_events"] and stats["regular_book_events"])
    stats["boundary_receipts_present"] = {
        "tick_first5m": stats["first5m_tick_count"] > 0,
        "book_first5m": stats["first5m_book_count"] > 0,
        "tick_last5m": stats["last5m_tick_count"] > 0,
        "book_last5m": stats["last5m_book_count"] > 0}


def audit_all(root, progress=None) -> tuple[list[dict], list[dict]]:
    """Audit canonical days, streaming events without retaining raw book lists.

    Source-byte checks precede parsing. Output is Python data only: callers own
    any authorized report/export. No calendar refresh, quote login, strategy
    replay, source repair or directory creation occurs here. Optional progress
    receives DAY_STARTED, EVENT_PROGRESS (each 100,000 quote rows), and
    DAY_FINISHED dictionaries; existing callers of audit_all(root) remain
    compatible.
    """
    base = _root(root)
    inventory = discover(base)
    days = defaultdict(list)
    for x in inventory:
        days[x.get("day", "UNKNOWN")].append(x)
    calendar, calendar_source = _calendar(base)
    sources = [calendar_source] if calendar_source else []
    rows = []
    total_bytes = sum(x.get("source_bytes", 0) for x in inventory)
    for day, runs in sorted(days.items()):
        selected = max(runs, key=_selection_rank)
        if progress:
            progress({"event": "DAY_STARTED", "day": day, "run_id": selected["run_id"],
                      "source_bytes": selected.get("source_bytes", 0)})
        row = {"day": day, "canonical_run_id": selected["run_id"], "run_count": len(runs),
               "all_run_ids": [r["run_id"] for r in runs],
               "different_candidate_seals_in_day": len({r.get("stage_a_seal_hash") for r in runs}) > 1,
               "candidate_seals_merged": False, "selection_policy": "ONE_CANONICAL_RUN_NEVER_MERGE",
               "calendar_open": day in calendar if calendar is not None else None,
               "volume_units_status": UNITS, "eligibility_status": "UNKNOWN_NOT_ARCHIVED",
               "reference_price_status": "UNKNOWN_NOT_ARCHIVED", "quote_limits_status": "UNKNOWN_NOT_ARCHIVED",
               "source_bytes": selected.get("source_bytes", 0), "all_discovered_source_bytes": total_bytes,
               "estimated_scan_minutes_low": selected.get("estimated_scan_minutes_low"),
               "estimated_scan_minutes_high": selected.get("estimated_scan_minutes_high"),
               "source_status": selected.get("status"), "source_mode": selected.get("mode"),
               "recorded_coverage_status": selected.get("coverage_status"),
               "recorded_stream_coverage_pass": selected.get("stream_coverage_pass"),
               "recorded_max_market_gap_seconds": selected.get("recorded_max_market_gap_seconds"),
               "callback_errors": selected.get("callback_errors"), "audit_status": "BLOCKED"}
        row["normalization_version"] = NORMALIZATION_VERSION
        try:
            doc = _inspect(Path(selected["run_dir"]), base)
            sources.extend(doc["sources"])
            opened = datetime.strptime(day, "%Y%m%d").replace(hour=9, tzinfo=TAIPEI)
            closed = opened.replace(hour=13, minute=30)
            seal_at = _maybe_stamp(doc["seal"].get("created_at"))
            watch_at = _maybe_stamp(doc["watchlist"].get("created_at"))
            start = _stamp(doc["manifest"]["started_at"])
            end = _stamp(doc["manifest"]["ended_at"])
            row.update(source_hash_verified=True, signal_date=str(doc["manifest"]["signal_date"]),
                       seal_created_at=seal_at.isoformat() if seal_at else None,
                       watchlist_created_at=watch_at.isoformat() if watch_at else None,
                       seal_available_before_open=seal_at <= opened if seal_at else None,
                       watchlist_available_before_open=watch_at <= opened if watch_at else None,
                       watchlist_created_before_run_start=watch_at <= start if watch_at else None,
                       signal_date_before_session=str(doc["manifest"]["signal_date"]) < day,
                       run_start_before_open=start <= opened, run_end_after_close=end >= closed,
                       candidate_count=len(doc["candidates"]),
                       point_in_time_evidence="DECLARED_ARCHIVED_TIMESTAMPS_NOT_INDEPENDENT_TIMESTAMP_ATTESTATION")
            counts = defaultdict(int)
            symbols = defaultdict(set)
            metadata = {str(x["stock_id"]): str(x.get("stock_name", x["stock_id"]))
                        for x in doc["watchlist"].get("stocks", []) + doc["watchlist"].get("market_context", [])}
            per_symbol = {symbol: _new_symbol_coverage(symbol, metadata.get(symbol, symbol), doc["candidates"])
                          for symbol in sorted(doc["candidates"] | CONTEXT)}
            symbol_previous = {}
            previous, first, last, gaps = {}, {}, {}, defaultdict(float)
            unknown_exchange = invalid = order_errors = no_sequence = duplicate_count = 0
            processed = 0
            seen = set()
            for stream, event in _events(doc):
                processed += 1
                if progress and processed % 100_000 == 0:
                    progress({"event": "EVENT_PROGRESS", "day": day, "quote_rows": processed})
                _update_symbol_coverage(per_symbol[event["symbol"]], event, stream, opened, closed, symbol_previous)
                counts[stream] += 1
                symbols[stream].add(event["symbol"])
                unknown_exchange += event["exchange_time"] is None
                invalid += not event["quote_valid"]
                order_errors += event["out_of_order"]
                no_sequence += not event["sequence_known"]
                identity = _identity(event)
                if identity is not None:
                    duplicate_count += identity in seen
                    seen.add(identity)
                at = event["received_at"]
                if opened <= at <= closed:
                    first.setdefault(stream, at)
                    last[stream] = max(at, last.get(stream, at))
                    if stream in previous:
                        gaps[stream] = max(gaps[stream], (at-previous[stream]).total_seconds())
                    previous[stream] = at
            for stream in doc["paths"]:
                if stream in first:
                    gaps[stream] = max(gaps[stream], (first[stream]-opened).total_seconds(),
                                       (closed-last[stream]).total_seconds())
            for stats in per_symbol.values():
                _finish_symbol_coverage(stats, opened, closed)
            reference_known = sum(any(h.get("reference_price") for h in stats["header_records"])
                                  for stats in per_symbol.values() if stats["is_candidate"])
            limits_known = sum(any(h.get("upper_limit") and h.get("lower_limit") for h in stats["header_records"])
                               for stats in per_symbol.values() if stats["is_candidate"])
            row.update(event_counts_observed=dict(counts),
                       per_symbol_coverage=per_symbol,
                       reference_price_known_candidate_count=reference_known,
                       quote_limits_known_candidate_count=limits_known,
                       reference_price_status="ARCHIVED_SERIAL_MINUS_ONE_HEADERS" if reference_known else "UNKNOWN_NOT_ARCHIVED",
                       quote_limits_status="ARCHIVED_SERIAL_MINUS_ONE_HEADERS" if limits_known else "UNKNOWN_NOT_ARCHIVED",
                       stream_symbol_counts={k: len(v) for k, v in symbols.items()},
                       context_symbols_observed=sorted(symbols.get("context_ticks", set()) | symbols.get("context_books", set())),
                       context_0050_complete="0050" in symbols.get("context_ticks", set()) and "0050" in symbols.get("context_books", set()),
                       context_2330_complete="2330" in symbols.get("context_ticks", set()) and "2330" in symbols.get("context_books", set()),
                       candidate_tick_symbols=len(symbols.get("ticks", set())),
                       candidate_book_symbols=len(symbols.get("books", set())),
                       stream_first_regular_receipt={k: v.isoformat() for k, v in first.items()},
                       stream_last_regular_receipt={k: v.isoformat() for k, v in last.items()},
                       max_gap_seconds_by_stream=dict(gaps),
                       gap_method="PER_SOURCE_STREAM_REGULAR_SESSION_WITH_OPEN_CLOSE_EDGES_NOT_GLOBAL_OR_PER_SYMBOL",
                       unknown_exchange_time_events=unknown_exchange, invalid_quote_events=invalid,
                       out_of_order_events=order_errors, missing_ingest_sequence_events=no_sequence,
                       duplicate_tick_events=duplicate_count,
                       ordering_status="ARCHIVED_SEQUENCE" if no_sequence == 0 else "LEGACY_CROSS_STREAM_ORDER_UNRECORDED",
                       point_in_time_pass=bool(seal_at and watch_at and seal_at <= opened and watch_at <= opened and str(doc["manifest"]["signal_date"]) < day),
                       market_data_available=bool(counts.get("ticks") and counts.get("books")),
                       audit_status="AUDITED_RESEARCH_INPUT_NOT_TRADABLE_CERTIFICATION")
        except (DataError, KeyError, TypeError, ValueError, OSError) as exc:
            sources.extend(getattr(exc, "sources", []))
            row.setdefault("source_hash_verified", False)
            row.update(error=str(exc))
        rows.append(row)
        if progress:
            progress({"event": "DAY_FINISHED", "day": day, "audit_status": row["audit_status"],
                      "source_hash_verified": row.get("source_hash_verified", False), "error": row.get("error")})
    return rows, sources
