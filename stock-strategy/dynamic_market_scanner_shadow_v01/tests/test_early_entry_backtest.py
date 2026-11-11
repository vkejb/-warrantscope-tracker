import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from dynamic_market_scanner_shadow_v01.early_entry_backtest import (
    _metrics, _ready_events, merged_quote_events,
)


class EarlyEntryBacktestTests(unittest.TestCase):
    def test_merge_preserves_cross_channel_receive_order(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = {
                "expanded_ticks.jsonl.gz": [("2026-11-11T01:00:02Z", "1")],
                "market_context_ticks.jsonl.gz": [("2026-11-11T01:00:01Z", "0050")],
                "expanded_books.jsonl.gz": [("2026-11-11T01:00:03Z", "2")],
                "market_context_books.jsonl.gz": [("2026-11-11T01:00:00Z", "0050")],
            }
            for name, values in rows.items():
                with gzip.open(root / name, "wt", encoding="utf-8") as handle:
                    for stamp, symbol in values:
                        handle.write(json.dumps({"received_at": stamp, "stock_id": symbol}) + "\n")
            got = [(kind, row["received_at"]) for kind, row in merged_quote_events(root)]
            self.assertEqual([stamp for _kind, stamp in got], sorted(stamp for _kind, stamp in got))

    def test_ready_selection_is_time_then_strength(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            base = {"event": "EARLY_STATE_TRANSITION", "state": "EARLY_ENTRY_READY_OBSERVED",
                    "entry_price": 50, "source_check_count": 5,
                    "volume_gate_reason": "VOLUME_GATE_PASSED"}
            rows = [
                {**base, "observed_at": "2026-11-11T09:10:00+08:00", "stock_id": "2",
                 "stock_name": "B", "relative_strength_5m": .02},
                {**base, "observed_at": "2026-11-11T09:10:00+08:00", "stock_id": "1",
                 "stock_name": "A", "relative_strength_5m": .03},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            got = _ready_events(path, "20261111")
            self.assertEqual(got[0]["stock_id"], "1")
            self.assertEqual(got[0]["trade_selection"], "SELECTED_EARLIEST_READY")

    def test_no_trade_days_are_zero_in_drawdown_not_fake_trades(self):
        trades = [{"variant": "E", "scenario": "BASE", "session_date": "20260102",
                   "status": "SCORED", "net_pnl": -100}]
        result = _metrics("E", "BASE", trades, ["20260101", "20260102"])
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["net_pnl_twd"], -100)
        self.assertEqual(result["max_drawdown_twd"], 100)


if __name__ == "__main__":
    unittest.main()
