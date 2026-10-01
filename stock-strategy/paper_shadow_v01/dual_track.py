"""Read-only A/B report for production entry with two fixed exit tracks."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from exit_profit_research_v01.readonly_acceptance import inspect_run
from yuanta_intraday_shadow_v01.collector import (
    DEFAULT_RUNTIME_DIR as DEFAULT_SOURCE_RUNTIME_DIR,
    canonical_bytes,
    sha256_file,
)
from yuanta_live_runtime_v01.strategy import LIVE_EXIT_POLICY

from .buffered_exit import POLICIES as BUFFERED_EXIT_POLICIES
from .runner import DEFAULT_RUNTIME_DIR, PAPER_CONTRACT_HASH, _verify_published


PRODUCTION = "PRODUCTION_ANTI_CHASE"
REFERENCE = "RECOVERY_NET_MFE_BUFFER_0_30_SHADOW"
DEFAULT_OUTPUT = DEFAULT_RUNTIME_DIR / "reports" / "fixed_dual_exit_latest.json"


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


REFERENCE_POLICY = next(
    asdict(policy) for policy in BUFFERED_EXIT_POLICIES
    if policy.name == REFERENCE
)
FROZEN_CONTRACT = {
    "a_variant": PRODUCTION,
    "a_rule_source": "yuanta_live_runtime_v01.strategy.LIVE_EXIT_POLICY",
    "a_policy": LIVE_EXIT_POLICY,
    "a_policy_hash": _hash(LIVE_EXIT_POLICY),
    "b_variant": REFERENCE,
    "b_rule_source": "paper_shadow_v01.buffered_exit.POLICIES",
    "b_policy": REFERENCE_POLICY,
    "b_policy_hash": _hash(REFERENCE_POLICY),
    "paper_contract_hash": PAPER_CONTRACT_HASH,
    "same_entry_quantity_and_cost_basis_required": True,
    "b_broker_submission_allowed": False,
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def build_dual_track(
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    *,
    source_runtime_dir: Path = DEFAULT_SOURCE_RUNTIME_DIR,
) -> dict[str, Any]:
    pairs = []
    rejected = []
    unpaired_days = []
    qualification_by_session = []
    seen_days: set[str] = set()
    duplicate_days: set[str] = set()
    verified = []
    for path in sorted((runtime_dir / "days").glob("*/*/manifest.json")):
        try:
            manifest = _verify_published(path.parent)
        except Exception as exc:
            rejected.append({"path": str(path), "reason": type(exc).__name__})
            continue
        if manifest.get("paper_contract_hash") != PAPER_CONTRACT_HASH:
            rejected.append({
                "path": str(path),
                "reason": "PAPER_CONTRACT_HASH_MISMATCH",
                "actual": manifest.get("paper_contract_hash"),
                "required": PAPER_CONTRACT_HASH,
            })
            continue
        day = str(manifest["session_date"])
        if day in seen_days:
            duplicate_days.add(day)
            continue
        seen_days.add(day)
        verified.append((day, path.parent))
    for day, root in verified:
        if day in duplicate_days:
            continue
        manifest = _verify_published(root)
        source_run_id = str(manifest.get("source_run_id") or "")
        source_run_dir = source_runtime_dir / "runs" / source_run_id
        try:
            qualification = inspect_run(source_run_dir)
            qualification_by_session.append({
                "session_date": day,
                "source_run_id": source_run_id,
                "recording_integrity_and_trust": qualification[
                    "qualification_dimensions"
                ]["recording_integrity_and_trust"],
                "decision_boundary_strategy_availability": qualification[
                    "qualification_dimensions"
                ]["decision_boundary_strategy_availability"],
                "actual_live_input_decision_parity": qualification[
                    "qualification_dimensions"
                ]["actual_live_input_decision_parity"],
            })
        except Exception as exc:
            qualification_by_session.append({
                "session_date": day,
                "source_run_id": source_run_id or None,
                "recording_integrity_and_trust": "UNKNOWN",
                "decision_boundary_strategy_availability": "UNKNOWN",
                "actual_live_input_decision_parity": "UNKNOWN",
                "reason": f"{type(exc).__name__}: {exc}",
            })
        rows = {
            str(row["strategy_variant"]): row
            for row in _read_jsonl(root / "paper_trades.jsonl")
            if row.get("strategy_variant") in {PRODUCTION, REFERENCE}
        }
        if set(rows) != {PRODUCTION, REFERENCE}:
            unpaired_days.append({
                "session_date": day,
                "reason": "MISSING_FIXED_A_OR_B_TRACK",
                "available_fixed_tracks": sorted(rows),
            })
            continue
        a, b = rows[PRODUCTION], rows[REFERENCE]
        entry_a = (a["stock_id"], a["entry_time"], float(a["entry_price"]), int(a["quantity"]))
        entry_b = (b["stock_id"], b["entry_time"], float(b["entry_price"]), int(b["quantity"]))
        if entry_a != entry_b:
            rejected.append({"session_date": day, "reason": "A_B_ENTRY_OR_QUANTITY_MISMATCH"})
            continue
        delta = float(b["net_pnl"]) - float(a["net_pnl"])
        effect = (
            "LOSS_REDUCED" if float(a["net_pnl"]) < 0 and delta > 0
            else "LOSS_WORSENED" if float(a["net_pnl"]) < 0 and delta < 0
            else "WINNER_GAIN" if float(a["net_pnl"]) > 0 and delta > 0
            else "WINNER_SACRIFICED" if float(a["net_pnl"]) > 0 and delta < 0
            else "UNCHANGED"
        )
        pairs.append({
            "session_date": day, "stock_id": a["stock_id"],
            "entry_time": a["entry_time"], "entry_price": float(a["entry_price"]),
            "quantity": int(a["quantity"]),
            "a_exit_time": a["exit_time"], "a_exit_price": float(a["exit_price"]),
            "a_exit_reason": a["exit_reason"], "a_holding_seconds": float(a["holding_seconds"]),
            "a_net_pnl_twd": float(a["net_pnl"]),
            "a_mfe_twd": a.get("mfe_net_pnl_at_exit"), "a_mae_twd": a.get("mae_net_pnl_at_exit"),
            "a_trigger_to_fill_delay_seconds": a.get("trigger_to_fill_delay_seconds", 0.0),
            "a_trigger_to_fill_price_gap": a.get("trigger_to_fill_price_gap", 0.0),
            "a_post_exit_5m_scorable": a.get("post_exit_5m_scorable"),
            "a_post_exit_5m_best_net_pnl": a.get("post_exit_5m_best_net_pnl"),
            "b_exit_time": b["exit_time"], "b_exit_price": float(b["exit_price"]),
            "b_exit_reason": b["exit_reason"], "b_holding_seconds": float(b["holding_seconds"]),
            "b_net_pnl_twd": float(b["net_pnl"]),
            "b_mfe_twd": b.get("mfe_net_pnl_at_exit"), "b_mae_twd": b.get("mae_net_pnl_at_exit"),
            "b_trigger_to_fill_delay_seconds": b.get("trigger_to_fill_delay_seconds", 0.0),
            "b_trigger_to_fill_price_gap": b.get("trigger_to_fill_price_gap", 0.0),
            "b_post_exit_5m_scorable": b.get("post_exit_5m_scorable"),
            "b_post_exit_5m_best_net_pnl": b.get("post_exit_5m_best_net_pnl"),
            "delta_twd": delta, "effect": effect,
            "a_profit_giveback_twd": max(
                0.0,
                float(a.get("mfe_net_pnl_at_exit") or 0.0)
                - float(a["net_pnl"]),
            ),
            "b_profit_giveback_twd": max(
                0.0,
                float(b.get("mfe_net_pnl_at_exit") or 0.0)
                - float(b["net_pnl"]),
            ),
            "winner_made_loss": float(a["net_pnl"]) > 0 and float(b["net_pnl"]) <= 0,
            "same_entry_and_quantity": True,
            "account_order_scope": "ONE_PRODUCTION_ENTRY_PER_DAY_TWO_SEPARATE_PAPER_EXITS",
        })
    positive_improvements = [row["delta_twd"] for row in pairs if row["delta_twd"] > 0]
    positive_total = sum(positive_improvements)
    improvement_by_day: dict[str, float] = {}
    for row in pairs:
        if row["delta_twd"] > 0:
            improvement_by_day[row["session_date"]] = (
                improvement_by_day.get(row["session_date"], 0.0)
                + row["delta_twd"]
            )
    zero_pair_reason = None
    if not pairs:
        zero_pair_reason = (
            "NO_VERIFIED_PAPER_DAYS" if not verified
            else "NO_DAYS_WITH_BOTH_FIXED_A_B_TRACKS"
        )
    report = {
        "analysis_id": "PAPER_SHADOW_FIXED_DUAL_EXIT_V0_1",
        "frozen_contract": FROZEN_CONTRACT,
        "program_hashes": {
            "dual_track.py": sha256_file(Path(__file__)),
            "runner.py": sha256_file(Path(__file__).with_name("runner.py")),
            "buffered_exit.py": sha256_file(
                Path(__file__).with_name("buffered_exit.py")
            ),
            "strategy.py": sha256_file(
                Path(__file__).resolve().parents[1]
                / "yuanta_live_runtime_v01" / "strategy.py"
            ),
        },
        "a_variant": PRODUCTION, "b_variant": REFERENCE,
        "paired_trades": len(pairs), "pairs": pairs,
        "zero_pair_reason": zero_pair_reason,
        "unpaired_days": unpaired_days,
        "a_net_pnl_twd": sum(row["a_net_pnl_twd"] for row in pairs),
        "b_net_pnl_twd": sum(row["b_net_pnl_twd"] for row in pairs),
        "net_difference_twd": sum(row["delta_twd"] for row in pairs),
        "losses_reduced": sum(row["effect"] == "LOSS_REDUCED" for row in pairs),
        "losses_worsened": sum(row["effect"] == "LOSS_WORSENED" for row in pairs),
        "winners_improved": sum(row["effect"] == "WINNER_GAIN" for row in pairs),
        "winners_sacrificed": sum(row["effect"] == "WINNER_SACRIFICED" for row in pairs),
        "winners_made_loss": sum(row["winner_made_loss"] for row in pairs),
        "a_profit_giveback_twd": sum(
            row["a_profit_giveback_twd"] for row in pairs
        ),
        "b_profit_giveback_twd": sum(
            row["b_profit_giveback_twd"] for row in pairs
        ),
        "maximum_single_trade_improvement_share": (
            max(positive_improvements) / positive_total
            if positive_total > 0 else None
        ),
        "maximum_session_improvement_share": (
            max(improvement_by_day.values()) / positive_total
            if positive_total > 0 and improvement_by_day else None
        ),
        "improvement_share_denominator_twd": (
            positive_total if positive_total > 0 else None
        ),
        "data_qualification_by_session": qualification_by_session,
        "unscorable_common_exit_quote_count": 0,
        "duplicate_days_excluded": sorted(duplicate_days),
        "rejected": rejected,
        "near_miss_included": False,
        "tracks_are_not_summed_as_one_account": True,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
    }
    return report


def publish_dual_track(
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    *,
    source_runtime_dir: Path = DEFAULT_SOURCE_RUNTIME_DIR,
    output: Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:
    """Atomically publish the read-only comparison after paper-day close."""
    report = build_dual_track(
        runtime_dir.resolve(), source_runtime_dir=source_runtime_dir.resolve(),
    )
    payload = canonical_bytes(report) + b"\n"
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f".{output.name}.tmp-{os.getpid()}"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        **report,
        "output_path": str(output),
        "report_hash": hashlib.sha256(payload).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument(
        "--source-runtime-dir", type=Path, default=DEFAULT_SOURCE_RUNTIME_DIR,
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.output:
        report = publish_dual_track(
            args.runtime_dir,
            source_runtime_dir=args.source_runtime_dir,
            output=args.output,
        )
    else:
        report = build_dual_track(
            args.runtime_dir.resolve(),
            source_runtime_dir=args.source_runtime_dir.resolve(),
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
