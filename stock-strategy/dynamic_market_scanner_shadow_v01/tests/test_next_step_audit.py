from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from dynamic_market_scanner_shadow_v01.next_step_audit import _capture_coverage, _same_time_rank


class NextStepAuditTest(unittest.TestCase):
    def test_chip_order_cannot_preempt_later_signal(self):
        early = {"session_date": "20261116", "stock_id": "1111",
                 "entry_time": "2026-11-16T09:10:00+08:00", "chip_pass": "False"}
        later = {"session_date": "20261116", "stock_id": "2222",
                 "entry_time": "2026-11-16T09:10:01+08:00", "chip_pass": "True"}
        rs = {("20261116", "1111"): 0.01, ("20261116", "2222"): 0.03}
        self.assertEqual(_same_time_rank([later, early], rs)[0]["stock_id"], "1111")
        later["entry_time"] = early["entry_time"]
        self.assertEqual(_same_time_rank([later, early], rs)[0]["stock_id"], "2222")

    def test_capture_missing_or_after_open_is_not_presignal(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            result = _capture_coverage("20261116", "20261113", base)
            self.assertTrue(all(row["status"] == "MISSING" for row in result.values()))
            target = base / "presignal_captures/20261116/TWSE_INSTITUTIONAL"
            target.mkdir(parents=True)
            payload = b"{}"
            (target / "raw.json").write_bytes(payload)
            import hashlib
            (target / "metadata.json").write_text(json.dumps({
                "source": "TWSE_INSTITUTIONAL", "source_day": "20261113",
                "target_day": "20261116", "sha256": hashlib.sha256(payload).hexdigest(),
                "status": "PRESIGNAL_CAPTURE",
                "captured_at_utc": datetime(2026, 11, 16, 2, tzinfo=timezone.utc).isoformat(),
            }), encoding="utf-8")
            result = _capture_coverage("20261116", "20261113", base)
            self.assertEqual(result["TWSE_INSTITUTIONAL"]["status"], "INVALID")


if __name__ == "__main__":
    unittest.main()
