from __future__ import annotations

import hashlib
import json
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"


def latest_seal(runtime_dir: Path = RUNTIME_DIR) -> dict | None:
    paths = sorted((runtime_dir / "seals").glob("*.json")) if (runtime_dir / "seals").is_dir() else []
    if not paths:
        return None
    value = json.loads(paths[-1].read_text(encoding="utf-8"))
    content = {key: value[key] for key in ("schema_version", "signal_date", "setup", "mode", "stocks", "model_hash", "model_spec_hash", "config_hash", "input_hash", "eligible_stock_count")}
    digest = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    if digest != value.get("seal_hash") or len(value["stocks"]) != 30:
        raise RuntimeError("latest Stage A seal validation failed")
    return value
