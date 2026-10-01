from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, time
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from paper_shadow_v01.runner import _load_market_context, _replay_paper_track
from yuanta_intraday_shadow_v01.collector import canonical_bytes
from yuanta_intraday_shadow_v01.direction_follow_backtest import load_session
from yuanta_live_runtime_v01.strategy import (
    ANTI_CHASE_ENTRY_POLICY,
    LiveDirectionEngine,
)

from .analysis import _manifest, _validate_pair, stitch_streams


TAIPEI = ZoneInfo("Asia/Taipei")
ANALYSIS_ID = "ANTI_CHASE_SENSITIVITY_STUDY_V0_1"
OPENING_KEY = "maximum_directional_opening_extension"
VWAP_KEY = "maximum_directional_vwap_extension"
VARIANTS = (
    {
        "variant_id": "BASELINE_2_0_1_25",
        "opening_limit": 0.020,
        "vwap_limit": 0.0125,
        "exempt_until": None,
    },
    {
        "variant_id": "RELAXED_3_0_2_0",
        "opening_limit": 0.030,
        "vwap_limit": 0.020,
        "exempt_until": None,
    },
    {
        "variant_id": "RELAXED_4_0_2_5",
        "opening_limit": 0.040,
        "vwap_limit": 0.025,
        "exempt_until": None,
    },
    {
        "variant_id": "RELAXED_5_0_4_0",
        "opening_limit": 0.050,
        "vwap_limit": 0.040,
        "exempt_until": None,
    },
    {
        "variant_id": "RELAXED_6_0_5_0",
        "opening_limit": 0.060,
        "vwap_limit": 0.050,
        "exempt_until": None,
    },
    {
        "variant_id": "EARLY_EXEMPT_UNTIL_0915",
        "opening_limit": 0.020,
        "vwap_limit": 0.0125,
        "exempt_until": "09:15",
    },
    {
        "variant_id": "EARLY_EXEMPT_UNTIL_0930",
        "opening_limit": 0.020,
        "vwap_limit": 0.0125,
        "exempt_until": "09:30",
    },
    {
        "variant_id": "EARLY_EXEMPT_UNTIL_0945",
        "opening_limit": 0.020,
        "vwap_limit": 0.0125,
        "exempt_until": "09:45",
    },
    {
        "variant_id": "EARLY_EXEMPT_UNTIL_1000",
        "opening_limit": 0.020,
        "vwap_limit": 0.0125,
        "exempt_until": "10:00",
    },
)


def _clock(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def effective_limits(variant: dict[str, Any], decision: datetime) -> tuple[float, float]:
    exempt_until = variant.get("exempt_until")
    if exempt_until and decision.astimezone(TAIPEI).time() < _clock(str(exempt_until)):
        return math.inf, math.inf
    return float(variant["opening_limit"]), float(variant["vwap_limit"])


@contextmanager
def _anti_chase_limits(opening: float, vwap: float) -> Iterator[None]:
    original = (ANTI_CHASE_ENTRY_POLICY[OPENING_KEY], ANTI_CHASE_ENTRY_POLICY[VWAP_KEY])
    ANTI_CHASE_ENTRY_POLICY[OPENING_KEY] = opening
    ANTI_CHASE_ENTRY_POLICY[VWAP_KEY] = vwap
    try:
        yield
    finally:
        ANTI_CHASE_ENTRY_POLICY[OPENING_KEY] = original[0]
        ANTI_CHASE_ENTRY_POLICY[VWAP_KEY] = original[1]


class AntiChaseStudyEngine(LiveDirectionEngine):
    """Backtest-only engine overlay; production defaults remain untouched."""

    def __init__(self, *args, study_variant: dict[str, Any], **kwargs):
        super().__init__(*args, **kwargs)
        self.study_variant = dict(study_variant)

    def choose_entry(self, decision: datetime, *, allow_short: bool):
        opening, vwap = effective_limits(self.study_variant, decision)
        with _anti_chase_limits(opening, vwap):
            return super().choose_entry(decision, allow_short=allow_short)


def _engine_factory(variant: dict[str, Any]):
    def build(*args, **kwargs):
        return AntiChaseStudyEngine(*args, study_variant=variant, **kwargs)

    return build


def _load_direct(run_dir: Path) -> dict[str, Any]:
    manifest = _manifest(run_dir)
    candidates, coverage = load_session([run_dir])
    market = _load_market_context(run_dir, manifest)
    return {
        "session_date": str(coverage["session_date"]),
        "quality": (
            "COMPLETE_ZERO_CALLBACK_ERRORS"
            if manifest["status"] == "COMPLETE"
            and int(manifest["event_counts"].get("callback_errors", 0)) == 0
            else "DIAGNOSTIC_ONLY"
        ),
        "source_run_ids": [manifest["run_id"]],
        "candidates": candidates,
        "market": market,
        "coverage": coverage,
    }


def _load_stitched(early_run: Path, late_run: Path) -> dict[str, Any]:
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
    return {
        "session_date": session_date,
        "quality": "DIAGNOSTIC_STITCH_WITH_4_UNTIMESTAMPED_CALLBACK_ERRORS",
        "source_run_ids": [early_manifest["run_id"], late_manifest["run_id"]],
        "candidates": stitch_streams(early_candidates, late_candidates, cutover),
        "market": stitch_streams(early_market, late_market, cutover),
        "coverage": {
            "session_date": session_date,
            "source_statuses": ["COMPLETE", "COMPLETE"],
            "started_at_taipei": early_coverage["started_at_taipei"],
            "ended_at_taipei": late_coverage["ended_at_taipei"],
            "callback_errors": 0,
        },
    }


def _trade_row(session: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    candidate_observations = []
    gate_reason_counts: dict[str, int] = {}
    for diagnostic in result.get("decision_diagnostics", []):
        for candidate in diagnostic.get("candidates", []):
            reason = str(candidate.get("gate_reason") or "UNKNOWN")
            gate_reason_counts[reason] = gate_reason_counts.get(reason, 0) + 1
            candidate_observations.append({
                "decision_time": diagnostic.get("decision_time"),
                "stock_id": candidate.get("stock_id"),
                "gate_reason": reason,
                "streak": candidate.get("streak"),
                "required_confirmations": candidate.get("required_confirmations"),
                "directional_opening_extension": candidate.get(
                    "directional_opening_extension"
                ),
                "directional_vwap_extension": candidate.get(
                    "directional_vwap_extension"
                ),
            })
    trade = result.get("trade")
    if trade is None:
        return {
            "session_date": session["session_date"],
            "quality": session["quality"],
            "source_run_ids": session["source_run_ids"],
            "status": result["reason"],
            "stock_id": None,
            "stock_name": None,
            "entry_time": None,
            "entry_price": None,
            "quantity": 0,
            "exit_time": None,
            "exit_price": None,
            "exit_reason": None,
            "net_pnl_twd": 0.0,
            "gate_reason_counts": gate_reason_counts,
            "candidate_observations": candidate_observations,
        }
    return {
        "session_date": session["session_date"],
        "quality": session["quality"],
        "source_run_ids": session["source_run_ids"],
        "status": result["reason"],
        "stock_id": trade["stock_id"],
        "stock_name": trade["stock_name"],
        "entry_time": trade["entry_time"],
        "entry_price": trade["entry_price"],
        "quantity": trade["quantity"],
        "exit_time": trade["exit_time"],
        "exit_price": trade["exit_price"],
        "exit_reason": trade["exit_reason"],
        "net_pnl_twd": trade["net_pnl"],
        "gate_reason_counts": gate_reason_counts,
        "candidate_observations": candidate_observations,
    }


def _metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = [float(row["net_pnl_twd"]) for row in trades if row["stock_id"]]
    winners = [value for value in pnl if value > 0]
    losers = [value for value in pnl if value < 0]
    gross_profit = sum(winners)
    gross_loss = sum(losers)
    return {
        "session_count": len(trades),
        "trade_count": len(pnl),
        "winning_trades": len(winners),
        "losing_trades": len(losers),
        "win_rate": len(winners) / len(pnl) if pnl else None,
        "gross_profit_twd": round(gross_profit, 2),
        "gross_loss_twd": round(gross_loss, 2),
        "net_pnl_twd": round(sum(pnl), 2),
        "average_pnl_per_trade_twd": round(sum(pnl) / len(pnl), 2) if pnl else None,
        "profit_factor": (
            round(gross_profit / abs(gross_loss), 6)
            if gross_loss < 0 else (None if not winners else "INFINITE")
        ),
    }


def run_study(
    run_20260929: Path,
    early_20260930: Path,
    late_20260930: Path,
    run_20261001: Path,
    *,
    capital_twd: int = 190_000,
) -> dict[str, Any]:
    sessions = [
        _load_direct(run_20260929.resolve()),
        _load_stitched(early_20260930.resolve(), late_20260930.resolve()),
        _load_direct(run_20261001.resolve()),
    ]
    results = []
    for variant in VARIANTS:
        rows = []
        for session in sessions:
            result = _replay_paper_track(
                session["candidates"],
                session["market"],
                session["coverage"],
                capital_twd=capital_twd,
                confirmation_60s=False,
                engine_factory=_engine_factory(variant),
            )
            rows.append(_trade_row(session, result))
        results.append({
            **variant,
            "trades": rows,
            "metrics": _metrics(rows),
        })
    baseline = results[0]["metrics"]["net_pnl_twd"]
    for result in results:
        result["metrics"]["net_pnl_difference_vs_baseline_twd"] = round(
            float(result["metrics"]["net_pnl_twd"]) - float(baseline), 2
        )
    report = {
        "analysis_id": ANALYSIS_ID,
        "capital_twd": capital_twd,
        "session_dates": [row["session_date"] for row in sessions],
        "controlled_constants": [
            "stock selection", "base signal", "0050 relative-strength gate",
            "confirmation counts", "position sizing", "exit policy",
            "fees", "tax", "slippage proxy",
        ],
        "variants": results,
        "sample_warning": (
            "Only three synchronized-0050 sessions are available; one is a "
            "diagnostic stitch. Results are descriptive and not production-ready."
        ),
        "production_behavior_changed": False,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    report["report_hash"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Anti-chase sensitivity study",
        "",
        "All variants keep the same stock universe, base signal, 0050 gate, confirmations, sizing, exits and costs.",
        "",
        "| Variant | Trades | W/L | Win rate | Net PnL | Avg/trade | PF | vs baseline |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["variants"]:
        m = row["metrics"]
        win_rate = "-" if m["win_rate"] is None else f"{100*m['win_rate']:.1f}%"
        pf = "-" if m["profit_factor"] is None else str(m["profit_factor"])
        avg = "-" if m["average_pnl_per_trade_twd"] is None else f"{m['average_pnl_per_trade_twd']:.0f}"
        lines.append(
            f"| {row['variant_id']} | {m['trade_count']} | {m['winning_trades']}/{m['losing_trades']} | "
            f"{win_rate} | {m['net_pnl_twd']:.0f} | {avg} | {pf} | "
            f"{m['net_pnl_difference_vs_baseline_twd']:+.0f} |"
        )
    lines.extend(["", "## Per-session trades", ""])
    lines.append("| Variant | Date | Trade | Entry | Exit | Reason | Net PnL |")
    lines.append("|---|---|---|---:|---:|---|---:|")
    for result in report["variants"]:
        for trade in result["trades"]:
            stock = f"{trade['stock_id']} {trade['stock_name']}" if trade["stock_id"] else "No trade"
            entry = "-" if trade["entry_price"] is None else f"{trade['entry_price']:.2f}"
            exit_price = "-" if trade["exit_price"] is None else f"{trade['exit_price']:.2f}"
            lines.append(
                f"| {result['variant_id']} | {trade['session_date']} | {stock} | {entry} | "
                f"{exit_price} | {trade['exit_reason'] or trade['status']} | {trade['net_pnl_twd']:.0f} |"
            )
    baseline = report["variants"][0]
    baseline_gates = {
        (
            trade["session_date"],
            candidate["decision_time"],
            candidate["stock_id"],
        ): candidate
        for trade in baseline["trades"]
        for candidate in trade["candidate_observations"]
    }
    changed_gates = []
    for result in report["variants"][1:]:
        for trade in result["trades"]:
            for candidate in trade["candidate_observations"]:
                key = (
                    trade["session_date"],
                    candidate["decision_time"],
                    candidate["stock_id"],
                )
                original = baseline_gates.get(key)
                if original and original["gate_reason"] != candidate["gate_reason"]:
                    changed_gates.append((result["variant_id"], trade["session_date"], original, candidate))
    if changed_gates:
        lines.extend([
            "",
            "## Changed gate outcomes",
            "",
            "This isolates candidates whose result changed specifically because of the anti-chase variant.",
            "",
            "| Variant | Date/time | Stock | Opening ext | VWAP ext | Baseline gate | New gate |",
            "|---|---|---|---:|---:|---|---|",
        ])
        for variant_id, session_date, original, candidate in changed_gates:
            stamp = str(candidate["decision_time"])[11:19]
            lines.append(
                f"| {variant_id} | {session_date} {stamp} | {candidate['stock_id']} | "
                f"{100*float(candidate['directional_opening_extension']):.2f}% | "
                f"{100*float(candidate['directional_vwap_extension']):.2f}% | "
                f"{original['gate_reason']} | {candidate['gate_reason']} |"
            )
    improved = [
        row["variant_id"] for row in report["variants"][1:]
        if row["metrics"]["net_pnl_difference_vs_baseline_twd"] > 0
    ]
    worsened = [
        row["variant_id"] for row in report["variants"][1:]
        if row["metrics"]["net_pnl_difference_vs_baseline_twd"] < 0
    ]
    unchanged = [
        row["variant_id"] for row in report["variants"][1:]
        if row["metrics"]["net_pnl_difference_vs_baseline_twd"] == 0
    ]
    lines.extend([
        "",
        "## Interpretation",
        "",
        f"- Improved variants: {', '.join(improved) if improved else 'none'}.",
        f"- Unchanged variants: {', '.join(unchanged) if unchanged else 'none'}.",
        f"- Worsened variants: {', '.join(worsened) if worsened else 'none'}.",
        "- A gate changing to CONFIRMATIONS_INCOMPLETE means relaxing anti-chase alone still did not authorize an entry.",
        "- These results do not support changing the production anti-chase rule from this sample.",
    ])
    lines.extend([
        "",
        "## Limitation",
        "",
        f"- {report['sample_warning']}",
        "- No variant was enabled in production or connected to a broker.",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest-only anti-chase sensitivity study")
    parser.add_argument("--run-20260929", required=True, type=Path)
    parser.add_argument("--early-20260930", required=True, type=Path)
    parser.add_argument("--late-20260930", required=True, type=Path)
    parser.add_argument("--run-20261001", required=True, type=Path)
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    report = run_study(
        args.run_20260929,
        args.early_20260930,
        args.late_20260930,
        args.run_20261001,
        capital_twd=args.capital,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(text, encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown_report(report), encoding="utf-8")
    if not args.json_output:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
