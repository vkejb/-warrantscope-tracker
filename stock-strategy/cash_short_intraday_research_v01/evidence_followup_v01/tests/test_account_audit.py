import ast
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import unittest

from cash_short_intraday_research_v01.evidence_followup_v01 import account_audit as audit


def config():
    return {"capital_twd": 190000, "risk_per_trade_twd": 3500,
            "maximum_trades_per_day": 1, "maximum_positions": 1,
            "board_lot_shares": 1000, "commission_rate": .000855,
            "minimum_commission": 20, "sell_tax_rate": .0015,
            "stress_scenarios": [{"name": "TEST"}]}


def fixture():
    cfg = config()
    signal = {"day": "20261005", "signal_id": "one", "symbol": "1234", "score": .7,
       "time": "2026-10-05T09:10:00+08:00", "side": "SHORT", "entry_model": "S1",
       "structure_high": 100.5}
    book = {"received_at": "2026-10-05T09:09:59+08:00", "bids": [(100, 5)],
       "asks": [(100.5, 5)], "source_file": "synthetic_not_market_data", "source_line": 1}
    entry_value, exit_value = 100000, 101000
    fees = audit.fee(entry_value, cfg)+audit.fee(exit_value, cfg)
    net = entry_value-exit_value-fees-150
    tables = {k: [] for k in ("trades", "daily_results", "portfolio_dispositions", "execution_issues", "fills")}
    for variant in audit.VARIANTS:
        tags = {"scenario": "TEST", "phase": "ACCOUNT_EXIT", "variant": variant,
                "day": "20261005", "signal_id": "one", "symbol": "1234"}
        t = {**tags, "requested_quantity": 1000, "quantity": 1000,
             "entry_unfilled_quantity": 0, "remaining_quantity": 0,
             "exit_time": "2026-10-05T09:20:01+08:00", "mark_time": "2026-10-05T09:20:00+08:00",
             "exit_trigger_time": "2026-10-05T09:20:00+08:00", "exit_reason": "TEST_EXIT",
             "exit_price": 101, "commission": fees, "sell_tax": 150, "net_pnl": net,
             "marked_net_pnl": net, "gross_pnl": -1000, "realized_pnl": net, "unrealized_pnl": 0}
        tables["trades"].extend([t, {**t, "phase": "FIXED_EXIT"}])
        tables["daily_results"].append({**tags, "starting_equity": 190000,
            "equity": 190000+net, "net_pnl": net, "realized_pnl": net, "unrealized_pnl": 0,
            "external_cashflow": 0, "entry_quantity": 1000, "end_quantity": 0})
        tables["portfolio_dispositions"].append({**tags, "time": signal["time"], "reason": "ADOPTED"})
        for phase, px, time in (("ENTRY", 100, "2026-10-05T09:10:01+08:00"),
                                ("EXIT", 101, "2026-10-05T09:20:01+08:00")):
            tables["fills"].append({**tags, "fill_phase": phase, "price": px,
                                    "time": time, "quantity": 1000})
    return cfg, signal, book, tables


class AccountAuditTests(unittest.TestCase):
    def test_fee_ceiling_and_minimum(self):
        self.assertEqual(audit.fee(100000, config()), 86)
        self.assertEqual(audit.fee(1, config()), 20)

    def test_tick_boundary_down(self):
        self.assertEqual(audit.shift(50, 1, -1), 49.95)
        self.assertEqual(audit.shift(100, 2, -1), 99.8)

    def test_tick_boundary_up(self):
        self.assertEqual(audit.shift(49.95, 2, 1), 50.1)

    def test_structural_short_risk_increases_with_cover_price(self):
        a = audit.structural_risk(100, 101, 1000, config())
        b = audit.structural_risk(100, 102, 1000, config())
        self.assertGreater(b, a)
        self.assertEqual(a, 1323)

    def test_sizing_cap_and_risk_separate(self):
        cfg, s, b, _ = fixture()
        r = audit.sizing_reconstruction(s, b, 190000, cfg)
        self.assertEqual(r["reconstructed_requested_quantity"], 1000)
        self.assertEqual(r["entry_sell_limit"], 99.8)
        self.assertEqual(r["one_lot_required_principal_plus_entry_fee"], 101087)

    def test_one_lot_funding_rejection(self):
        cfg, s, b, _ = fixture()
        r = audit.sizing_reconstruction(s, b, 101000, cfg)
        self.assertEqual(r["reconstructed_requested_quantity"], 0)
        self.assertEqual(r["one_lot_funding_shortfall"], 87)

    def test_risk_rejection_not_cap_rejection(self):
        cfg, s, b, _ = fixture(); s["structure_high"] = 110
        r = audit.sizing_reconstruction(s, b, 190000, cfg)
        self.assertEqual(r["reconstructed_requested_quantity"], 0)
        self.assertEqual(r["one_lot_funding_shortfall"], 0)
        self.assertGreater(r["one_lot_estimated_structure_net_loss"], 3500)

    def test_absent_book_remains_unknown_not_zero_request(self):
        cfg, s, _, _ = fixture()
        r = audit.sizing_reconstruction(s, None, 190000, cfg)
        self.assertIsNone(r["reconstructed_requested_quantity"])

    def test_arithmetic_ledger_checks(self):
        cfg, s, b, t = fixture()
        result = audit.build_audit(t, [s], {"one": b}, cfg)
        self.assertEqual(len(result["account_daily"]), 3)
        self.assertTrue(all(g["unexplained_residual"] == 0 for g in result["account_gap_summary"]))
        self.assertTrue(all(c["arithmetic_residual"] == 0 for c in result["account_cost_reconciliation"]))

    def test_equity_corruption_rejected(self):
        cfg, s, b, t = fixture(); t["daily_results"][0]["equity"] += 1
        with self.assertRaisesRegex(AssertionError, "DAILY_EQUITY"):
            audit.build_audit(t, [s], {"one": b}, cfg)

    def test_quantity_overfill_rejected(self):
        cfg, s, b, t = fixture(); t["fills"][1]["quantity"] += 1000
        with self.assertRaisesRegex(AssertionError, "QUANTITY_CONSERVATION"):
            audit.build_audit(t, [s], {"one": b}, cfg)

    def test_fee_corruption_rejected(self):
        cfg, s, b, t = fixture(); t["trades"][0]["commission"] += 1
        with self.assertRaisesRegex(AssertionError, "COST_OR_PNL"):
            audit.build_audit(t, [s], {"one": b}, cfg)

    def test_requested_quantity_mismatch_rejected(self):
        cfg, s, b, t = fixture(); t["trades"][0]["requested_quantity"] += 1000
        with self.assertRaisesRegex(AssertionError, "SIZING_DIFFERS"):
            audit.build_audit(t, [s], {"one": b}, cfg)

    def test_exact_original_pure_function_parity_enforced(self):
        cfg, s, b, t = fixture()
        with self.assertRaisesRegex(AssertionError, "PINNED_ORIGINAL_FUNCTION"):
            audit.build_audit(t, [s], {"one": b}, cfg, original_sizer=lambda *args: (0, "OK"))

    def test_shared_fill_contract_mismatch_rejected(self):
        cfg, s, b, t = fixture()
        tags = {"scenario": "TEST", "phase": "FIXED_EXIT", "variant": "E0",
                "day": s["day"], "signal_id": "one", "symbol": s["symbol"]}
        t["fills"].append({**tags, "fill_phase": "ENTRY", "price": 99,
                           "time": "2026-10-05T09:10:01+08:00", "quantity": 1000})
        with self.assertRaisesRegex(AssertionError, "FILL_CONTRACT_MISMATCH"):
            audit.check_shared_account_fixed_contracts(t)

    def test_same_boundary_candidate_sort_preserves_score_order(self):
        cfg, s, b, t = fixture()
        s2 = {**s, "signal_id": "aaa", "symbol": "5678", "score": .1}
        for variant in audit.VARIANTS:
            t["portfolio_dispositions"].append({"scenario": "TEST", "phase": "ACCOUNT_EXIT", "variant": variant,
                "day": s["day"], "time": s["time"], "signal_id": "aaa", "symbol": "5678", "reason": "DAILY_TRADE_LIMIT"})
        result = audit.build_audit(t, [s,s2], {"one": b, "aaa": b}, cfg)
        rows = [e for e in result["account_events"] if e["variant"] == "E0" and e["event_type"] == "SIGNAL_DECISION"]
        self.assertEqual([r["signal_id"] for r in rows], ["one", "aaa"])

    def test_extra_account_entry_exactly_bridges_gap(self):
        cfg, s, b, t = fixture()
        extra = deepcopy(t["trades"][2]); extra["signal_id"] = "extra"; extra["day"] = "20261016"
        extra["marked_net_pnl"] = -712; extra["net_pnl"] = -712
        t["trades"].append(extra)
        summary, bridge = audit.reconcile_gap(t, "TEST", "E1")
        self.assertEqual(summary["account_minus_fixed_increment"], -712)
        self.assertEqual(summary["bridge_sum"], -712)
        self.assertEqual(summary["unexplained_residual"], 0)
        self.assertEqual(next(b for b in bridge if b["signal_id"] == "extra")["bridge_reason"],
                         "ADDITIONAL_ACCOUNT_ENTRY_NOT_IN_FIXED_COHORT")

    def test_empty_account_no_invented_win_rate(self):
        empty = {"trades": []}
        summary, bridge = audit.reconcile_gap(empty, "TEST", "E1")
        self.assertEqual(summary["account_entries"], 0)
        self.assertEqual(summary["account_minus_fixed_increment"], 0)
        self.assertNotIn("win_rate", summary)

    def test_unvalued_trade_is_not_silently_dropped(self):
        cfg, s, b, t = fixture()
        next(r for r in t["trades"] if r["phase"] == "ACCOUNT_EXIT" and r["variant"] == "E1")["marked_net_pnl"] = None
        summary, bridge = audit.reconcile_gap(t, "TEST", "E1")
        self.assertIsNone(summary["account_increment"])
        self.assertIsNone(summary["unexplained_residual"])
        self.assertEqual(summary["unvalued_trade_count"], 1)

    def test_report_numbers_are_payload_driven(self):
        cfg, s, b, t = fixture()
        payload = {"tables": audit.build_audit(t, [s], {"one": b}, cfg), "checks": {"synthetic": True}}
        original = audit.report(payload)
        payload["tables"]["account_gap_summary"][0]["fixed_increment"] = 812.345
        changed = audit.report(payload)
        self.assertNotEqual(original, changed)
        self.assertIn("812.345", changed)

    def test_report_empty_evidence_does_not_invent_first_divergence(self):
        payload = {"tables": {k: [] for k in ("account_gap_summary", "account_differences",
                   "account_decisions", "account_events", "account_gap_bridge")}}
        result = audit.report(payload)
        self.assertIn("不得憑空指定第一次", result)
        self.assertNotIn("signal_id=", result)

    def test_no_operational_module_imports(self):
        tree = ast.parse(Path(audit.__file__).read_text())
        imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        self.assertFalse(any(any(word in module for word in ("strategy", "adapter", "broker", "requests", "socket")) for module in imports))


if __name__ == "__main__":
    unittest.main()
