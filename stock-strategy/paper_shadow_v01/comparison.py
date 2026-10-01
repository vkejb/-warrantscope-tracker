"""Verified cross-day comparison for current-contract paper variants."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import mean, median
from typing import Any

from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file

from .runner import (
    DEFAULT_RUNTIME_DIR,
    PAPER_CONTRACT_HASH,
    _verify_published,
)


ANALYSIS_ID = "PAPER_SHADOW_CROSS_DAY_COMPARISON_V0_1"
REFERENCE_VARIANT = "PRODUCTION_ANTI_CHASE"
BUFFERED_VARIANTS = (
    "RECOVERY_NET_MFE_BUFFER_0_30_SHADOW",
    "RECOVERY_NET_MFE_BUFFER_0_40_SHADOW",
    "LIQUIDITY_QUALIFIED_RECOVERY_NET_MFE_0_30_SHADOW",
)
COMPATIBLE_CONTRACT_HASHES = frozenset({
    PAPER_CONTRACT_HASH,
    # Two-buffer contract used before the liquidity challenger was added.
    "ceff174a5c3ae16db529ceae9a08fd145e94e42310412c9da931b75860a27966",
})
MINIMUM_PAIRED_TRADES = 20


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _profit_factor(pnls: list[float]) -> float | str | None:
    profit = sum(value for value in pnls if value > 0)
    loss = abs(sum(value for value in pnls if value < 0))
    if loss:
        return round(profit / loss, 6)
    return "INFINITE" if profit else None


def _drawdown(rows: list[dict[str, Any]]) -> float:
    equity = peak = drawdown = 0.0
    for row in sorted(rows, key=lambda item: (item["session_date"], item["entry_time"])):
        equity += float(row["net_pnl"])
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return round(drawdown, 2)


def _metrics(variant: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(row["net_pnl"]) for row in rows]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    retentions = []
    for row in rows:
        full_mfe = max(
            float(row.get("full_path_mfe_net_pnl") or 0),
            float(row.get("mfe_net_pnl_at_exit") or 0),
            float(row.get("post_exit_best_net_pnl") or 0),
        )
        if float(row["net_pnl"]) > 0 and full_mfe > 0:
            retentions.append(float(row["net_pnl"]) / full_mfe)
    return {
        "variant": variant,
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(rows) if rows else None,
        "gross_profit_twd": round(sum(wins), 2),
        "gross_loss_twd": round(abs(sum(losses)), 2),
        "net_pnl_twd": round(sum(pnls), 2),
        "average_pnl_twd": round(mean(pnls), 2) if pnls else None,
        "average_winner_twd": round(mean(wins), 2) if wins else None,
        "maximum_winner_twd": round(max(wins), 2) if wins else None,
        "average_loser_twd": round(mean(losses), 2) if losses else None,
        "profit_factor": _profit_factor(pnls),
        "maximum_drawdown_twd": _drawdown(rows),
        "average_profit_retention_ratio": (
            round(mean(retentions), 6) if retentions else None
        ),
        "median_profit_retention_ratio": (
            round(median(retentions), 6) if retentions else None
        ),
        "negative_mfe_exits": sum(
            row.get("exit_reason") == "BUFFERED_NET_MFE_PROFIT_PROTECTION"
            and float(row["net_pnl"]) < 0 for row in rows
        ),
    }


def _numeric_pf(value: float | str | None) -> float:
    if value == "INFINITE":
        return math.inf
    return float(value or 0.0)


def build_comparison(runtime_dir: Path = DEFAULT_RUNTIME_DIR) -> dict[str, Any]:
    verified_by_day: dict[str, tuple[Path, dict[str, Any]]] = {}
    duplicate_days: set[str] = set()
    rejected = []
    for path in sorted((runtime_dir / "days").glob("*/*/manifest.json")):
        try:
            manifest = _verify_published(path.parent)
        except Exception as exc:
            rejected.append({"path": str(path), "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if manifest.get("paper_contract_hash") not in COMPATIBLE_CONTRACT_HASHES:
            continue
        day = str(manifest["session_date"])
        if day in verified_by_day:
            duplicate_days.add(day)
        else:
            verified_by_day[day] = (path.parent, manifest)
    for day in duplicate_days:
        verified_by_day.pop(day, None)

    trades = []
    for day, (root, _manifest) in sorted(verified_by_day.items()):
        for row in _read_jsonl(root / "paper_trades.jsonl"):
            if row.get("strategy_variant") in {REFERENCE_VARIANT, *BUFFERED_VARIANTS}:
                trades.append({**row, "session_date": day})

    keyed: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    for row in trades:
        key = (str(row["session_date"]), str(row["stock_id"]), str(row["entry_time"]))
        keyed.setdefault(key, {})[str(row["strategy_variant"])] = row
    paired = {
        variant: [
            (group[REFERENCE_VARIANT], group[variant])
            for group in keyed.values()
            if REFERENCE_VARIANT in group and variant in group
        ]
        for variant in BUFFERED_VARIANTS
    }
    reference_rows = [group[REFERENCE_VARIANT] for group in keyed.values() if REFERENCE_VARIANT in group]
    metrics = {REFERENCE_VARIANT: _metrics(REFERENCE_VARIANT, reference_rows)}
    pair_summaries = {}
    for variant, pairs in paired.items():
        variant_rows = [row for _reference, row in pairs]
        metrics[variant] = _metrics(variant, variant_rows)
        differences = [
            float(candidate["net_pnl"]) - float(reference["net_pnl"])
            for reference, candidate in pairs
        ]
        total = round(sum(differences), 2)
        leave_one_out = [round(total - value, 2) for value in differences]
        ref_metric = _metrics(
            f"{variant}_MATCHED_REFERENCE", [reference for reference, _candidate in pairs]
        )
        candidate_metric = metrics[variant]
        original_winner_pairs = [
            (reference, candidate)
            for reference, candidate in pairs
            if float(reference["net_pnl"]) > 0
        ]
        original_winner_reference_total = round(sum(
            float(reference["net_pnl"])
            for reference, _candidate in original_winner_pairs
        ), 2)
        original_winner_candidate_total = round(sum(
            float(candidate["net_pnl"])
            for _reference, candidate in original_winner_pairs
        ), 2)
        original_winners_made_nonpositive = sum(
            float(candidate["net_pnl"]) <= 0
            for _reference, candidate in original_winner_pairs
        )
        largest_reference_winner_preserved = True
        if original_winner_pairs:
            largest_reference, largest_candidate = max(
                original_winner_pairs,
                key=lambda pair: float(pair[0]["net_pnl"]),
            )
            largest_reference_winner_preserved = (
                float(largest_candidate["net_pnl"])
                >= float(largest_reference["net_pnl"])
            )
        reference_retention = ref_metric["average_profit_retention_ratio"]
        candidate_retention = candidate_metric["average_profit_retention_ratio"]
        retention_not_worse = (
            reference_retention is not None
            and candidate_retention is not None
            and float(candidate_retention) >= float(reference_retention)
        )
        original_winner_pnl_not_worse = (
            original_winner_candidate_total >= original_winner_reference_total
        )
        avg_loss_not_worse = (
            candidate_metric["average_loser_twd"] is None
            or ref_metric["average_loser_twd"] is None
            or float(candidate_metric["average_loser_twd"])
            >= float(ref_metric["average_loser_twd"])
        )
        evidence_gate = (
            len(pairs) >= MINIMUM_PAIRED_TRADES
            and total > 0
            and all(value > 0 for value in leave_one_out)
            and _numeric_pf(candidate_metric["profit_factor"])
            >= _numeric_pf(ref_metric["profit_factor"])
            and avg_loss_not_worse
            and candidate_metric["negative_mfe_exits"] == 0
            and original_winners_made_nonpositive == 0
            and original_winner_pnl_not_worse
            and largest_reference_winner_preserved
            and retention_not_worse
        )
        pair_summaries[variant] = {
            "paired_trades": len(pairs),
            "matched_reference_metrics": ref_metric,
            "net_difference_twd": total,
            "trades_improved": sum(value > 0 for value in differences),
            "trades_worsened": sum(value < 0 for value in differences),
            "leave_one_out_min_difference_twd": min(leave_one_out, default=0.0),
            "improvement_survives_every_leave_one_out": (
                bool(differences) and total > 0 and all(value > 0 for value in leave_one_out)
            ),
            "average_loss_not_worse": avg_loss_not_worse,
            "original_winner_count": len(original_winner_pairs),
            "original_winners_improved": sum(
                float(candidate["net_pnl"]) > float(reference["net_pnl"])
                for reference, candidate in original_winner_pairs
            ),
            "original_winners_worsened": sum(
                float(candidate["net_pnl"]) < float(reference["net_pnl"])
                for reference, candidate in original_winner_pairs
            ),
            "original_winners_made_nonpositive": original_winners_made_nonpositive,
            "original_winner_reference_pnl_twd": original_winner_reference_total,
            "original_winner_candidate_pnl_twd": original_winner_candidate_total,
            "original_winner_pnl_not_worse": original_winner_pnl_not_worse,
            "largest_reference_winner_preserved": largest_reference_winner_preserved,
            "average_profit_retention_not_worse": retention_not_worse,
            "shadow_evidence_gate_pass": evidence_gate,
        }

    minimum_pairs = min(
        (row["paired_trades"] for row in pair_summaries.values()), default=0,
    )
    report = {
        "analysis_id": ANALYSIS_ID,
        "paper_contract_hash": PAPER_CONTRACT_HASH,
        "compatible_paper_contract_hashes": sorted(COMPATIBLE_CONTRACT_HASHES),
        "status": (
            "EVALUATION_READY" if minimum_pairs >= MINIMUM_PAIRED_TRADES else "COLLECTING"
        ),
        "verified_session_days": sorted(verified_by_day),
        "verified_session_count": len(verified_by_day),
        "ambiguous_duplicate_session_days": sorted(duplicate_days),
        "rejected_manifests": rejected,
        "minimum_paired_trades_required": MINIMUM_PAIRED_TRADES,
        "shadow_evidence_gate_definition": {
            "minimum_paired_trades": MINIMUM_PAIRED_TRADES,
            "positive_total_net_difference": True,
            "positive_every_leave_one_trade_out_difference": True,
            "profit_factor_not_worse": True,
            "average_loss_not_worse": True,
            "negative_mfe_exits_allowed": 0,
            "original_winners_made_nonpositive_allowed": 0,
            "original_winner_total_pnl_not_worse": True,
            "largest_reference_winner_not_worse": True,
            "average_full_path_mfe_retention_not_worse": True,
        },
        "metrics": metrics,
        "paired_comparisons": pair_summaries,
        "production_change_authorized": False,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    report["report_hash"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


def write_comparison(runtime_dir: Path = DEFAULT_RUNTIME_DIR) -> dict[str, Any]:
    report = build_comparison(runtime_dir)
    target_dir = runtime_dir / "comparison"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "latest.json"
    temporary = target_dir / f".latest.tmp-{os.getpid()}"
    temporary.write_bytes(canonical_bytes(report) + b"\n")
    os.replace(temporary, target)
    if sha256_file(target) != hashlib.sha256(canonical_bytes(report) + b"\n").hexdigest():
        raise RuntimeError("paper comparison write verification failed")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    args = parser.parse_args(argv)
    print(json.dumps(write_comparison(args.runtime_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
