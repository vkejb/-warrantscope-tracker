from __future__ import annotations

from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from exit_profit_research_v01.resistance_overlay import (
    Book,
    OverlayConfig,
    Tick,
    executable_fill,
    signed_flow,
)


TAIPEI = ZoneInfo("Asia/Taipei")


class ResistanceOverlayTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 2, 9, 15, tzinfo=TAIPEI)

    def test_signed_flow_uses_only_causal_window(self):
        ticks = [
            Tick(self.now, self.now - timedelta(seconds=20), 70, 69.9, 70, 100, "1", 1),
            Tick(self.now, self.now - timedelta(seconds=4), 70, 69.9, 70, 30, "1", 2),
            Tick(self.now, self.now - timedelta(seconds=2), 70, 69.9, 70, 70, "0", 3),
        ]
        self.assertAlmostEqual(signed_flow(ticks, self.now, 5), -0.4)

    def test_execution_walks_observed_bid_depth(self):
        books = [Book(
            self.now,
            (71.0, 70.9, 70.8, 70.7, 70.6),
            (1, 3, 10, 10, 10),
            (71.1, 71.2, 71.3, 71.4, 71.5),
            (1, 1, 1, 1, 1),
        )]
        fill = executable_fill(books, self.now, 2000, 0)
        self.assertIsNotNone(fill)
        self.assertAlmostEqual(fill["fill_price"], 70.95)

    def test_insufficient_five_level_depth_is_unscorable(self):
        books = [Book(
            self.now,
            (71.0, 70.9), (0, 1),
            (71.1, 71.2), (1, 1),
        )]
        self.assertIsNone(executable_fill(books, self.now, 2000, 0))

    def test_preregistered_config_does_not_encode_fixed_price(self):
        config = OverlayConfig("X", "SAME_PRICE_ASK", 1, 5, 0.1, 1.5, 3, 1.0)
        self.assertFalse(hasattr(config, "resistance_price"))


if __name__ == "__main__":
    unittest.main()
