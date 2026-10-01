from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_shadow_v01.comparison import write_comparison
from paper_shadow_v01.dual_track import publish_dual_track
from paper_shadow_v01.runner import publish_paper_day
from yuanta_intraday_shadow_v01.collector import AppendOnlyRun, WatchItem
from yuanta_intraday_shadow_v01.postprocess import process_run


class RestoredPipelineAcceptanceTests(unittest.TestCase):
    """End-to-end fixture acceptance without broker or notification I/O."""

    def _archive(self, runtime_dir: Path) -> Path:
        stocks = [
            WatchItem(str(1000 + rank), f"fixture-{rank}", rank, 1.0 / rank, "TWSE")
            for rank in range(1, 31)
        ]
        run = AppendOnlyRun(
            runtime_dir,
            {"signal_date": "20261001", "seal_hash": "a" * 64},
            stocks,
            {"fixture": True},
            compress=True,
        )
        start = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)

        # Every stock has enough tick/book evidence for analysis. The first
        # stock and 0050 also provide a continuous market clock through 13:30.
        checkpoints = (0, 5, 15, 30, 60, 90, 269, 270)
        for item in stocks:
            for index, minute in enumerate(checkpoints):
                stamp = start + timedelta(minutes=minute)
                received = stamp.isoformat().replace("+00:00", "Z")
                price = 100.0 + index * 0.01
                run.append("ticks", {
                    "received_at": received,
                    "stock_id": item.stock_id,
                    "deal_price": str(price),
                    "deal_volume": "10",
                    "buy_price": str(price - 0.1),
                    "sell_price": str(price),
                    "in_out_flag": "1",
                    "serial_no": index + 1,
                })
                run.append("books", {
                    "received_at": received,
                    "stock_id": item.stock_id,
                    "buy_prices": [str(price - 0.1)] * 5,
                    "sell_prices": [str(price)] * 5,
                    "buy_volumes": ["100"] * 5,
                    "sell_volumes": ["50"] * 5,
                })

        for minute in range(271):
            stamp = start + timedelta(minutes=minute)
            received = stamp.isoformat().replace("+00:00", "Z")
            price = 100.0 + minute * 0.001
            run.append("ticks", {
                "received_at": received,
                "stock_id": stocks[0].stock_id,
                "deal_price": str(price),
                "deal_volume": "1",
                "buy_price": str(price - 0.1),
                "sell_price": str(price),
                "in_out_flag": "1",
                "serial_no": 1000 + minute,
            })
            run.append("books", {
                "received_at": received,
                "stock_id": stocks[0].stock_id,
                "buy_prices": [str(price - 0.1)] * 5,
                "sell_prices": [str(price)] * 5,
                "buy_volumes": ["100"] * 5,
                "sell_volumes": ["50"] * 5,
            })
            run.append("market_context_ticks", {
                "received_at": received,
                "stock_id": "0050",
                "deal_price": str(200.0 + minute * 0.001),
                "deal_volume": "10",
                "buy_price": "199.9",
                "sell_price": "200.0",
                "in_out_flag": "1",
                "serial_no": minute + 1,
            })
            run.append("market_context_books", {
                "received_at": received,
                "stock_id": "0050",
                "buy_prices": ["199.9"] * 5,
                "sell_prices": ["200.0"] * 5,
                "buy_volumes": ["100"] * 5,
                "sell_volumes": ["50"] * 5,
            })

        manifest = run.finalize(
            status="COMPLETE",
            started_at="2026-10-02T00:50:00Z",
            ended_at="2026-10-02T05:35:00Z",
        )
        self.assertEqual(manifest["watchlist_count"], 30)
        self.assertGreater(manifest["market_context_event_counts"]["0050"]["ticks"], 0)
        self.assertGreater(manifest["market_context_event_counts"]["0050"]["books"], 0)
        return run.run_dir

    def test_archive_analysis_paper_reports_and_notification_wiring(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_runtime = root / "fixture-source"
            paper_runtime = root / "fixture-paper"
            run_dir = self._archive(source_runtime)

            with patch(
                "yuanta_intraday_shadow_v01.postprocess.load_into_environment",
                return_value="FIXTURE_KEYCHAIN",
            ), patch(
                "yuanta_intraday_shadow_v01.postprocess.notify",
                return_value={"TELEGRAM": "FIXTURE_SUCCESS"},
            ) as notify:
                processed = process_run(run_dir, source_runtime)

            self.assertEqual(processed["session"]["coverage_status"], "FULL_SESSION")
            self.assertEqual(processed["notification"]["TELEGRAM"], "FIXTURE_SUCCESS")
            self.assertEqual(notify.call_count, 1)
            self.assertIn("訂閱 Top30", notify.call_args.args[4])
            self.assertIn("30. 1030 fixture-30", notify.call_args.args[4])

            paper = publish_paper_day(
                run_dir,
                processed["session"],
                runtime_dir=paper_runtime,
            )
            comparison = write_comparison(paper_runtime)
            dual = publish_dual_track(
                paper_runtime,
                source_runtime_dir=source_runtime,
                output=root / "acceptance" / "FIXTURE_fixed_dual_exit.json",
            )

            self.assertEqual(paper["actual_orders"], 0)
            self.assertEqual(paper["actual_fills"], 0)
            self.assertEqual(paper["broker_connections"], 0)
            self.assertTrue((paper_runtime / "comparison" / "latest.json").is_file())
            self.assertIn(comparison["status"], {"COLLECTING", "EVALUATION_READY"})
            self.assertFalse(dual["frozen_contract"]["b_broker_submission_allowed"])
            self.assertEqual(dual["actual_orders"], 0)
            self.assertEqual(dual["actual_fills"], 0)
            self.assertEqual(dual["broker_connections"], 0)
            self.assertTrue(Path(dual["output_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
