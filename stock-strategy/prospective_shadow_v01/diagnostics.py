from __future__ import annotations

import json
from pathlib import Path

from multi_setup_study_v01.config import CFG as MULTI_CFG
from multi_setup_study_v01.setup_detectors import is_compact_retest
from reversal_event_study_v01.config import CFG as REVERSAL_CFG
from reversal_event_study_v01.study import build_pattern_observation

from .config import CFG, Config
from .detector import assert_frozen_contract
from .market_data_provider import (
    ExistingDailyDataProvider,
    MarketDataSnapshot,
    normalize_date,
)
from .storage import SCAN_FIELDS, _read_csv, sha256_file


def compact_failure_reasons(geometry: dict) -> list[str]:
    """Explain the frozen compact predicate without changing its meaning."""

    reasons: list[str] = []
    if int(geometry["pivot_separation_sessions"]) > 7:
        reasons.append("PIVOT_SEPARATION_SESSIONS_GT_7")
    if float(geometry["bottom_difference"]) <= 0:
        reasons.append("BOTTOM_DIFFERENCE_NOT_POSITIVE")
    return reasons


def _candidate(stock, local_index: int, legacy, accepted: bool) -> dict:
    geometry = legacy.geometry
    first_index = int(geometry["first_pivot_index"])
    second_index = int(geometry["pivot_index"])
    geometry_pass = is_compact_retest(geometry, MULTI_CFG)
    reasons = compact_failure_reasons(geometry)
    if not accepted:
        reasons.append("PARENT_N_RETEST_REJECTED_BY_FROZEN_COOLDOWN")
    condition_results = {
        "pivot_separation_sessions_le_7": (
            int(geometry["pivot_separation_sessions"]) <= 7
        ),
        "bottom_difference_gt_0": float(geometry["bottom_difference"]) > 0,
    }
    return {
        "stock_id": legacy.code,
        "stock_name": legacy.name,
        "signal_date": legacy.signal_date,
        "signal_close": legacy.signal_close,
        "first_pivot_date": str(geometry["first_pivot_date"]),
        "first_pivot_price": stock.bars[first_index].close,
        "second_pivot_date": str(geometry["pivot_date"]),
        "second_pivot_price": stock.bars[second_index].close,
        "pivot_separation_sessions": int(
            geometry["pivot_separation_sessions"]
        ),
        "bottom_difference": float(geometry["bottom_difference"]),
        "intervening_bounce": float(geometry["intervening_bounce"]),
        "confirmation_rebound": float(geometry["confirmation_rebound"]),
        "confirmation_break_vs_prior_high": float(
            geometry["confirmation_break_vs_prior_high"]
        ),
        "raw_overlap": bool(legacy.raw_overlap),
        "accepted_n_retest": accepted,
        "compact_geometry_pass": geometry_pass,
        "n_compact_retest": accepted and geometry_pass,
        "compact_condition_results": condition_results,
        "compact_failure_reasons": reasons,
    }


def diagnose_snapshot(
    snapshot: MarketDataSnapshot,
    signal_date: str,
    cfg: Config = CFG,
) -> dict:
    """Replay the frozen detector in memory and return all target-date parents."""

    assert_frozen_contract(cfg)
    target = normalize_date(signal_date)
    if snapshot.data_through_date != target:
        raise RuntimeError("diagnostic snapshot must be loaded exactly through T")
    if target not in snapshot.benchmark.calendar:
        raise RuntimeError("target session is absent from the market calendar")
    if any(day > target for day in snapshot.benchmark.calendar):
        raise RuntimeError("benchmark contains data after T")

    candidates: list[dict] = []
    target_bars = 0
    cooldown = REVERSAL_CFG.causal_cooldown_sessions
    for stock in snapshot.prepared_stocks:
        if any(bar.date > target for bar in stock.bars):
            raise RuntimeError(f"provider leaked post-T bar for {stock.code}")
        if any(bar.date == target for bar in stock.bars):
            target_bars += 1
        last_accepted_parent: int | None = None
        for local_index in range(
            REVERSAL_CFG.feature_lookback_sessions, len(stock.bars)
        ):
            bar = stock.bars[local_index]
            if bar.date > target:
                raise RuntimeError(f"provider leaked post-T bar for {stock.code}")
            legacy = build_pattern_observation(
                stock, local_index, snapshot.benchmark, REVERSAL_CFG
            )
            if legacy is None or legacy.pattern != cfg.parent_setup:
                continue
            accepted = last_accepted_parent is None or (
                legacy.calendar_index - last_accepted_parent >= cooldown
            )
            if accepted:
                last_accepted_parent = legacy.calendar_index
            if bar.date == target:
                candidates.append(_candidate(stock, local_index, legacy, accepted))

    candidates.sort(key=lambda row: (row["stock_id"], row["signal_date"]))
    accepted = [row for row in candidates if row["accepted_n_retest"]]
    compact = [row for row in candidates if row["n_compact_retest"]]
    return {
        "signal_date": target,
        "compact_rule": cfg.compact_rule,
        "raw_n_retest_count": len(candidates),
        "accepted_n_retest_count": len(accepted),
        "n_compact_retest_count": len(compact),
        "stocks_with_target_bar": target_bars,
        "raw_n_retest_candidates": candidates,
        "accepted_n_retest_candidates": accepted,
        "n_compact_retest_candidates": compact,
    }


def _read_readiness(path: Path, target: str) -> tuple[list[Path], Path, str]:
    if not path.is_file():
        raise FileNotFoundError(f"sealed readiness audit is absent: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") != "READY" or payload.get("ready") is not True:
        raise RuntimeError("sealed readiness audit did not pass")
    if normalize_date(payload.get("target_date", "")) != target:
        raise RuntimeError("readiness audit target date does not match")
    provider_audit = payload.get("prospective_provider_audit", {})
    sources = provider_audit.get("sources")
    if not isinstance(sources, list) or not sources:
        raise RuntimeError("readiness audit has no prospective source list")
    archives = [Path(str(value)) for value in sources]
    calendar = path.parent.parent / "trading_calendar.csv"
    expected_calendar_hash = str(
        payload.get("trading_calendar", {}).get("calendar_sha256", "")
    )
    if not calendar.is_file() or sha256_file(calendar) != expected_calendar_hash:
        raise RuntimeError("sealed trading-calendar hash mismatch")
    expected_manifest = str(payload.get("prospective_input_manifest_hash", ""))
    if expected_manifest != provider_audit.get("input_manifest_hash"):
        raise RuntimeError("readiness provider/input manifest hashes disagree")
    return archives, calendar, expected_manifest


def run_sealed_diagnostics(
    signal_date: str,
    store_dir: Path,
    readiness_audit: Path,
    cfg: Config = CFG,
) -> dict:
    """Read and replay one sealed scan without constructing a writable store."""

    assert_frozen_contract(cfg)
    target = normalize_date(signal_date)
    scans_path = Path(store_dir) / cfg.scans_filename
    scans = _read_csv(scans_path, SCAN_FIELDS)
    matches = [row for row in scans if row["signal_date"] == target]
    if len(matches) != 1 or matches[0]["scan_status"] != "COMPLETE":
        raise RuntimeError("date does not have exactly one sealed COMPLETE scan")
    sealed = matches[0]
    if sealed["prospective_config_hash"] != cfg.fingerprint():
        raise RuntimeError("sealed scan prospective config hash mismatch")
    if sealed["compact_rule"] != cfg.compact_rule:
        raise RuntimeError("sealed scan compact rule mismatch")

    archives, calendar, expected_manifest = _read_readiness(
        Path(readiness_audit), target
    )
    if sealed["input_manifest_hash"] != expected_manifest:
        raise RuntimeError("sealed scan/readiness input hashes disagree")
    snapshot = ExistingDailyDataProvider(
        archives, trading_calendar_path=calendar
    ).load_through(target)
    if snapshot.input_manifest_hash != expected_manifest:
        raise RuntimeError("replayed input manifest hash does not match sealed scan")

    result = diagnose_snapshot(snapshot, target, cfg)
    count_fields = {
        "raw_n_retest_count": "raw_n_retest_count",
        "accepted_n_retest_count": "accepted_n_retest_count",
        "n_compact_retest_count": "compact_count",
        "stocks_with_target_bar": "stocks_with_target_bar",
    }
    for result_field, scan_field in count_fields.items():
        if result[result_field] != int(sealed[scan_field]):
            raise RuntimeError(
                f"diagnostic {result_field} does not match sealed scan"
            )
    return {
        "command": "diagnostics",
        "mode": "READ_ONLY_NO_LEDGER_WRITES",
        "sealed_scan_verified": True,
        "input_manifest_hash": snapshot.input_manifest_hash,
        "prospective_config_hash": cfg.fingerprint(),
        **result,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
