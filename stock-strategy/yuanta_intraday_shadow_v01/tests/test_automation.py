from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from yuanta_intraday_shadow_v01 import auto_runner
from yuanta_intraday_shadow_v01.yuanta_keychain import BINDING_SERVICE, SERVICES, _binding, load_credentials, status


class AutomationTests(unittest.TestCase):
    def test_keychain_binding_and_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            pfx = Path(temp) / "certificate.pfx"; pfx.write_bytes(b"fixture")
            values = {"pfx": str(pfx), "pfx_password": "secret-a", "account": "1234-1234567", "trading_password": "secret-b"}
            reverse = {service: values[key] for key, service in SERVICES.items()}
            reverse[BINDING_SERVICE] = _binding(values)
            with patch("yuanta_intraday_shadow_v01.yuanta_keychain._read", side_effect=lambda service, **_: reverse.get(service)):
                loaded = load_credentials()
                self.assertEqual(loaded["account"], "S12341234567")
                self.assertEqual(status()["status"], "KEYCHAIN_CONFIGURED")

    def test_binding_mismatch_fails_closed(self):
        with patch("yuanta_intraday_shadow_v01.yuanta_keychain._read", return_value="wrong"):
            with self.assertRaises(RuntimeError):
                load_credentials()

    def test_readiness_uses_official_calendar_and_previous_seal(self):
        zone = ZoneInfo("Asia/Taipei")
        with tempfile.TemporaryDirectory() as temp:
            calendar = Path(temp) / "calendar.csv"
            calendar.write_text("date\n2026-09-21\n2026-09-22\n")
            seal = {"signal_date": "20260921", "stocks": [{}] * 30}
            with patch.object(auto_runner, "CALENDAR", calendar), patch.object(auto_runner, "latest_seal", return_value=seal):
                self.assertEqual(auto_runner._readiness(datetime(2026, 9, 22, 8, 50, tzinfo=zone)), ("READY", "20260921"))

    def test_nontrading_day_is_quiet(self):
        zone = ZoneInfo("Asia/Taipei")
        with tempfile.TemporaryDirectory() as temp:
            calendar = Path(temp) / "calendar.csv"; calendar.write_text("date\n2026-09-21\n")
            with patch.object(auto_runner, "CALENDAR", calendar):
                self.assertEqual(auto_runner._readiness(datetime(2026, 9, 22, 8, 50, tzinfo=zone)), ("NON_TRADING_DAY", ""))

    def test_automation_sources_have_no_order_api(self):
        root = Path(__file__).parents[1]
        source = "".join((root / name).read_text() for name in ("auto_runner.py", "yuanta_keychain.py", "postprocess.py"))
        for forbidden in ("SendStockOrder", "SendFutureOrder", "StockOrder(", "FutureOrder("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
