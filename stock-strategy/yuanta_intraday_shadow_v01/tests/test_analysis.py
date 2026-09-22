from pathlib import Path
import tempfile
import unittest

from yuanta_intraday_shadow_v01.analysis import SPEC_HASH, analyze_run, publish_analysis
from yuanta_intraday_shadow_v01.collector import AppendOnlyRun, WatchItem, utc_now


class AnalysisTests(unittest.TestCase):
    def _run(self, root: Path) -> Path:
        stocks = [WatchItem(str(1000 + i), f"股票{i}", i, i / 100.0, "TWSE") for i in range(1, 31)]
        run = AppendOnlyRun(root, {"signal_date": "20260921", "seal_hash": "a" * 64}, stocks, {})
        for item in stocks:
            for index in range(12):
                price = 100 + index * 0.1
                run.append("ticks", {
                    "received_at": f"2026-09-22T01:30:{index:02d}.000Z", "signal_date": "20260921",
                    "stock_id": item.stock_id, "stock_name": item.stock_name, "market": item.market,
                    "stage_a_rank": item.rank, "stage_a_score": item.score,
                    "deal_price": str(price), "deal_volume": "10",
                })
                run.append("books", {
                    "received_at": f"2026-09-22T01:30:{index:02d}.100Z", "signal_date": "20260921",
                    "stock_id": item.stock_id, "stock_name": item.stock_name, "market": item.market,
                    "stage_a_rank": item.rank, "stage_a_score": item.score,
                    "buy_prices": ["99.9"] * 5, "sell_prices": ["100.1"] * 5,
                    "buy_volumes": ["20"] * 5, "sell_volumes": ["10"] * 5,
                })
        run.finalize(status="COMPLETE", started_at=utc_now(), ended_at=utc_now())
        return run.run_dir

    def test_features_cover_exact_watchlist(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp) / "quotes")
            rows, summary = analyze_run(run_dir)
            self.assertEqual(len(rows), 30)
            self.assertEqual(summary["stocks"], 30)
            self.assertTrue(all(row["spec_hash"] == SPEC_HASH for row in rows))
            self.assertTrue(all(row["state"] == "SUPPORTED_MOMENTUM" for row in rows))

    def test_analysis_is_immutable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self._run(root / "quotes")
            publish_analysis(run_dir, root / "analyses")
            with self.assertRaises(FileExistsError):
                publish_analysis(run_dir, root / "analyses")

    def test_tampered_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp) / "quotes")
            with (run_dir / "ticks.jsonl").open("a") as handle:
                handle.write("{}\n")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                analyze_run(run_dir)

    def test_no_order_api(self):
        root = Path(__file__).parents[1]
        source = (root / "analysis.py").read_text() + (root / "analysis_main.py").read_text()
        for forbidden in ("SendStockOrder", "SendFutureOrder", "StockOrder(", "FutureOrder("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
