from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from dynamic_market_scanner_shadow_v01.prospective_v2 import (
    PROSPECTIVE_POLICY_HASH, aggregate_prospective, publish_prospective_v2,
)
from dynamic_market_scanner_shadow_v01.strict_replay import _write_csv
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file


class ProspectiveV2Test(unittest.TestCase):
    def write_day(self, root: Path, day: str, pnl: float):
        target = root / "days" / day / f"run-{day}"
        target.mkdir(parents=True)
        trades = [
            {"session_date": day, "variant": "V2_RECENT_VOLUME_ACCELERATION",
             "scenario": scenario, "status": "SCORED", "net_pnl": pnl,
             "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0}
            for scenario in ("BASE", "STRESS")
        ]
        _write_csv(target / "trades.csv", trades)
        manifest = {
            "session_date": day, "prospective_policy_hash": PROSPECTIVE_POLICY_HASH,
            "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
            "artifacts": {"trades.csv": sha256_file(target / "trades.csv")},
        }
        manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        (target / "manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")

    def test_empty_runtime_is_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            result = aggregate_prospective(Path(folder))
            self.assertEqual(result["status"], "PENDING_MINIMUM_NEW_SAMPLE")
            self.assertEqual(result["prospective_session_count"], 0)

    def test_first_mature_prefix_is_final(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            start = date(2026, 11, 10)
            for index in range(10):
                pnl = -50 if index == 0 else 100
                self.write_day(root, (start + timedelta(days=index)).strftime("%Y%m%d"), pnl)
            # A later catastrophic day must not rewrite the first frozen review.
            self.write_day(root, (start + timedelta(days=10)).strftime("%Y%m%d"), -10_000)
            result = aggregate_prospective(root)
            self.assertEqual(result["status"], "ELIGIBLE_FOR_SEPARATE_UAT_REVIEW_ONLY")
            self.assertEqual(result["review_cutoff_date"], "20261119")
            self.assertGreater(result["review_metrics"]["BASE"]["net_pnl_twd"], 0)
            self.assertLess(result["current_metrics"]["BASE"]["net_pnl_twd"], 0)

    def test_known_date_is_rejected_before_source_access(self):
        with self.assertRaisesRegex(RuntimeError, "rejects known date"):
            publish_prospective_v2(Path("/definitely/missing"), {
                "session_date": "20261109", "coverage_status": "FULL_SESSION"
            })


if __name__ == "__main__":
    unittest.main()
