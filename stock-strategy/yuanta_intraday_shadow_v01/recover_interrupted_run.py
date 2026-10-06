"""Recover complete JSONL records from an interrupted compressed quote run.

The source directory is never modified.  The recovered copy is always marked
PARTIAL_SESSION and records that Top30 rows were reconstructed from the
overlapping expanded stream when the original small gzip buffer never reached
disk before process termination.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import zlib

from .collector import canonical_bytes, sha256_file


STREAMS = (
    "expanded_ticks.jsonl.gz", "expanded_books.jsonl.gz",
    "market_context_ticks.jsonl.gz", "market_context_books.jsonl.gz",
)
PLAIN = ("callback_errors.jsonl", "decision_evidence.jsonl", "subscription_evidence.jsonl")
REQUIRED_EVIDENCE_FIELDS = {
    "run_id", "subscription_generation", "event_kind",
    "callback_received_at", "ingest_sequence", "ingest_accepted", "ingest_reason",
}


def _iter_complete_gzip_lines(path: Path):
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    pending = b""
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            pending += decoder.decompress(chunk)
            lines = pending.split(b"\n")
            pending = lines.pop()
            for line in lines:
                if line:
                    yield line
    # A missing gzip trailer is expected.  Never emit an incomplete JSON line.


def _copy_complete_plain(source: Path, target: Path) -> int:
    count = 0
    with source.open("rb") as reader, target.open("xb") as writer:
        for line in reader:
            if not line.endswith(b"\n"):
                break
            json.loads(line)
            writer.write(line)
            count += 1
    return count


def _stage_a_row(row: dict, metadata: dict) -> dict:
    result = dict(row)
    result["stock_name"] = metadata["stock_name"]
    result["market"] = metadata["market"]
    result["stage_a_rank"] = metadata["rank"]
    result["stage_a_score"] = metadata["score"]
    result["role"] = "STAGE_A_CANDIDATE"
    for key in ("expanded_rank", "industry", "industry_code"):
        result.pop(key, None)
    return result


def _subscription_symbols(snapshot: dict) -> set[str]:
    symbols = {
        str(row["stock_id"])
        for row in snapshot.get("expanded_shadow_universe", {}).get("stocks", [])
    }
    symbols |= {str(row["stock_id"]) for row in snapshot.get("market_context", [])}
    if bool(snapshot.get("stage_a_quotes_enabled", True)):
        symbols |= {str(row["stock_id"]) for row in snapshot.get("stocks", [])}
    return symbols


def recover(source_dir: Path, output_root: Path) -> dict:
    source_dir = source_dir.resolve()
    watchlist_path = source_dir / "watchlist.json"
    snapshot = json.loads(watchlist_path.read_text(encoding="utf-8"))
    run_id = str(snapshot["run_id"])
    target = output_root.resolve() / (run_id + "_recovered_partial")
    target.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(watchlist_path, target / "watchlist.json")
    stage_a = {str(row["stock_id"]): row for row in snapshot["stocks"]}
    reconstruct_top30 = bool(snapshot.get("stage_a_quotes_enabled", True))
    counts = {name.removesuffix(".jsonl.gz"): 0 for name in STREAMS}
    counts.update({"ticks": 0, "books": 0, "callback_errors": 0,
                   "decision_evidence": 0, "subscription_evidence": 0,
                   "evidenced_quote_events": 0, "observation_failures": 0})
    market_context_counts = {
        str(row["stock_id"]): {"ticks": 0, "books": 0}
        for row in snapshot.get("market_context", [])
    }
    earliest = None
    latest = None

    top_handles = {
        "expanded_ticks.jsonl.gz": gzip.GzipFile(filename=str(target / "ticks.jsonl.gz"), mode="xb", mtime=0),
        "expanded_books.jsonl.gz": gzip.GzipFile(filename=str(target / "books.jsonl.gz"), mode="xb", mtime=0),
    }
    try:
        for name in STREAMS:
            destination = target / name
            with gzip.GzipFile(filename=str(destination), mode="xb", mtime=0) as writer:
                for raw in _iter_complete_gzip_lines(source_dir / name):
                    row = json.loads(raw)
                    encoded = canonical_bytes(row) + b"\n"
                    writer.write(encoded)
                    key = name.removesuffix(".jsonl.gz")
                    counts[key] += 1
                    if REQUIRED_EVIDENCE_FIELDS <= row.keys() and all(row.get(field) is not None for field in REQUIRED_EVIDENCE_FIELDS):
                        counts["evidenced_quote_events"] += 1
                    stamp = row.get("callback_received_at") or row.get("received_at")
                    if stamp:
                        earliest = stamp if earliest is None or stamp < earliest else earliest
                        latest = stamp if latest is None or stamp > latest else latest
                    symbol = str(row.get("stock_id", ""))
                    if name.startswith("market_context_") and symbol in market_context_counts:
                        market_context_counts[symbol]["ticks" if "ticks" in name else "books"] += 1
                    if reconstruct_top30 and name in top_handles and symbol in stage_a:
                        top_handles[name].write(canonical_bytes(_stage_a_row(row, stage_a[symbol])) + b"\n")
                        counts["ticks" if "ticks" in name else "books"] += 1
                        if REQUIRED_EVIDENCE_FIELDS <= row.keys() and all(row.get(field) is not None for field in REQUIRED_EVIDENCE_FIELDS):
                            counts["evidenced_quote_events"] += 1
    finally:
        for handle in top_handles.values():
            handle.close()

    for name in PLAIN:
        counts[name.removesuffix(".jsonl")] = _copy_complete_plain(source_dir / name, target / name)
    counts["observation_failures"] = sum(
        1 for line in (target / "callback_errors.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("error_type") == "OBSERVATION_WRITE_FAILURE"
    )
    callback_error_types = {}
    for line in (target / "callback_errors.jsonl").read_text(encoding="utf-8").splitlines():
        key = str(json.loads(line).get("error_type", "UNSPECIFIED"))
        callback_error_types[key] = callback_error_types.get(key, 0) + 1
    quote_count = sum(counts[name] for name in (
        "ticks", "books", "market_context_ticks", "market_context_books",
        "expanded_ticks", "expanded_books",
    ))
    expanded = snapshot.get("expanded_shadow_universe", {})
    symbols = _subscription_symbols(snapshot)
    artifact_names = ("watchlist.json", "ticks.jsonl.gz", "books.jsonl.gz", *STREAMS, *PLAIN)
    manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "status": "PARTIAL_SESSION",
        "started_at": earliest or snapshot.get("created_at"),
        "ended_at": latest or datetime.now(timezone.utc).isoformat(),
        "signal_date": snapshot["signal_date"],
        "stage_a_seal_hash": snapshot["stage_a_seal_hash"],
        "watchlist_count": len(stage_a),
        "subscription_count": len(symbols),
        "event_counts": counts,
        "callback_error_types": dict(sorted(callback_error_types.items())),
        "market_context_event_counts": market_context_counts,
        "observation_evidence": {
            "schema_version": 1,
            "event_fields_embedded_in_quote_streams": True,
            "write_mode": "RECOVERED_COMPLETE_JSONL_RECORDS_FROM_INTERRUPTED_GZIP",
            "queue_used": False,
            "queue_overflow_count": 0,
            "quote_event_count": quote_count,
            "evidenced_quote_event_count": counts["evidenced_quote_events"],
            "observation_failure_count": counts["observation_failures"],
        },
        "artifacts": {name: sha256_file(target / name) for name in artifact_names},
        "mode": snapshot["mode"],
        "error_type": "INTERRUPTED_BEFORE_ARCHIVE_FINALIZATION",
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_order_calls": 0,
        "terminal_flat_confirmed_at": None,
        "expanded_universe_count": expanded.get("count", 0),
        "expanded_universe_seal_hash": expanded.get("seal_hash", ""),
        "recovery": {
            "source_run_dir": str(source_dir),
            "source_preserved": True,
            "gzip_trailer_was_missing": True,
            "incomplete_final_json_records_discarded": True,
            "top30_reconstructed_from_overlapping_expanded_stream": reconstruct_top30,
            "normal_complete_session_claimed": False,
        },
    }
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (target / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return {"run_dir": str(target), "manifest": manifest}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    result = recover(args.source_run, args.output_root)
    print(json.dumps({"run_dir": result["run_dir"],
                      "status": result["manifest"]["status"],
                      "event_counts": result["manifest"]["event_counts"]},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
