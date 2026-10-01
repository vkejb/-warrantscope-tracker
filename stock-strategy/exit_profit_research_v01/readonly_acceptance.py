"""Read-only acceptance check for one newly archived trading day."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC


TAIPEI = ZoneInfo("Asia/Taipei")


def _received_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TAIPEI)


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def clock_on(session_date: str, value: str) -> datetime:
    base = datetime.strptime(session_date, "%Y%m%d").replace(tzinfo=TAIPEI)
    hour, minute = map(int, value.split(":"))
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def verify_run(run_dir: Path) -> dict[str, Any]:
    manifest = json.loads(
        (run_dir / "run_manifest.json").read_text(encoding="utf-8")
    )
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    manifest_ok = (
        hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
        == manifest.get("manifest_hash")
    )
    failures = []
    for name, digest in manifest.get("artifacts", {}).items():
        path = run_dir / name
        if not path.is_file():
            failures.append(f"MISSING:{name}")
        elif sha256_file(path) != digest:
            failures.append(f"HASH_MISMATCH:{name}")
    started = _received_at(str(manifest["started_at"]))
    ended = _received_at(str(manifest["ended_at"]))
    session_date = ended.strftime("%Y%m%d")
    required_start = clock_on(session_date, str(SPEC["entry_start"]))
    required_end = clock_on(session_date, str(SPEC["hard_exit_time"]))
    return {
        "manifest": manifest,
        "manifest_hash_valid": manifest_ok,
        "artifact_failures": failures,
        "session_date": session_date,
        "started_at": started,
        "ended_at": ended,
        "strategy_clock_coverage": (
            started <= required_start and ended >= required_end
        ),
    }


def summarize_stream(run_dir: Path, filename: str) -> dict[str, Any]:
    path = run_dir / filename
    if not path.is_file():
        return {"event_count": 0, "missing_file": True}
    count = invalid_quote_time = zero_serial = 0
    symbols: Counter[str] = Counter()
    first = last = None
    for row in read_jsonl(path):
        received = _received_at(str(row["received_at"]))
        count += 1
        symbols[str(row.get("stock_id") or "")] += 1
        first = received if first is None else min(first, received)
        last = received if last is None else max(last, received)
        if "ticks" in filename:
            try:
                datetime.strptime(str(row.get("quote_time") or ""), "%H:%M:%S.%f")
            except ValueError:
                invalid_quote_time += 1
            zero_serial += int(int(row.get("serial_no") or 0) == 0)
    return {
        "event_count": count,
        "symbol_count": len(symbols),
        "first_received_at": first.isoformat() if first else None,
        "last_received_at": last.isoformat() if last else None,
        "events_by_symbol": dict(sorted(symbols.items())),
        "invalid_quote_time_count": invalid_quote_time,
        "zero_serial_count": zero_serial,
        "missing_file": False,
    }


def _evidence_audit(run_dir: Path, manifest: dict) -> dict:
    artifacts = manifest.get("artifacts", {})
    quote_event_count = evidenced_count = accepted_count = rejected_count = 0
    sequences: set[int] = set()
    duplicate_sequences = 0
    rejection_reasons: Counter[str] = Counter()
    required = {
        "run_id", "subscription_generation", "event_kind",
        "callback_received_at", "ingest_sequence", "ingest_accepted",
        "ingest_reason",
    }
    for name in (
        "ticks.jsonl.gz", "ticks.jsonl", "books.jsonl.gz", "books.jsonl",
        "market_context_ticks.jsonl.gz", "market_context_ticks.jsonl",
        "market_context_books.jsonl.gz", "market_context_books.jsonl",
    ):
        if name in artifacts and (run_dir / name).is_file():
            for row in read_jsonl(run_dir / name):
                quote_event_count += 1
                if not (
                    required.issubset(row)
                    and all(row.get(key) is not None for key in required)
                ):
                    continue
                evidenced_count += 1
                sequence = int(row["ingest_sequence"])
                duplicate_sequences += int(sequence in sequences)
                sequences.add(sequence)
                if row.get("ingest_accepted") is True:
                    accepted_count += 1
                elif row.get("ingest_accepted") is False:
                    rejected_count += 1
                    rejection_reasons[str(row.get("ingest_reason"))] += 1
    decisions = list(read_jsonl(run_dir / "decision_evidence.jsonl")) if (
        run_dir / "decision_evidence.jsonl"
    ).is_file() else []
    subscriptions = list(read_jsonl(run_dir / "subscription_evidence.jsonl")) if (
        run_dir / "subscription_evidence.jsonl"
    ).is_file() else []
    sequence_complete = bool(sequences) and (
        duplicate_sequences == 0
        and len(sequences) == max(sequences)
        and min(sequences) == 1
    )
    stale_decisions = [
        row for row in decisions
        if row.get("diagnostics", {}).get("reason") == "BENCHMARK_MISSING_OR_STALE"
    ]
    subscription_accepted_count = sum(
        row.get("event") == "SUBSCRIBE_ACCEPTED" for row in subscriptions
    )
    observation_failures = int(
        manifest.get("event_counts", {}).get("observation_failures", 0)
    )
    stale_causes: Counter[str] = Counter()
    stale_examples = []
    for row in stale_decisions:
        raw = row.get("raw_quote_status", {}).get("0050", {})
        state = row.get("engine_state_summary", {}).get("0050", {})
        if observation_failures:
            cause = "LOGGER_OR_DATA_PATH_FAILURE"
        elif subscription_accepted_count == 0:
            cause = "SUBSCRIPTION_OR_CONNECTION_EVIDENCE_MISSING"
        elif not raw.get("last_raw_tick_callback_at"):
            cause = "NO_TICK_CALLBACK"
        elif raw.get("last_tick_ingest_accepted") is False:
            cause = "TICK_CALLBACK_REJECTED_BY_ACTUAL_INGEST"
        elif (
            raw.get("last_raw_book_callback_at")
            and raw.get("last_raw_book_callback_at")
            > raw.get("last_raw_tick_callback_at", "")
        ):
            cause = "BOOK_UPDATED_WITHOUT_FRESH_ACCEPTED_TICK"
        else:
            cause = "UNKNOWN_NO_FRESH_ACCEPTED_TICK"
        stale_causes[cause] += 1
        if len(stale_examples) < 5:
            stale_examples.append({
                "decision_time": row.get("decision_time"), "cause": cause,
                "subscription_generation": raw.get("subscription_generation"),
                "last_raw_tick_callback_at": raw.get("last_raw_tick_callback_at"),
                "last_raw_tick_exchange_time": raw.get("last_raw_tick_exchange_time"),
                "last_tick_ingest_reason": raw.get("last_tick_ingest_reason"),
                "last_accepted_tick_exchange_time": state.get("last_accepted_exchange_time"),
                "last_accepted_tick_received_at": state.get("last_accepted_received_at"),
                "last_raw_book_callback_at": raw.get("last_raw_book_callback_at"),
            })
    return {
        "quote_event_count": quote_event_count,
        "evidenced_quote_event_count": evidenced_count,
        "event_evidence_complete": quote_event_count > 0 and evidenced_count == quote_event_count,
        "ingest_sequence_complete": sequence_complete,
        "first_ingest_sequence": min(sequences) if sequences else None,
        "last_ingest_sequence": max(sequences) if sequences else None,
        "accepted_event_count": accepted_count,
        "rejected_event_count": rejected_count,
        "rejection_reasons": dict(rejection_reasons),
        "decision_evidence_count": len(decisions),
        "stale_no_trade_decision_count": len(stale_decisions),
        "stale_0050_cause_counts": dict(stale_causes),
        "stale_0050_examples": stale_examples,
        "decision_watermarks_monotonic": all(
            int(left.get("ingest_sequence_watermark", -1))
            <= int(right.get("ingest_sequence_watermark", -1))
            for left, right in zip(decisions, decisions[1:])
        ),
        "subscription_generations": sorted({
            int(row["subscription_generation"]) for row in subscriptions
            if row.get("subscription_generation") is not None
        }),
        "subscription_accepted_count": subscription_accepted_count,
    }


def inspect_run(run_dir: Path) -> dict:
    verified = verify_run(run_dir)
    manifest = verified["manifest"]
    artifacts = manifest.get("artifacts", {})
    streams = {
        name: summarize_stream(run_dir, name)
        for name in (
            "ticks.jsonl.gz", "books.jsonl.gz",
            "market_context_ticks.jsonl.gz", "market_context_books.jsonl.gz",
        )
    }
    callback_errors = int(manifest.get("event_counts", {}).get("callback_errors", 0))
    observation_failures = int(
        manifest.get("event_counts", {}).get("observation_failures", 0)
    )
    benchmark = manifest.get("market_context_event_counts", {}).get("0050", {})
    integrity_pass = (
        verified["manifest_hash_valid"]
        and not verified["artifact_failures"]
        and manifest.get("status") == "COMPLETE"
    )
    session_date = verified["session_date"]
    opening_required = clock_on(session_date, "09:00")
    holding_required = clock_on(session_date, "13:20")
    dependency_clock_coverage = (
        verified["started_at"] <= opening_required
        and verified["ended_at"] >= holding_required
    )
    execution_activity = any(
        int(manifest.get(name, 0)) > 0
        for name in ("actual_orders", "actual_fills", "broker_order_calls")
    )
    terminal_flat_confirmed = bool(manifest.get("terminal_flat_confirmed_at"))
    holding_lifecycle_coverage = (
        not execution_activity or terminal_flat_confirmed
    )
    archive_pass = (
        integrity_pass
        and dependency_clock_coverage
        and holding_lifecycle_coverage
        and callback_errors == 0
        and int(benchmark.get("ticks", 0)) > 0
        and int(benchmark.get("books", 0)) > 0
    )
    evidence = _evidence_audit(run_dir, manifest)
    semantic_proven = (
        archive_pass
        and observation_failures == 0
        and evidence["event_evidence_complete"]
        and evidence["ingest_sequence_complete"]
        and evidence["decision_evidence_count"] > 0
        and evidence["decision_watermarks_monotonic"]
        and evidence["subscription_accepted_count"] > 0
    )
    status = (
        "FORMAL_SOURCE_ELIGIBLE"
        if archive_pass and semantic_proven
        else "ARCHIVE_ELIGIBLE_SEMANTIC_PARITY_UNPROVEN"
        if archive_pass
        else "ARCHIVE_NOT_ELIGIBLE"
    )
    return {
        "run_dir": str(run_dir), "run_id": manifest.get("run_id"),
        "mode": manifest.get("mode"), "session_date": verified["session_date"],
        "status": status, "read_only": True,
        "manifest_hash_valid": verified["manifest_hash_valid"],
        "artifact_failures": verified["artifact_failures"],
        "source_status": manifest.get("status"),
        "callback_errors": callback_errors,
        "observation_failures": observation_failures,
        "callback_error_types": manifest.get("callback_error_types"),
        "strategy_clock_coverage": verified["strategy_clock_coverage"],
        "strategy_dependency_clock_coverage": dependency_clock_coverage,
        "holding_lifecycle_coverage": holding_lifecycle_coverage,
        "terminal_flat_confirmed_at": manifest.get("terminal_flat_confirmed_at"),
        "required_opening_state_start": opening_required.isoformat(),
        "required_holding_end": holding_required.isoformat(),
        "started_at": verified["started_at"].isoformat(),
        "ended_at": verified["ended_at"].isoformat(),
        "benchmark_0050_counts": benchmark,
        "streams": streams,
        "qualification_dimensions": {
            "recording_integrity_and_trust": (
                "PASS" if archive_pass and observation_failures == 0
                else "FAIL_OR_UNKNOWN"
            ),
            "decision_boundary_strategy_availability": (
                "AVAILABLE_WITH_RULED_NO_TRADE_BOUNDARIES"
                if evidence["decision_evidence_count"] > 0
                and evidence["stale_no_trade_decision_count"] > 0
                else "AVAILABLE" if evidence["decision_evidence_count"] > 0
                else "UNPROVEN_LEGACY_ARCHIVE"
            ),
            "actual_live_input_decision_parity": (
                "PROVEN_BY_INGEST_AND_DECISION_LEDGER"
                if semantic_proven else "SEMANTIC_PARITY_UNPROVEN"
            ),
        },
        "observation_evidence": evidence,
        "semantic_parity_proven": semantic_proven,
        "missing_for_formal_parity": [] if semantic_proven else [
            "engine_received_at_before_archive_write",
            "per_event_accepted_rejected_and_reason",
            "subscription_ack_and_reconnect_epoch_by_symbol",
            "global_callback_sequence_across_tick_and_book_streams",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(inspect_run(args.run_dir.resolve()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
