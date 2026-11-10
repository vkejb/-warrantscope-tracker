from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.causal_auction import auction_close


class CausalAuctionTest(unittest.TestCase):
    def at(self, hour, minute, second=0):
        return datetime(2026, 11, 10, hour, minute, second,
                        tzinfo=ZoneInfo("Asia/Taipei"))

    def test_window_is_start_inclusive_close_exclusive(self):
        self.assertEqual(auction_close(self.at(9, 5)), self.at(9, 10))
        self.assertEqual(auction_close(self.at(9, 9, 59)), self.at(9, 10))
        self.assertEqual(auction_close(self.at(9, 10)), self.at(9, 15))

    def test_outside_entry_range_is_rejected(self):
        self.assertIsNone(auction_close(self.at(9, 4, 59)))
        self.assertIsNone(auction_close(self.at(13, 10)))


if __name__ == "__main__":
    unittest.main()
