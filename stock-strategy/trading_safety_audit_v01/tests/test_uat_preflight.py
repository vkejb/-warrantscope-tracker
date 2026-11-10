import json
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest

from trading_safety_audit_v01.uat_preflight import build_preflight


class UatPreflightTests(unittest.TestCase):
    def test_preflight_is_read_only_and_sanitised(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "position_baseline.json").write_text("{}\n", encoding="utf-8")
            (runtime / "position_baseline.meta.json").write_text("{}\n", encoding="utf-8")
            (runtime / "heartbeat.json").write_text(
                json.dumps({"state": "STOPPED_CLEAN", "submit_live": False}),
                encoding="utf-8",
            )
            db = sqlite3.connect(runtime / "live-orders.sqlite")
            db.execute("CREATE TABLE orders(id TEXT)")
            db.commit(); db.close()
            plist = root / "agent.plist"
            plist.write_bytes(plistlib.dumps({
                "Label": "test",
                "WorkingDirectory": str(root),
                "ProgramArguments": ["python", "serve", "--runtime-dir", str(runtime)],
            }))
            before = (runtime / "live-orders.sqlite").read_bytes()
            report = build_preflight(launch_agent=plist)
            after = (runtime / "live-orders.sqlite").read_bytes()
            self.assertEqual(before, after)
            self.assertEqual(report["status"], "READY_FOR_EXPLICIT_BROKER_READONLY_PROBE")
            self.assertEqual(report["broker_query_boundary"], "READ_ONLY_ALLOWLIST_VERIFIED")
            self.assertEqual(report["actual_orders"], 0)
            self.assertFalse(report["broker_login_performed"])
            self.assertNotIn("account", json.dumps(report).lower())


if __name__ == "__main__":
    unittest.main()
