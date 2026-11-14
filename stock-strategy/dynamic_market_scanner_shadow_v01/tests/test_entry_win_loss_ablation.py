from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.entry_win_loss_ablation import (
    _account_metrics, _daily_changes, _pre_signal_flow, gate_passes,
)


TAIPEI = ZoneInfo("Asia/Taipei")


class EntryWinLossAblationTest(unittest.TestCase):
    def test_pre_signal_flow_never_uses_later_received_trade(self):
        signal = datetime(2026, 11, 13, 9, 10, tzinfo=TAIPEI)
        rows = [
            {"time": signal - timedelta(seconds=10),
             "received_at": signal - timedelta(seconds=9),
             "flag": "1", "volume": 100, "price": 100, "bid": 99, "ask": 101},
            {"time": signal - timedelta(seconds=5),
             "received_at": signal + timedelta(seconds=1),
             "flag": "0", "volume": 900, "price": 100, "bid": 99, "ask": 101},
            {"time": signal + timedelta(seconds=1),
             "received_at": signal + timedelta(seconds=1),
             "flag": "0", "volume": 900, "price": 100, "bid": 99, "ask": 101},
        ]
        delta, count, proxy = _pre_signal_flow({"ticks": rows}, signal)
        self.assertEqual((delta, count, proxy), (1.0, 1, 0))

    def test_missing_flow_does_not_pass_gate(self):
        passed, reason = gate_passes({"pre_120s_volume_delta": None}, "FLOW_120S_0_5")
        self.assertFalse(passed)
        self.assertIn("UNKNOWN", reason)

    def test_reselection_is_counted_as_new_trade_not_deleted_loss(self):
        account = [
            {"session_date": "20261113", "gate": "BASE_RS1_180S",
             "scenario": "BASE", "stock_id": "1111", "status": "SCORED",
             "net_pnl_twd": -3000},
            {"session_date": "20261113", "gate": "MARKET_BULLISH",
             "scenario": "BASE", "stock_id": "2222", "status": "SCORED",
             "net_pnl_twd": -3500},
        ]
        changes = _daily_changes(account, ["20261113"])
        row = next(item for item in changes if item["gate"] == "MARKET_BULLISH"
                   and item["scenario"] == "BASE")
        self.assertEqual(row["change"], "RESELECTED")
        self.assertEqual(row["delta_twd"], -500)

    def test_unscorable_position_keeps_account_pnl_unknown(self):
        metric = _account_metrics([
            {"session_date": "20261113", "status": "UNSCORABLE_EXIT_FRESH_QUOTE_MISSING",
             "net_pnl_twd": None},
        ], ["20261113"])
        self.assertIsNone(metric["net_pnl_twd"])
        self.assertIsNone(metric["maximum_drawdown_twd"])


if __name__ == "__main__":
    unittest.main()
