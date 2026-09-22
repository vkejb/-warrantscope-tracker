from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from yuanta_intraday_shadow_v01.collector import AppendOnlyRun, WatchItem
from yuanta_intraday_shadow_v01.session_analysis import build_session_rows, publish_session_analysis


class SessionAnalysisTests(unittest.TestCase):
    def _full_run(self, root: Path) -> Path:
        stocks = [WatchItem(str(1000 + i), f"股票{i}", i, i / 100.0, "TWSE") for i in range(1, 31)]
        run = AppendOnlyRun(root, {"signal_date": "20260921", "seal_hash": "a" * 64}, stocks, {}, compress=True)
        stamps = ("01:01", "01:06", "01:16", "01:31", "02:01", "02:31", "05:29")
        for item in stocks:
            for index, hm in enumerate(stamps):
                received = f"2026-09-22T{hm}:00.000Z"
                run.append("ticks", {"received_at": received, "stock_id": item.stock_id, "deal_price": str(100 + index), "deal_volume": "10"})
                run.append("books", {"received_at": received, "stock_id": item.stock_id, "buy_prices": ["99"] * 5, "sell_prices": ["101"] * 5, "buy_volumes": ["20"] * 5, "sell_volumes": ["10"] * 5})
        heartbeat = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)
        while heartbeat <= datetime(2026, 9, 22, 5, 30, tzinfo=timezone.utc):
            run.append("books", {"received_at": heartbeat.isoformat().replace("+00:00", "Z"), "stock_id": stocks[0].stock_id, "buy_prices": ["99"] * 5, "sell_prices": ["101"] * 5, "buy_volumes": ["20"] * 5, "sell_volumes": ["10"] * 5})
            heartbeat += timedelta(minutes=1)
        run.finalize(status="COMPLETE", started_at="2026-09-22T00:55:00.000Z", ended_at="2026-09-22T05:35:00.000Z")
        return run.run_dir

    def test_full_session_matures_fixed_checkpoints(self):
        with tempfile.TemporaryDirectory() as temp:
            run = self._full_run(Path(temp) / "quotes")
            features, outcomes, summary = build_session_rows(run)
            self.assertEqual(len(features), 150)
            self.assertEqual(len(outcomes), 150)
            self.assertEqual(summary["coverage_status"], "FULL_SESSION")
            self.assertEqual(summary["mature_outcome_rows"], 150)

    def test_session_publish_is_immutable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = self._full_run(root / "quotes")
            publish_session_analysis(run, root / "analysis")
            with self.assertRaises(FileExistsError):
                publish_session_analysis(run, root / "analysis")


if __name__ == "__main__":
    unittest.main()
