from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from prospective_notifications_v01.notifier import daily_message, notify, warning_message


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


if __name__ == "__main__":
    unittest.main()
