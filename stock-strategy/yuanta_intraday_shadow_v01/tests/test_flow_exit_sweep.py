from datetime import datetime, timedelta
import math
import unittest
from zoneinfo import ZoneInfo

from yuanta_intraday_shadow_v01.flow_exit_sweep import (
    FlowVariant,
    _first_executable_point_at_or_after,
    flow_snapshot,
    rule_triggered,
)
from yuanta_intraday_shadow_v01.exit_parameter_sweep import (
    PathPoint,
    TradePath,
)


TAIPEI = ZoneInfo("Asia/Taipei")


class FlowExitSweepTests(unittest.TestCase):
    def _data(self):
        end = datetime(2026, 9, 24, 9, 10, tzinfo=TAIPEI)
        ticks = []

        for index in range(61):
            at = end - timedelta(seconds=60 - index)

            if index < 31:
                flag = "1"
                volume = 10.0
            else:
                flag = "0"
                volume = 20.0

            ticks.append({
                "time": at,
                "price": 100.0,
                "volume": volume,
                "bid": 99.9,
                "ask": 100.0,
                "flag": flag,
                "serial": index,
            })

        books = [{
            "time": end,
            "buy_volume": 100.0,
            "sell_volume": 300.0,
            "best_bid": 99.9,
            "best_ask": 100.0,
        }]

        return end, {
            "ticks": ticks,
            "books": books,
            "tick_times": [row["time"] for row in ticks],
            "book_times": [row["time"] for row in books],
        }

    def test_snapshot_uses_only_past_window_and_computes_flow(self):
        end, data = self._data()
        snapshot = flow_snapshot(
            data,
            end,
            peak_buy_qty_30=500.0,
        )

        self.assertGreater(snapshot.sell_qty_30, snapshot.buy_qty_30)
        self.assertLess(snapshot.normalized_delta_30, 0)
        self.assertLess(snapshot.book_imbalance, 0)
        self.assertGreaterEqual(snapshot.buy_decay_from_peak, 0.30)
        self.assertGreater(snapshot.max_consecutive_sell_ticks_30, 0)

    def test_two_of_three_negative_rule(self):
        end, data = self._data()
        snapshot = flow_snapshot(
            data,
            end,
            peak_buy_qty_30=500.0,
        )

        self.assertTrue(
            rule_triggered(
                FlowVariant("X", "FLOW_2_OF_3_NEG"),
                snapshot,
                previous=None,
                seen_buy_ratio_above_1_5=False,
            )
        )

    def test_sell_ratio_rule_handles_zero_buy(self):
        end, data = self._data()

        for row in data["ticks"]:
            if row["time"] >= end - timedelta(seconds=30):
                row["flag"] = "0"

        snapshot = flow_snapshot(
            data,
            end,
            peak_buy_qty_30=100.0,
        )

        self.assertTrue(math.isinf(snapshot.sell_buy_ratio_30))
        self.assertTrue(
            rule_triggered(
                FlowVariant("X", "SELL_BUY_RATIO_1_50_30"),
                snapshot,
                previous=None,
                seen_buy_ratio_above_1_5=False,
            )
        )


    def test_stale_tick_window_is_not_used_for_flow(self):
        end, data = self._data()

        snapshot = flow_snapshot(
            data,
            end + timedelta(seconds=10),
            peak_buy_qty_30=500.0,
        )

        self.assertIsNone(snapshot)

    def test_flow_exit_waits_for_next_executable_quote(self):
        decision = datetime(2026, 9, 24, 9, 9, tzinfo=TAIPEI)
        trigger = decision + timedelta(minutes=1)
        executable = trigger + timedelta(milliseconds=250)

        path = TradePath(
            session_date="20260924",
            stock_id="3605",
            stock_name="測試股",
            decision_time=decision,
            entry_price=100.0,
            quantity=1000,
            notional_used=100000.0,
            points=(
                PathPoint(
                    at=executable,
                    exit_price=99.5,
                    projected_net_pnl=-800.0,
                    current_return=-0.008,
                    reversal=False,
                ),
                PathPoint(
                    at=decision.replace(hour=13, minute=20),
                    exit_price=100.0,
                    projected_net_pnl=-323.0,
                    current_return=-0.00323,
                    reversal=False,
                ),
            ),
        )

        point = _first_executable_point_at_or_after(path, trigger)

        self.assertIsNotNone(point)
        self.assertEqual(point.at, executable)

    def test_consecutive_confirmation_requires_adjacent_windows(self):
        end, data = self._data()

        first = flow_snapshot(
            data,
            end,
            peak_buy_qty_30=500.0,
        )

        second = type(first)(
            **{
                **first.__dict__,
                "at": end + timedelta(seconds=30),
            }
        )

        self.assertTrue(
            rule_triggered(
                FlowVariant("X", "FLOW_2_OF_3_NEG_2X"),
                second,
                previous=first,
                seen_buy_ratio_above_1_5=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
