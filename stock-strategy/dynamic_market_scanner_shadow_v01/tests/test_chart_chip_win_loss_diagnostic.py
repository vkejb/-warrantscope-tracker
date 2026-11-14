import unittest

from dynamic_market_scanner_shadow_v01.chart_chip_win_loss_diagnostic import _group, _label


class DiagnosticTest(unittest.TestCase):
    def test_unknown_is_not_zero_or_loss(self):
        self.assertEqual(_label(None), "UNKNOWN")
        self.assertEqual(_label(""), "UNKNOWN")
        self.assertEqual(_label("0"), "FLAT")
        self.assertEqual(_label("-1"), "LOSS")

    def test_group_keeps_unknown(self):
        rows = [{"chart_pass": "True", "chip_pass": "True", "session_date": "20261006",
                 "fixed_30m_label": "WIN", "pnl_twd": 100},
                {"chart_pass": "True", "chip_pass": "True", "session_date": "20261006",
                 "fixed_30m_label": "UNKNOWN", "pnl_twd": None}]
        result = _group(rows, "fixed_30m_label")[0]
        self.assertEqual((result["signals_or_trades"], result["scored"],
                          result["wins"], result["unknown"], result["distinct_dates"]),
                         (2, 1, 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
