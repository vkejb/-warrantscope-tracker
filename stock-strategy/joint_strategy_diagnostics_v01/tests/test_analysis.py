from __future__ import annotations

import unittest

from joint_strategy_diagnostics_v01.analysis import _anti_chase_pass, _signed_volume


class JointStrategyDiagnosticsTests(unittest.TestCase):
    def test_anti_chase_requires_both_location_checks(self):
        self.assertTrue(_anti_chase_pass({
            "directional_opening_extension": 0.02,
            "directional_vwap_extension": 0.0125,
        }))
        self.assertFalse(_anti_chase_pass({
            "directional_opening_extension": 0.0201,
            "directional_vwap_extension": 0.01,
        }))

    def test_signed_volume_uses_inside_outside_flag(self):
        row = {"flag": "1", "volume": 3, "bid": 10, "ask": 11, "price": 10}
        self.assertEqual(_signed_volume(row), 3)
        row["flag"] = "0"
        self.assertEqual(_signed_volume(row), -3)


if __name__ == "__main__":
    unittest.main()
