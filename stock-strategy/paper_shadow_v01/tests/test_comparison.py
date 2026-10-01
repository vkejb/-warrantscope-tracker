from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from paper_shadow_v01.comparison import (
    BUFFERED_VARIANTS,
    PAPER_CONTRACT_HASH,
    build_comparison,
    write_comparison,
)
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file


class PaperComparisonTests(unittest.TestCase):
    @staticmethod
    def _day(
        root: Path,
        day: str,
        production: float,
        buffer30: float,
        buffer40: float,
        *,
        buffered_exit_reason: str = "BUFFERED_NET_MFE_PROFIT_PROTECTION",
        contract_hash: str = PAPER_CONTRACT_HASH,
    ) -> None:
        target = root / "days" / day / f"paper-{day}"
        target.mkdir(parents=True)
        rows = [
            {
                "session_date": day, "stock_id": "3094", "entry_time": f"{day[:4]}-{day[4:6]}-{day[6:]}T09:10:00+08:00",
                "strategy_variant": "PRODUCTION_ANTI_CHASE", "net_pnl": production,
                "exit_reason": "HARD_EXIT",
                "mfe_net_pnl_at_exit": max(production, 1000.0),
                "post_exit_best_net_pnl": 1200.0,
                "full_path_mfe_net_pnl": 1200.0,
            },
            {
                "session_date": day, "stock_id": "3094", "entry_time": f"{day[:4]}-{day[4:6]}-{day[6:]}T09:10:00+08:00",
                "strategy_variant": BUFFERED_VARIANTS[0], "net_pnl": buffer30,
                "exit_reason": buffered_exit_reason,
                "mfe_net_pnl_at_exit": max(1000.0, buffer30),
                "post_exit_best_net_pnl": max(1200.0, buffer30),
                "full_path_mfe_net_pnl": max(1200.0, buffer30),
            },
            {
                "session_date": day, "stock_id": "3094", "entry_time": f"{day[:4]}-{day[4:6]}-{day[6:]}T09:10:00+08:00",
                "strategy_variant": BUFFERED_VARIANTS[1], "net_pnl": buffer40,
                "exit_reason": buffered_exit_reason,
                "mfe_net_pnl_at_exit": max(1000.0, buffer40),
                "post_exit_best_net_pnl": max(1200.0, buffer40),
                "full_path_mfe_net_pnl": max(1200.0, buffer40),
            },
        ]
        trades = target / "paper_trades.jsonl"
        trades.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        manifest = {
            "session_date": day,
            "paper_run_id": f"paper-{day}",
            "status": "PAPER_VARIANTS_EVALUATED",
            "paper_contract_hash": contract_hash,
            "artifacts": {"paper_trades.jsonl": sha256_file(trades)},
        }
        manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        (target / "manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")

    def test_verified_cross_day_pairing_and_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._day(root, "20261002", -1000.0, -400.0, -200.0)
            self._day(root, "20261005", 1000.0, 1200.0, 1300.0)
            report = build_comparison(root)
            self.assertEqual(report["status"], "COLLECTING")
            self.assertEqual(report["verified_session_count"], 2)
            self.assertEqual(
                report["shadow_evidence_gate_definition"]
                ["original_winners_made_nonpositive_allowed"],
                0,
            )
            self.assertEqual(
                report["paired_comparisons"][BUFFERED_VARIANTS[1]]["net_difference_twd"],
                1100.0,
            )
            self.assertEqual(report["metrics"][BUFFERED_VARIANTS[1]]["negative_mfe_exits"], 1)
            guard = report["paired_comparisons"][BUFFERED_VARIANTS[1]]
            self.assertEqual(guard["original_winners_made_nonpositive"], 0)
            self.assertTrue(guard["average_profit_retention_not_worse"])
            written = write_comparison(root)
            saved = json.loads((root / "comparison" / "latest.json").read_text())
            self.assertEqual(saved["report_hash"], written["report_hash"])

    def test_duplicate_day_is_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._day(root, "20261002", -1000.0, -400.0, -200.0)
            original = root / "days" / "20261002" / "paper-20261002"
            duplicate = root / "days" / "20261002" / "paper-duplicate"
            duplicate.mkdir()
            for source in original.iterdir():
                (duplicate / source.name).write_bytes(source.read_bytes())
            report = build_comparison(root)
            self.assertEqual(report["verified_session_count"], 0)
            self.assertEqual(report["ambiguous_duplicate_session_days"], ["20261002"])

    def test_previous_two_buffer_contract_remains_comparable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._day(
                root, "20261002", -1000.0, -400.0, -200.0,
                contract_hash=(
                    "ceff174a5c3ae16db529ceae9a08fd145e94e42310412c9da"
                    "931b75860a27966"
                ),
            )
            report = build_comparison(root)
            self.assertEqual(report["verified_session_count"], 1)
            self.assertEqual(
                report["paired_comparisons"][BUFFERED_VARIANTS[0]]["paired_trades"],
                1,
            )
            self.assertEqual(
                report["paired_comparisons"][BUFFERED_VARIANTS[2]]["paired_trades"],
                0,
            )

    def test_gate_rejects_candidate_that_cuts_largest_original_winner(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(20):
                day = f"202611{index + 1:02d}"
                production = 5000.0 if index == 0 else -1000.0
                candidate = 4000.0 if index == 0 else -100.0
                self._day(root, day, production, candidate, candidate)
            report = build_comparison(root)
            guard = report["paired_comparisons"][BUFFERED_VARIANTS[0]]
            self.assertGreater(guard["net_difference_twd"], 0)
            self.assertFalse(guard["largest_reference_winner_preserved"])
            self.assertFalse(guard["original_winner_pnl_not_worse"])
            self.assertFalse(guard["shadow_evidence_gate_pass"])

    def test_gate_can_pass_when_losses_and_profit_retention_both_improve(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(20):
                day = f"202612{index + 1:02d}"
                production = 5000.0 if index == 0 else -1000.0
                candidate = 5500.0 if index == 0 else -100.0
                self._day(
                    root, day, production, candidate, candidate,
                    buffered_exit_reason="RECOVERY_AWARE_EARLY_FAILURE",
                )
            report = build_comparison(root)
            guard = report["paired_comparisons"][BUFFERED_VARIANTS[0]]
            self.assertTrue(guard["average_loss_not_worse"])
            self.assertTrue(guard["original_winner_pnl_not_worse"])
            self.assertTrue(guard["largest_reference_winner_preserved"])
            self.assertTrue(guard["average_profit_retention_not_worse"])
            self.assertTrue(guard["shadow_evidence_gate_pass"])


if __name__ == "__main__":
    unittest.main()
