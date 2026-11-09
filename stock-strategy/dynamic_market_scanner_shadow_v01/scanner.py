"""Convert causal production diagnostics into an auditable candidate lifecycle."""

from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from typing import Iterable

from expanded_shadow_universe_v01.paper import DEFAULT_RUNTIME_DIR as EXPANDED_PAPER_RUNTIME
from expanded_shadow_universe_v01.policy import POLICY as EXPANDED_POLICY
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file

from .policy import POLICY


MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_RUNTIME_DIR = MODULE_DIR / "runtime"
ANALYSIS_ID = "DYNAMIC_MARKET_SCANNER_SHADOW_V1_1"
POLICY_HASH = hashlib.sha256(canonical_bytes(POLICY)).hexdigest()
PASS_REASONS = frozenset(POLICY["approved_gate_reasons"])
OBSERVE_REASONS = frozenset(POLICY["observing_gate_reasons"])


def _read_jsonl(path: Path) -> Iterable[dict]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _commission(notional: float) -> int:
    return max(
        int(POLICY["minimum_commission_twd"]),
        math.ceil(notional * float(POLICY["commission_rate_each_side"])),
    )


def affordable_quantity(entry_price: float) -> tuple[int, int, float]:
    """Return fee-aware board-lot quantity, entry fee, and total cash required."""
    if not math.isfinite(entry_price) or entry_price <= 0:
        return 0, 0, 0.0
    if entry_price > float(POLICY["maximum_entry_price_twd"]) + 1e-12:
        return 0, 0, 0.0
    lot = int(POLICY["board_lot_shares"])
    capital = float(POLICY["capital_twd"])
    quantity = math.floor(capital / (entry_price * lot)) * lot
    while quantity >= lot:
        notional = entry_price * quantity
        fee = _commission(notional)
        total = notional + fee
        if total <= capital + 1e-9:
            return quantity, fee, total
        quantity -= lot
    return 0, 0, 0.0


class DynamicCandidateScanner:
    """State overlay for diagnostics already produced causally every 30 seconds."""

    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.events: list[dict] = []

    def _event(self, *, at: str, symbol: str, state: str, reason: str,
               candidate: dict | None = None, previous: str | None = None) -> dict:
        candidate = candidate or {}
        row = {
            "decision_time": at,
            "stock_id": symbol,
            "stock_name": str(candidate.get("stock_name", "")),
            "previous_state": previous or self.states.get(symbol, "MONITORING"),
            "state": state,
            "reason": reason,
            "score": candidate.get("score"),
            "gate_reason": candidate.get("gate_reason"),
            "entry_price": candidate.get("entry_price"),
            "engine_quantity": candidate.get("quantity"),
            "fee_aware_quantity": None,
            "entry_fee_twd": None,
            "cash_required_twd": None,
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_order_calls": 0,
        }
        price = candidate.get("entry_price")
        if price is not None:
            quantity, fee, cash = affordable_quantity(float(price))
            row.update({
                "fee_aware_quantity": quantity,
                "entry_fee_twd": fee,
                "cash_required_twd": round(cash, 2),
            })
        self.states[symbol] = state
        self.events.append(row)
        return row

    def process(self, diagnostic: dict) -> list[dict]:
        at = str(diagnostic.get("decision_time", ""))
        candidates = list(diagnostic.get("candidates") or [])
        current = {str(row.get("stock_id", "")) for row in candidates if row.get("stock_id")}
        emitted: list[dict] = []
        for symbol, previous in sorted(self.states.items()):
            if previous in {"OBSERVING", "READY"} and symbol not in current:
                emitted.append(self._event(
                    at=at, symbol=symbol, state="EXPIRED",
                    reason="SIGNAL_NO_LONGER_PRESENT", previous=previous,
                ))

        assessed: list[tuple[dict, str, str]] = []
        selected = str(diagnostic.get("selected_stock_id", ""))
        for candidate in candidates:
            symbol = str(candidate["stock_id"])
            price = float(candidate.get("entry_price") or 0)
            quantity, _fee, _cash = affordable_quantity(price)
            gate = str(candidate.get("gate_reason", "UNKNOWN"))
            if price > float(POLICY["maximum_entry_price_twd"]) + 1e-12:
                state, reason = "REJECTED", "ENTRY_PRICE_ABOVE_190"
            elif quantity < int(POLICY["board_lot_shares"]):
                state, reason = "REJECTED", "CAPITAL_INSUFFICIENT_AFTER_ENTRY_FEE"
            elif gate in PASS_REASONS:
                state = "PAPER_SELECTED" if symbol == selected else "READY"
                reason = "SELECTED_BY_PRODUCTION_SCORE" if symbol == selected else "APPROVED_NOT_SELECTED"
            elif gate in OBSERVE_REASONS:
                state, reason = "OBSERVING", gate
            else:
                state, reason = "REJECTED", gate
            assessed.append((candidate, state, reason))

        active = [row for row in assessed if row[1] in {"OBSERVING", "READY"}]
        keep = {
            str(row[0]["stock_id"])
            for row in sorted(
                active,
                key=lambda item: (-float(item[0].get("score") or 0), str(item[0]["stock_id"])),
            )[: int(POLICY["maximum_active_observations"])]
        }
        for candidate, state, reason in assessed:
            symbol = str(candidate["stock_id"])
            if state in {"OBSERVING", "READY"} and symbol not in keep:
                state, reason = "REJECTED", "ACTIVE_OBSERVATION_CAP"
            emitted.append(self._event(
                at=at, symbol=symbol, state=state, reason=reason,
                candidate=candidate,
            ))
        return emitted

    def finish(self, at: str) -> None:
        for symbol, previous in sorted(self.states.items()):
            if previous in {"OBSERVING", "READY"}:
                self._event(
                    at=at, symbol=symbol, state="EXPIRED",
                    reason="SESSION_DIAGNOSTIC_END", previous=previous,
                )


def _verify_paper_source(path: Path) -> dict:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != manifest.get("manifest_hash"):
        raise RuntimeError("expanded paper manifest hash mismatch")
    for field in ("actual_orders", "actual_fills", "broker_order_calls"):
        if int(manifest.get(field, 0)) != 0:
            raise RuntimeError(f"dynamic scanner rejects nonzero {field}")
    for name, digest in manifest.get("artifacts", {}).items():
        if sha256_file(path / name) != digest:
            raise RuntimeError(f"expanded paper artifact hash mismatch: {name}")
    return manifest


def _write_events(path: Path, rows: list[dict]) -> None:
    fields = list(rows[0]) if rows else ["decision_time", "stock_id", "state", "reason"]
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def publish_dynamic_scan(
    paper_manifest: dict,
    *,
    paper_runtime_dir: Path = EXPANDED_PAPER_RUNTIME,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
) -> dict:
    """Publish immutable lifecycle diagnostics from an expanded paper day."""
    day = str(paper_manifest["session_date"])
    paper_run_id = str(paper_manifest["paper_run_id"])
    source = paper_runtime_dir / "days" / day / paper_run_id
    verified = _verify_paper_source(source)
    if verified.get("manifest_hash") != paper_manifest.get("manifest_hash"):
        raise RuntimeError("expanded paper identity changed before dynamic scan")
    if float(EXPANDED_POLICY["maximum_close_twd"]) != float(POLICY["maximum_entry_price_twd"]):
        raise RuntimeError("premarket and intraday price caps differ")
    scanner = DynamicCandidateScanner()
    diagnostics = list(_read_jsonl(source / "decision_diagnostics.jsonl"))
    for diagnostic in diagnostics:
        scanner.process(diagnostic)
    scanner.finish(str(diagnostics[-1].get("decision_time", "")) if diagnostics else "")
    trades = list(_read_jsonl(source / "paper_trades.jsonl"))
    production = next(
        (row for row in trades if row.get("strategy_variant") == "PRODUCTION_ANTI_CHASE"),
        None,
    )
    confirmation = next(
        (row for row in trades if row.get("strategy_variant") == "ANTI_CHASE_PLUS_60S_CONFIRMATION"),
        None,
    )
    selected = [row for row in scanner.events if row["state"] == "PAPER_SELECTED"]
    identity = {
        "source_expanded_paper_manifest_hash": verified["manifest_hash"],
        "dynamic_policy_hash": POLICY_HASH,
        "analysis_id": ANALYSIS_ID,
    }
    scan_run_id = hashlib.sha256(canonical_bytes(identity)).hexdigest()[:24]
    target = runtime_dir / "days" / day / scan_run_id
    if target.exists():
        return json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    temporary = target.parent / f".{scan_run_id}.tmp-{os.getpid()}"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir()
    try:
        _write_events(temporary / "candidate_lifecycle.csv", scanner.events)
        state_counts: dict[str, int] = {}
        for row in scanner.events:
            state_counts[row["state"]] = state_counts.get(row["state"], 0) + 1
        summary = {
            "analysis_id": ANALYSIS_ID,
            "scan_run_id": scan_run_id,
            "session_date": day,
            "status": "SHADOW_DIAGNOSTIC_COMPLETE",
            "premarket_universe_cap": int(POLICY["maximum_premarket_symbols"]),
            "premarket_close_price_cap_twd": float(EXPANDED_POLICY["maximum_close_twd"]),
            "intraday_entry_price_cap_twd": float(POLICY["maximum_entry_price_twd"]),
            "fee_aware_capital_check": True,
            "decision_count": len(diagnostics),
            "candidate_assessment_count": sum(bool(row.get("stock_id")) for row in scanner.events),
            "unique_candidate_count": len({row["stock_id"] for row in scanner.events if row.get("stock_id")}),
            "state_counts": dict(sorted(state_counts.items())),
            "selected_signal_count": len(selected),
            "selected_stock_ids": [row["stock_id"] for row in selected],
            "production_paper_trade_count": int(production is not None),
            "production_paper_net_pnl_twd": float(production.get("net_pnl", 0)) if production else 0.0,
            "confirmation_60s_trade_count": int(confirmation is not None),
            "confirmation_60s_net_pnl_twd": float(confirmation.get("net_pnl", 0)) if confirmation else 0.0,
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_order_calls": 0,
            "interpretation": "POST_SESSION_CAUSAL_SHADOW_NOT_LIVE_EXECUTION",
        }
        (temporary / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
        artifacts = {
            name: sha256_file(temporary / name)
            for name in ("candidate_lifecycle.csv", "summary.json")
        }
        manifest = {
            **summary,
            **identity,
            "dynamic_policy": POLICY,
            "artifacts": artifacts,
        }
        manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        (temporary / "manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
        temporary.rename(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return json.loads((target / "manifest.json").read_text(encoding="utf-8"))
