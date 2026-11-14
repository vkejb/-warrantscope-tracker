import unittest

from dynamic_market_scanner_shadow_v01.chart_chip_selection import (
    ACTIVE, _previous_date, chart_pass, chip_pass, gate, prior_bars,
)
import json


class ChartChipSelectionTest(unittest.TestCase):
    def test_signal_day_bar_cannot_enter_chart(self):
        bars = [{"date": f"202610{day:02d}", "close": float(day), "volume": 1000}
                for day in range(1, 21)]
        bars.append({"date": "20261021", "close": 999.0, "volume": 1000})
        selected = prior_bars(bars, "20261021", "20261020")
        self.assertEqual(selected[-1]["close"], 20.0)
        self.assertEqual(len(selected), 20)

    def test_prior_trading_day_missing_is_unknown(self):
        bars = [{"date": f"202610{day:02d}", "close": float(day), "volume": 1000}
                for day in range(1, 21)]
        self.assertIsNone(prior_bars(bars, "20261022", "20261021"))
        self.assertIsNone(chart_pass(None))

    def test_chip_unknown_is_not_failure_or_pass(self):
        self.assertIsNone(chip_pass(None, "2330"))
        self.assertEqual(gate(True, None, "CHART_AND_CHIP"), (False, "DATA_UNKNOWN"))
        self.assertEqual(gate(False, True, "CHART_AND_CHIP"), (False, "GATE_REJECTED"))
        rows = {"2330": {"foreign": 1000, "investment_trust": -500, "dealer": 100}}
        self.assertTrue(chip_pass(rows, "2330"))

    def test_chart_definition(self):
        bars = [{"date": str(day), "close": float(day), "volume": 100}
                for day in range(1, 21)]
        self.assertTrue(chart_pass(bars))
        bars[-1]["close"] = 1.0
        self.assertFalse(chart_pass(bars))

    def test_20261027_prior_market_session_excludes_closures(self):
        active = json.loads(ACTIVE.read_text(encoding="utf-8"))
        self.assertEqual(_previous_date(active, "20261027"), "20261022")


if __name__ == "__main__":
    unittest.main()
