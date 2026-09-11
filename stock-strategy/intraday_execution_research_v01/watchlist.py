from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from surge_event_study_v01.data import sha256_file

from .config import CFG


def canonical_hash(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _stock_code_string(value: object) -> str:
    """Preserve a frozen artifact's string identity, including leading zeroes."""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _paths(stock_strategy: Path) -> tuple[Path, Path, Path]:
    root = stock_strategy / "upside_opportunity_ranking_v01"
    return root / "runtime/ranking_store.npz", root / "stage_a_model_spec.json", root / "run_manifest.json"


def load_frozen_stage_a(stock_strategy: Path) -> tuple[dict[str, np.ndarray], dict]:
    store, spec, manifest = _paths(stock_strategy)
    expected = (
        (store, CFG.expected_stage_a_store_sha256),
        (spec, CFG.expected_stage_a_spec_sha256),
        (manifest, CFG.expected_stage_a_manifest_sha256),
    )
    for path, digest in expected:
        if sha256_file(path) != digest:
            raise RuntimeError(f"frozen Stage A artifact drifted: {path}")
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    fingerprint = manifest_payload["stage_a_model_fingerprints"]["LINEAR_RIDGE_MFE10"]
    if fingerprint != CFG.expected_stage_a_model_fingerprint:
        raise RuntimeError("frozen Stage A model fingerprint drifted")
    with np.load(store, allow_pickle=False) as payload:
        arrays = {name: payload[name].copy() for name in ("meta", "stage_a_scores", "stage_a_ranks")}
    return arrays, {
        "store_sha256": CFG.expected_stage_a_store_sha256,
        "model_spec_sha256": CFG.expected_stage_a_spec_sha256,
        "run_manifest_sha256": CFG.expected_stage_a_manifest_sha256,
        "model_id": "LINEAR_RIDGE_MFE10",
        "model_fingerprint": fingerprint,
        "stage_a_refit_count": 0,
    }


def build_watchlist(stock_strategy: Path, signal_date: str) -> dict:
    day = int(signal_date.replace("-", ""))
    arrays, provenance = load_frozen_stage_a(stock_strategy)
    available_dates = np.unique(arrays["meta"]["signal_date"])
    later = available_dates[available_dates > day]
    if not np.any(available_dates == day):
        raise ValueError(f"signal date absent from frozen Stage A store: {signal_date}")
    if not len(later):
        raise ValueError("no following frozen market session for subscription date")
    selected = (arrays["meta"]["signal_date"] == day) & (arrays["stage_a_ranks"] > 0) & (arrays["stage_a_ranks"] <= CFG.stage_a_top_k)
    indices = np.flatnonzero(selected)
    if len(indices) != CFG.stage_a_top_k:
        raise RuntimeError(f"expected exact frozen Top30, got {len(indices)}")
    indices = indices[np.argsort(arrays["stage_a_ranks"][indices], kind="stable")]
    subscription_day = str(int(later[0]))
    subscription_formatted = f"{subscription_day[:4]}-{subscription_day[4:6]}-{subscription_day[6:]}"
    body = {
        "study_id": CFG.study_id,
        "schema_version": CFG.schema_version,
        "signal_date": signal_date,
        "subscription_trading_date": subscription_day,
        "subscription_trading_date_formatted": subscription_formatted,
        "selection": "EXACT_PUBLISHED_STAGE_A_TOP30",
        "frozen_model": provenance,
        "symbols": [
            {
                "stock_code": _stock_code_string(arrays["meta"]["stock_code"][index]),
                "signal_date": signal_date,
                "stage_a_rank": int(arrays["stage_a_ranks"][index]),
                "stage_a_score": float(arrays["stage_a_scores"][index]),
                "top30_membership": True,
                "signal_close": None,
            }
            for index in indices
        ],
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    body["watchlist_sha256"] = canonical_hash(body)
    return body


def export_watchlist(stock_strategy: Path, runtime: Path, signal_date: str) -> tuple[Path, dict]:
    payload = build_watchlist(stock_strategy, signal_date)
    target = runtime / "watchlists" / f"{signal_date.replace('-', '')}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing != payload:
            raise RuntimeError("immutable watchlist differs from deterministic rebuild")
        return target, payload
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(target)
    return target, payload


def verify_watchlist(payload: dict) -> None:
    supplied = payload.get("watchlist_sha256")
    body = {key: value for key, value in payload.items() if key != "watchlist_sha256"}
    if supplied != canonical_hash(body):
        raise RuntimeError("watchlist hash mismatch")
    if payload.get("selection") != "EXACT_PUBLISHED_STAGE_A_TOP30" or len(payload.get("symbols", [])) != 30:
        raise RuntimeError("watchlist membership contract failed")
