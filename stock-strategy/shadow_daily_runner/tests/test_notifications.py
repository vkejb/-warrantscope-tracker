from __future__ import annotations

from contextlib import redirect_stdout
import csv
from dataclasses import replace
from datetime import datetime
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from shadow_daily_runner import notifications
from shadow_daily_runner.config import CFG
from shadow_daily_runner.main import main
from shadow_daily_runner.notifications import (
    FINAL_READINESS_FAILED,
    HISTORICAL_PREFLIGHT_FAILED,
    SEALED_WITH_SIGNALS,
    SEALED_ZERO_SIGNAL,
    build_failure_payload,
    build_sealed_payload,
    deliver_notification,
    notification_status,
    notify_sealed_target,
    send_test_notification,
)
from shadow_daily_runner.pipeline import PreparedInputs
from shadow_daily_runner.runner import attempt, runner_status


def temporary_config(root: Path, **values):
    return replace(
        CFG,
        runtime_dir=root / "runtime",
        shadow_store_dir=root / "shadow",
        **values,
    )


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def seed_ledgers(cfg) -> dict[str, bytes]:
    cfg.shadow_store_dir.mkdir(parents=True, exist_ok=True)
    values = {
        "prospective_signals.csv": b"stock_id,stock_name,signal_date\n",
        "prospective_outcomes.csv": b"signal_date\n",
        "prospective_scan_log.csv": b"signal_date\n",
        "shadow_status.json": b"{}\n",
    }
    for name, payload in values.items():
        (cfg.shadow_store_dir / name).write_bytes(payload)
    return values


def verified_status(signal_date: str) -> dict:
    return {
        "last_successful_signal_date": signal_date,
        "signals_sha256": "a" * 64,
        "outcomes_sha256": "b" * 64,
        "scans_sha256": "c" * 64,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }


def seed_verified_target(
    cfg,
    signal_date: str = "20260910",
    signals: list[dict[str, str]] | None = None,
) -> None:
    rows = signals or []
    seed_ledgers(cfg)
    write_csv(
        cfg.shadow_store_dir / "prospective_signals.csv",
        ["stock_id", "stock_name", "signal_date"],
        [
            {
                "stock_id": row["stock_id"],
                "stock_name": row.get("stock_name", ""),
                "signal_date": signal_date,
            }
            for row in rows
        ],
    )
    status = verified_status(signal_date)
    (cfg.shadow_store_dir / "shadow_status.json").write_text(
        json.dumps(status), encoding="utf-8"
    )
    cfg.runner_state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.runner_state_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "completed_targets": {
                    signal_date: {
                        "signal_count": len(rows),
                        "ledger_status": status,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def failed_preparation(cfg, *, historical: bool = False):
    audit_path = cfg.audit_dir / "readiness.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit = {
        "status": "READINESS_FAILED",
        "failure_reason": "fixture incomplete",
    }
    if historical:
        audit["historical_preflight"] = {"status": "FAIL_CLOSED"}
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    def prepare(**_kwargs):
        return PreparedInputs(
            target_date="20260910",
            trading_day=True,
            ready=False,
            archives=(),
            calendar_path=cfg.calendar_path,
            audit_path=audit_path,
            audit=audit,
        )

    return prepare


class NotificationPayloadTests(unittest.TestCase):
    def test_success_zero_signal_notification_payload(self):
        payload = build_sealed_payload("20260910", [])
        self.assertEqual(SEALED_ZERO_SIGNAL, payload["notification_type"])
        self.assertEqual("WarrantScope Shadow", payload["title"])
        self.assertEqual("9/10 已封存｜N Compact 0 檔", payload["message"])

    def test_success_signal_notification_payload_uses_optional_name(self):
        payload = build_sealed_payload(
            "20260910",
            [
                {"stock_id": "2330", "stock_name": "台積電"},
                {"stock_id": "XXXX", "stock_name": ""},
            ],
        )
        self.assertEqual(SEALED_WITH_SIGNALS, payload["notification_type"])
        self.assertEqual(
            "9/10 已封存｜N Compact 2 檔：2330 台積電、XXXX",
            payload["message"],
        )

    def test_max_displayed_stock_count_is_five(self):
        rows = [
            {"stock_id": f"{code:04d}", "stock_name": f"股票{code}"}
            for code in range(1, 8)
        ]
        message = build_sealed_payload("20260910", rows)["message"]
        self.assertIn("0005 股票5", message)
        self.assertNotIn("0006 股票6", message)
        self.assertNotIn("0007 股票7", message)
        self.assertTrue(message.endswith("，另 2 檔"))

    def test_failure_payloads_are_short_and_classified(self):
        generic = build_failure_payload("20260910", "fixture incomplete")
        historical = build_failure_payload(
            "20260910", "historical preflight failed closed: hash"
        )
        self.assertEqual(FINAL_READINESS_FAILED, generic["notification_type"])
        self.assertEqual("9/10 尚未封存｜Readiness FAIL", generic["message"])
        self.assertEqual(
            HISTORICAL_PREFLIGHT_FAILED,
            historical["notification_type"],
        )
        self.assertEqual(
            "9/10 Shadow FAIL｜Historical preflight",
            historical["message"],
        )

    def test_osascript_uses_absolute_path_literal_argv_and_no_shell(self):
        title = 'title "$(touch bad)"'
        message = "message `touch bad`\n下一行"
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch(
            "shadow_daily_runner.notifications.subprocess.run",
            return_value=completed,
        ) as run:
            notifications._send_macos_notification(title, message)
        command = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual("/usr/bin/osascript", command[0])
        self.assertIn("on run argv", command[2])
        self.assertNotIn(title, command[2])
        self.assertNotIn(message, command[2])
        self.assertEqual([title, message], command[-2:])
        self.assertIs(options["shell"], False)
        self.assertFalse(options["check"])


class NotificationDeliveryTests(unittest.TestCase):
    def test_duplicate_notification_suppression_persists_required_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(Path(temporary))
            payload = build_sealed_payload("20260910", [])
            sender_calls: list[tuple[str, str]] = []

            def sender(title: str, message: str) -> None:
                sender_calls.append((title, message))

            first = deliver_notification(payload, cfg=cfg, sender=sender)
            second = deliver_notification(payload, cfg=cfg, sender=sender)
            self.assertEqual("SENT", first["status"])
            self.assertEqual("SUPPRESSED_DUPLICATE", second["status"])
            self.assertEqual(1, len(sender_calls))
            state = json.loads(
                (cfg.runtime_dir / "notifications" / "notification_state.json").read_text()
            )
            record = state["notifications"][0]
            for name in (
                "signal_date",
                "notification_type",
                "payload_hash",
                "sent_at",
                "success",
            ):
                self.assertIn(name, record)
            self.assertTrue(record["success"])

    def test_failed_delivery_is_retryable_and_logged_without_second_layer(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(Path(temporary))
            payload = build_failure_payload("20260910")
            calls = 0

            def sender(_title: str, _message: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("fixture denied")

            first = deliver_notification(payload, cfg=cfg, sender=sender)
            second = deliver_notification(payload, cfg=cfg, sender=sender)
            self.assertEqual("FAILED", first["status"])
            self.assertEqual("SENT", second["status"])
            self.assertEqual(2, calls)
            log = (cfg.logs_dir / "notification.log").read_text(encoding="utf-8")
            self.assertIn("fixture denied", log)
            self.assertIn('"result": "FAILED"', log)

    def test_sealed_target_retry_does_not_notify_twice(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(Path(temporary))
            seed_verified_target(cfg)
            with patch("shadow_daily_runner.notifications._send_macos_notification") as send:
                first = notify_sealed_target("20260910", cfg)
                second = notify_sealed_target("20260910", cfg)
            self.assertEqual("SENT", first["status"])
            self.assertEqual("SUPPRESSED_DUPLICATE", second["status"])
            send.assert_called_once_with(
                "WarrantScope Shadow",
                "9/10 已封存｜N Compact 0 檔",
            )

    def test_notification_failure_does_not_alter_any_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(Path(temporary))
            seed_verified_target(cfg)
            before = {
                path.name: path.read_bytes()
                for path in cfg.shadow_store_dir.iterdir()
                if path.is_file()
            }
            with patch(
                "shadow_daily_runner.notifications._send_macos_notification",
                side_effect=OSError("notification disabled"),
            ):
                result = notify_sealed_target("20260910", cfg)
            after = {
                path.name: path.read_bytes()
                for path in cfg.shadow_store_dir.iterdir()
                if path.is_file()
            }
            self.assertEqual("FAILED", result["status"])
            self.assertEqual(before, after)

    def test_test_notification_does_not_create_dedup_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(Path(temporary))
            result = send_test_notification(cfg, sender=lambda _title, _message: None)
            self.assertEqual("NOTIFICATION_TEST_SENT", result["status"])
            self.assertFalse(
                (cfg.runtime_dir / "notifications" / "notification_state.json").exists()
            )


class NotificationRunnerIntegrationTests(unittest.TestCase):
    def _run_failed_attempt(self, hour: int, minute: int, cfg):
        return attempt(
            now=datetime(
                2026,
                9,
                10,
                hour,
                minute,
                tzinfo=ZoneInfo("Asia/Taipei"),
            ),
            cfg=cfg,
            prepare=failed_preparation(cfg),
        )

    def test_1430_fail_does_not_notify(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(Path(temporary))
            seed_ledgers(cfg)
            with patch("shadow_daily_runner.notifications._send_macos_notification") as send:
                result = self._run_failed_attempt(14, 30, cfg)
            self.assertEqual("SKIPPED_BEFORE_FINAL_ATTEMPT", result["notification"]["status"])
            send.assert_not_called()

    def test_1500_fail_does_not_notify(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(Path(temporary))
            seed_ledgers(cfg)
            with patch("shadow_daily_runner.notifications._send_macos_notification") as send:
                result = self._run_failed_attempt(15, 0, cfg)
            self.assertEqual("SKIPPED_BEFORE_FINAL_ATTEMPT", result["notification"]["status"])
            send.assert_not_called()

    def test_1530_fail_does_not_notify(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(Path(temporary))
            seed_ledgers(cfg)
            with patch("shadow_daily_runner.notifications._send_macos_notification") as send:
                result = self._run_failed_attempt(15, 30, cfg)
            self.assertEqual("SKIPPED_BEFORE_FINAL_ATTEMPT", result["notification"]["status"])
            send.assert_not_called()

    def test_final_1600_fail_notifies_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(Path(temporary))
            seed_ledgers(cfg)
            with patch("shadow_daily_runner.notifications._send_macos_notification") as send:
                first = self._run_failed_attempt(16, 0, cfg)
                second = self._run_failed_attempt(16, 0, cfg)
            self.assertEqual("READINESS_FAILED_NO_LEDGER_WRITE", first["status"])
            self.assertEqual("SENT", first["notification"]["status"])
            self.assertEqual("SUPPRESSED_DUPLICATE", second["notification"]["status"])
            send.assert_called_once_with(
                "WarrantScope Shadow",
                "9/10 尚未封存｜Readiness FAIL",
            )

    def test_final_window_comes_from_configured_attempt_times(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(
                Path(temporary),
                attempt_times=("14:30", "15:45"),
            )
            seed_ledgers(cfg)
            with patch("shadow_daily_runner.notifications._send_macos_notification") as send:
                result = self._run_failed_attempt(15, 45, cfg)
            self.assertEqual("SENT", result["notification"]["status"])
            send.assert_called_once()

    def test_historical_preflight_fail_uses_distinct_final_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(Path(temporary))
            seed_ledgers(cfg)
            local = datetime(2026, 9, 10, 16, 0, tzinfo=ZoneInfo("Asia/Taipei"))
            with patch("shadow_daily_runner.notifications._send_macos_notification") as send:
                result = attempt(
                    now=local,
                    cfg=cfg,
                    prepare=failed_preparation(cfg, historical=True),
                )
            self.assertEqual(
                HISTORICAL_PREFLIGHT_FAILED,
                result["notification"]["notification_type"],
            )
            send.assert_called_once_with(
                "WarrantScope Shadow",
                "9/10 Shadow FAIL｜Historical preflight",
            )

    def test_notification_transport_failure_does_not_fail_success_pipeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = temporary_config(root)
            seed_ledgers(cfg)
            archive = root / "ready.zip"
            archive.write_bytes(b"fixture")
            calendar = root / "calendar.csv"
            calendar.write_text("date\n2026-09-10\n", encoding="utf-8")

            def ready_prepare(**_kwargs):
                return PreparedInputs(
                    target_date="20260910",
                    trading_day=True,
                    ready=True,
                    archives=(archive,),
                    calendar_path=calendar,
                    audit_path=root / "ready.json",
                    audit={"status": "READY"},
                )

            daily = {
                "signal_date": "20260910",
                "actual_orders": 0,
                "actual_fills": 0,
                "outcomes": {"status": "NO_ACTIVE_SIGNALS"},
                "input_manifest_hash": "d" * 64,
                "run_manifest": "runs/fixture/run_manifest.json",
                "scan": {"status": "APPENDED", "appended_signals": 0},
                "signal_count": 0,
            }
            status = verified_status("20260910")

            def run_json(command, _cfg):
                if command[-1] == "status":
                    (cfg.shadow_store_dir / "shadow_status.json").write_text(
                        json.dumps(status), encoding="utf-8"
                    )
                    return {"status": status}
                return daily

            with (
                patch("shadow_daily_runner.runner._run_json", side_effect=run_json),
                patch(
                    "shadow_daily_runner.notifications._send_macos_notification",
                    side_effect=OSError("notifications denied"),
                ),
            ):
                result = attempt(
                    now=datetime(
                        2026,
                        9,
                        10,
                        14,
                        30,
                        tzinfo=ZoneInfo("Asia/Taipei"),
                    ),
                    cfg=cfg,
                    prepare=ready_prepare,
                )
            self.assertEqual("SUCCESS", result["status"])
            self.assertEqual("FAILED", result["notification"]["status"])
            self.assertEqual(0, result["actual_orders"])
            self.assertEqual(0, result["actual_fills"])
            self.assertEqual(0, result["broker_connections"])

    def test_notification_failure_does_not_change_failed_pipeline_or_ledgers(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(Path(temporary))
            before = seed_ledgers(cfg)
            with patch(
                "shadow_daily_runner.notifications._send_macos_notification",
                side_effect=OSError("notifications denied"),
            ):
                result = self._run_failed_attempt(16, 0, cfg)
            self.assertEqual("READINESS_FAILED_NO_LEDGER_WRITE", result["status"])
            self.assertEqual("FAILED", result["notification"]["status"])
            for name, payload in before.items():
                self.assertEqual(payload, (cfg.shadow_store_dir / name).read_bytes())

    def test_status_adds_notification_without_changing_safety_counters(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(Path(temporary))
            seed_ledgers(cfg)
            payload = build_sealed_payload("20260910", [])
            deliver_notification(payload, cfg=cfg, sender=lambda _title, _message: None)
            result = runner_status(cfg)
            self.assertEqual("20260910", result["notification"]["last_signal_date"])
            self.assertEqual(SEALED_ZERO_SIGNAL, result["notification"]["last_type"])
            self.assertTrue(result["notification"]["last_success"])
            self.assertEqual(0, result["actual_orders"])
            self.assertEqual(0, result["actual_fills"])
            self.assertEqual(0, result["broker_connections"])


class NotificationCliTests(unittest.TestCase):
    def test_test_notification_sends_once_and_never_runs_strategy_paths(self):
        sent = {
            "status": "NOTIFICATION_TEST_SENT",
            "title": "WarrantScope Shadow",
            "message": "通知測試成功",
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
        }
        output = io.StringIO()
        with (
            patch("shadow_daily_runner.main.send_test_notification", return_value=sent) as notify,
            patch("shadow_daily_runner.main.attempt") as run_attempt,
            patch("shadow_daily_runner.main.prepare_inputs") as prepare,
            patch("shadow_daily_runner.main.repair_history") as repair,
            patch("shadow_daily_runner.main.run_historical_preflight") as preflight,
            redirect_stdout(output),
        ):
            code = main(["test-notification"])
        self.assertEqual(0, code)
        notify.assert_called_once_with(CFG)
        run_attempt.assert_not_called()
        prepare.assert_not_called()
        repair.assert_not_called()
        preflight.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual("NOTIFICATION_TEST_SENT", payload["status"])
        self.assertEqual(0, payload["actual_orders"])
        self.assertEqual(0, payload["actual_fills"])
        self.assertEqual(0, payload["broker_connections"])

    def test_test_notification_failure_has_clear_status_and_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(Path(temporary))
            result = send_test_notification(
                cfg,
                sender=lambda _title, _message: (_ for _ in ()).throw(
                    OSError("not authorized")
                ),
            )
        self.assertEqual("NOTIFICATION_TEST_FAILED", result["status"])
        self.assertIn("not authorized", result["diagnostic"])
        self.assertEqual(0, result["actual_orders"])
        self.assertEqual(0, result["actual_fills"])
        self.assertEqual(0, result["broker_connections"])

    def test_empty_notification_status_is_additive_and_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = temporary_config(Path(temporary))
            result = notification_status(cfg)
        self.assertIsNone(result["last_signal_date"])
        self.assertIsNone(result["last_type"])
        self.assertIsNone(result["last_sent_at"])
        self.assertIsNone(result["last_success"])


if __name__ == "__main__":
    unittest.main()
