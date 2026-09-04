import unittest

from .backtest import net_return, simulate
from .data_loader import Bar
from .signals import Signal


def bars(closes, highs=None, lows=None, opens=None):
    highs=highs or closes; lows=lows or closes; opens=opens or closes
    return [Bar(f"202201{i+1:02d}","1234","測試",3_000_000,opens[i],highs[i],lows[i],closes[i]) for i in range(len(closes))]


def signal():
    return Signal("1234","測試","20220101",100,95,2,0.03,1,"Breakout",0.02,0.01,0.03)


def dates(count):
    return [f"202201{i+1:02d}" for i in range(count)]


class V21RulesTest(unittest.TestCase):
    def test_close_confirmed_stop_exits_next_open(self):
        data=bars([100,94,93],opens=[100,100,93],highs=[101,101,94],lows=[99,93,92])
        trades,_=simulate([signal()],{"1234":data},dates(3))
        self.assertEqual(trades[0]["exit_reason"],"Close Confirmed Stop")
        self.assertEqual(trades[0]["exit_price"],93)

    def test_day8_uses_same_day_close(self):
        closes=[100]+[102]*8
        data=bars(closes,opens=[100]*9,highs=[103]*9,lows=[99]*9)
        trades,_=simulate([signal()],{"1234":data},dates(9))
        self.assertEqual(trades[0]["exit_reason"],"Day 8 Time Stop")
        self.assertEqual(trades[0]["exit_date"],"20220109")
        self.assertEqual(trades[0]["exit_price"],102)

    def test_costs_reduce_return(self):
        self.assertLess(net_return(100,105,0),0.05)
        self.assertLess(net_return(100,105,.001),net_return(100,105,0))

    def test_missing_actual_t1_open_cancels_entry(self):
        data = [
            Bar("20220101", "1234", "測試", 3_000_000, 100, 101, 99, 100),
            Bar("20220103", "1234", "測試", 3_000_000, 100, 101, 99, 100),
        ]
        trades, events = simulate([signal()], {"1234": data}, dates(3))
        self.assertEqual(trades, [])
        self.assertEqual(events[0]["cancel_reason"], "Missing T+1 Open")

    def test_suspension_still_counts_as_a_holding_day(self):
        data = [
            Bar(date, "1234", "測試", 3_000_000, 100, 103, 99, 102)
            for date in dates(9)
            if date != "20220104"
        ]
        trades, _ = simulate([signal()], {"1234": data}, dates(9))
        self.assertEqual(trades[0]["holding_days"], 8)
        self.assertEqual(trades[0]["exit_date"], "20220109")
        self.assertEqual(trades[0]["exit_reason"], "Day 8 Time Stop")

    def test_missing_d1_open_after_exit_signal_is_censored(self):
        data = [
            Bar("20220101", "1234", "測試", 3_000_000, 100, 101, 99, 100),
            Bar("20220102", "1234", "測試", 3_000_000, 100, 101, 93, 94),
            Bar("20220104", "1234", "測試", 3_000_000, 93, 94, 92, 93),
        ]
        trades, events = simulate([signal()], {"1234": data}, dates(4))
        self.assertEqual(trades, [])
        self.assertEqual(events[0]["status"], "censored")
        self.assertEqual(
            events[0]["cancel_reason"], "Missing D+1 Open After Exit Signal"
        )


if __name__=="__main__": unittest.main()
