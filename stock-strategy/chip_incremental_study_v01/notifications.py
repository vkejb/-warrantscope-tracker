from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


TITLE = "WarrantScope Chip Acquisition"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2).encode())
            handle.flush()
            os.fsync(handle.fileno())
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def _log(runtime: Path, row: dict) -> None:
    path = runtime / "logs/notification.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _apple_string(value: str) -> str:
    clean = value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " — ")
    return f'"{clean}"'


def send_notification(runtime: Path, event_type: str, completed: int, expected: int, message: str) -> bool:
    payload = {"event_type": event_type, "completed_pairs": completed, "expected_pairs": expected,
               "title": TITLE, "message": message}
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    state = runtime / "notifications" / f"{event_type}_{completed}_{digest}.json"
    if state.exists():
        if bool(json.loads(state.read_text()).get("success")):
            return True
    error = None
    success = False
    try:
        script = f"display notification {_apple_string(message)} with title {_apple_string(TITLE)}"
        result = subprocess.run(["/usr/bin/osascript", "-e", script], text=True, capture_output=True, timeout=15)
        success = result.returncode == 0
        error = None if success else (result.stderr.strip() or f"osascript exit {result.returncode}")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    row = {**payload, "payload_hash": digest, "sent_at": _now(), "success": success,
           "result": "SENT" if success else "FAILED", "error": error}
    _atomic_json(state, row)
    _log(runtime, row)
    return success


def audit_runtime_cache(cache_dir: Path, progress: dict) -> list[str]:
    from .sources import SOURCE_ORDER, _load_cached
    errors = []
    count = 0
    for source in SOURCE_ORDER:
        root = cache_dir / "entries" / source
        for entry in sorted(root.iterdir()) if root.exists() else ():
            if not entry.is_dir():
                continue
            try:
                cached = _load_cached(cache_dir, source, int(entry.name))
                if cached is None:
                    raise RuntimeError("cache pair absent")
                count += 1
            except Exception as exc:
                errors.append(f"{source}:{entry.name}:{type(exc).__name__}")
    expected_count = int(progress.get("complete_source_date_pairs", 0))
    if count != expected_count:
        errors.append(f"CACHE_COUNT_MISMATCH:{count}!={expected_count}")
    return errors


def finalize_batch(runtime: Path, progress: dict, target_pairs: int | None,
                   integrity_errors: list[str] | None = None, checkpoint_ok: bool = True) -> str | None:
    completed = int(progress.get("complete_source_date_pairs", 0))
    expected = int(progress.get("expected_source_date_pairs", 0))
    remaining = max(0, expected - completed)
    stop = str(progress.get("stop_reason") or "")
    errors = list(integrity_errors or [])
    if errors:
        send_notification(runtime, "INTEGRITY_FAILURE", completed, expected,
                          f"Phase 1 FAIL：Integrity error\ncompleted {completed} / {expected}")
        return "INTEGRITY_FAILURE"
    if completed == expected and expected:
        if checkpoint_ok:
            send_notification(runtime, "ALL_PAIRS_COMPLETE", completed, expected,
                              f"官方籌碼資料下載完成：{completed} / {expected}\nCoverage audit ready")
            return "ALL_PAIRS_COMPLETE"
        return None
    if any(token in stop for token in ("RATE_LIMITED", "REQUEST_ERROR", "PARSE_ERROR", "HTTP", "CDN", "TRANSPORT")):
        reason = stop.split(":", 1)[0].replace("_", " ")
        send_notification(runtime, "OFFICIAL_TRANSPORT_STOP", completed, expected,
                          f"Phase 1 已停止：{completed} / {expected}\n原因：{reason}")
        return "OFFICIAL_TRANSPORT_STOP"
    if target_pairs is not None and completed >= target_pairs and checkpoint_ok:
        send_notification(runtime, "REACHED_TARGET_CHECKPOINT", completed, expected,
                          f"Phase 1 完成：{completed} / {expected} pairs\n剩餘 {remaining}")
        return "REACHED_TARGET_CHECKPOINT"
    return None


def write_runtime_checkpoint(runtime: Path, progress: dict, integrity_errors: list[str]) -> Path:
    completed = int(progress["complete_source_date_pairs"])
    payload = {"created_at": _now(), "progress": progress, "integrity_errors": integrity_errors,
               "integrity_pass": not integrity_errors, "model_fit_count": 0,
               "actual_orders": 0, "actual_fills": 0, "broker_connections": 0}
    path = runtime / "notifications/checkpoints" / f"acquisition_{completed:06d}.json"
    _atomic_json(path, payload)
    return path


def test_notification(runtime: Path) -> bool:
    return send_notification(runtime, "NOTIFICATION_TEST", 0, 0, "通知測試成功")
