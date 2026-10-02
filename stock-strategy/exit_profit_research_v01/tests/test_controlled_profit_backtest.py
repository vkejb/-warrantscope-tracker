from __future__ import annotations

from datetime import datetime, timedelta
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from exit_profit_research_v01.controlled_profit_backtest import (
    R_PRIMARY,
    ResistanceState,
    _first_limit_touch,
    _touch_status,
    market_ioc_fill,
    observe_resistance,
)
from exit_profit_research_v01.resistance_overlay import Book, Tick, load_books
from yuanta_live_runtime_v01.strategy import LiveDirectionEngine, ManagedPosition


TAIPEI = ZoneInfo("Asia/Taipei")


class ControlledProfitBacktestTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 2, 9, 15, tzinfo=TAIPEI)

    def book(self, seconds: float, *, buy=100, sell=200, ask=101.5):
        return Book(
            self.at + timedelta(seconds=seconds),
            (100.5, 100.0, 99.5, 99.0, 98.5),
            (buy, 0, 0, 0, 0),
            (ask, 102.0, 102.5, 103.0, 103.5),
            (sell, 0, 0, 0, 0),
        )

    def tick(self, seconds: float, price: float, serial: int, flag="0", bid=None):
        at = self.at + timedelta(seconds=seconds)
        return Tick(
            at, at, price, price - 0.1 if bid is None else bid,
            price + 0.1, 10, flag, serial,
        )

    def test_locked_limit_bid_only_book_is_preserved_and_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row = {
                "stock_id": "3094", "received_at": "2026-10-02T01:15:00.000Z",
                "buy_prices": ["74.4", "74.3", "74.2", "74.1", "74.0"],
                "buy_volumes": ["3", "1", "1", "1", "1"],
                "sell_prices": ["0", "0", "0", "0", "0"],
                "sell_volumes": ["0", "0", "0", "0", "0"],
            }
            with gzip.open(root / "books.jsonl.gz", "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")
            books = load_books(root, "3094")
            self.assertEqual(len(books), 1)
            fill = market_ioc_fill(books, books[0].received_at, 2000, 0)
            self.assertEqual(fill["status"], "FILLED")
            self.assertTrue(fill["ask_side_absent"])
            self.assertEqual(fill["fill_price"], 74.4)

    def test_partial_market_ioc_never_over_sells(self):
        books = [Book(
            self.at, (71.0, 70.9), (1, 0), (71.1,), (1,),
        )]
        fill = market_ioc_fill(books, self.at, 2000, 0)
        self.assertEqual(fill["status"], "PARTIALLY_FILLED")
        self.assertEqual(fill["filled_shares"], 1000)
        self.assertEqual(fill["remaining_shares"], 1000)
        self.assertEqual(fill["filled_shares"] + fill["remaining_shares"], 2000)

    def test_market_sentinel_is_never_treated_as_execution_price(self):
        books = [Book(
            self.at, (99999.9999, 67.7, 67.6), (500, 3, 2), (0.0,), (0,),
        )]
        fill = market_ioc_fill(
            books, self.at, 2000, 0, maximum_legal_price=67.7,
        )
        self.assertEqual(fill["status"], "FILLED")
        self.assertEqual(fill["fill_price"], 67.7)

    def test_first_limit_touch_is_actual_trade_and_session_bounded(self):
        ticks = [
            self.tick(0, 74.3, 1),
            self.tick(1, 74.4, 2),
            self.tick(2, 74.4, 3),
        ]
        touched = _first_limit_touch(
            ticks, self.at, {"status": "VERIFIED", "upper_limit": 74.4},
        )
        self.assertIs(touched, ticks[1])
        after_close = Tick(
            self.at.replace(hour=13, minute=30, second=1),
            self.at.replace(hour=13, minute=30, second=1),
            74.4, 74.4, 0.0, 1, "1", 4,
        )
        self.assertIsNone(_first_limit_touch(
            [after_close], self.at,
            {"status": "VERIFIED", "upper_limit": 74.4},
        ))

    def test_unverified_limit_never_guesses_from_high(self):
        self.assertIsNone(_first_limit_touch(
            [self.tick(1, 999.0, 1)], self.at,
            {"status": "UNVERIFIED", "upper_limit": None},
        ))

    def test_limit_touch_after_original_exit_is_not_a_live_position_trigger(self):
        touch = self.tick(10, 74.4, 1)
        self.assertEqual(
            _touch_status(
                touch, {"status": "VERIFIED"}, self.at + timedelta(seconds=5),
            ),
            "AFTER_EARLIER_EXIT",
        )

    def test_confirmed_prior_high_near_zone_rejection_is_causal_and_triggers_once(self):
        engine = LiveDirectionEngine({"X": "X"})
        position = ManagedPosition("X", "X", "LONG", 1000, 100.0, "t", self.at)
        state = ResistanceState(100.0, self.at)
        layers = {key: False for key in (
            "profitable_peak", "prior_high_confirmed", "departed_prior_high_zone",
            "near_prior_high_observed",
            "price_bid_failure",
            "same_price_sell_pressure", "bid_depth_weakening",
            "buyer_flow_weakening", "positive_executable_net", "triggered",
        )}
        ticks = [
            self.tick(0, 105.0, 1, "1", 104.9),
            self.tick(1, 104.0, 2, "0", 103.9),
            self.tick(2, 103.5, 3, "0", 103.4),
            self.tick(3, 104.5, 4, "1", 104.4),
            self.tick(4.2, 103.5, 5, "0", 103.4),
        ]
        books = [
            self.book(2.0, buy=100), self.book(2.6, buy=70),
            self.book(3.2, buy=50),
        ]
        seen = []
        trigger = None
        for item in ticks:
            engine.ingest_tick(
                "X", at=item.exchange_time, received_at=item.received_at,
                price=item.price, volume=item.volume, bid=item.bid, ask=item.ask,
                flag=item.flag, serial=item.serial,
            )
            seen.append(item)
            trigger = observe_resistance(
                state=state, config=R_PRIMARY, tick=item, ticks_seen=seen,
                books=books, position=position, engine=engine, layers=layers,
            ) or trigger
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["anchor_high_price"], 105.0)
        self.assertEqual(trigger["observation_price"], 104.5)
        self.assertLess(trigger["observation_price"], trigger["anchor_high_price"])
        self.assertLess(
            datetime.fromisoformat(trigger["observation_time"]),
            datetime.fromisoformat(trigger["trigger_time"]),
        )
        self.assertTrue(layers["price_bid_failure"])
        self.assertTrue(layers["triggered"])
        # Once consumed, the state machine cannot emit a duplicate sell intent.
        self.assertIsNone(observe_resistance(
            state=state, config=R_PRIMARY, tick=ticks[-1], ticks_seen=seen,
            books=books, position=position, engine=engine, layers=layers,
        ))

    def test_lower_rebound_does_not_replace_confirmed_prior_high(self):
        engine = LiveDirectionEngine({"X": "X"})
        position = ManagedPosition("X", "X", "LONG", 1000, 100.0, "t", self.at)
        state = ResistanceState(105.0, self.at)
        layers = {key: False for key in (
            "profitable_peak", "prior_high_confirmed", "departed_prior_high_zone",
            "near_prior_high_observed",
            "price_bid_failure",
            "same_price_sell_pressure", "bid_depth_weakening",
            "buyer_flow_weakening", "positive_executable_net", "triggered",
        )}
        ticks = [
            self.tick(1, 104.0, 1, "0"),  # confirms 105.0 as the prior high
            self.tick(2, 103.5, 2, "0"),
            self.tick(3, 104.5, 3, "1"),  # lower rebound high
        ]
        seen = []
        for current in ticks:
            seen.append(current)
            observe_resistance(
                state=state, config=R_PRIMARY, tick=current, ticks_seen=seen,
                books=[], position=position, engine=engine, layers=layers,
            )
        self.assertEqual(state.anchor_high_price, 105.0)
        self.assertNotEqual(state.anchor_high_price, 104.5)

    def test_five_level_pressure_is_optional_not_a_primary_gate(self):
        engine = LiveDirectionEngine({"X": "X"})
        position = ManagedPosition("X", "X", "LONG", 1000, 100.0, "t", self.at)
        ticks = [
            self.tick(0, 105.0, 1, "1", 104.9),
            self.tick(1, 104.0, 2, "0", 103.9),
            self.tick(2, 103.5, 3, "0", 103.4),
            self.tick(3, 104.5, 4, "1", 104.4),
            self.tick(4, 103.5, 5, "0", 103.4),
        ]
        def run(required):
            state = ResistanceState(100.0, self.at)
            layers = {key: False for key in (
                "profitable_peak", "prior_high_confirmed", "departed_prior_high_zone",
                "near_prior_high_observed",
                "price_bid_failure", "same_price_sell_pressure", "bid_depth_weakening",
                "buyer_flow_weakening", "positive_executable_net", "triggered",
            )}
            seen = []
            result = None
            for current in ticks:
                seen.append(current)
                result = observe_resistance(
                    state=state, config=R_PRIMARY, tick=current, ticks_seen=seen,
                    books=[], position=position, engine=engine, layers=layers,
                    require_five_level_confirmation=required,
                ) or result
            return result
        self.assertIsNotNone(run(False))
        self.assertIsNone(run(True))

    def test_conditional_scan_does_not_consume_trigger_before_a_exit_gate(self):
        engine = LiveDirectionEngine({"X": "X"})
        position = ManagedPosition("X", "X", "LONG", 1000, 100.0, "t", self.at)
        state = ResistanceState(100.0, self.at)
        layers = {key: False for key in (
            "profitable_peak", "prior_high_confirmed", "departed_prior_high_zone",
            "near_prior_high_observed", "price_bid_failure",
            "same_price_sell_pressure", "bid_depth_weakening",
            "buyer_flow_weakening", "positive_executable_net", "triggered",
        )}
        ticks = [
            self.tick(0, 105.0, 1, "1", 104.9),
            self.tick(1, 104.0, 2, "0", 103.9),
            self.tick(2, 103.5, 3, "0", 103.4),
            self.tick(3, 104.5, 4, "1", 104.4),
            self.tick(4, 103.5, 5, "0", 103.4),
            self.tick(5, 103.5, 6, "0", 103.4),
        ]
        seen = []
        for current in ticks[:-1]:
            seen.append(current)
            self.assertIsNone(observe_resistance(
                state=state, config=R_PRIMARY, tick=current, ticks_seen=seen,
                books=[], position=position, engine=engine, layers=layers,
                allow_trigger=False,
            ))
        self.assertFalse(state.triggered)
        seen.append(ticks[-1])
        result = observe_resistance(
            state=state, config=R_PRIMARY, tick=ticks[-1], ticks_seen=seen,
            books=[], position=position, engine=engine, layers=layers,
            allow_trigger=True,
        )
        self.assertIsNotNone(result)
        self.assertTrue(state.triggered)


if __name__ == "__main__":
    unittest.main()
