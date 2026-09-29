"""Diagnostic anti-chase filters using only information known at entry time."""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Mapping

from signal_quality_diagnostics_v01.analysis import build_report as build_quality_report
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file


ANALYSIS_ID = "ANTI_CHASE_DIAGNOSTICS_V0_1"


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    family: str
    description: str
    keep: Callable[[dict[str, Any]], bool]
    posthoc_reference: bool = False


def _maximum(name: str, family: str, field: str, maximum: float) -> Gate:
    return Gate(
        name,
        family,
        f"keep only when {field} <= {maximum}",
        lambda row: row.get(field) is not None and float(row[field]) <= maximum,
    )


def _at_most(row: dict[str, Any], field: str, maximum: float) -> bool:
    value = row.get(field)
    return value is not None and math.isfinite(float(value)) and float(value) <= maximum


GATES = (
    *(
        _maximum(f"VWAP_MAX_{int(value * 10000)}BP", "VWAP_EXTENSION", "directional_vwap_extension", value)
        for value in (0.0075, 0.0100, 0.0125, 0.0150, 0.0200)
    ),
    *(
        _maximum(f"RETURN_5M_MAX_{int(value * 100)}PCT", "FIVE_MINUTE_EXTENSION", "directional_return_300s", value)
        for value in (0.02, 0.03, 0.04, 0.05)
    ),
    *(
        _maximum(f"RETURN_1M_MAX_{int(value * 10000)}BP", "ONE_MINUTE_EXTENSION", "directional_return_60s", value)
        for value in (0.0075, 0.0100, 0.0150, 0.0200)
    ),
    *(
        _maximum(f"BREAKOUT_MAX_{int(value * 10000)}BP", "BREAKOUT_OVERSHOOT", "breakout_overshoot", value)
        for value in (0.0025, 0.0050, 0.0075, 0.0100)
    ),
    *(
        _maximum(f"OPEN_MAX_{int(value * 100)}PCT", "OPENING_EXTENSION", "directional_opening_extension", value)
        for value in (0.02, 0.03, 0.05)
    ),
    *(
        _maximum(f"VOLUME_STRENGTH_MAX_{str(value).replace('.', '_')}", "FLOW_SATURATION", "volume_strength", value)
        for value in (0.80, 0.90, 0.95)
    ),
    Gate(
        "ANTI_CHASE_LOOSE_V1", "OPEN_AND_VWAP", "opening extension<=3% and VWAP extension<=1.5%",
        lambda row: all((
            _at_most(row, "directional_opening_extension", 0.030),
            _at_most(row, "directional_vwap_extension", 0.015),
        )),
    ),
    Gate(
        "ANTI_CHASE_BALANCED_V1", "OPEN_AND_VWAP", "opening extension<=2% and VWAP extension<=1.25%",
        lambda row: all((
            _at_most(row, "directional_opening_extension", 0.020),
            _at_most(row, "directional_vwap_extension", 0.0125),
        )),
    ),
    Gate(
        "ANTI_CHASE_STRICT_V1", "OPEN_AND_VWAP", "opening extension<=2% and VWAP extension<=1%",
        lambda row: all((
            _at_most(row, "directional_opening_extension", 0.020),
            _at_most(row, "directional_vwap_extension", 0.010),
        )),
    ),
    Gate(
        "EXTENSION_LOOSE_V1", "COMPOSITE", "VWAP<=2%, 5m<=4%, 1m<=2%, breakout<=1%",
        lambda row: all((
            _at_most(row, "directional_vwap_extension", 0.020),
            _at_most(row, "directional_return_300s", 0.040),
            _at_most(row, "directional_return_60s", 0.020),
            _at_most(row, "breakout_overshoot", 0.010),
        )),
    ),
    Gate(
        "EXTENSION_BALANCED_V1", "COMPOSITE", "VWAP<=1.5%, 5m<=3%, 1m<=1.5%, breakout<=0.75%",
        lambda row: all((
            _at_most(row, "directional_vwap_extension", 0.015),
            _at_most(row, "directional_return_300s", 0.030),
            _at_most(row, "directional_return_60s", 0.015),
            _at_most(row, "breakout_overshoot", 0.0075),
        )),
    ),
    Gate(
        "EXTENSION_STRICT_V1", "COMPOSITE", "VWAP<=1%, 5m<=2%, 1m<=1%, breakout<=0.5%",
        lambda row: all((
            _at_most(row, "directional_vwap_extension", 0.010),
            _at_most(row, "directional_return_300s", 0.020),
            _at_most(row, "directional_return_60s", 0.010),
            _at_most(row, "breakout_overshoot", 0.005),
        )),
    ),
    Gate(
        "SCORE_LT_0_70_POSTHOC_REFERENCE",
        "POSTHOC_SCORE_REFERENCE",
        "reference only: score < 0.70, discovered in the same sample",
        lambda row: float(row["score"]) < 0.70,
        posthoc_reference=True,
    ),
)


def _profit_factor(rows: list[dict[str, Any]]) -> float | None:
    gross_profit = sum(max(0.0, float(row["realized_net_pnl"])) for row in rows)
    gross_loss = -sum(min(0.0, float(row["realized_net_pnl"])) for row in rows)
    return gross_profit / gross_loss if gross_loss else None


def evaluate_gate(
    rows: list[dict[str, Any]], gate: Gate,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scored = [row for row in rows if row["outcome"] in {"WINNER", "LOSER"}]
    kept = [row for row in scored if gate.keep(row)]
    removed = [row for row in scored if not gate.keep(row)]
    baseline_net = sum(float(row["realized_net_pnl"]) for row in scored)
    kept_net = sum(float(row["realized_net_pnl"]) for row in kept)
    removed_winners = [row for row in removed if row["outcome"] == "WINNER"]
    removed_losers = [row for row in removed if row["outcome"] == "LOSER"]
    extension_fields = (
        "directional_vwap_extension", "directional_return_60s",
        "directional_return_300s", "breakout_overshoot",
    )
    removed_with_missing_extension_data = [
        row for row in removed
        if any(row.get(field) is None for field in extension_fields)
    ]
    loo_deltas = []
    for omitted in scored:
        remaining = [row for row in scored if row is not omitted]
        baseline = sum(float(row["realized_net_pnl"]) for row in remaining)
        filtered = sum(
            float(row["realized_net_pnl"]) for row in remaining if gate.keep(row)
        )
        loo_deltas.append(filtered - baseline)
    session_deltas = []
    for date in sorted({str(row["session_date"]) for row in scored}):
        session = [row for row in scored if str(row["session_date"]) == date]
        original = sum(float(row["realized_net_pnl"]) for row in session)
        filtered = sum(float(row["realized_net_pnl"]) for row in session if gate.keep(row))
        session_deltas.append(filtered - original)
    metrics = {
        "gate": gate.name,
        "family": gate.family,
        "description": gate.description,
        "posthoc_reference": gate.posthoc_reference,
        "baseline_trades": len(scored),
        "kept_trades": len(kept),
        "removed_trades": len(removed),
        "kept_winners": sum(row["outcome"] == "WINNER" for row in kept),
        "kept_losers": sum(row["outcome"] == "LOSER" for row in kept),
        "avoided_losers": len(removed_losers),
        "removed_winners": len(removed_winners),
        "removed_with_missing_extension_data": len(removed_with_missing_extension_data),
        "removed_trade_loser_rate": len(removed_losers) / len(removed) if removed else None,
        "baseline_net_pnl": baseline_net,
        "filtered_net_pnl": kept_net,
        "net_pnl_difference": kept_net - baseline_net,
        "saved_loser_pnl": -sum(float(row["realized_net_pnl"]) for row in removed_losers),
        "lost_winner_pnl": sum(float(row["realized_net_pnl"]) for row in removed_winners),
        "filtered_average_pnl": mean(float(row["realized_net_pnl"]) for row in kept) if kept else None,
        "filtered_win_rate": (
            sum(row["outcome"] == "WINNER" for row in kept) / len(kept) if kept else None
        ),
        "baseline_profit_factor": _profit_factor(scored),
        "filtered_profit_factor": _profit_factor(kept),
        "leave_one_out_delta_min": min(loo_deltas) if loo_deltas else None,
        "leave_one_out_delta_max": max(loo_deltas) if loo_deltas else None,
        "leave_one_out_positive": bool(loo_deltas and min(loo_deltas) > 0),
        "sessions_improved": sum(value > 0 for value in session_deltas),
        "sessions_harmed": sum(value < 0 for value in session_deltas),
        "shadow_candidate": bool(
            not gate.posthoc_reference
            and len(removed_winners) == 0
            and len(removed_losers) >= 2
            and loo_deltas
            and min(loo_deltas) > 0
        ),
    }
    impacts = [{
        "gate": gate.name,
        "trade_id": row["trade_id"],
        "session_date": row["session_date"],
        "symbol": row["symbol"],
        "stock_name": row["stock_name"],
        "side": row["side"],
        "outcome": row["outcome"],
        "score": row["score"],
        "directional_vwap_extension": row["directional_vwap_extension"],
        "directional_return_60s": row["directional_return_60s"],
        "directional_return_300s": row["directional_return_300s"],
        "directional_opening_extension": row["directional_opening_extension"],
        "breakout_overshoot": row["breakout_overshoot"],
        "volume_strength": row["volume_strength"],
        "realized_net_pnl": row["realized_net_pnl"],
        "decision": "KEEP" if gate.keep(row) else "SKIP_AS_OVEREXTENDED",
        "extension_data_complete": all(
            row.get(field) is not None for field in extension_fields
        ),
    } for row in scored]
    return metrics, impacts


def build_report(
    session_runs: Mapping[str, list[Path]], capital: int = 190_000,
) -> dict[str, Any]:
    quality = build_quality_report(session_runs, capital)
    metrics, impacts = [], []
    for gate in GATES:
        row, trade_impacts = evaluate_gate(quality["per_trade"], gate)
        metrics.append(row)
        impacts.extend(trade_impacts)
    ranked = sorted(
        metrics,
        key=lambda row: (
            bool(row["posthoc_reference"]),
            int(row["removed_winners"]),
            -int(row["avoided_losers"]),
            -float(row["net_pnl_difference"]),
        ),
    )
    return {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "RETROSPECTIVE_FILTER_DIAGNOSTIC_ONLY_NOT_A_PRODUCTION_RULE",
        "trade_universe": quality["trade_universe"],
        "outcome_policy": quality["outcome_policy"],
        "capital_twd": capital,
        "baseline_counts": quality["counts"],
        "baseline_net_pnl": sum(
            float(row["realized_net_pnl"])
            for row in quality["per_trade"]
            if row["outcome"] in {"WINNER", "LOSER"}
        ),
        "gate_results": metrics,
        "ranked_results": ranked,
        "trade_impacts": impacts,
        "coverage": quality["coverage"],
        "limitations": [
            "17 scorable independent signals across only three archived sessions",
            "all source sessions are PARTIAL_SESSION; 2026-09-23 has callback errors",
            "independent signals overlap and are not an executable portfolio",
            "all thresholds are in-sample diagnostics; no production choice is authorized",
        ],
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "strategy_changed": False,
        "live_behavior_changed": False,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _fmt(value: Any, digits: int = 0) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "N/A"
    return f"{float(value):,.{digits}f}"


def _markdown(report: dict[str, Any]) -> str:
    rows = report["ranked_results"]
    lines = [
        "# Anti-chase diagnostics",
        "",
        "Backtest-only entry-filter diagnostics using information available at the signal time. No strategy or live behavior was changed.",
        "",
        f"Baseline scorable trades: {report['baseline_counts']['scored']}; baseline net PnL: {_fmt(report['baseline_net_pnl'])} TWD.",
        "",
        "| Gate | Kept | Avoided losers | Removed winners | Filtered net | Delta | Win rate | PF | LOO min | Shadow candidate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        win_rate = "N/A" if row["filtered_win_rate"] is None else f"{row['filtered_win_rate'] * 100:.1f}%"
        lines.append(
            f"| {row['gate']} | {row['kept_trades']} | {row['avoided_losers']} | "
            f"{row['removed_winners']} | {_fmt(row['filtered_net_pnl'])} | "
            f"{_fmt(row['net_pnl_difference'])} | {win_rate} | "
            f"{_fmt(row['filtered_profit_factor'], 2)} | {_fmt(row['leave_one_out_delta_min'])} | "
            f"{row['shadow_candidate']} |"
        )
    candidates = [row for row in rows if row["shadow_candidate"]]
    lines.extend(["", "## Interpretation", ""])
    if candidates:
        best = candidates[0]
        lines.append(
            f"The strongest shadow-only candidate under the predeclared robustness ordering is {best['gate']}: "
            f"it avoided {best['avoided_losers']} losers, removed no winners, and improved diagnostic net PnL by "
            f"{_fmt(best['net_pnl_difference'])} TWD. This is not production validation."
        )
    else:
        lines.append("No gate met the minimum shadow-candidate robustness conditions.")
    lines.extend([
        "",
        "The score<0.70 result is included only as a post-hoc reference and is ineligible for recommendation.",
        "All source sessions are partial, and one session has callback errors. Collect full sessions before any production change.",
        "",
    ])
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "report.md").write_text(_markdown(report), encoding="utf-8")
    _write_csv(output_dir / "anti_chase_grid.csv", report["gate_results"])
    _write_csv(output_dir / "anti_chase_ranked.csv", report["ranked_results"])
    _write_csv(output_dir / "anti_chase_trade_impacts.csv", report["trade_impacts"])
    artifacts = {
        path.name: sha256_file(path)
        for path in sorted(output_dir.iterdir())
        if path.is_file() and path.name != "run_manifest.json"
    }
    manifest = {
        "analysis_id": ANALYSIS_ID,
        "artifact_hashes": artifacts,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "strategy_changed": False,
        "live_behavior_changed": False,
    }
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
