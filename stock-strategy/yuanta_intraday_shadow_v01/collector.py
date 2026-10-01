"""Append-only Yuanta quote collector for the sealed Stage A Top30."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import gzip
import io
import json
from pathlib import Path
import threading
import uuid

from shadow_daily_runner.normalize import parse_tpex, parse_twse
from shadow_daily_runner.sources import SourceSnapshot
from stage_a_prospective_watchlist_v01.seal_store import latest_seal


MODULE_DIR = Path(__file__).resolve().parent
STOCK_STRATEGY_DIR = MODULE_DIR.parent
DEFAULT_RUNTIME_DIR = MODULE_DIR / "runtime"
DEFAULT_STAGE_A_RUNTIME = STOCK_STRATEGY_DIR / "stage_a_prospective_watchlist_v01" / "runtime"
DEFAULT_EOD_AUDIT_DIR = STOCK_STRATEGY_DIR / "shadow_daily_runner" / "runtime" / "audit"
MARKET_CONTEXT_ITEMS = (
    # Quote-only benchmark. It is deliberately outside the sealed Stage A Top30.
    # rank=0 and score=0 are never used by the strategy or research ranking.
    # The values keep WatchItem's existing schema backward compatible.
    ("0050", "元大台灣50", 0, 0.0, "TWSE"),
)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class WatchItem:
    stock_id: str
    stock_name: str
    rank: int
    score: float
    market: str


def market_context_items() -> list[WatchItem]:
    return [WatchItem(*values) for values in MARKET_CONTEXT_ITEMS]


def subscription_items(items: list[WatchItem]) -> list[WatchItem]:
    """Return the sealed Top30 plus permanent quote-only market benchmarks."""
    result = list(items)
    subscribed = {item.stock_id for item in result}
    for item in market_context_items():
        if item.stock_id not in subscribed:
            result.append(item)
            subscribed.add(item.stock_id)
    return result


def _source_snapshot(metadata: dict) -> SourceSnapshot:
    path = Path(str(metadata["path"]))
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != metadata.get("sha256") or path.stem != digest:
        raise RuntimeError(f"official source hash mismatch: {path.name}")
    return SourceSnapshot(
        source=str(metadata["source"]), request_url=str(metadata["request_url"]),
        retrieved_at_utc=str(metadata["retrieved_at_utc"]), sha256=digest,
        path=path, payload=payload,
    )


def official_market_map(signal_date: str, audit_dir: Path = DEFAULT_EOD_AUDIT_DIR) -> tuple[dict[str, str], dict]:
    audit_path = audit_dir / f"official_eod_through_{signal_date}.json"
    if not audit_path.is_file():
        raise RuntimeError(f"official EOD audit missing for {signal_date}")
    audit_bytes = audit_path.read_bytes()
    audit = json.loads(audit_bytes)
    matches: dict[str, dict] = {}
    for archive in audit.get("archives", []):
        for source in archive.get("sources", []):
            if str(source.get("response_date")) != signal_date:
                continue
            name = str(source.get("source", ""))
            if name == f"twse_eod_{signal_date}":
                matches["TWSE"] = source
            elif name == f"tpex_eod_{signal_date}":
                matches["TPEX"] = source
    if set(matches) != {"TWSE", "TPEX"}:
        raise RuntimeError(f"official TWSE/TPEx sources are not uniquely available for {signal_date}")
    parsed = {
        "TWSE": parse_twse(_source_snapshot(matches["TWSE"]), signal_date),
        "TPEX": parse_tpex(_source_snapshot(matches["TPEX"]), signal_date),
    }
    market_map: dict[str, str] = {}
    for market, result in parsed.items():
        if result.errors:
            raise RuntimeError(f"official {market} parse contains unresolved errors")
        for row in result.rows:
            code = row["code"]
            if code in market_map and market_map[code] != market:
                raise RuntimeError(f"ambiguous official market identity: {code}")
            market_map[code] = market
    provenance = {
        "audit_path": str(audit_path),
        "audit_sha256": hashlib.sha256(audit_bytes).hexdigest(),
        "sources": {market: {"path": source["path"], "sha256": source["sha256"], "request_url": source["request_url"]} for market, source in sorted(matches.items())},
    }
    return market_map, provenance


def load_stage_a_watchlist(stage_a_runtime: Path = DEFAULT_STAGE_A_RUNTIME, audit_dir: Path = DEFAULT_EOD_AUDIT_DIR) -> tuple[dict, list[WatchItem], dict]:
    seal = latest_seal(stage_a_runtime)
    if seal is None:
        raise RuntimeError("no sealed Stage A watchlist exists")
    stocks = seal.get("stocks", [])
    if len(stocks) != 30 or len({str(row["stock_id"]) for row in stocks}) != 30:
        raise RuntimeError("sealed Stage A watchlist must contain exactly 30 unique stocks")
    if sorted(int(row["rank"]) for row in stocks) != list(range(1, 31)):
        raise RuntimeError("sealed Stage A ranks must be exactly 1..30")
    market_map, provenance = official_market_map(str(seal["signal_date"]), audit_dir)
    missing = sorted(str(row["stock_id"]) for row in stocks if str(row["stock_id"]) not in market_map)
    if missing:
        raise RuntimeError(f"Stage A stocks missing official market identity: {','.join(missing)}")
    items = [WatchItem(str(row["stock_id"]), str(row["stock_name"]), int(row["rank"]), float(row["score"]), market_map[str(row["stock_id"])]) for row in sorted(stocks, key=lambda item: int(item["rank"]))]
    return seal, items, provenance


class AppendOnlyRun:
    """Exclusive run directory with line-flushed append-only event files."""

    ALLOWED_MODES = {
        "SHADOW_ONLY_READ_ONLY_QUOTES",
        "OBSERVE_ONLY_QUOTES",
        "LIVE_TRADING_QUOTES",
    }

    def __init__(
        self,
        runtime_dir: Path,
        seal: dict,
        items: list[WatchItem],
        provenance: dict,
        *,
        compress: bool = False,
        mode: str = "SHADOW_ONLY_READ_ONLY_QUOTES",
    ):
        if mode not in self.ALLOWED_MODES:
            raise ValueError(f"unsupported archive mode: {mode}")
        self.mode = mode
        self.subscription_count = len(subscription_items(items))
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "_" + uuid.uuid4().hex[:8]
        self.run_dir = runtime_dir / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.compressed = compress
        self.tick_path = self.run_dir / ("ticks.jsonl.gz" if compress else "ticks.jsonl")
        self.book_path = self.run_dir / ("books.jsonl.gz" if compress else "books.jsonl")
        self.market_tick_path = self.run_dir / (
            "market_context_ticks.jsonl.gz" if compress else "market_context_ticks.jsonl"
        )
        self.market_book_path = self.run_dir / (
            "market_context_books.jsonl.gz" if compress else "market_context_books.jsonl"
        )
        # Callback diagnostics intentionally live in a separate, uncompressed
        # append-only artifact.  Only a small allow-list of non-secret routing
        # metadata is accepted by callback_error(); raw broker payloads and
        # exception messages must never be persisted here.
        self.callback_error_path = self.run_dir / "callback_errors.jsonl"
        self.decision_evidence_path = self.run_dir / "decision_evidence.jsonl"
        self.subscription_evidence_path = self.run_dir / "subscription_evidence.jsonl"
        self._raw_files = []
        if compress:
            tick_raw = self.tick_path.open("xb")
            book_raw = self.book_path.open("xb")
            market_tick_raw = self.market_tick_path.open("xb")
            market_book_raw = self.market_book_path.open("xb")
            self._raw_files = [tick_raw, book_raw, market_tick_raw, market_book_raw]
            self._tick = io.TextIOWrapper(gzip.GzipFile(fileobj=tick_raw, mode="wb", mtime=0), encoding="utf-8", write_through=True)
            self._book = io.TextIOWrapper(gzip.GzipFile(fileobj=book_raw, mode="wb", mtime=0), encoding="utf-8", write_through=True)
            self._market_tick = io.TextIOWrapper(gzip.GzipFile(fileobj=market_tick_raw, mode="wb", mtime=0), encoding="utf-8", write_through=True)
            self._market_book = io.TextIOWrapper(gzip.GzipFile(fileobj=market_book_raw, mode="wb", mtime=0), encoding="utf-8", write_through=True)
        else:
            self._tick = self.tick_path.open("x", encoding="utf-8", buffering=1)
            self._book = self.book_path.open("x", encoding="utf-8", buffering=1)
            self._market_tick = self.market_tick_path.open("x", encoding="utf-8", buffering=1)
            self._market_book = self.market_book_path.open("x", encoding="utf-8", buffering=1)
        self._callback_errors = self.callback_error_path.open(
            "x", encoding="utf-8", buffering=1,
        )
        self._decision_evidence = self.decision_evidence_path.open(
            "x", encoding="utf-8", buffering=1,
        )
        self._subscription_evidence = self.subscription_evidence_path.open(
            "x", encoding="utf-8", buffering=1,
        )
        self._lock = threading.Lock()
        self.counts = {
            "ticks": 0,
            "books": 0,
            "market_context_ticks": 0,
            "market_context_books": 0,
            "callback_errors": 0,
            "evidenced_quote_events": 0,
            "decision_evidence": 0,
            "subscription_evidence": 0,
            "observation_failures": 0,
        }
        self.callback_error_types: dict[str, int] = {}
        self.market_context_counts = {
            item.stock_id: {"ticks": 0, "books": 0}
            for item in market_context_items()
        }
        self.snapshot = {
            "schema_version": 2, "run_id": self.run_id, "created_at": utc_now(),
            "signal_date": seal["signal_date"], "stage_a_seal_hash": seal["seal_hash"],
            "market_provenance": provenance,
            "stocks": [{"stock_id": x.stock_id, "stock_name": x.stock_name, "rank": x.rank, "score": x.score, "market": x.market} for x in items],
            "market_context": [
                {
                    "stock_id": x.stock_id,
                    "stock_name": x.stock_name,
                    "market": x.market,
                    "role": "MARKET_BENCHMARK",
                }
                for x in market_context_items()
            ],
            "mode": self.mode, "compression": "gzip" if compress else "none",
            "compressed_flush_interval_events": 100 if compress else 1,
        }
        (self.run_dir / "watchlist.json").write_bytes(canonical_bytes(self.snapshot) + b"\n")

    def append(self, kind: str, event: dict) -> None:
        if kind not in {"ticks", "books", "market_context_ticks", "market_context_books"}:
            raise ValueError(kind)
        payload = canonical_bytes(event).decode("utf-8") + "\n"
        with self._lock:
            handles = {
                "ticks": self._tick,
                "books": self._book,
                "market_context_ticks": self._market_tick,
                "market_context_books": self._market_book,
            }
            handle = handles[kind]
            handle.write(payload)
            self.counts[kind] += 1
            required = {
                "run_id", "subscription_generation", "event_kind",
                "callback_received_at", "ingest_sequence", "ingest_accepted",
                "ingest_reason",
            }
            if required.issubset(event) and all(
                event.get(key) is not None for key in required
            ):
                self.counts["evidenced_quote_events"] += 1
            if kind.startswith("market_context_"):
                symbol = str(event.get("stock_id", ""))
                if symbol in self.market_context_counts:
                    counter = "ticks" if kind.endswith("ticks") else "books"
                    self.market_context_counts[symbol][counter] += 1
            if not self.compressed or self.counts[kind] % 100 == 0:
                handle.flush()

    def append_decision_evidence(self, event: dict) -> None:
        with self._lock:
            self._decision_evidence.write(
                canonical_bytes(event).decode("utf-8") + "\n"
            )
            self._decision_evidence.flush()
            self.counts["decision_evidence"] += 1

    def append_subscription_evidence(self, event: dict) -> None:
        with self._lock:
            self._subscription_evidence.write(
                canonical_bytes(event).decode("utf-8") + "\n"
            )
            self._subscription_evidence.flush()
            self.counts["subscription_evidence"] += 1

    def observation_failure(
        self, *, phase: str, stock_id: str = "", event_kind: str = "",
    ) -> None:
        """Make missing observation evidence invalidate completeness visibly."""
        with self._lock:
            self.counts["observation_failures"] += 1
        self.callback_error(
            "OBSERVATION_WRITE_FAILURE",
            callback_name=event_kind,
            stock_id=stock_id,
            phase=phase,
        )

    def callback_error(
        self,
        error_type: str = "UNSPECIFIED",
        *,
        callback_name: str = "",
        stock_id: str = "",
        phase: str = "",
    ) -> None:
        """Persist a sanitized callback failure without the broker payload.

        Callback names, listed stock codes and parser phases are sufficient to
        locate recurring adapter defects.  Free-form exception messages are
        deliberately excluded because a vendor exception may contain account
        or certificate details.
        """
        with self._lock:
            self.counts["callback_errors"] += 1
            key = str(error_type or "UNSPECIFIED")
            self.callback_error_types[key] = self.callback_error_types.get(key, 0) + 1
            event = {
                "received_at": utc_now(),
                "error_type": key[:120],
                "callback_name": str(callback_name or "")[:120],
                "stock_id": str(stock_id or "")[:16],
                "phase": str(phase or "")[:120],
            }
            self._callback_errors.write(
                canonical_bytes(event).decode("utf-8") + "\n"
            )
            self._callback_errors.flush()

    def finalize(
        self,
        *,
        status: str,
        started_at: str,
        ended_at: str,
        error_type: str = "",
        actual_orders: int = 0,
        actual_fills: int = 0,
        broker_order_calls: int = 0,
        terminal_flat_confirmed_at: str | None = None,
    ) -> dict:
        for name, value in {
            "actual_orders": actual_orders,
            "actual_fills": actual_fills,
            "broker_order_calls": broker_order_calls,
        }.items():
            if int(value) < 0:
                raise ValueError(f"{name} must be non-negative")

        with self._lock:
            for handle in (
                self._tick,
                self._book,
                self._market_tick,
                self._market_book,
                self._callback_errors,
                self._decision_evidence,
                self._subscription_evidence,
            ):
                handle.flush()
                handle.close()
            for raw in self._raw_files:
                raw.close()

        manifest = {
            "schema_version": 2, "run_id": self.run_id, "status": status,
            "started_at": started_at, "ended_at": ended_at,
            "signal_date": self.snapshot["signal_date"], "stage_a_seal_hash": self.snapshot["stage_a_seal_hash"],
            "watchlist_count": len(self.snapshot["stocks"]),
            "subscription_count": self.subscription_count,
            "event_counts": dict(self.counts),
            "callback_error_types": dict(sorted(self.callback_error_types.items())),
            "market_context_event_counts": self.market_context_counts,
            "observation_evidence": {
                "schema_version": 1,
                "event_fields_embedded_in_quote_streams": True,
                "write_mode": "EXISTING_SYNCHRONOUS_ARCHIVE_FAIL_VISIBLE",
                "queue_used": False,
                "queue_overflow_count": 0,
                "quote_event_count": sum(
                    int(self.counts[name]) for name in (
                        "ticks", "books", "market_context_ticks",
                        "market_context_books",
                    )
                ),
                "evidenced_quote_event_count": int(
                    self.counts["evidenced_quote_events"]
                ),
                "observation_failure_count": int(
                    self.counts["observation_failures"]
                ),
            },
            "artifacts": {
                "watchlist.json": sha256_file(self.run_dir / "watchlist.json"),
                self.tick_path.name: sha256_file(self.tick_path),
                self.book_path.name: sha256_file(self.book_path),
                self.market_tick_path.name: sha256_file(self.market_tick_path),
                self.market_book_path.name: sha256_file(self.market_book_path),
                self.callback_error_path.name: sha256_file(
                    self.callback_error_path
                ),
                self.decision_evidence_path.name: sha256_file(
                    self.decision_evidence_path
                ),
                self.subscription_evidence_path.name: sha256_file(
                    self.subscription_evidence_path
                ),
            },
            "mode": self.mode,
            "error_type": error_type,
            "actual_orders": int(actual_orders),
            "actual_fills": int(actual_fills),
            "broker_order_calls": int(broker_order_calls),
            "terminal_flat_confirmed_at": terminal_flat_confirmed_at,
        }
        manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        (self.run_dir / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
        return manifest
