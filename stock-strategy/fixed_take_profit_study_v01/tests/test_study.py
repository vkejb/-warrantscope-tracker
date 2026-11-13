from __future__ import annotations

from datetime import datetime
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from fixed_take_profit_study_v01.study import (
    HYBRID_ARM, HYBRID_FLOOR, RISK_TWD, _exit_reason, _metrics,
)


TAIPEI = ZoneInfo("Asia/Taipei")


class _Quote:
    def __init__(self, price: float):
        self.price = price


class _Position:
    pass


class _Engine:
    def __init__(self, pnl: float, breached: bool = False):
        self.pnl = pnl
        self.breached = breached
        self.observations = 0

    def safe_exit_quote(self, *_args, **_kwargs):
        return _Quote(100.0)

    def projected_net(self, *_args):
        return self.pnl

    def _observe_mfe(self, *_args, **_kwargs):
        self.observations += 1

    def _mfe_floor_breached(self, *_args):
        return self.breached


class FixedTakeProfitStudyTest(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 11, 13, 10, 0, tzinfo=TAIPEI)

    def test_fixed_one_r_uses_after_cost_net_threshold(self):
        reason, _, mfe, _ = _exit_reason(
            _Engine(RISK_TWD), _Position(), self.at,
            "FIXED_NET_1R", False, -500.0, False,
        )
        self.assertEqual(reason, "FIXED_NET_TAKE_PROFIT")
        self.assertEqual(mfe, RISK_TWD)

    def test_fixed_one_and_half_r_does_not_exit_at_one_r(self):
        reason, *_ = _exit_reason(
            _Engine(RISK_TWD), _Position(), self.at,
            "FIXED_NET_1_5R", False, 0.0, False,
        )
        self.assertIsNone(reason)

    def test_hybrid_arms_then_locks_early_profit(self):
        reason, _, mfe, armed = _exit_reason(
            _Engine(HYBRID_ARM), _Position(), self.at,
            "HYBRID_EARLY_FLOOR", False, 0.0, False,
        )
        self.assertIsNone(reason)
        self.assertTrue(armed)
        reason, _, _, armed = _exit_reason(
            _Engine(HYBRID_FLOOR), _Position(), self.at,
            "HYBRID_EARLY_FLOOR", False, mfe, armed,
        )
        self.assertEqual(reason, "EARLY_NET_PROFIT_FLOOR")
        self.assertTrue(armed)

    def test_hard_stop_has_priority_over_profit_rules(self):
        reason, *_ = _exit_reason(
            _Engine(-RISK_TWD), _Position(), self.at,
            "HYBRID_EARLY_FLOOR", False, HYBRID_ARM, True,
        )
        self.assertEqual(reason, "STOP_LOSS")

    def test_zero_trade_metrics_do_not_invent_win_rate_or_pf(self):
        metric = _metrics([], ["20261113"])
        self.assertIsNone(metric["win_rate"])
        self.assertIsNone(metric["profit_factor"])
        self.assertEqual(metric["net_pnl_twd"], 0)

    def test_module_has_no_broker_or_live_runtime_import(self):
        source = (Path(__file__).parents[1] / "study.py").read_text(encoding="utf-8")
        self.assertNotIn("order_adapter", source)
        self.assertNotIn("broker_api", source)
        self.assertNotIn("yuanta_live_runtime_v01.main", source)


if __name__ == "__main__":
    unittest.main()
