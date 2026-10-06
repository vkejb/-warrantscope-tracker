"""Status must distinguish liveness, fresh quotes and exit-only recovery."""
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from yuanta_live_runtime_v01.trading_bot_service import build_status, render_status


class StatusSafetyTests(TestCase):
    def status(self, *, state, quote_age=None):
        with TemporaryDirectory(prefix="status-safety-") as temporary:
            path = Path(temporary)
            now = datetime.now(timezone.utc)
            row = {"at": now.isoformat(), "pid": 123, "state": state,
                   "environment": "PROD", "submit_live": True}
            if quote_age is not None:
                row["last_quote_at"] = (now - timedelta(seconds=quote_age)).isoformat()
            (path / "heartbeat.json").write_text(json.dumps(row))
            with patch("yuanta_live_runtime_v01.trading_bot_service.runtime_health",
                       return_value=SimpleNamespace(healthy=True)):
                return build_status(path)

    def test_healthy_exit_only_controller_never_claims_normal_market_monitoring(self):
        for age in (None, 0, 40):
            with self.subTest(age=age):
                status = self.status(state="EXIT_ONLY_RECOVERY", quote_age=age)
                self.assertFalse(status["monitoring_market"])
                self.assertTrue(status["exit_only_recovery"])
                text = render_status(status)
                self.assertIn("目前監控市場：否", text)
                self.assertIn("退出恢復：進行中", text)

    def test_running_without_fresh_quotes_is_not_certified_monitoring(self):
        for age in (None, 20, 40, -5):
            with self.subTest(age=age):
                self.assertFalse(self.status(state="RUNNING", quote_age=age)["monitoring_market"])
        status = self.status(state="RUNNING", quote_age=0)
        self.assertTrue(status["monitoring_market"])
        self.assertFalse(status["exit_only_recovery"])

    def test_preopen_wait_is_healthy_but_never_claims_market_monitoring(self):
        status = self.status(state="PREOPEN_WAITING")
        self.assertEqual(status["runtime_state"], "LIVE_PREOPEN_WAITING")
        self.assertFalse(status["monitoring_market"])
        self.assertEqual(status["quote_health"], "NO_QUOTE_YET")
        text = render_status(status)
        self.assertIn("實盤已武裝，等待開盤行情", text)
        self.assertIn("行情完整前禁止送單", text)
