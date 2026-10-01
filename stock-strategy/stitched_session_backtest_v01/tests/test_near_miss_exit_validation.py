from __future__ import annotations

import unittest

from stitched_session_backtest_v01.near_miss_exit_validation import (
    _first_ids,
    trade_fingerprint,
)


class NearMissExitValidationTests(unittest.TestCase):
    def test_fingerprint_uses_entry_identity(self):
        row = {
            "session_date": "20261001",
            "symbol": "3094",
            "entry_time": "2026-10-01T09:37:30+08:00",
            "entry_price": 64.2,
            "quantity": 2000,
        }
        self.assertEqual(
            trade_fingerprint(row),
            "20261001|3094|2026-10-01T09:37:30+08:00|64.200000|2000",
        )

    def test_first_ids_deduplicates_by_requested_fields(self):
        events = [
            {"validation_trade_id": "later", "session_date": "d", "symbol": "A",
             "entry_time": "2026-01-01T09:02:00+08:00"},
            {"validation_trade_id": "first", "session_date": "d", "symbol": "A",
             "entry_time": "2026-01-01T09:01:00+08:00"},
            {"validation_trade_id": "other", "session_date": "d", "symbol": "B",
             "entry_time": "2026-01-01T09:03:00+08:00"},
        ]
        self.assertEqual(
            _first_ids(events, ("session_date", "symbol")),
            {"first", "other"},
        )
        self.assertEqual(_first_ids(events, ("session_date",)), {"first"})


if __name__ == "__main__":
    unittest.main()
