from dynamic_market_scanner_shadow_v01.premarket_pool_filter_study import (
    _before_open, _gate_status,
)
import unittest


class PremarketPoolFilterStudyTest(unittest.TestCase):
    def test_unknown_is_not_reject_or_pass(self):
        self.assertEqual(_gate_status(None, True, "CHART"), "UNKNOWN")
        self.assertEqual(_gate_status(True, None, "CHART_AND_CHIP"), "UNKNOWN")
        self.assertEqual(_gate_status(False, True, "CHART"), "REJECT")
        self.assertEqual(_gate_status(True, True, "CHART_AND_CHIP"), "PASS")
        self.assertEqual(_gate_status(None, None, "BASE"), "PASS")

    def test_seal_must_precede_target_market_open(self):
        self.assertTrue(_before_open("2026-11-15T23:59:59Z", "20261116"))
        self.assertFalse(_before_open("2026-11-16T01:00:00Z", "20261116"))


if __name__ == "__main__":
    unittest.main()
