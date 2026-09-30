from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from paper_shadow_v01.runner import (
    PAPER_CONTRACT_HASH,
    _load_market_context,
    replay_paper_session,
)
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import load_session
from yuanta_live_runtime_v01.strategy import (
    ANTI_CHASE_ENTRY_POLICY,
    LIVE_EXIT_POLICY,
    LONG_MARKET_REGIME_POLICY,
)


TAIPEI = ZoneInfo("Asia/Taipei")
ANALYSIS_ID = "STITCHED_CURRENT_STRATEGY_BACKTEST_V0_1"


def _manifest(path: Path) -> dict[str, Any]:
    value = json.loads((path / "run_manifest.json").read_text(encoding="utf-8"))
    unsigned = {key: item for key, item in value.items() if key != "manifest_hash"}
    if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != value.get("manifest_hash"):
        raise RuntimeError(f"source manifest hash mismatch: {path}")
    return value


def _validate_pair(early: dict[str, Any], late: dict[str, Any]) -> None:
    if early.get("status") != "COMPLETE" or late.get("status") != "COMPLETE":
        raise RuntimeError("both source runs must be COMPLETE")
    if early.get("signal_date") != late.get("signal_date"):
        raise RuntimeError("source signal dates differ")
    if early.get("stage_a_seal_hash") != late.get("stage_a_seal_hash"):
        raise RuntimeError("source Stage A seals differ")


def stitch_streams(
    early: dict[str, dict[str, Any]],
    late: dict[str, dict[str, Any]],
    cutover: datetime,
) -> dict[str, dict[str, Any]]:
    """Use the early archive before cutover and the late archive afterwards."""
    result: dict[str, dict[str, Any]] = {}
    for symbol in sorted(set(early) | set(late)):
        left = early.get(
            symbol,
            {"ticks": [], "books": [], "meta": {"stock_name": symbol}},
        )
        right = late.get(
            symbol,
            {"ticks": [], "books": [], "meta": left.get("meta", {})},
        )
        ticks = [row for row in left.get("ticks", []) if row["time"] < cutover]
        ticks.extend(row for row in right.get("ticks", []) if row["time"] >= cutover)
        books = [row for row in left.get("books", []) if row["time"] < cutover]
        books.extend(row for row in right.get("books", []) if row["time"] >= cutover)
        ticks.sort(key=lambda row: (row["time"], row.get("serial", 0)))
        books.sort(key=lambda row: row["time"])
        result[symbol] = {
            "ticks": ticks,
            "books": books,
            "tick_times": [row["time"] for row in ticks],
            "book_times": [row["time"] for row in books],
            "meta": left.get("meta") or right.get("meta") or {"stock_name": symbol},
        }
    return result


def _decision_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(str(row.get("decision") or "UNKNOWN") for row in rows)
    gates: Counter[str] = Counter()
    symbols: Counter[str] = Counter()
    near_misses: list[dict[str, Any]] = []
    for row in rows:
        for candidate in row.get("candidates") or []:
            reason = str(candidate.get("gate_reason") or "UNKNOWN")
            symbol = str(candidate.get("stock_id") or "UNKNOWN")
            gates[reason] += 1
            symbols[symbol] += 1
            near_misses.append(
                {
                    "decision_time": row.get("decision_time"),
                    "stock_id": symbol,
                    "stock_name": candidate.get("stock_name"),
                    "score": candidate.get("score"),
                    "gate_reason": reason,
                    "opening_extension": candidate.get("directional_opening_extension"),
                    "vwap_extension": candidate.get("directional_vwap_extension"),
                    "relative_strength_5m": candidate.get("relative_strength_5m"),
                    "required_confirmations": candidate.get("required_confirmations"),
                    "streak": candidate.get("streak"),
                }
            )
    return {
        "decision_windows": len(rows),
        "decisions": dict(sorted(decisions.items())),
        "near_miss_gate_counts": dict(sorted(gates.items())),
        "near_miss_symbol_counts": dict(sorted(symbols.items())),
        "near_misses": near_misses,
    }


def _benchmark_coverage(streams: dict[str, dict[str, Any]]) -> dict[str, Any]:
    data = streams.get("0050", {"ticks": [], "books": []})
    stamps = sorted(
        [row["time"] for row in data.get("ticks", [])]
        + [row["time"] for row in data.get("books", [])]
    )
    gaps = [
        (right - left).total_seconds()
        for left, right in zip(stamps, stamps[1:])
    ]
    return {
        "first_event": stamps[0].isoformat() if stamps else None,
        "last_event": stamps[-1].isoformat() if stamps else None,
        "event_count": len(stamps),
        "maximum_event_gap_seconds": max(gaps) if gaps else None,
    }


def run_backtest(
    early_run: Path,
    late_run: Path,
    *,
    capital_twd: int = 190_000,
    cutover_times: tuple[str, ...] = ("09:27:15.505", "09:30:00", "10:00:00", "12:00:00"),
) -> dict[str, Any]:
    early_run = early_run.resolve()
    late_run = late_run.resolve()
    early_manifest = _manifest(early_run)
    late_manifest = _manifest(late_run)
    _validate_pair(early_manifest, late_manifest)
    watchlist = json.loads(
        (early_run / "watchlist.json").read_text(encoding="utf-8")
    )

    early_candidates, early_coverage = load_session([early_run])
    late_candidates, late_coverage = load_session([late_run])
    early_market = _load_market_context(early_run, early_manifest)
    late_market = _load_market_context(late_run, late_manifest)
    session_date = datetime.fromisoformat(
        str(late_coverage["started_at_taipei"])
    ).strftime("%Y%m%d")

    variants: list[dict[str, Any]] = []
    primary_result: dict[str, Any] | None = None
    primary_candidates: dict[str, dict[str, Any]] | None = None
    primary_market: dict[str, dict[str, Any]] | None = None
    for index, text in enumerate(cutover_times):
        format_text = "%Y%m%d %H:%M:%S.%f" if "." in text else "%Y%m%d %H:%M:%S"
        cutover = datetime.strptime(
            f"{session_date} {text}", format_text
        ).replace(tzinfo=TAIPEI)
        candidates = stitch_streams(early_candidates, late_candidates, cutover)
        market = stitch_streams(early_market, late_market, cutover)

        # The early source recorded four aggregate callback errors without
        # timestamps. Selected archive rows are still used as a diagnostic
        # reconstruction, never promoted to certified FULL_SESSION evidence.
        replay_coverage = {
            "session_date": session_date,
            "source_statuses": ["COMPLETE", "COMPLETE"],
            "started_at_taipei": early_coverage["started_at_taipei"],
            "ended_at_taipei": late_coverage["ended_at_taipei"],
            "callback_errors": 0,
        }
        replay = replay_paper_session(
            candidates,
            market,
            replay_coverage,
            capital_twd=capital_twd,
        )
        summary = _decision_summary(replay["decision_diagnostics"])
        variant = {
            "cutover_time": cutover.isoformat(),
            "production_status": replay["reason"],
            "production_trade": replay["trade"],
            "confirmation_status": replay["confirmation_reason"],
            "confirmation_trade": replay["confirmation_trade"],
            **summary,
        }
        variants.append(variant)
        if index == 0:
            primary_result = replay
            primary_candidates = candidates
            primary_market = market

    assert primary_result is not None
    assert primary_candidates is not None
    assert primary_market is not None
    invariant = len({
        (
            row["production_status"],
            json.dumps(row["production_trade"], sort_keys=True, default=str),
            row["confirmation_status"],
            json.dumps(row["confirmation_trade"], sort_keys=True, default=str),
        )
        for row in variants
    }) == 1
    source_errors = int(early_manifest["event_counts"].get("callback_errors", 0)) + int(
        late_manifest["event_counts"].get("callback_errors", 0)
    )
    primary_summary = _decision_summary(primary_result["decision_diagnostics"])
    expected_symbols = {
        str(row["stock_id"])
        for row in watchlist.get("stocks", [])
    }
    missing_symbols = sorted(expected_symbols - set(primary_candidates))
    report = {
        "analysis_id": ANALYSIS_ID,
        "session_date": session_date,
        "signal_date": early_manifest["signal_date"],
        "stage_a_seal_hash": early_manifest["stage_a_seal_hash"],
        "paper_contract_hash": PAPER_CONTRACT_HASH,
        "capital_twd": capital_twd,
        "current_strategy": {
            "entry_policy": ANTI_CHASE_ENTRY_POLICY,
            "exit_policy": LIVE_EXIT_POLICY,
            "market_regime_policy": LONG_MARKET_REGIME_POLICY,
            "direction": "LONG_ONLY",
        },
        "source_runs": [
            {
                "role": "EARLY_SESSION",
                "run_id": early_manifest["run_id"],
                "manifest_hash": early_manifest["manifest_hash"],
                "started_at": early_manifest["started_at"],
                "ended_at": early_manifest["ended_at"],
                "callback_errors": early_manifest["event_counts"].get("callback_errors", 0),
            },
            {
                "role": "LATE_SESSION",
                "run_id": late_manifest["run_id"],
                "manifest_hash": late_manifest["manifest_hash"],
                "started_at": late_manifest["started_at"],
                "ended_at": late_manifest["ended_at"],
                "callback_errors": late_manifest["event_counts"].get("callback_errors", 0),
            },
        ],
        "data_quality": {
            "status": "DIAGNOSTIC_ONLY_NOT_CERTIFIED_FULL_SESSION",
            "same_stage_a_seal": True,
            "source_callback_errors": source_errors,
            "callback_error_limitation": (
                "early source callback errors are aggregate and untimestamped; "
                "the stitched replay cannot prove they occurred outside the selected interval"
            ),
            "benchmark_0050": _benchmark_coverage(primary_market),
            "watchlist_symbol_count": len(expected_symbols),
            "candidate_symbol_count": len(primary_candidates),
            "candidate_symbols_without_executable_quotes": missing_symbols,
            "non_executable_quote_handling": (
                "symbols without a positive bid and ask are excluded exactly as in the live engine"
            ),
        },
        "result": {
            "production_status": primary_result["reason"],
            "production_trade": primary_result["trade"],
            "production_net_pnl_twd": (
                primary_result["trade"]["net_pnl"] if primary_result["trade"] else 0
            ),
            "confirmation_status": primary_result["confirmation_reason"],
            "confirmation_trade": primary_result["confirmation_trade"],
            "confirmation_net_pnl_twd": (
                primary_result["confirmation_trade"]["net_pnl"]
                if primary_result["confirmation_trade"] else 0
            ),
            **primary_summary,
        },
        "cutover_sensitivity": {
            "result_invariant": invariant,
            "variants": variants,
        },
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "interpretation": "BACKTEST_ONLY_CURRENT_STRATEGY_DIAGNOSTIC_NOT_LIVE_EXECUTION",
    }
    report["report_hash"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


def markdown_report(report: dict[str, Any]) -> str:
    result = report["result"]
    quality = report["data_quality"]
    lines = [
        "# 2026-09-30 stitched current-strategy backtest",
        "",
        f"- Status: `{quality['status']}`",
        f"- Stage A seal: `{report['stage_a_seal_hash']}`",
        f"- Current paper contract: `{report['paper_contract_hash']}`",
        f"- Entry policy: `{report['current_strategy']['entry_policy']['policy_id']}`",
        f"- Exit policy: `{report['current_strategy']['exit_policy']['policy_id']}`",
        f"- Market policy: `{report['current_strategy']['market_regime_policy']['policy_id']}`",
        f"- Production track: `{result['production_status']}`",
        f"- Production net PnL: NT${result['production_net_pnl_twd']}",
        f"- 60-second confirmation track: `{result['confirmation_status']}`",
        f"- 60-second confirmation net PnL: NT${result['confirmation_net_pnl_twd']}",
        f"- Decision windows: {result['decision_windows']}",
        f"- Cutover sensitivity invariant: {report['cutover_sensitivity']['result_invariant']}",
        "",
        "## Decision summary",
        "",
    ]
    for key, value in result["decisions"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Near-miss gates", ""])
    for key, value in result["near_miss_gate_counts"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Near-miss details", ""])
    lines.append("| Time | Stock | Score | Gate |")
    lines.append("|---|---|---:|---|")
    for row in result["near_misses"]:
        time_text = str(row["decision_time"] or "").split("T")[-1].split("+")[0]
        lines.append(
            f"| {time_text} | {row['stock_id']} {row.get('stock_name') or ''} "
            f"| {float(row.get('score') or 0):.4f} | {row['gate_reason']} |"
        )
    lines.extend(
        [
            "",
            "## Limitation",
            "",
            f"- Source callback errors: {quality['source_callback_errors']}",
            "- 8227 had no positive executable bid/ask pair and was therefore excluded by the live-engine quote rules.",
            f"- {quality['callback_error_limitation']}",
            "- This is a backtest-only reconstruction. It did not connect to a broker or submit orders.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay the current strategy on stitched archives")
    parser.add_argument("--early-run", required=True, type=Path)
    parser.add_argument("--late-run", required=True, type=Path)
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    report = run_backtest(args.early_run, args.late_run, capital_twd=args.capital)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(rendered, encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown_report(report), encoding="utf-8")
    if not args.json_output:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
