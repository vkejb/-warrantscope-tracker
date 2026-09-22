"""Post-seal analysis and notification; failures never alter quote artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from prospective_notifications_v01.keychain import load_into_environment
from prospective_notifications_v01.notifier import notify

from .analysis import publish_analysis
from .collector import DEFAULT_RUNTIME_DIR
from .session_analysis import publish_session_analysis


def process_run(run_dir: Path, runtime_dir: Path = DEFAULT_RUNTIME_DIR) -> dict:
    quality_dir = publish_analysis(run_dir, runtime_dir / "analyses")
    session_dir = publish_session_analysis(run_dir, runtime_dir / "session_analyses")
    quality = json.loads((quality_dir / "analysis_manifest.json").read_text())
    session = json.loads((session_dir / "session_manifest.json").read_text())
    message = "\n".join([
        f"【Stage A 盤中資料｜{session['session_date']}】", "只讀行情封存完成",
        f"逐筆／五檔 run：{quality['source_run_id']}", f"Coverage：{session['coverage_status']}",
        "狀態：" + "、".join(f"{key} {value}" for key, value in quality["state_counts"].items() if value),
        f"analysis hash：{quality['analysis_hash'][:12]}", "SHADOW_ONLY｜不是買賣訊號｜無下單",
    ])
    try:
        keychain = load_into_environment()
        delivery = notify("YUANTA_INTRADAY_SHADOW", session["session_date"], quality["analysis_hash"], "INTRADAY_COMPLETE", message)
    except Exception as exc:
        keychain = "NOTIFICATION_LOAD_FAILED"
        delivery = {"status": "FAILED_AFTER_SEAL", "error_type": type(exc).__name__}
    return {"quality": quality, "session": session, "notification": delivery, "notification_credentials": keychain}
