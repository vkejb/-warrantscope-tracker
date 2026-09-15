from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from cross_sectional_alpha_ranking_v01.config import FEATURE_NAMES
from cross_sectional_alpha_ranking_v01.preprocessing import rank_percentile, topk_indices
from intraday_execution_research_v01.config import CFG as FROZEN_CFG
from prospective_shadow_v01.market_data_provider import ExistingDailyDataProvider
from surge_event_study_v01.features import iter_signal_dates
from upside_opportunity_ranking_v01.models import UpsideModel
from winner_coverage_taxonomy_v01.features import build_taxonomy_features
from winner_coverage_taxonomy_v01.config import FEATURE_NAMES as TAXONOMY_FEATURE_NAMES

from .seal_store import latest_seal


MODULE_DIR = Path(__file__).resolve().parent
MODEL_SPEC = MODULE_DIR.parent / "upside_opportunity_ranking_v01" / "stage_a_model_spec.json"
RUNTIME_DIR = MODULE_DIR / "runtime"
ACTIVATION_DATE = "20260916"
TOP_K = 30


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def frozen_model() -> tuple[UpsideModel, str]:
    raw = MODEL_SPEC.read_bytes()
    if _digest(raw) != FROZEN_CFG.expected_stage_a_spec_sha256:
        raise RuntimeError("published Stage A model-spec hash mismatch")
    spec = json.loads(raw)
    model = UpsideModel(**{**spec["stage_a_primary_model"], "feature_names": tuple(spec["stage_a_primary_model"]["feature_names"]), "coefficients": tuple(spec["stage_a_primary_model"]["coefficients"])})
    if model.name != "LINEAR_RIDGE_MFE10" or model.fingerprint() != FROZEN_CFG.expected_stage_a_model_fingerprint:
        raise RuntimeError("published Stage A model fingerprint mismatch")
    if model.feature_names != FEATURE_NAMES or model.regularization_alpha != 0.1:
        raise RuntimeError("published Stage A feature/alpha contract mismatch")
    return model, _digest(raw)


def build_watchlist(snapshot, signal_date: str) -> dict:
    if snapshot.data_through_date != signal_date:
        raise RuntimeError("Stage A input is not physically truncated at signal date")
    model, model_spec_hash = frozen_model()
    blocks = list(iter_signal_dates(snapshot.prepared_stocks, snapshot.benchmark, signal_date, signal_date))
    if len(blocks) != 1 or blocks[0][0] != signal_date:
        raise RuntimeError("Stage A signal date absent from frozen universe calendar")
    contexts = blocks[0][2]
    rows = []
    raw_features = []
    for observation, stock, index in contexts:
        try:
            taxonomy_vector = build_taxonomy_features(stock, index, snapshot.benchmark)
        except ValueError:
            continue
        rows.append((str(observation.code), observation.name))
        mapping = dict(zip(TAXONOMY_FEATURE_NAMES, taxonomy_vector))
        raw_features.append(tuple(mapping[name] for name in FEATURE_NAMES))
    if len(rows) < TOP_K:
        raise RuntimeError(f"expected at least 30 eligible stocks; actual {len(rows)}")
    raw = np.asarray(raw_features, dtype=np.float64)
    transformed = np.empty(raw.shape, dtype=np.float32)
    for column in range(raw.shape[1]):
        transformed[:, column] = rank_percentile(raw[:, column], missing_value=0.5)
    scores = model.predict(transformed)
    selected = topk_indices(scores, np.asarray([int(code) for code, _ in rows]), TOP_K, largest=True)
    stocks = [
        {"stock_id": rows[int(index)][0], "stock_name": rows[int(index)][1], "rank": rank, "score": float(scores[int(index)])}
        for rank, index in enumerate(selected, 1)
    ]
    if len(stocks) != TOP_K or len({row["stock_id"] for row in stocks}) != TOP_K:
        raise RuntimeError("Stage A Top30 count/identity validation failed")
    content = {
        "schema_version": "1", "signal_date": signal_date,
        "setup": "FROZEN_STAGE_A_TOP30", "mode": "SHADOW_ONLY",
        "stocks": stocks, "model_hash": model.fingerprint(),
        "model_spec_hash": model_spec_hash,
        "config_hash": _digest(_canonical({"activation_date": ACTIVATION_DATE, "top_k": TOP_K, "feature_names": FEATURE_NAMES})),
        "input_hash": snapshot.input_manifest_hash,
        "eligible_stock_count": len(rows),
    }
    return content


def seal_current(archives: list[Path], calendar: Path, *, now: datetime | None = None, runtime_dir: Path = RUNTIME_DIR, expected_input_hash: str | None = None) -> dict:
    local = (now or datetime.now(ZoneInfo("Asia/Taipei"))).astimezone(ZoneInfo("Asia/Taipei"))
    date = local.strftime("%Y%m%d")
    if date < ACTIVATION_DATE or local.strftime("%H:%M") < "14:25" or local.strftime("%H:%M") > "16:05":
        raise RuntimeError("Stage A prospective seal refused outside activation/current-day attempt window")
    snapshot = ExistingDailyDataProvider(archives, trading_calendar_path=calendar).load_through(date)
    if expected_input_hash is not None and snapshot.input_manifest_hash != expected_input_hash:
        raise RuntimeError("Stage A input manifest differs from sealed N input")
    content = build_watchlist(snapshot, date)
    seal_hash = _digest(_canonical(content))
    payload = {**content, "created_at": local.isoformat(), "seal_hash": seal_hash, "status": "SEALED", "actual_orders": 0, "actual_fills": 0, "broker_connections": 0}
    seals = runtime_dir / "seals"
    seals.mkdir(parents=True, exist_ok=True)
    path = seals / f"{date}.json"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = json.loads(path.read_text(encoding="utf-8"))
        old = {key: existing[key] for key in content}
        if _digest(_canonical(old)) != existing.get("seal_hash"):
            raise RuntimeError("existing Stage A seal hash mismatch")
        if old != content:
            raise RuntimeError("duplicate Stage A seal conflicts with sealed input/model")
        return {"status": "ALREADY_SEALED", "signal_date": date, "seal_hash": existing["seal_hash"], "input_hash": existing["input_hash"], "count": len(existing["stocks"]), "stocks": existing["stocks"]}
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    return {"status": "SEALED", "signal_date": date, "seal_hash": seal_hash, "input_hash": content["input_hash"], "count": TOP_K, "stocks": content["stocks"]}
