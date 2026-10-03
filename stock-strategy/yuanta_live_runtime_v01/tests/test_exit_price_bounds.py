"""Offline EXIT price validation; no vendor API, credentials or broker session."""
from datetime import datetime, timedelta
from unittest import TestCase

from yuanta_live_runtime_v01.strategy import LiveDirectionEngine, ManagedPosition, TAIPEI


class ExitPriceBoundsTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 5, 10, 0, tzinfo=TAIPEI)
        self.engine = LiveDirectionEngine({"TEST": "Synthetic stock"})
        self.position = ManagedPosition("TEST", "Synthetic stock", "LONG", 1000, 70,
                                        "mock-entry", self.now)

    def limits(self, lower=63, upper=77, *, at=None, tick_size=None):
        stamp = self.now if at is None else at
        return self.engine.record_exit_price_limits("TEST", at=stamp, received_at=stamp,
            lower_limit=lower, upper_limit=upper, tick_size=tick_size)

    def quote(self, bid, ask, *, at=None):
        stamp = self.now if at is None else at
        self.engine.record_exit_quote("TEST", at=stamp, received_at=stamp, bid=bid, ask=ask)
        return self.engine.safe_exit_quote(self.position, stamp)

    def test_long_at_reported_daily_floor_does_not_send_below_floor(self):
        self.assertTrue(self.limits())
        self.assertEqual(self.quote(63, 63.1).price, 63)
        self.assertFalse(self.engine.exit_price_is_valid("TEST", 62.9, self.now))
        self.assertEqual(self.engine.evaluate_exit(self.position, self.now).reason, "STOP_LOSS")

    def test_short_at_reported_daily_ceiling_does_not_send_above_ceiling(self):
        self.position.side = "SHORT"
        self.assertTrue(self.limits())
        self.assertEqual(self.quote(76.9, 77).price, 77)
        self.assertFalse(self.engine.exit_price_is_valid("TEST", 77.1, self.now))

    def test_no_reported_bounds_uses_actual_bid_without_guessing_daily_floor(self):
        self.assertEqual(self.quote(63, 63.1).price, 63)

    def test_no_reported_bounds_uses_actual_ask_without_guessing_daily_ceiling(self):
        self.position.side = "SHORT"
        self.assertEqual(self.quote(76.9, 77).price, 77)

    def test_reported_bounds_retain_shift_within_valid_range(self):
        self.assertTrue(self.limits())
        self.assertEqual(self.quote(70, 70.1).price, 69.9)
        self.position.side = "SHORT"
        self.assertEqual(self.quote(70, 70.1).price, 70.2)

    def test_long_adjacent_tick_is_valid_below_price_band_transition(self):
        cases = ((10, 9.99), (50, 49.95), (100, 99.9), (500, 499.5), (1000, 999))
        for bid, expected in cases:
            with self.subTest(bid=bid):
                self.assertTrue(self.limits(lower=1, upper=2000))
                self.assertEqual(self.quote(bid, 0).price, expected)
                self.assertTrue(self.engine.exit_price_is_valid("TEST", expected, self.now))

    def test_short_adjacent_tick_is_valid_at_price_band_transition(self):
        self.position.side = "SHORT"
        for ask, expected in ((9.99, 10), (49.95, 50), (99.9, 100), (499.5, 500), (999, 1000)):
            with self.subTest(ask=ask):
                self.assertTrue(self.limits(lower=1, upper=2000))
                self.assertEqual(self.quote(0, ask).price, expected)
                self.assertTrue(self.engine.exit_price_is_valid("TEST", expected, self.now))

    def test_non_tick_executable_side_fails_closed(self):
        self.assertIsNone(self.quote(63.03, 63.1))
        self.assertTrue(self.limits())
        self.assertIsNone(self.quote(63.03, 63.1))

    def test_non_tick_opposite_side_fails_closed(self):
        self.assertIsNone(self.quote(63, 63.03))

    def test_unknown_symbol_and_invalid_position_side_fail_closed(self):
        self.assertFalse(self.engine.exit_price_is_valid("UNKNOWN", 70, self.now))
        self.position.side = "UNKNOWN"
        self.assertIsNone(self.quote(70, 70.1))

    def test_float_roundoff_is_normalized_to_exact_tick(self):
        self.assertEqual(self.quote(63.10000000000001, 63.2).price, 63.1)

    def test_executable_side_outside_reported_bounds_fails_closed(self):
        self.assertTrue(self.limits())
        self.assertIsNone(self.quote(62.9, 63))
        self.position.side = "SHORT"
        self.assertIsNone(self.quote(77, 77.1))

    def test_invalid_current_day_bounds_invalidate_older_bounds(self):
        for lower, upper in ((0, 77), (63, "nan"), (78, 77), (63.03, 77)):
            with self.subTest(lower=lower, upper=upper):
                self.assertTrue(self.limits())
                self.assertFalse(self.limits(lower, upper))
                self.assertIsNone(self.quote(70, 70.1))
                self.assertFalse(self.engine.exit_price_is_valid("TEST", 70, self.now))

    def test_previous_day_bounds_are_not_reused_for_new_day(self):
        self.assertTrue(self.limits())
        tomorrow = self.now + timedelta(days=1)
        self.assertEqual(self.quote(80, 80.1, at=tomorrow).price, 80)

    def test_reconnect_quote_reset_preserves_known_same_day_bounds(self):
        self.assertTrue(self.limits())
        self.engine.reset_exit_quotes()
        self.assertEqual(self.quote(63, 63.1).price, 63)

    def test_explicit_instrument_tick_size_is_supported_without_percentage_guess(self):
        self.assertTrue(self.limits(lower=61.23, upper=78.76, tick_size="0.01"))
        self.assertEqual(self.quote(63.03, 63.04).price, 63.02)
        self.assertFalse(self.engine.exit_price_is_valid("TEST", 63.025, self.now))

    def test_invalid_explicit_tick_size_fails_closed(self):
        self.assertFalse(self.limits(tick_size="nan"))
        self.assertIsNone(self.quote(70, 70.1))

    def test_old_limit_update_cannot_replace_newer_official_limits(self):
        self.assertTrue(self.limits())
        old = self.now - timedelta(seconds=1)
        self.assertFalse(self.limits(lower=60, upper=80, at=old))
        self.assertEqual(self.quote(63, 63.1).price, 63)

    def test_daily_limits_do_not_feed_entry_ticks_or_make_candidates(self):
        self.assertTrue(self.limits())
        self.assertEqual(len(self.engine._states["TEST"].ticks), 0)
        self.assertEqual(self.engine._states["TEST"].cumulative_volume, 0)
        self.assertIsNone(self.engine.choose_entry(self.now, allow_short=False))
