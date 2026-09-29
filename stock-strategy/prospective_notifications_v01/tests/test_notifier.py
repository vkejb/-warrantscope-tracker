from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest
import urllib.error
from unittest.mock import patch

from prospective_notifications_v01.notifier import chip_not_ready_message, chip_watch_message, daily_message, entry_state_message, notify, warning_message


class _TelegramResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return b'{"ok": true}'


class NotificationTests(unittest.TestCase):
    def test_success_is_idempotent_failed_is_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            with patch("prospective_notifications_v01.notifier._telegram", side_effect=[("FAILED", 503), ("SUCCESS", 200)]):
                self.assertEqual(notify("M", "20260916", "hash", "SEALED", "hello", ledger=ledger, providers=("TELEGRAM",))["TELEGRAM"], "FAILED")
                self.assertEqual(notify("M", "20260916", "hash", "SEALED", "hello", ledger=ledger, providers=("TELEGRAM",))["TELEGRAM"], "SUCCESS")
            self.assertEqual(notify("M", "20260916", "hash", "SEALED", "hello", ledger=ledger, providers=("TELEGRAM",))["TELEGRAM"], "ALREADY_SENT")
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertNotIn("token", ledger.read_text())

    def test_transient_network_failure_is_retried(self):
        with patch.dict("os.environ", {
            "WARRANTSCOPE_TELEGRAM_BOT_TOKEN": "private-token",
            "WARRANTSCOPE_TELEGRAM_CHAT_ID": "12345",
        }, clear=True), patch(
            "prospective_notifications_v01.notifier.urllib.request.urlopen",
            side_effect=[urllib.error.URLError("temporary"), _TelegramResponse()],
        ) as urlopen, patch("prospective_notifications_v01.notifier.time.sleep") as sleep:
            result = notify(
                "M", "20260929", "retry-hash", "SEALED", "hello",
                ledger=Path(tempfile.mkdtemp()) / "ledger.jsonl",
                providers=("TELEGRAM",),
            )
        self.assertEqual(result["TELEGRAM"], "SUCCESS")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_permanent_client_error_is_not_retried_and_is_sanitized(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "WARRANTSCOPE_TELEGRAM_BOT_TOKEN": "private-token",
            "WARRANTSCOPE_TELEGRAM_CHAT_ID": "12345",
        }, clear=True), patch(
            "prospective_notifications_v01.notifier.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("redacted", 401, "unauthorized", {}, None),
        ) as urlopen:
            ledger = Path(directory) / "ledger.jsonl"
            result = notify("M", "20260929", "bad-auth", "SEALED", "hello", ledger=ledger, providers=("TELEGRAM",))
            row = json.loads(ledger.read_text())
            ledger_text = ledger.read_text()
        self.assertEqual(result["TELEGRAM"], "FAILED")
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(row["error_category"], "HTTP_CLIENT_ERROR")
        self.assertEqual(row["attempt_count"], 1)
        self.assertNotIn("private-token", ledger_text)

    def test_not_configured_does_not_block(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {}, clear=True):
                result = notify("M", "20260916", "hash", "SEALED", "hello", ledger=Path(directory) / "ledger.jsonl", providers=("TELEGRAM",))
            self.assertEqual(result["TELEGRAM"], "NOT_CONFIGURED")

    def test_combined_daily_and_warning_sanitization(self):
        stage = {"status": "SEALED", "count": 30, "seal_hash": "abc123", "stocks": [{"rank": i, "stock_id": str(2000+i), "stock_name": "測試", "score": 0.1} for i in range(1, 31)]}
        text = daily_message("20260916", {"raw_n_retest_count": "1", "compact_count": "0", "record_hash": "nhash"}, stage)
        self.assertIn("Top30 完整名單", text)
        self.assertIn("Raw N_RETEST 1", text)
        self.assertIn("30. ", text)
        self.assertIn("不是買進訊號", text)
        self.assertIn("NO_SIGNAL", text)
        with self.assertRaises(ValueError):
            daily_message("20260916", {}, {**stage, "count": 29})
        self.assertNotIn("secret", warning_message("20260916", "M", "ERR", "/Users/a secret token"))

    def test_frozen_classifications_are_in_daily_message(self):
        stage = {"status": "SEALED", "count": 30, "seal_hash": "abc123", "stocks": [{"rank": i, "stock_id": str(2000+i), "stock_name": "測試", "score": 0.1} for i in range(1, 31)]}
        states = ("READY", "WATCH", "COOLING_BUT_WEAK", "OVERHEATED")
        entry = {"stage_a_seal_hash": "abc123", "seal_hash": "statehash", "stocks": [{"stock_id": str(2000+i), "stock_name": "測試", "classification": states[(i-1) % 4], "stage_a_rank": i, "stage_a_score": 0.1} for i in range(1, 31)]}
        text = daily_message("20260921", {"raw_n_retest_count": 0, "compact_count": 0}, stage, entry)
        for label in states:
            self.assertIn(label, text)
        supplement = entry_state_message("20260921", entry)
        self.assertIn("不受籌碼影響", supplement)
        self.assertLess(supplement.index("Stage A Top30 排行"), supplement.index("固定 Entry State"))
        self.assertIn("30. 2030 測試 0.1000", supplement)

    def test_chip_watch_is_never_presented_as_validated_prediction(self):
        text = chip_watch_message(
            "20260921", [{"stock_id": "3605", "stock_name": "宏致", "classification": "OVERHEATED", "chip_tags": ["外資買超"]}], "a" * 64,
            ready_sources=["TWSE_INSTITUTIONAL", "TPEX_INSTITUTIONAL"], missing_sources=["TWSE_MARGIN"],
        )
        self.assertIn("未驗證、非交易訊號", text)
        self.assertIn("未證明籌碼可穩定預測隔日漲停", text)
        self.assertIn("資料狀態：部分來源", text)
        self.assertIn("缺少：TWSE 融資融券", text)

    def test_final_not_ready_message_explains_no_candidate_was_sent(self):
        text = chip_not_ready_message("20260921", "OfficialNotReady: TWSE_MARGIN not published")
        self.assertIn("TWSE 融資融券", text)
        self.assertIn("未產生或發送", text)
        self.assertIn("fail-closed", text)


if __name__ == "__main__":
    unittest.main()
