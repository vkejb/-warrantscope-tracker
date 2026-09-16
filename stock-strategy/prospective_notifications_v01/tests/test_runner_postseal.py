from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from shadow_daily_runner.config import RunnerConfig
from shadow_daily_runner.runner import _postseal_notifications


class RunnerPostsealTests(unittest.TestCase):
    def test_stage_failure_cannot_mutate_n_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "n"
            store.mkdir()
            scan = store / "prospective_scan_log.csv"
            scan.write_text("signal_date,scan_status,record_hash,input_manifest_hash,raw_n_retest_count,compact_count\n20260916,COMPLETE,nhash,input,1,0\n", encoding="utf-8")
            (store / "prospective_signals.csv").write_text("signal_date,stock_id\n", encoding="utf-8")
            cfg = RunnerConfig(runtime_dir=root / "runtime", shadow_store_dir=store, stage_a_runtime_dir=root / "stage")
            prepared = type("Ready", (), {"ready": True, "archives": (root / "archive.zip",), "calendar_path": root / "calendar.csv"})()
            before = scan.read_bytes()
            local = datetime(2026, 9, 16, 15, 0, tzinfo=ZoneInfo("Asia/Taipei"))
            with patch("shadow_daily_runner.runner._run_json", side_effect=RuntimeError("model exception")), patch("prospective_notifications_v01.notifier.notify", return_value={"TELEGRAM": "FAILED"}):
                result = _postseal_notifications("20260916", prepared, cfg, local)
            self.assertEqual(result["status"], "STAGE_A_FAILED_N_REMAINS_SEALED")
            self.assertEqual(scan.read_bytes(), before)
            self.assertTrue((cfg.logs_dir / "postseal_errors.jsonl").is_file())

    def test_notification_exception_is_after_both_seals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "n"
            store.mkdir()
            scan = store / "prospective_scan_log.csv"
            scan.write_text("signal_date,scan_status,record_hash,input_manifest_hash,raw_n_retest_count,compact_count\n20260916,COMPLETE,nhash,input,0,0\n", encoding="utf-8")
            (store / "prospective_signals.csv").write_text("signal_date,stock_id\n", encoding="utf-8")
            cfg = RunnerConfig(runtime_dir=root / "runtime", shadow_store_dir=store, stage_a_runtime_dir=root / "stage")
            prepared = type("Ready", (), {"ready": True, "archives": (root / "archive.zip",), "calendar_path": root / "calendar.csv"})()
            local = datetime(2026, 9, 16, 15, 0, tzinfo=ZoneInfo("Asia/Taipei"))
            sealed_stage = {"status": "SEALED", "signal_date": "20260916", "seal_hash": "stagehash", "count": 30, "stocks": []}
            before = scan.read_bytes()
            with patch("shadow_daily_runner.runner._run_json", return_value=sealed_stage), patch("prospective_notifications_v01.notifier.daily_message", return_value="daily"), patch("prospective_notifications_v01.notifier.notify", side_effect=OSError("notification filesystem unavailable")):
                result = _postseal_notifications("20260916", prepared, cfg, local)
            self.assertEqual(result["status"], "SEALED")
            self.assertEqual(result["notification"]["status"], "FAILED_AFTER_SEAL")
            self.assertEqual(scan.read_bytes(), before)

    def test_existing_matching_stage_seal_skips_refreshed_input_and_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "n"
            store.mkdir()
            scan = store / "prospective_scan_log.csv"
            scan.write_text("signal_date,scan_status,record_hash,input_manifest_hash,raw_n_retest_count,compact_count\n20260916,COMPLETE,nhash,sealed-input,10,0\n", encoding="utf-8")
            (store / "prospective_signals.csv").write_text("signal_date,stock_id\n", encoding="utf-8")
            cfg = RunnerConfig(runtime_dir=root / "runtime", shadow_store_dir=store, stage_a_runtime_dir=root / "stage")
            prepared = type("Ready", (), {"ready": True, "archives": (root / "different-refresh.zip",), "calendar_path": root / "calendar.csv"})()
            local = datetime(2026, 9, 16, 15, 0, tzinfo=ZoneInfo("Asia/Taipei"))
            sealed = {
                "signal_date": "20260916", "seal_hash": "stagehash",
                "input_hash": "sealed-input",
                "stocks": [{"rank": i, "stock_id": str(2000 + i), "stock_name": "測試", "score": 0.1} for i in range(1, 31)],
            }
            with patch("stage_a_prospective_watchlist_v01.seal_store.latest_seal", return_value=sealed), patch("shadow_daily_runner.runner._run_json") as run_json, patch("prospective_notifications_v01.notifier.daily_message", return_value="daily"), patch("prospective_notifications_v01.notifier.notify", return_value={"TELEGRAM": "ALREADY_SENT"}) as notify:
                result = _postseal_notifications("20260916", prepared, cfg, local)
            self.assertEqual(result["status"], "SEALED")
            self.assertEqual(result["stage_a"]["status"], "ALREADY_SEALED")
            run_json.assert_not_called()
            self.assertEqual(notify.call_args.args[0], "WARRANTSCOPE_DAILY")


if __name__ == "__main__":
    unittest.main()
