import unittest

from dynamic_market_scanner_shadow_v01.next_session_signal_audit import (
    HOLIDAY_CORRECTIONS, _pct, _status,
)


class NextSessionSignalAuditTest(unittest.TestCase):
    def test_return_direction(self):
        self.assertAlmostEqual(_pct(105, 100), 5.0)
        self.assertAlmostEqual(_pct(95, 100), -5.0)
        with self.assertRaises(ValueError):
            _pct(0, 100)

    def test_status_does_not_turn_missing_data_into_zero(self):
        self.assertEqual(_status("20261116", "20261115", "20261112", None), "PENDING")
        self.assertEqual(_status("20261113", "20261115", "20261112", None), "MISSING_ARCHIVE")
        self.assertEqual(_status("20261112", "20261115", "20261112", None), "MISSING_BAR")
        self.assertEqual(_status("20261112", "20261115", "20261112", {"close": 42}), "COMPLETE")

    def test_official_holiday_removed_from_target_calendar(self):
        self.assertIn("20261009", HOLIDAY_CORRECTIONS)


if __name__ == "__main__":
    unittest.main()
