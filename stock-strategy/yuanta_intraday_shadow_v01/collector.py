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
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "_" + uuid.uuid4().hex[:8]
        self.run_dir = runtime_dir / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.compressed = compress
        self.tick_path = self.run_dir / ("ticks.jsonl.gz" if compress else "ticks.jsonl")
        self.book_path = self.run_dir / ("books.jsonl.gz" if compress else "books.jsonl")
        self._raw_files = []
        if compress:
            tick_raw = self.tick_path.open("xb")
            book_raw = self.book_path.open("xb")
            self._raw_files = [tick_raw, book_raw]
            self._tick = io.TextIOWrapper(gzip.GzipFile(fileobj=tick_raw, mode="wb", mtime=0), encoding="utf-8", write_through=True)
            self._book = io.TextIOWrapper(gzip.GzipFile(fileobj=book_raw, mode="wb", mtime=0), encoding="utf-8", write_through=True)
        else:
            self._tick = self.tick_path.open("x", encoding="utf-8", buffering=1)
            self._book = self.book_path.open("x", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self.counts = {"ticks": 0, "books": 0, "callback_errors": 0}
        self.snapshot = {
            "schema_version": 1, "run_id": self.run_id, "created_at": utc_now(),
            "signal_date": seal["signal_date"], "stage_a_seal_hash": seal["seal_hash"],
            "market_provenance": provenance,
            "stocks": [{"stock_id": x.stock_id, "stock_name": x.stock_name, "rank": x.rank, "score": x.score, "market": x.market} for x in items],
            "mode": self.mode, "compression": "gzip" if compress else "none",
            "compressed_flush_interval_events": 100 if compress else 1,
        }
        (self.run_dir / "watchlist.json").write_bytes(canonical_bytes(self.snapshot) + b"\n")

    def append(self, kind: str, event: dict) -> None:
        if kind not in {"ticks", "books"}:
            raise ValueError(kind)
        payload = canonical_bytes(event).decode("utf-8") + "\n"
        with self._lock:
            handle = self._tick if kind == "ticks" else self._book
            handle.write(payload)
            self.counts[kind] += 1
            if not self.compressed or self.counts[kind] % 100 == 0:
                handle.flush()

    def callback_error(self) -> None:
        with self._lock:
            self.counts["callback_errors"] += 1

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
    ) -> dict:
        for name, value in {
            "actual_orders": actual_orders,
            "actual_fills": actual_fills,
            "broker_order_calls": broker_order_calls,
        }.items():
            if int(value) < 0:
                raise ValueError(f"{name} must be non-negative")

        with self._lock:
            self._tick.flush(); self._book.flush(); self._tick.close(); self._book.close()
            for raw in self._raw_files:
                raw.close()

        manifest = {
            "schema_version": 1, "run_id": self.run_id, "status": status,
            "started_at": started_at, "ended_at": ended_at,
            "signal_date": self.snapshot["signal_date"], "stage_a_seal_hash": self.snapshot["stage_a_seal_hash"],
            "watchlist_count": len(self.snapshot["stocks"]), "event_counts": dict(self.counts),
            "artifacts": {"watchlist.json": sha256_file(self.run_dir / "watchlist.json"), self.tick_path.name: sha256_file(self.tick_path), self.book_path.name: sha256_file(self.book_path)},
            "mode": self.mode,
            "error_type": error_type,
            "actual_orders": int(actual_orders),
            "actual_fills": int(actual_fills),
            "broker_order_calls": int(broker_order_calls),
        }
        manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        (self.run_dir / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
        return manifest
