from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from yuanta_intraday_shadow_v01.collector import AppendOnlyRun, WatchItem
from yuanta_intraday_shadow_v01.live_parity_backtest import (
    EXECUTION_MODEL,
    build_report,
    replay_session,
)
from yuanta_intraday_shadow_v01.direction_follow_backtest import load_session


class LiveParityBacktestTests(unittest.TestCase):
    def _run(self, root: Path) -> Path:
        run = AppendOnlyRun(
            root,
            {"signal_date": "20260921", "seal_hash": "a" * 64},
            [WatchItem("1001", "測試股", 1, 0.1, "TWSE")],
            {},
            compress=True,
        )

        start = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)

        for index in range(600):
            stamp = start.timestamp() + index
            received = (
                datetime.fromtimestamp(stamp, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )

            if index < 300:
                price = 40.0
            elif index <= 420:
                price = 40.0 + (index - 299) * 0.05
            else:
                price = 46.05 - (index - 420) * 0.1

            run.append(
                "ticks",
                {
                    "received_at": received,
                    "stock_id": "1001",
                    "deal_price": str(price),
                    "deal_volume": "20",
                    "buy_price": str(price - 0.1),
                    "sell_price": str(price),
                    "in_out_flag": "1",
                    "serial_no": index,
                },
            )

            run.append(
                "books",
                {
                    "received_at": received,
                    "stock_id": "1001",
                    "buy_prices": [str(price - 0.1)] * 5,
                    "sell_prices": [str(price)] * 5,
                    "buy_volumes": ["100"] * 5,
                    "sell_volumes": ["20"] * 5,
                },
            )

        run.finalize(
            status="COMPLETE",
            started_at="2026-09-22T00:50:00.000Z",
            ended_at="2026-09-22T05:35:00.000Z",
        )
        return run.run_dir

    def test_replay_uses_live_engine_as_long_only_read_only_proxy(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp))
            report = build_report({"20260922": [run_dir]})

            self.assertEqual(report["session_count"], 1)
            self.assertEqual(report["trade_count"], 1)
            self.assertEqual(report["trades"][0]["side"], "LONG")
            self.assertEqual(report["trades"][0]["quantity"] % 1000, 0)
            self.assertLessEqual(report["trades"][0]["notional_used"], 190000)
            self.assertEqual(
                report["trades"][0]["execution_model"],
                EXECUTION_MODEL,
            )
            self.assertTrue(report["long_only"])
            self.assertEqual(report["actual_orders"], 0)
            self.assertEqual(report["actual_fills"], 0)
            self.assertEqual(report["broker_order_calls"], 0)

    def test_incomplete_or_callback_error_session_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp))
            stocks, coverage = load_session([run_dir])

            bad = dict(coverage)
            bad["callback_errors"] = 1

            with self.assertRaisesRegex(RuntimeError, "callback_errors=0"):
                replay_session(stocks, bad)

            bad = dict(coverage)
            bad["source_statuses"] = ["STOPPED_BY_USER"]

            with self.assertRaisesRegex(RuntimeError, "status COMPLETE"):
                replay_session(stocks, bad)


if __name__ == "__main__":
    unittest.main()
