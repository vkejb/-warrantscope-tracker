from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from paper_shadow_v01.runner import _load_market_context, _jsonable, replay_paper_session
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC,
    _decision_times,
    load_session,
)
from yuanta_intraday_shadow_v01.live_parity_backtest import (
    _feed_until,
    _parse_stamp,
)
from yuanta_live_runtime_v01.strategy import (
    LONG_MARKET_REGIME_POLICY,
    LiveDirectionEngine,
)

from .analysis import _manifest, _validate_pair, stitch_streams
from .near_miss_counterfactual import (
    discover_near_misses,
    replay_independent_entry,
)


TAIPEI = ZoneInfo("Asia/Taipei")
ANALYSIS_ID = "RELATIVE_STRENGTH_GATE_CONTROLLED_STUDY_V0_1"
THRESHOLD_KEYS = (
    "bullish_min_relative_strength",
    "neutral_min_relative_strength",
    "bearish_min_relative_strength",
)


@contextmanager
def relative_strength_gate(enabled: bool) -> Iterator[None]:
    """Backtest-only policy override that always restores production values."""
    original = {key: LONG_MARKET_REGIME_POLICY[key] for key in THRESHOLD_KEYS}
    if not enabled:
        for key in THRESHOLD_KEYS:
            LONG_MARKET_REGIME_POLICY[key] = -1_000_000.0
    try:
        yield
    finally:
        LONG_MARKET_REGIME_POLICY.update(original)


def _trade_summary(replay: dict[str, Any]) -> dict[str, Any]:
    trade = replay.get("trade")
    return {
        "status": replay.get("reason"),
        "stock_id": trade.get("stock_id") if trade else None,
        "stock_name": trade.get("stock_name") if trade else None,
        "entry_time": trade.get("entry_time") if trade else None,
        "exit_time": trade.get("exit_time") if trade else None,
        "exit_reason": trade.get("exit_reason") if trade else None,
        "net_pnl_twd": float(trade.get("net_pnl", 0)) if trade else 0.0,
    }


def _scan_gates(
    candidates: dict[str, dict[str, Any]],
    market: dict[str, dict[str, Any]],
    session_date: str,
    *,
    capital_twd: int,
    rs_enabled: bool,
) -> dict[tuple[str, str], dict[str, Any]]:
    combined = {**candidates, **market}
    metadata = {
        symbol: str(data.get("meta", {}).get("stock_name", symbol))
        for symbol, data in combined.items()
    }
    engine = LiveDirectionEngine(
        metadata,
        capital_twd=capital_twd,
        candidate_symbols=set(candidates),
        benchmark_symbol=str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"]),
    )
    tick_indexes = {symbol: 0 for symbol in combined}
    book_indexes = {symbol: 0 for symbol in combined}
    output: dict[tuple[str, str], dict[str, Any]] = {}
    with relative_strength_gate(rs_enabled):
        for decision in _decision_times(
            session_date, SPEC["entry_start"], SPEC["last_entry_time"]
        ):
            _feed_until(engine, combined, tick_indexes, book_indexes, decision)
            selected = engine.choose_entry(decision, allow_short=False)
            for row in engine.last_entry_diagnostics.get("candidates", []):
                key = (decision.isoformat(), str(row["stock_id"]))
                output[key] = {
                    "decision_time": decision.isoformat(),
                    "stock_id": str(row["stock_id"]),
                    "stock_name": row.get("stock_name"),
                    "gate_reason": row.get("gate_reason"),
                    "selected": selected is not None and selected.stock_id == row["stock_id"],
                    "streak": row.get("streak"),
                    "required_confirmations": row.get("required_confirmations", 1),
                    "relative_strength_5m": row.get("relative_strength_5m"),
                    "opening_extension": row.get("directional_opening_extension"),
                    "vwap_extension": row.get("directional_vwap_extension"),
                }
    return output


def analyze_session(
    *,
    session_date: str,
    label: str,
    candidates: dict[str, dict[str, Any]],
    market: dict[str, dict[str, Any]],
    coverage: dict[str, Any],
    capital_twd: int,
    source_quality: str,
) -> dict[str, Any]:
    with relative_strength_gate(True):
        baseline = replay_paper_session(
            candidates, market, coverage, capital_twd=capital_twd
        )
        rejected = [
            row
            for row in discover_near_misses(
                candidates, market, session_date, capital_twd=capital_twd
            )
            if row["gate_reason"] == "RELATIVE_STRENGTH_BELOW_THRESHOLD"
        ]
    with relative_strength_gate(False):
        no_rs = replay_paper_session(
            candidates, market, coverage, capital_twd=capital_twd
        )
    baseline_scan = _scan_gates(
        candidates, market, session_date,
        capital_twd=capital_twd, rs_enabled=True,
    )
    no_rs_scan = _scan_gates(
        candidates, market, session_date,
        capital_twd=capital_twd, rs_enabled=False,
    )
    forced_paths = [
        replay_independent_entry(
            candidates,
            market,
            _parse_stamp(coverage["ended_at_taipei"]),
            row,
            capital_twd=capital_twd,
        )
        for row in rejected
    ]
    transitions = []
    marginally_eligible = []
    for key, before in baseline_scan.items():
        if before["gate_reason"] != "RELATIVE_STRENGTH_BELOW_THRESHOLD":
            continue
        after = no_rs_scan.get(key)
        transition = {
            **before,
            "without_rs_gate_reason": after.get("gate_reason") if after else None,
            "without_rs_selected": bool(after and after.get("selected")),
        }
        transitions.append(transition)
        if after and after.get("gate_reason") in {
            "BASE_SIGNAL_CONFIRMED", "MARKET_GATE_PASSED"
        }:
            marginally_eligible.append(transition)
    base = _trade_summary(baseline)
    disabled = _trade_summary(no_rs)
    return {
        "session_date": session_date,
        "label": label,
        "source_quality": source_quality,
        "baseline": base,
        "relative_strength_disabled": disabled,
        "realized_pnl_difference_twd": round(
            disabled["net_pnl_twd"] - base["net_pnl_twd"], 2
        ),
        "relative_strength_rejections": len(transitions),
        "marginally_eligible_without_rs": len(marginally_eligible),
        "gate_transitions": transitions,
        "rejected_signal_forced_paths": forced_paths,
    }


def _direct_session(run_dir: Path, capital_twd: int) -> dict[str, Any]:
    manifest = _manifest(run_dir)
    candidates, coverage = load_session([run_dir])
    market = _load_market_context(run_dir, manifest)
    return analyze_session(
        session_date=str(coverage["session_date"]),
        label=manifest["run_id"],
        candidates=candidates,
        market=market,
        coverage=coverage,
        capital_twd=capital_twd,
        source_quality=str(coverage.get("coverage_status") or manifest["status"]),
    )


def _stitched_session(early_run: Path, late_run: Path, capital_twd: int) -> dict[str, Any]:
    early_manifest, late_manifest = _manifest(early_run), _manifest(late_run)
    _validate_pair(early_manifest, late_manifest)
    early_candidates, early_coverage = load_session([early_run])
    late_candidates, late_coverage = load_session([late_run])
    early_market = _load_market_context(early_run, early_manifest)
    late_market = _load_market_context(late_run, late_manifest)
    session_date = str(late_coverage["session_date"])
    cutover = datetime.strptime(
        f"{session_date} 09:27:15.505", "%Y%m%d %H:%M:%S.%f"
    ).replace(tzinfo=TAIPEI)
    candidates = stitch_streams(early_candidates, late_candidates, cutover)
    market = stitch_streams(early_market, late_market, cutover)
    coverage = {
        "session_date": session_date,
        "source_statuses": ["COMPLETE", "COMPLETE"],
        "started_at_taipei": early_coverage["started_at_taipei"],
        "ended_at_taipei": late_coverage["ended_at_taipei"],
        "callback_errors": 0,
    }
    return analyze_session(
        session_date=session_date,
        label=f"{early_manifest['run_id']}+{late_manifest['run_id']}",
        candidates=candidates,
        market=market,
        coverage=coverage,
        capital_twd=capital_twd,
        source_quality="DIAGNOSTIC_STITCH_WITH_4_UNTIMESTAMPED_CALLBACK_ERRORS",
    )


def _cluster_forced_paths(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate repeated same-symbol signals inside a 15-minute episode."""
    rows = sorted(
        [
            {"session_date": session["session_date"], **row}
            for session in sessions
            for row in session["rejected_signal_forced_paths"]
            if row.get("scorable")
        ],
        key=lambda row: (row["session_date"], row["stock_id"], row["decision_time"]),
    )
    output = []
    last: dict[tuple[str, str], datetime] = {}
    for row in rows:
        key = (row["session_date"], row["stock_id"])
        at = datetime.fromisoformat(row["decision_time"])
        prior = last.get(key)
        if prior is not None and (at - prior).total_seconds() <= 15 * 60:
            last[key] = at
            continue
        output.append(row)
        last[key] = at
    return output


def run_study(
    direct_run: Path,
    early_run: Path,
    late_run: Path,
    *,
    capital_twd: int = 190_000,
) -> dict[str, Any]:
    sessions = [
        _direct_session(direct_run.resolve(), capital_twd),
        _stitched_session(early_run.resolve(), late_run.resolve(), capital_twd),
    ]
    baseline_total = sum(row["baseline"]["net_pnl_twd"] for row in sessions)
    disabled_total = sum(
        row["relative_strength_disabled"]["net_pnl_twd"] for row in sessions
    )
    forced = [
        row
        for session in sessions
        for row in session["rejected_signal_forced_paths"]
        if row.get("scorable")
    ]
    clustered = _cluster_forced_paths(sessions)
    report = {
        "analysis_id": ANALYSIS_ID,
        "capital_twd": capital_twd,
        "controlled_change": (
            "Set only the three regime-specific minimum relative-strength thresholds "
            "to a non-binding value. All other entry, confirmation, anti-chase, sizing, "
            "exit, fee, tax and slippage behavior remains unchanged."
        ),
        "sessions": sessions,
        "controlled_result": {
            "session_count": len(sessions),
            "baseline_net_pnl_twd": round(baseline_total, 2),
            "relative_strength_disabled_net_pnl_twd": round(disabled_total, 2),
            "net_difference_twd": round(disabled_total - baseline_total, 2),
            "relative_strength_rejections": sum(
                row["relative_strength_rejections"] for row in sessions
            ),
            "marginally_eligible_without_rs": sum(
                row["marginally_eligible_without_rs"] for row in sessions
            ),
        },
        "rejected_path_diagnostic": {
            "event_count": len(forced),
            "winning_events": sum(float(row["net_pnl_twd"]) > 0 for row in forced),
            "losing_events": sum(float(row["net_pnl_twd"]) < 0 for row in forced),
            "event_net_pnl_twd": round(sum(float(row["net_pnl_twd"]) for row in forced), 2),
            "cluster_count": len(clustered),
            "cluster_winners": sum(float(row["net_pnl_twd"]) > 0 for row in clustered),
            "cluster_losers": sum(float(row["net_pnl_twd"]) < 0 for row in clustered),
            "cluster_net_pnl_twd": round(
                sum(float(row["net_pnl_twd"]) for row in clustered), 2
            ),
            "cluster_first_signals": clustered,
            "interpretation": (
                "Forced paths bypass every remaining gate and are signal-quality diagnostics, "
                "not the causal PnL impact of disabling relative strength."
            ),
        },
        "conclusion": (
            "NO_MEASURABLE_MARGINAL_EFFECT_IN_AVAILABLE_SAMPLE"
            if abs(disabled_total - baseline_total) < 1e-9
            else "MEASURABLE_MARGINAL_EFFECT_IN_AVAILABLE_SAMPLE"
        ),
        "limitations": [
            "Only two sessions contain synchronized 0050 market context for this policy.",
            "Neither session is certified as a clean multi-day validation sample.",
            "The six rejected events include repeated signals from the same stock episode.",
            "No production strategy or live behavior was changed.",
        ],
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    report["report_hash"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


def markdown_report(report: dict[str, Any]) -> str:
    controlled = report["controlled_result"]
    diagnostic = report["rejected_path_diagnostic"]
    lines = [
        "# Relative-strength gate controlled backtest",
        "",
        "## Controlled daily comparison",
        "",
        "| Session | Source quality | Baseline trade / PnL | RS disabled trade / PnL | Difference | RS rejects | Newly eligible |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for row in report["sessions"]:
        base, disabled = row["baseline"], row["relative_strength_disabled"]
        base_trade = f"{base['stock_id'] or '-'} / {base['net_pnl_twd']:.0f}"
        disabled_trade = f"{disabled['stock_id'] or '-'} / {disabled['net_pnl_twd']:.0f}"
        lines.append(
            f"| {row['session_date']} | {row['source_quality']} | {base_trade} | "
            f"{disabled_trade} | {row['realized_pnl_difference_twd']:.0f} | "
            f"{row['relative_strength_rejections']} | {row['marginally_eligible_without_rs']} |"
        )
    lines.extend([
        "",
        f"- Baseline total: NT${controlled['baseline_net_pnl_twd']:.0f}",
        f"- RS disabled total: NT${controlled['relative_strength_disabled_net_pnl_twd']:.0f}",
        f"- Difference: NT${controlled['net_difference_twd']:.0f}",
        f"- Newly eligible after removing only RS: {controlled['marginally_eligible_without_rs']}",
        "",
        "## Rejected-path diagnostic (not causal strategy PnL)",
        "",
        f"- Raw rejected events: {diagnostic['event_count']} ({diagnostic['winning_events']} win / {diagnostic['losing_events']} loss), forced-entry sum NT${diagnostic['event_net_pnl_twd']:.0f}",
        f"- 15-minute episode clusters: {diagnostic['cluster_count']} ({diagnostic['cluster_winners']} win / {diagnostic['cluster_losers']} loss), first-signal sum NT${diagnostic['cluster_net_pnl_twd']:.0f}",
        "- These paths deliberately bypass confirmation and anti-chase gates and therefore do not measure the isolated RS gate effect.",
        "",
        "## Conclusion",
        "",
        f"`{report['conclusion']}`",
        "",
        "The available sample does not show that the RS gate changed an actual entry or realized PnL. The rejected paths lean negative, but the sample is too small and correlated to validate the gate.",
        "",
        "## Limitations",
        "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Controlled study of the 0050 relative-strength gate")
    parser.add_argument("--direct-run", required=True, type=Path)
    parser.add_argument("--early-run", required=True, type=Path)
    parser.add_argument("--late-run", required=True, type=Path)
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    report = run_study(
        args.direct_run, args.early_run, args.late_run, capital_twd=args.capital
    )
    rendered = json.dumps(_jsonable(report), ensure_ascii=False, indent=2) + "\n"
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
