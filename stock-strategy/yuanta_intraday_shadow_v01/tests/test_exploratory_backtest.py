import json
from pathlib import Path
import tempfile
import unittest

from yuanta_intraday_shadow_v01.collector import AppendOnlyRun, WatchItem
from yuanta_intraday_shadow_v01.exploratory_backtest import build_candidates, replay_strategy


class ExploratoryBacktestTests(unittest.TestCase):
    def _run(self, root: Path) -> Path:
        items = [WatchItem("1001", "測試股", 1, 0.1, "TWSE")]
        run = AppendOnlyRun(root, {"signal_date": "20260921", "seal_hash": "a" * 64}, items, {}, compress=True)
        for stamp, price, bid, ask in (
            ("2026-09-22T01:30:00.000Z", 100, 99.5, 100),
            ("2026-09-22T01:34:59.000Z", 102, 101.5, 102),
            ("2026-09-22T01:35:01.000Z", 102, 101.5, 102),
            ("2026-09-22T05:25:00.000Z", 105, 104.5, 105),
        ):
            run.append("ticks", {
                "received_at": stamp, "stock_id": "1001", "deal_price": str(price),
                "deal_volume": "10", "buy_price": str(bid), "sell_price": str(ask),
            })
        run.finalize(status="COMPLETE", started_at="2026-09-22T01:29:00.000Z", ended_at="2026-09-22T05:30:00.000Z")
        return run.run_dir

    def test_cost_aware_long_replay_never_orders(self):
        with tempfile.TemporaryDirectory() as temp:
            candidates, coverage = build_candidates([self._run(Path(temp))])
            trades = replay_strategy("momentum_long", candidates)
            self.assertEqual(coverage["coverage_status"], "PARTIAL_SESSION")
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0].quantity, 1000)
            self.assertGreater(trades[0].commission + trades[0].sell_tax, 0)
            self.assertGreater(trades[0].net_pnl, 0)

    def test_manifest_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp))
            path = run_dir / "run_manifest.json"
            manifest = json.loads(path.read_text())
            manifest["status"] = "FAILED"
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError, "manifest hash mismatch"):
                build_candidates([run_dir])


if __name__ == "__main__":
    unittest.main()
