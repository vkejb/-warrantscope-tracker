from __future__ import annotations

import unittest
from types import SimpleNamespace

from surge_event_study_v01.models import Bar, PreparedStock

from stage_a_macd_kd_filter_study_v01.study import indicators_for_stock, simulate_portfolio


def sample_stock() -> PreparedStock:
    bars = [Bar(f"2020{(index // 28) + 1:02d}{(index % 28) + 1:02d}", "1234", "TEST", 1_000_000, 100 + index, 101 + index, 99 + index, 100 + index) for index in range(70)]
    closes = [bar.close for bar in bars]
    prefix = [0.0]
    for value in closes: prefix.append(prefix[-1] + value)
    volume = [0.0]
    for bar in bars: volume.append(volume[-1] + bar.volume)
    return PreparedStock(
        "1234", "TEST", bars, list(range(70)), [0] * 70,
        prefix, volume, [0.0] * 71, [0.0] * 70,
        [0.0] * 71, [0.0] * 71, [0.0] * 70, [0.0] * 71,
    )


class IndicatorTests(unittest.TestCase):
    def test_no_future_leakage(self):
        stock = sample_stock(); target = int(stock.bars[60].date)
        before = indicators_for_stock(stock, {target})[(target, 1234)]
        changed = sample_stock()
        future = changed.bars[61]
        changed.bars[61] = Bar(future.date, future.code, future.name, future.volume, 1, 1000, 1, 900)
        after = indicators_for_stock(changed, {target})[(target, 1234)]
        self.assertEqual(before, after)

    def test_uptrend_macd_is_bullish_and_kd_is_bounded(self):
        stock = sample_stock(); target = int(stock.bars[-1].date)
        value = indicators_for_stock(stock, {target})[(target, 1234)]
        self.assertGreater(value.dif, 0)
        self.assertTrue(0 <= value.k <= 100)
        self.assertTrue(0 <= value.d <= 100)

    def test_filter_is_entry_only_and_repeated_signal_does_not_add(self):
        calendar = ["20200101", "20200102", "20200103", "20200104"]
        stage = {"20200101": [1234], "20200102": [1234], "20200103": []}
        eligible = {"20200101": {1234}, "20200102": set(), "20200103": set()}
        bars = {
            (1234, day): SimpleNamespace(open=100.0, close=101.0, volume=1000)
            for day in calendar
        }
        summary, trades, _equity = simulate_portfolio("TEST", calendar, stage, eligible, bars)
        self.assertEqual(summary["closed_trades"], 1)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["entry_date"], "20200102")
        self.assertEqual(trades[0]["exit_date"], "20200104")


if __name__ == "__main__":
    unittest.main()
