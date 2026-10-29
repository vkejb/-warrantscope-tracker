"""Synthetic-only execution contracts: no SDK, real files, or network."""
from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from cash_short_intraday_research_v01.execution import cost, execute_ioc, shift_price, tick_size


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 2, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        self.quote = {"symbol": "TEST", "received_at": self.at,
                      "bids": [(100, 100)], "asks": [(100.5, 100)]}
        self.cfg = {"strict_evidence": False, "depth_unit_multiplier": 1000}

    def book(self, seconds=1, *, bid=100, ask=100.5, depth=100, sequence=1, **extra):
        at = self.at + timedelta(seconds=seconds)
        return {"symbol": "TEST", "received_at": at, "exchange_time": at,
                "sequence": sequence, "bids": [(bid, depth)], "asks": [(ask, depth)],
                "lower_limit": 90, "upper_limit": 110, **extra}

    def run_ioc(self, books, qty=1000, action="SELL", config=None, **kwargs):
        return execute_ioc(action, qty, self.at, self.quote, books,
                           self.cfg if config is None else config, **kwargs)

    def test_stock_tick_boundaries_and_assets(self):
        for price, step in ((9.99, .01), (10, .05), (49.95, .05), (50, .1),
                            (100, .5), (500, 1), (1000, 5)):
            self.assertEqual(tick_size(price), step)
        for price in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                tick_size(price)
        with self.assertRaises(ValueError):
            tick_size(100, "ETF")

    def test_roundtrip_short_tax_on_entry_and_per_order_fees(self):
        values = cost("SHORT", 100, 90, 1000, {})
        self.assertEqual((values["gross"], values["commission"], values["tax"], values["net"]),
                         (10000, 163, 150, 9687))
        self.assertEqual(cost("LONG", 90, 100, 1000, {})["tax"], 150)
        self.assertEqual(cost("SHORT", 1, .9, 1000, {})["commission"], 40)
        self.assertEqual(cost("SHORT", 100, 90, 0, {})["net"], 0)

    def test_public_shift_price_rechecks_every_band(self):
        self.assertEqual(shift_price(49.95, 2, 1), 50.1)
        self.assertEqual(shift_price(50, 2, -1), 49.9)
        self.assertEqual(shift_price(100, 1, -1), 99.9)
        self.assertEqual(shift_price(10, 1, -1), 9.99)
        for args in ((50, -1, 1), (50, 1.5, -1), (50, 1, 0), (50.05, 1, 1)):
            with self.assertRaises(ValueError):
                shift_price(*args)

    def test_float_vwap_does_not_inflate_exact_notional_fee_tax(self):
        values = cost("SHORT", 200000 / 3000, 60, 3000, {})
        self.assertEqual(values["commission_entry"], 171)
        self.assertEqual(values["tax"], 300)

    def test_strict_defaults_block_unknown_units_and_eligibility(self):
        result = self.run_ioc([self.book()], config={})
        self.assertEqual(result["filled_qty"], 0)
        self.assertEqual(result["scenario_status"], "BLOCKED_STRICT_EVIDENCE")
        self.assertIn("UNKNOWN_UNITS", result["issues"])
        self.assertIn("UNKNOWN_ELIGIBILITY", result["issues"])

    def test_explicitly_verified_inputs_can_run_strict(self):
        result = self.run_ioc([self.book()], config={"strict_evidence": True,
                              "unit_verified": True, "eligibility_verified": True})
        self.assertEqual(result["filled_qty"], 1000)
        self.assertNotIn("UNKNOWN_UNITS", result["issues"])

    def test_explicit_ineligibility_is_never_bypassed_by_conditional(self):
        result = self.run_ioc([self.book()], config={**self.cfg, "eligible": False})
        self.assertEqual(result["filled_qty"], 0)
        self.assertIn("EXPLICITLY_INELIGIBLE", result["issues"])

    def test_old_signal_book_cannot_fill_and_first_new_snapshot_is_used(self):
        result = self.run_ioc([self.book(0, bid=100), self.book(1), self.book(2, bid=100.5)])
        self.assertEqual(result["fills"][0]["price"], 99.9)
        self.assertEqual(result["completion_time"], (self.at + timedelta(seconds=1)).isoformat())
        self.assertEqual(result["limit_price"], 99.8)

    def test_zero_delay_still_cannot_reuse_signal_snapshot(self):
        result = self.run_ioc([self.book(0)], config={**self.cfg, "delay_seconds": 0})
        self.assertEqual(result["filled_qty"], 0)

    def test_delay_stress_three_seconds(self):
        result = self.run_ioc([self.book(1), self.book(3)], config={**self.cfg, "delay_seconds": 3})
        self.assertEqual(result["book_time"], (self.at + timedelta(seconds=3)).isoformat())

    def test_first_timing_valid_snapshot_not_later_more_liquid_snapshot(self):
        result = self.run_ioc([self.book(1, depth=0), self.book(2, depth=1000)])
        self.assertEqual(result["filled_qty"], 0)

    def test_book_arrival_window_inclusive_boundary(self):
        self.assertEqual(self.run_ioc([self.book(4)])["filled_qty"], 1000)
        self.assertEqual(self.run_ioc([self.book(4.001)])["filled_qty"], 0)

    def test_future_receipt_is_not_chosen_ahead_of_in_window_book(self):
        result = self.run_ioc([self.book(20), self.book(2, sequence=2)])
        self.assertEqual(result["fills"][0]["book_sequence"], "2")

    def test_future_exchange_first_book_is_filtered_before_selection(self):
        bad = self.book(1, exchange_time=self.at + timedelta(seconds=1.501))
        result = self.run_ioc([bad, self.book(2, sequence=2)])
        self.assertEqual(result["fills"][0]["book_sequence"], "2")
        self.assertIn("BOOK_EXCHANGE_TIME_FUTURE", result["issues"])

    def test_stale_exchange_first_book_is_filtered_before_selection(self):
        bad = self.book(1, exchange_time=self.at - timedelta(seconds=10))
        result = self.run_ioc([bad, self.book(2, sequence=2)])
        self.assertEqual(result["filled_qty"], 1000)
        self.assertIn("BOOK_EXCHANGE_TIME_STALE", result["issues"])

    def test_unknown_exchange_time_remains_conditional(self):
        book = self.book()
        del book["exchange_time"]
        result = self.run_ioc([book])
        self.assertEqual(result["filled_qty"], 1000)
        self.assertIn("BOOK_EXCHANGE_TIME_UNKNOWN", result["issues"])
        self.assertEqual(result["scenario_status"], "CONDITIONAL_SCENARIO")

    def test_assumed_lots_and_assumed_shares_are_not_unit_verification(self):
        lot = self.run_ioc([self.book(depth=100)])
        share = self.run_ioc([self.book(depth=100)], config={**self.cfg, "depth_unit_multiplier": 1})
        self.assertEqual((lot["filled_qty"], share["filled_qty"]), (1000, 0))
        self.assertIn("ASSUMED_LOTS_DEPTH_MULTIPLIER_1000", lot["issues"])
        self.assertIn("ASSUMED_SHARES_DEPTH_MULTIPLIER_1", share["issues"])

    def test_partial_is_whole_lot_and_quantity_is_conserved(self):
        result = self.run_ioc([self.book(depth=20000)], qty=3000,
                              config={**self.cfg, "depth_unit_multiplier": 1})
        self.assertEqual((result["filled_qty"], result["remaining_qty"]), (2000, 1000))
        self.assertEqual(sum(row["quantity"] for row in result["fills"]), result["filled_qty"])
        self.assertIsNone(result["completion_time"])

    def test_sub_lot_capacity_is_zero_not_odd_lot_partial(self):
        self.assertEqual(self.run_ioc([self.book(depth=9)])["filled_qty"], 0)
        with self.assertRaises(ValueError):
            self.run_ioc([self.book()], qty=1500)

    def test_depth_is_not_replenished_by_duplicate_event(self):
        used = {}
        first = self.run_ioc([self.book(depth=10)], used_depth=used)
        repeated = self.run_ioc([self.book(depth=10), self.book(depth=10)], used_depth=used)
        self.assertEqual((first["filled_qty"], repeated["filled_qty"]), (1000, 0))
        self.assertEqual(sum(value for value in used.values() if isinstance(value, int)), 1000)
        self.assertIn("BOOK_DEPTH_ALREADY_CONSUMED", repeated["issues"])

    def test_new_sequence_only_positive_depth_increment_restores_capacity(self):
        used = {}
        self.run_ioc([self.book(depth=10)], used_depth=used)
        result = self.run_ioc([self.book(depth=10, sequence=2)], used_depth=used)
        self.assertEqual(result["filled_qty"], 0)
        increase = self.run_ioc([self.book(depth=20, sequence=3)], used_depth=used)
        self.assertEqual(increase["filled_qty"], 1000)

    def test_price_moving_to_another_level_does_not_replenish_depth(self):
        used = {}
        first = self.run_ioc([self.book(depth=10)], used_depth=used)
        moved = self.book(sequence=2)
        moved["bids"] = [(100.5, 0), (100.0, 10)]
        second = self.run_ioc([moved], used_depth=used)
        self.assertEqual(first["filled_qty"], 1000)
        self.assertEqual(second["filled_qty"], 0)
        self.assertIn("PRICE_DEPTH_NO_NEW_WHOLE_LOT_CAPACITY", second["issues"])

    def test_price_leaves_and_returns_without_depth_growth_no_new_capacity(self):
        used = {}
        self.run_ioc([self.book(depth=10)], used_depth=used)
        absent = self.book(bid=100.5, depth=0, sequence=2)
        self.assertEqual(self.run_ioc([absent], used_depth=used)["filled_qty"], 0)
        returned = self.run_ioc([self.book(depth=10, sequence=3)], used_depth=used)
        self.assertEqual(returned["filled_qty"], 0)

    def test_depth_decline_cannot_increase_capacity_or_reset_consumption(self):
        used = {}
        self.run_ioc([self.book(depth=10)], used_depth=used)
        self.assertEqual(self.run_ioc([self.book(depth=5, sequence=2)], used_depth=used)["filled_qty"], 0)
        self.assertEqual(self.run_ioc([self.book(depth=10, sequence=3)], used_depth=used)["filled_qty"], 0)
        self.assertEqual(self.run_ioc([self.book(depth=15, sequence=4)], used_depth=used)["filled_qty"], 1000)

    def test_unused_price_capacity_is_retained_but_never_recreated(self):
        used = {}
        self.assertEqual(self.run_ioc([self.book(depth=100)], used_depth=used)["filled_qty"], 1000)
        second = self.run_ioc([self.book(depth=100, sequence=2)], qty=10000, used_depth=used)
        self.assertEqual((second["filled_qty"], second["remaining_qty"]), (9000, 1000))
        self.assertEqual(self.run_ioc([self.book(depth=100, sequence=3)], used_depth=used)["filled_qty"], 0)

    def test_changed_depth_under_same_snapshot_identity_is_not_a_new_increment(self):
        used = {}
        self.run_ioc([self.book(depth=10)], used_depth=used)
        conflict = self.run_ioc([self.book(depth=20)], used_depth=used)
        self.assertEqual(conflict["filled_qty"], 0)
        self.assertIn("DUPLICATE_SNAPSHOT_DEPTH_CONFLICT", conflict["issues"])

    def test_repeated_price_inside_one_snapshot_is_not_summed_twice(self):
        book = self.book(depth=10)
        book["bids"] = [(100, 10), (100.0, 20)]
        result = self.run_ioc([book], qty=2000)
        self.assertEqual((result["filled_qty"], result["remaining_qty"]), (1000, 1000))
        self.assertIn("DUPLICATE_PRICE_LEVEL_EXCLUDED", result["issues"])

    def test_adverse_cost_penalty_cannot_cross_entry_limit(self):
        result = self.run_ioc([self.book(bid=99.8)])
        self.assertEqual(result["filled_qty"], 0)
        self.assertIn("ADVERSE_PRICE_OUTSIDE_IOC_LIMIT", result["issues"])
        buy = self.run_ioc([self.book(ask=101.5)], action="BUY")
        self.assertEqual(buy["filled_qty"], 0)

    def test_cover_has_no_entry_limit_but_still_has_five_level_capacity(self):
        result = self.run_ioc([self.book(ask=105)], action="BUY", config={**self.cfg, "is_cover": True})
        self.assertEqual(result["filled_qty"], 1000)
        self.assertIsNone(result["limit_price"])
        self.assertEqual(result["fills"][0]["price"], 105.5)

    def test_sixth_level_never_used(self):
        book = self.book()
        book["bids"] = [(100, 0)] * 5 + [(100, 1000)]
        self.assertEqual(self.run_ioc([book])["filled_qty"], 0)

    def test_daily_limits_constrain_even_unlimited_cover(self):
        result = self.run_ioc([self.book(ask=110)], action="BUY", config={**self.cfg, "is_cover": True})
        self.assertEqual(result["filled_qty"], 0)
        self.assertIn("BOOK_PRICE_OUTSIDE_DAILY_LIMITS", result["issues"])

    def test_unknown_daily_limits_mark_conditional_and_sentinels_do_not_fill(self):
        book = self.book()
        del book["upper_limit"]
        del book["lower_limit"]
        result = self.run_ioc([book])
        self.assertIn("DAILY_PRICE_LIMITS_UNKNOWN", result["issues"])
        book["bids"] = [(99999.9999, 1000)]
        self.assertEqual(self.run_ioc([book])["filled_qty"], 0)

    def test_fractional_raw_depth_is_rejected_not_truncated(self):
        result = self.run_ioc([self.book(depth=10.9)])
        self.assertEqual(result["filled_qty"], 0)
        self.assertIn("INVALID_BOOK_PRICE_OR_DEPTH", result["issues"])

    def test_closing_auction_never_fills(self):
        self.at = self.at.replace(hour=13, minute=24, second=59)
        self.quote["received_at"] = self.at
        result = self.run_ioc([self.book(1)])
        self.assertEqual(result["filled_qty"], 0)
        self.assertIn("CLOSING_AUCTION_EXCLUDED", result["issues"])

    def test_signal_quote_future_receipt_cannot_form_limit(self):
        self.quote["received_at"] = self.at + timedelta(milliseconds=1)
        result = self.run_ioc([self.book()])
        self.assertIn("SIGNAL_QUOTE_FUTURE_RECEIPT", result["issues"])
        self.assertEqual(result["filled_qty"], 0)

    def test_stock_band_boundary_uses_legal_downward_ticks(self):
        self.quote["bids"] = [(50, 100)]
        result = self.run_ioc([self.book(bid=50, lower_limit=45, upper_limit=55)])
        self.assertEqual(result["limit_price"], 49.9)
        self.assertEqual(result["fills"][0]["price"], 49.95)

    def test_array_archive_fields_and_missing_sequence_supported(self):
        book = self.book()
        del book["bids"]
        del book["asks"]
        del book["sequence"]
        book.update(buy_prices=[100], buy_volumes=[100], sell_prices=[100.5], sell_volumes=[100])
        self.assertEqual(self.run_ioc([book])["filled_qty"], 1000)

    def test_frozen_config_aliases_and_cost_breakdown(self):
        result = self.run_ioc([self.book(1), self.book(3)], config={**self.cfg,
                              "latency_seconds": 3, "quote_wait_seconds": 3,
                              "depth_participation": .1, "execution_cutoff": "13:25"})
        self.assertEqual(result["book_time"], (self.at + timedelta(seconds=3)).isoformat())
        fees = cost("SHORT", 100, 90, 1000, {})
        self.assertEqual(fees["commission"], fees["commission_entry"] + fees["commission_exit"])

    def test_crossed_or_out_of_order_book_is_not_selected(self):
        result = self.run_ioc([self.book(1, bid=101, ask=100),
                               self.book(1.5, out_of_order=True), self.book(2, sequence=3)])
        self.assertEqual(result["fills"][0]["book_sequence"], "3")
        self.assertIn("CROSSED_BOOK_EXCLUDED", result["issues"])
        self.assertIn("OUT_OF_ORDER_BOOK_EXCLUDED", result["issues"])

    def test_synthetic_sequence_cannot_replenish_same_receipt_depth(self):
        used = {}
        first = self.book(depth=10, sequence=1, sequence_known=False)
        duplicate = {**first, "sequence": 999}
        self.assertEqual(self.run_ioc([first], used_depth=used)["filled_qty"], 1000)
        self.assertEqual(self.run_ioc([duplicate], used_depth=used)["filled_qty"], 0)

    def test_buy_cover_must_not_bypass_price_grid(self):
        result = self.run_ioc([self.book(ask=105.1)], action="BUY",
                              config={**self.cfg, "is_cover": True})
        self.assertEqual(result["filled_qty"], 0)
        self.assertIn("INVALID_BOOK_PRICE_OR_DEPTH", result["issues"])

    def test_long_exit_sell_has_no_entry_limit_but_obeys_book_constraints(self):
        result = self.run_ioc([self.book(bid=95)], action="SELL",
                              config={**self.cfg, "is_exit": True})
        self.assertEqual(result["filled_qty"], 1000)
        self.assertIsNone(result["limit_price"])
        self.assertEqual(result["fills"][0]["price"], 94.9)
        wrong_cover = self.run_ioc([self.book()], action="SELL",
                                   config={**self.cfg, "is_cover": True, "is_exit": True})
        self.assertEqual(wrong_cover["filled_qty"], 0)
        self.assertIn("INVALID_COVER_SIDE", wrong_cover["issues"])


if __name__ == "__main__":
    unittest.main()
