from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from dynamic_market_scanner_shadow_v01.prospective_v2 import (
    PROSPECTIVE_POLICY_HASH, aggregate_prospective, publish_prospective_v2,
)
from dynamic_market_scanner_shadow_v01.prospective_v2_main import (
    load_or_publish_session,
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

    def test_current_metrics_expose_concentration_and_drawdown(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.write_day(root, "20261110", 100)
            self.write_day(root, "20261111", -50)
            self.write_day(root, "20261112", 20)
            metrics = aggregate_prospective(root)["current_metrics"]["BASE"]
            self.assertEqual(metrics["wins"], 2)
            self.assertEqual(metrics["losses"], 1)
            self.assertAlmostEqual(metrics["win_rate"], 2 / 3)
            self.assertAlmostEqual(metrics["mean_trade_twd"], 70 / 3)
            self.assertEqual(metrics["median_trade_twd"], 20)
            self.assertEqual(metrics["best_day"], "20261110")
            self.assertEqual(metrics["net_pnl_ex_best_day_twd"], -30)
            self.assertEqual(metrics["worst_day"], "20261111")
            self.assertEqual(metrics["max_drawdown_twd"], 50)

    def test_known_date_is_rejected_before_source_access(self):
        with self.assertRaisesRegex(RuntimeError, "rejects known date"):
            publish_prospective_v2(Path("/definitely/missing"), {
                "session_date": "20261109", "coverage_status": "FULL_SESSION"
            })

    def test_offline_entry_reuses_matching_sealed_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run_dir = root / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            source = {"run_id": "run-1", "manifest_hash": "source-hash"}
            (run_dir / "run_manifest.json").write_bytes(canonical_bytes(source) + b"\n")
            session_dir = root / "session_analyses" / "run-1"
            session_dir.mkdir(parents=True)
            session = {
                "session_date": "20261110", "coverage_status": "FULL_SESSION",
                "source_run_id": "run-1", "source_manifest_hash": "source-hash",
                "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
            }
            session["analysis_hash"] = hashlib.sha256(canonical_bytes(session)).hexdigest()
            (session_dir / "session_manifest.json").write_bytes(
                canonical_bytes(session) + b"\n"
            )
            self.assertEqual(load_or_publish_session(run_dir, root), session)

    def test_offline_entry_rejects_tampered_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run_dir = root / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            source = {"run_id": "run-1", "manifest_hash": "source-hash"}
            (run_dir / "run_manifest.json").write_bytes(canonical_bytes(source) + b"\n")
            session_dir = root / "session_analyses" / "run-1"
            session_dir.mkdir(parents=True)
            (session_dir / "session_manifest.json").write_text(
                json.dumps({
                    "analysis_hash": "wrong", "source_run_id": "run-1",
                    "source_manifest_hash": "source-hash",
                }), encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                load_or_publish_session(run_dir, root)


if __name__ == "__main__":
    unittest.main()
