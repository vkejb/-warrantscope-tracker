from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from yuanta_intraday_shadow_v01.collector import AppendOnlyRun, WatchItem
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC,
    _loss_recovery_exit,
    build_report,
    signal_at,
)
from yuanta_intraday_shadow_v01.direction_signal_validation import build_validation_report


class DirectionFollowBacktestTests(unittest.TestCase):
    def test_loss_recovery_requires_two_points_and_positive_pnl(self):
        self.assertTrue(_loss_recovery_exit(-0.025, 0.001))
        self.assertFalse(_loss_recovery_exit(-0.015, 0.001))
        self.assertFalse(_loss_recovery_exit(-0.025, -0.001))

    def test_zero_top_of_book_fails_closed(self):
        decision = datetime(2026, 9, 24, 1, 5, 30, tzinfo=timezone.utc)
        ticks = []

        for index in range(10):
            stamp = decision - timedelta(seconds=9 - index)
            ticks.append({
                "time": stamp,
                "price": 100.0,
                "volume": 10.0,
                "bid": 99.5,
                "ask": 100.0,
                "flag": "1",
                "serial": index,
            })

        books = [{
            "time": decision,
            "buy_volume": 100.0,
            "sell_volume": 100.0,
            "best_bid": 0.0,
            "best_ask": 0.0,
        }]

        data = {
            "ticks": ticks,
            "books": books,
            "tick_times": [row["time"] for row in ticks],
            "book_times": [row["time"] for row in books],
            "meta": {"stock_name": "測試股"},
        }

        self.assertIsNone(signal_at("1001", data, decision))

    def _run(self, root: Path) -> Path:
        run = AppendOnlyRun(
            root, {"signal_date": "20260921", "seal_hash": "a" * 64},
            [WatchItem("1001", "測試股", 1, 0.1, "TWSE")], {}, compress=True,
        )
        start = datetime(2026, 9, 22, 1, 31, tzinfo=timezone.utc)
        for index in range(340):
            stamp = start.timestamp() + index
            received = datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")
            if index < 180:
                price = 40.0
            elif index <= 300:
                price = 40.0 + (index - 179) * 0.05
            else:
                price = 46.05 - (index - 300) * 0.1
            run.append("ticks", {
                "received_at": received, "stock_id": "1001", "deal_price": str(price),
                "deal_volume": "20", "buy_price": str(price - 0.1), "sell_price": str(price),
                "in_out_flag": "1", "serial_no": index,
            })
            run.append("books", {
                "received_at": received, "stock_id": "1001", "buy_prices": [str(price - 0.1)] * 5,
                "sell_prices": [str(price)] * 5, "buy_volumes": ["100"] * 5, "sell_volumes": ["20"] * 5,
            })
        run.finalize(status="COMPLETE", started_at="2026-09-22T01:30:00.000Z", ended_at="2026-09-22T05:30:00.000Z")
        return run.run_dir

    def test_causal_direction_replay_stays_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp))
            report = build_report({"20260922": [run_dir]})
            self.assertEqual(report["trade_count"], 1)
            self.assertEqual(report["trades"][0]["side"], "LONG")
            self.assertEqual(report["trades"][0]["quantity"], 4000)
            self.assertEqual(report["trades"][0]["exit_reason"], "LOSS_RECOVERY_TO_PROFIT")
            self.assertLessEqual(report["trades"][0]["notional_used"], 190000)
            self.assertEqual(SPEC["entry_confirmations"], 1)
            self.assertEqual(SPEC["stop_loss_net_twd"], 5000)
            self.assertEqual(report["actual_orders"], 0)
            self.assertEqual(report["actual_fills"], 0)
            self.assertEqual(report["broker_order_calls"], 0)

    def test_independent_signal_validation_is_not_a_portfolio(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self._run(Path(temp))
            report = build_validation_report({"20260922": [run_dir]})
            self.assertEqual(report["scored_trade_count"], 1)
            self.assertIn("NOT_AN_EXECUTABLE_190K_PORTFOLIO", report["interpretation"])
            self.assertEqual(report["actual_orders"], 0)


if __name__ == "__main__":
    unittest.main()
