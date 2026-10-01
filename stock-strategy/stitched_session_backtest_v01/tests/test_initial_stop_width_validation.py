from __future__ import annotations

import unittest

from stitched_session_backtest_v01.initial_stop_width_validation import (
    BUFFERS_R,
    HARD_STOP_WIDTHS_R,
    variant_name,
)


class InitialStopWidthValidationTests(unittest.TestCase):
    def test_grid_is_fixed(self):
        self.assertEqual(HARD_STOP_WIDTHS_R, (0.75, 0.90, 1.00, 1.10, 1.25, 1.50, 1.75, 2.00))
        self.assertEqual(BUFFERS_R, (0.30, 0.40))

    def test_variant_name_is_deterministic(self):
        self.assertEqual(
            variant_name(1.25, 0.40),
            "RECOVERY_BUFFERED__STOP_1.25R__BUFFER_0.40R",
        )


if __name__ == "__main__":
    unittest.main()
