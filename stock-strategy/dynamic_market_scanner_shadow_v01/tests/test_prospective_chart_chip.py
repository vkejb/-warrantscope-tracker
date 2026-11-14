from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from dynamic_market_scanner_shadow_v01.prospective_chart_chip import (
    _write_once, capture, horizon_mark,
)


class ProspectiveChartChipTest(unittest.TestCase):
    def test_capture_validates_and_reuses_immutable_sources(self):
        calls = []
        def fake_fetch(source, day):
            calls.append(source)
            if source == "TWSE_INSTITUTIONAL":
                payload = {"date": int(day), "stat": "OK",
                           "fields": ["證券代號", "外陸資買賣超股數(不含外資自營商)",
                                      "投信買賣超股數", "自營商買賣超股數"],
                           "data": [["2330", "1000", "0", "0"]]}
            else:
                row = ["0"] * 23
                row[0] = "1234"
                payload = {"date": int(day), "stat": "ok", "tables": [{"data": [row]}]}
            return json.dumps(payload).encode(), f"https://example.invalid/{source}"
        with tempfile.TemporaryDirectory() as folder:
            now = datetime(2026, 11, 15, tzinfo=timezone.utc)
            first = capture("20261113", "20261116", out=Path(folder),
                            now=now, fetch=fake_fetch)
            second = capture("20261113", "20261116", out=Path(folder),
                             now=now, fetch=fake_fetch)
            self.assertEqual(len(calls), 2)
            self.assertEqual(first, second)
            self.assertEqual(len(first["sources"]), 2)

    def test_capture_rejects_wrong_prior_session_before_network(self):
        def no_network(*_args):
            self.fail("network must not be called")
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                capture("20261112", "20261116", out=Path(folder),
                        now=datetime(2026, 11, 15, tzinfo=timezone.utc), fetch=no_network)

    def test_capture_rejects_post_open_before_network(self):
        def no_network(*_args):
            self.fail("network must not be called")
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                capture("20261113", "20261116", out=Path(folder),
                        now=datetime(2026, 11, 16, 2, tzinfo=timezone.utc), fetch=no_network)

    def test_immutable_capture_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "raw.json"
            _write_once(path, b"one")
            _write_once(path, b"one")
            with self.assertRaises(RuntimeError):
                _write_once(path, b"two")

    def test_stale_quote_is_not_future_filled(self):
        entry_time = datetime(2026, 11, 16, 9, 10, tzinfo=timezone(timedelta(hours=8)))
        entry = {"entry_time": entry_time, "entry_price": 100.0, "quantity": 1000}
        old_time = entry_time + timedelta(minutes=1)
        later_time = entry_time + timedelta(minutes=6)
        book = {"time": old_time, "best_bid": 101.0}
        tick = {"time": old_time, "price": 101.0}
        future = {"time": later_time, "price": 105.0}
        data = {"books": [book], "ticks": [tick, future],
                "tick_times": [old_time, later_time]}
        mark = horizon_mark(data, entry, 5)
        self.assertEqual(mark["mark_status"], "PARTIAL_OR_STALE")
        self.assertIsNone(mark["net_pnl_twd"])
        self.assertIsNone(mark["trade_price_change_pct"])


if __name__ == "__main__":
    unittest.main()
