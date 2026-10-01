from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from paper_shadow_v01.dual_track import (
    PRODUCTION,
    REFERENCE,
    build_dual_track,
    publish_dual_track,
)
from paper_shadow_v01.runner import PAPER_CONTRACT_HASH
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file


class DualTrackTests(unittest.TestCase):
    def test_only_fixed_a_b_are_paired_with_same_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "days" / "20261002" / "paper-one"
            target.mkdir(parents=True)
            base = {
                "session_date": "20261002", "stock_id": "3094",
                "entry_time": "2026-10-02T09:10:00+08:00", "entry_price": 100,
                "quantity": 1000, "exit_time": "2026-10-02T09:20:00+08:00",
                "exit_price": 99, "exit_reason": "TEST", "holding_seconds": 600,
                "mfe_net_pnl_at_exit": 500, "mae_net_pnl_at_exit": -1200,
                "trigger_to_fill_delay_seconds": 0, "trigger_to_fill_price_gap": 0,
                "post_exit_5m_scorable": True, "post_exit_5m_best_net_pnl": 200,
            }
            rows = [
                {**base, "strategy_variant": PRODUCTION, "net_pnl": -1200},
                {**base, "strategy_variant": REFERENCE, "net_pnl": -500},
                {**base, "strategy_variant": "UNRELATED_NEAR_MISS", "net_pnl": 99999},
            ]
            trades = target / "paper_trades.jsonl"
            trades.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
            )
            manifest = {
                "session_date": "20261002", "paper_run_id": "paper-one",
                "source_run_id": "source-one",
                "paper_contract_hash": PAPER_CONTRACT_HASH,
                "artifacts": {"paper_trades.jsonl": sha256_file(trades)},
            }
            manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
            (target / "manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
            report = build_dual_track(root)
        self.assertEqual(report["paired_trades"], 1)
        self.assertEqual(report["net_difference_twd"], 700)
        self.assertEqual(report["losses_reduced"], 1)
        self.assertFalse(report["near_miss_included"])
        self.assertTrue(report["pairs"][0]["same_entry_and_quantity"])
        self.assertEqual(
            report["frozen_contract"]["paper_contract_hash"],
            PAPER_CONTRACT_HASH,
        )
        self.assertEqual(report["a_profit_giveback_twd"], 1700)
        self.assertEqual(report["b_profit_giveback_twd"], 1000)
        self.assertEqual(report["maximum_single_trade_improvement_share"], 1.0)
        self.assertEqual(
            report["data_qualification_by_session"][0][
                "actual_live_input_decision_parity"
            ],
            "UNKNOWN",
        )

    def test_zero_pair_has_explicit_reason_and_atomic_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "paper"
            output = Path(temp) / "report.json"
            report = publish_dual_track(
                root,
                source_runtime_dir=Path(temp) / "source",
                output=output,
            )
            written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["paired_trades"], 0)
        self.assertEqual(report["zero_pair_reason"], "NO_VERIFIED_PAPER_DAYS")
        self.assertEqual(written["paired_trades"], 0)
        self.assertNotIn("output_path", written)


if __name__ == "__main__":
    unittest.main()
