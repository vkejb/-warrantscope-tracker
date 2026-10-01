from __future__ import annotations

import unittest

from stitched_session_backtest_v01.path_failure_stop_validation import (
    CONFIRMATION_MODES,
    CausalState,
    confirmation_passes,
    variants,
)


class PathFailureStopValidationTests(unittest.TestCase):
    def test_grid_is_fixed(self):
        rows = variants()
        self.assertEqual(len(rows), 72)
        self.assertEqual({row.confirmation_mode for row in rows}, set(CONFIRMATION_MODES))

    def test_confirmation_modes(self):
        state = CausalState(
            breakout_broken=True,
            vwap_broken=True,
            relative_strength_5m=-0.004,
            vwap_rs_failed=True,
            adverse_flow_components=1,
            flow_2of3_failed=False,
        )
        self.assertTrue(confirmation_passes("PATH_ONLY", state))
        self.assertTrue(confirmation_passes("BREAKOUT", state))
        self.assertTrue(confirmation_passes("VWAP_RS", state))
        self.assertFalse(confirmation_passes("FLOW_2OF3", state))
        self.assertTrue(confirmation_passes("ANY_1OF3", state))
        self.assertTrue(confirmation_passes("TWO_OF3", state))

    def test_two_of_three_requires_two(self):
        state = CausalState(True, False, None, False, 0, False)
        self.assertFalse(confirmation_passes("TWO_OF3", state))


if __name__ == "__main__":
    unittest.main()
