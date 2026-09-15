from __future__ import annotations

import csv
from datetime import datetime, time
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from typing import Callable

from .config import CFG, RunnerConfig, STOCK_STRATEGY_DIR
from .io_utils import append_jsonl, atomic_write_json, process_lock, sha256_file, utc_timestamp
from .notifications import notification_status, safe_notify_attempt_result
from .pipeline import PreparedInputs, prepare_inputs, public_result, taipei_now


def _postseal_notifications(target: str, prepared: PreparedInputs | None, cfg: RunnerConfig, local: datetime) -> dict:
    """Side effects only after the N ledger has sealed; never alter N records."""
    from prospective_notifications_v01.notifier import daily_message, notify, warning_message
    activation_date = "20260916"

    if target < activation_date:
        return {"status": "PRE_ACTIVATION_LEGACY_MACOS_ONLY"}
    scan_rows = _read_csv(cfg.shadow_store_dir / "prospective_scan_log.csv")
    scan = next((row for row in reversed(scan_rows) if row.get("signal_date") == target and row.get("scan_status") == "COMPLETE"), None)
    if scan is None:
        try:
            warning = notify("STAGE_A", target, "NO_N_SCAN", "WARNING", warning_message(target, "Stage A", "N_SCAN_NOT_SEALED"))
        except Exception:
            warning = {"status": "FAILED_LOCAL_LOG_ONLY"}
        return {"status": "N_SCAN_NOT_SEALED", "warning": warning}
    if prepared is None or not prepared.ready:
        try:
            warning = notify("STAGE_A", target, scan["record_hash"], "WARNING", warning_message(target, "Stage A", "OFFICIAL_INPUT_NOT_READY"))
        except Exception:
            warning = {"status": "FAILED_LOCAL_LOG_ONLY"}
        return {"status": "STAGE_A_INPUT_NOT_READY", "warning": warning}
    try:
        stage_python = os.environ.get(
            "WARRANTSCOPE_STAGE_A_PYTHON",
            "/Users/linyunyan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3",
        )
        if not Path(stage_python).is_file():
            raise RuntimeError("Stage A Python with frozen model dependencies is unavailable")
        stage = _run_json([
            stage_python, "-B", "-m", "stage_a_prospective_watchlist_v01.main",
            "seal-current", "--archives", *[str(path) for path in prepared.archives],
            "--trading-calendar", str(prepared.calendar_path),
            "--expected-input-hash", scan["input_manifest_hash"],
        ], cfg)
        if stage["signal_date"] != target:
            raise RuntimeError("Stage A seal date differs from N seal")
    except Exception as exc:
        # N seal remains authoritative.  The next scheduled attempt may retry
        # Stage A without rerunning or rewriting the sealed N scan.
        append_jsonl(cfg.logs_dir / "postseal_errors.jsonl", {"at": utc_timestamp(), "target_date": target, "module": "STAGE_A", "traceback": traceback.format_exc()})
        message = warning_message(target, "Stage A", type(exc).__name__, str(exc))
        try:
            warning = notify("STAGE_A", target, scan["record_hash"], "WARNING", message, providers=("TELEGRAM", "MACOS_LOCAL_NOTIFICATION"))
        except Exception:
            warning = {"status": "FAILED_LOCAL_LOG_ONLY"}
        return {"status": "STAGE_A_FAILED_N_REMAINS_SEALED", "error_type": type(exc).__name__, "warning": warning}
    stage_summary = {key: stage[key] for key in ("status", "signal_date", "seal_hash", "count")}
    try:
        compact_rows = [row for row in _read_csv(cfg.shadow_store_dir / "prospective_signals.csv") if row.get("signal_date") == target]
        message = daily_message(target, {**scan, "compact_candidates": compact_rows}, stage)
        notification = notify("WARRANTSCOPE_DAILY", target, f"{scan['record_hash']}:{stage['seal_hash']}", "SEALED_DAILY", message)
    except Exception as exc:
        append_jsonl(cfg.logs_dir / "postseal_errors.jsonl", {"at": utc_timestamp(), "target_date": target, "module": "NOTIFICATION", "traceback": traceback.format_exc()})
        notification = {"status": "FAILED_AFTER_SEAL", "error_type": type(exc).__name__}
    return {"status": "SEALED", "stage_a": stage_summary, "notification": notification}


LEDGER_FILENAMES = (
    "prospective_signals.csv",
    "prospective_outcomes.csv",
    "prospective_scan_log.csv",
    "shadow_status.json",
)


def _read_json(path: Path, default: object) -> object:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _ledger_hashes(cfg: RunnerConfig) -> dict[str, str | None]:
    return {
        name: sha256_file(cfg.shadow_store_dir / name)
        if (cfg.shadow_store_dir / name).is_file()
        else None
        for name in LEDGER_FILENAMES
    }


def _record_attempt(cfg: RunnerConfig, payload: dict) -> None:
    append_jsonl(
        cfg.attempt_log_path,
        {
            "schema_version": "1",
            "recorded_at_utc": utc_timestamp(),
            **payload,
        },
    )


def _finish_attempt(result: dict, local: datetime, cfg: RunnerConfig) -> dict:
    _record_attempt(cfg, result)
    postseal = result.get("postseal", {})
    combined_delivery = isinstance(postseal, dict) and postseal.get("status") == "SEALED"
    finished = {
        **result,
        "notification": {"status": "COMBINED_POSTSEAL_DELIVERY"} if combined_delivery else safe_notify_attempt_result(result, local, cfg),
    }
    if result.get("status") == "READINESS_FAILED_NO_LEDGER_WRITE" and result.get("target_date", "") >= "20260916":
        try:
            from prospective_notifications_v01.notifier import notify, warning_message
            reason = result.get("readiness", {}).get("audit", {}).get("reason", "official input/readiness failed")
            finished["telegram_warning"] = notify(
                "N_PROSPECTIVE", result["target_date"],
                sha256_file(cfg.runner_state_path) if cfg.runner_state_path.is_file() else "NO_RUNNER_STATE",
                "WARNING", warning_message(result["target_date"], "N prospective", "READINESS_FAILED", str(reason)),
                providers=("TELEGRAM",),
            )
        except Exception:
            finished["telegram_warning"] = {"status": "FAILED_LOCAL_LOG_ONLY"}
    return finished


def _inside_attempt_window(local: datetime, cfg: RunnerConfig) -> bool:
    current = local.time().replace(tzinfo=None)
    return (
        time.fromisoformat(cfg.earliest_attempt_time)
        <= current
        <= time.fromisoformat(cfg.latest_attempt_time)
    )


def _run_json(command: list[str], cfg: RunnerConfig) -> dict:
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": cfg.timezone,
    }
    completed = subprocess.run(
        command,
        cwd=STOCK_STRATEGY_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        raise RuntimeError(
            f"command failed ({completed.returncode}): {stderr or stdout or command[0]}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("shadow command returned non-JSON output") from exc
    if not isinstance(value, dict):
        raise RuntimeError("shadow command returned a non-object result")
    return value


def _status_command(cfg: RunnerConfig) -> list[str]:
    return [
        sys.executable,
        "-B",
        "-m",
        "prospective_shadow_v01.main",
        "--store-dir",
        str(cfg.shadow_store_dir),
        "status",
    ]


def _run_daily_command(prepared: PreparedInputs, cfg: RunnerConfig) -> list[str]:
    return [
        sys.executable,
        "-B",
        "-m",
        "prospective_shadow_v01.main",
        "--store-dir",
        str(cfg.shadow_store_dir),
        "run-daily",
        "--archives",
        *[str(path) for path in prepared.archives],
        "--trading-calendar",
        str(prepared.calendar_path),
    ]


def _validate_shadow_result(target: str, result: dict, status_result: dict) -> dict:
    if result.get("signal_date") != target:
        raise RuntimeError("run-daily returned a different signal date")
    if result.get("actual_orders") != 0 or result.get("actual_fills") != 0:
        raise RuntimeError("run-daily safety counters are not zero")
    if "outcomes" not in result:
        raise RuntimeError("run-daily did not execute its built-in outcome updater")
    status = status_result.get("status")
    if not isinstance(status, dict):
        raise RuntimeError("shadow status payload is missing")
    for name in ("actual_orders", "actual_fills", "broker_connections"):
        if status.get(name) != 0:
            raise RuntimeError(f"shadow safety counter is not zero: {name}")
    if status.get("last_successful_signal_date") != target:
        raise RuntimeError("ledger status did not seal the requested target date")
    return status


def attempt(
    *,
    now: datetime | None = None,
    cfg: RunnerConfig = CFG,
    prepare: Callable[..., PreparedInputs] = prepare_inputs,
) -> dict:
    local = taipei_now(now, cfg)
    target = local.strftime("%Y%m%d")
    result: dict
    with process_lock(cfg.lock_path):
        state = _read_json(
            cfg.runner_state_path,
            {"schema_version": "1", "completed_targets": {}},
        )
        if not isinstance(state, dict) or not isinstance(state.get("completed_targets"), dict):
            raise RuntimeError("runner state is malformed")
        if target in state["completed_targets"]:
            result = {
                "status": "ALREADY_SUCCEEDED_NO_OP",
                "target_date": target,
                "completed": state["completed_targets"][target],
                "actual_orders": 0,
                "actual_fills": 0,
                "broker_connections": 0,
            }
            if target >= "20260916" and _inside_attempt_window(local, cfg):
                try:
                    retry_inputs = prepare(now=now, cfg=cfg)
                    result["postseal"] = _postseal_notifications(target, retry_inputs, cfg, local)
                except Exception as exc:
                    result["postseal"] = {"status": "RETRY_NOT_READY", "error_type": type(exc).__name__}
                if result["postseal"].get("status") != "SEALED":
                    result["status"] = "STAGE_A_PENDING_N_SEALED"
            return _finish_attempt(result, local, cfg)
        if target < cfg.first_scheduled_target:
            result = {
                "status": "REFUSED_PRE_START",
                "target_date": target,
                "reason": "external runner cannot backfill 2026-09-07 or any earlier date",
                "actual_orders": 0,
                "actual_fills": 0,
                "broker_connections": 0,
            }
            return _finish_attempt(result, local, cfg)
        if not _inside_attempt_window(local, cfg):
            result = {
                "status": "REFUSED_OUTSIDE_ATTEMPT_WINDOW",
                "target_date": target,
                "local_time": local.isoformat(),
                "allowed_window": [cfg.earliest_attempt_time, cfg.latest_attempt_time],
                "reason": "late launchd wakeups must not create retrospective seals",
                "actual_orders": 0,
                "actual_fills": 0,
                "broker_connections": 0,
            }
            return _finish_attempt(result, local, cfg)

        before = _ledger_hashes(cfg)
        prepared = prepare(now=now, cfg=cfg)
        if not prepared.trading_day:
            after = _ledger_hashes(cfg)
            if before != after:
                raise RuntimeError("prospective ledger changed during non-trading-day check")
            result = {
                "status": "NON_TRADING_DAY_NO_OP",
                "target_date": target,
                "readiness": public_result(prepared),
                "ledger_hashes": after,
                "actual_orders": 0,
                "actual_fills": 0,
                "broker_connections": 0,
            }
            return _finish_attempt(result, local, cfg)
        if not prepared.ready:
            after = _ledger_hashes(cfg)
            if before != after:
                raise RuntimeError("prospective ledger changed while readiness failed")
            result = {
                "status": "READINESS_FAILED_NO_LEDGER_WRITE",
                "target_date": target,
                "readiness": public_result(prepared),
                "ledger_hashes_before": before,
                "ledger_hashes_after": after,
                "actual_orders": 0,
                "actual_fills": 0,
                "broker_connections": 0,
            }
            return _finish_attempt(result, local, cfg)

        daily_result = _run_json(_run_daily_command(prepared, cfg), cfg)
        # run-daily already calls update_outcomes_from_snapshot before it
        # validates the ledgers.  A second CLI call would be a redundant reload.
        status_result = _run_json(_status_command(cfg), cfg)
        verified_status = _validate_shadow_result(target, daily_result, status_result)
        completed = {
            "completed_at_utc": utc_timestamp(),
            "readiness_audit": str(prepared.audit_path),
            "input_manifest_hash": daily_result.get("input_manifest_hash"),
            "run_manifest": daily_result.get("run_manifest"),
            "scan": daily_result.get("scan"),
            "outcomes": daily_result.get("outcomes"),
            "signal_count": daily_result.get("signal_count"),
            "ledger_status": verified_status,
        }
        state["completed_targets"][target] = completed
        state.update(
            {
                "schema_version": "1",
                "updated_at_utc": utc_timestamp(),
                "last_successful_target": target,
                "execution_mode": "SHADOW_ONLY_NOT_SUBMITTED",
                "actual_orders": 0,
                "actual_fills": 0,
                "broker_connections": 0,
            }
        )
        atomic_write_json(cfg.runner_state_path, state)
        result = {
            "status": "SUCCESS",
            "target_date": target,
            "run_daily": daily_result,
            "ledger_status": verified_status,
            "outcome_update_mode": "BUILT_IN_TO_RUN_DAILY",
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
        }
        if target >= "20260916":
            result["postseal"] = _postseal_notifications(target, prepared, cfg, local)
            if result["postseal"].get("status") != "SEALED":
                result["status"] = "STAGE_A_PENDING_N_SEALED"
        return _finish_attempt(result, local, cfg)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def runner_status(cfg: RunnerConfig = CFG) -> dict:
    state = _read_json(cfg.runner_state_path, {"schema_version": "1", "completed_targets": {}})
    signals = _read_csv(cfg.shadow_store_dir / "prospective_signals.csv")
    outcomes = _read_csv(cfg.shadow_store_dir / "prospective_outcomes.csv")
    scans = _read_csv(cfg.shadow_store_dir / "prospective_scan_log.csv")
    latest_target = scans[-1]["signal_date"] if scans else None
    latest_scan = scans[-1] if scans else None
    candidates = [
        {
            key: row.get(key, "")
            for key in (
                "stock_id",
                "stock_name",
                "signal_close",
                "first_pivot_date",
                "first_pivot_price",
                "second_pivot_date",
                "second_pivot_price",
                "pivot_separation_sessions",
                "bottom_difference",
                "intervening_bounce",
                "confirmation_rebound",
                "confirmation_break_vs_prior_high",
                "signal_return_1",
                "signal_tr_vs_prior5",
                "post_vs_pre_pivot_range",
                "close_vs_sma20",
                "signal_volume_ratio_20",
                "average_volume_20",
            )
        }
        for row in signals
        if row.get("signal_date") == latest_target
    ]
    return {
        "runner_state": state,
        "latest_signal_date": latest_target,
        "latest_scan": latest_scan,
        "latest_n_compact_candidates": candidates,
        "prospective_signals_rows": len(signals),
        "prospective_outcomes_revision_rows": len(outcomes),
        "prospective_scan_log_rows": len(scans),
        "ledger_hashes": _ledger_hashes(cfg),
        "notification": notification_status(cfg),
        "execution_mode": "SHADOW_ONLY_NOT_SUBMITTED",
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
