"""Synthetic portfolio regressions plus static import-boundary checks.

These tests never load quote archives, invoke a main program, or construct a
broker.  The mocked portfolio cases test desired safety invariants, including
regressions discovered during independent review.
"""
import ast
from datetime import datetime, timedelta
import hashlib
import json
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "cash_short_intraday_research_v01"
SAFE_LOCAL_CLOSURE = {
    "cash_short_intraday_research_v01", "cash_short_intraday_research_v01.replay",
    "cash_short_intraday_research_v01.execution", "cash_short_intraday_research_v01.signals",
    "yuanta_live_runtime_v01", "yuanta_live_runtime_v01.strategy", "yuanta_live_runtime_v01.risk_manager",
    "yuanta_intraday_shadow_v01", "yuanta_intraday_shadow_v01.direction_follow_backtest",
    "yuanta_intraday_shadow_v01.exploratory_backtest", "yuanta_intraday_shadow_v01.collector",
    "shadow_daily_runner", "shadow_daily_runner.normalize", "shadow_daily_runner.sources",
    "shadow_daily_runner.config", "shadow_daily_runner.io_utils",
    "stage_a_prospective_watchlist_v01", "stage_a_prospective_watchlist_v01.seal_store",
}


def _module_file(name):
    p = ROOT.joinpath(*name.split("."))
    if p.with_suffix(".py").is_file():
        return p.with_suffix(".py")
    if (p / "__init__.py").is_file():
        return p / "__init__.py"
    return None


def _closure(start):
    """Resolve local import source files without importing their code."""
    pending, seen = [start], {}
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        source = _module_file(name)
        if source is None:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        seen[name] = tree
        parts = name.split(".")
        pending.extend(".".join(parts[:n]) for n in range(1, len(parts)))
        package = parts if source.name == "__init__.py" else parts[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                pending.extend(item.name for item in node.names if _module_file(item.name))
            elif isinstance(node, ast.ImportFrom):
                prefix = package[:len(package) - node.level + 1] if node.level else []
                base = ".".join(prefix + ([node.module] if node.module else []))
                if _module_file(base):
                    pending.append(base)
                pending.extend(base + "." + item.name for item in node.names
                               if _module_file(base + "." + item.name))
    return seen


def _protocol_hashes():
    return {name: hashlib.sha256((MODULE / name).read_bytes()).hexdigest() if (MODULE/name).exists() else 'NOT_CREATED_IN_SOURCE_ONLY_CHECKOUT'
            for name in ("config.json", "EXPERIMENT_SPEC.md", "freeze.json")}


class StaticSafetyTests(unittest.TestCase):
    def test_exact_strategy_dependency_closure_excludes_main_broker_sdk(self):
        closure = _closure("cash_short_intraday_research_v01.replay")
        self.assertEqual(set(closure), SAFE_LOCAL_CLOSURE)
        forbidden = {"exec", "eval", "__import__", "import_module"}
        for name, tree in closure.items():
            self.assertNotIn(".main", name)
            self.assertNotIn("yuanta_broker_execution", name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, forbidden, name)
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    imports = [x.name for x in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    for imported in imports:
                        self.assertFalse(imported.startswith(("clr", "System", "YuantaSparkAPI", "yuanta_broker_execution_v01")),
                                         (name, imported))

    def test_research_run_entrypoint_imports_no_live_main_or_sdk(self):
        expected = SAFE_LOCAL_CLOSURE | {"cash_short_intraday_research_v01.run",
                                        "cash_short_intraday_research_v01.data",
                                        "cash_short_intraday_research_v01.freeze"}
        self.assertEqual(set(_closure("cash_short_intraday_research_v01.run")), expected)

    def test_research_execution_signal_replay_have_no_io_or_protocol_mutation(self):
        # Do not apply this assertion to dormant legacy helper functions in the
        # dependency closure: they contain I/O but are not invoked by replay.
        bad_calls = {"open", "write_text", "write_bytes", "unlink", "rename",
                     "mkdir", "rmdir", "remove", "rmtree", "Popen", "urlopen", "connect",
                     "create_connection", "system", "exec", "eval", "__import__"}
        for name in ("execution", "signals", "replay"):
            tree = ast.parse((MODULE / (name + ".py")).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    method = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
                    self.assertNotIn(method, bad_calls, (name, getattr(node, "lineno", None)))
                    if (isinstance(node.func, ast.Attribute) and method == "replace"
                            and isinstance(node.func.value, ast.Name)):
                        # datetime.replace()/str.replace() are not file writes.
                        self.assertNotIn(node.func.value.id, {"os", "Path", "path", "source", "destination"})


class PortfolioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Static boundary is established before importing the pure strategy
        # classes.  Import must not attempt operational network/subprocess work.
        if set(_closure("cash_short_intraday_research_v01.replay")) != SAFE_LOCAL_CLOSURE:
            raise AssertionError("UNREVIEWED_LOCAL_DEPENDENCY_CLOSURE")
        with patch("socket.create_connection", side_effect=AssertionError("NO_NETWORK")), \
             patch("subprocess.run", side_effect=AssertionError("NO_SUBPROCESS")), \
             patch("subprocess.Popen", side_effect=AssertionError("NO_SUBPROCESS")):
            cls.replay = import_module("cash_short_intraday_research_v01.replay")
            cls.runner = import_module("cash_short_intraday_research_v01.run")

    def setUp(self):
        self.at = datetime(2026, 10, 2, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        self.config = {"capital_twd": 190000, "risk_per_trade_twd": 3500,
                       "daily_risk_budget_twd": 3500, "book_stale_seconds": 10,
                       "commission_rate": .000855, "minimum_commission": 20,
                       "sell_tax_rate": .0015, "execution_cutoff": "13:25", "cover_start": "13:20",
                       "latency_seconds": 1, "quote_wait_seconds": 3, "extra_slippage_ticks": 1,
                       "depth_unit_multiplier": 1000, "strict_evidence": False}
        self.before = _protocol_hashes()
        self.frozen_config = json.loads((MODULE / "config.json").read_text())

    def tearDown(self):
        self.assertEqual(_protocol_hashes(), self.before, "FROZEN_PROTOCOL_WAS_MODIFIED")

    def signal(self, name="one", side="SHORT", score=1):
        return {"signal_id": name, "day": "20261002", "time": self.at, "symbol": "TEST",
                "side": side, "entry_model": "S1" if side == "SHORT" else "LONG",
                "score": score, "structure_high": 101, "features": {}}

    def book(self, seconds, ask=100.5):
        at = self.at + timedelta(seconds=seconds)
        return {"kind": "book", "symbol": "TEST", "received_at": at, "exchange_time": at,
                "bids": [(100, 100)], "asks": [(ask, 100)], "sequence": seconds}

    def mock_trade(self, signal):
        return {"day": "20261002", "symbol": "TEST", "side": signal["side"], "quantity": 1000,
                "entry_price": 100, "entry_unfilled_quantity": 0, "remaining_quantity": 0,
                "realized_pnl": 100, "unrealized_pnl": 0, "marked_net_pnl": 100,
                "commission": 40, "sell_tax": 150, "path": [{"time": self.at, "net": 100}],
                "model_cash_delta": 100, "restricted_short_proceeds": 0,
                "net_pnl": 100, "gross_pnl": 290, "mfe_net_twd": 100, "mae_net_twd": -20,
                "holding_seconds": 10, "maximum_profit_giveback_twd": 0}

    def test_target_cap_reserves_worst_adverse_entry_limit_and_fee(self):
        signal = self.signal(side="LONG")
        quantity, _ = self.replay.target_quantity(signal, {"asks": [(94.9, 100)]}, 190000, self.config)
        self.assertEqual(quantity, 1000, "2000 shares can cost more than 190000 after entry slippage and fees")

    def test_target_cap_uses_legal_cross_band_limit_not_fixed_initial_step(self):
        signal = self.signal(side="LONG")
        quantity, _ = self.replay.target_quantity(signal, {"asks": [(49.95, 100)]}, 150400, self.config)
        self.assertEqual(quantity, 2000, "legal 2-tick BUY limit is 50.10, not off-grid 50.05")

    def test_vwap_notional_fee_tax_does_not_ceil_binary_float_noise(self):
        # Legal fills 1000@66.6 +2000@66.7 have exact entry notional 200000.
        result = self.replay._mark("SHORT", 200000 / 3000, 3000, 0,
                                  [{"price": 60, "quantity": 3000, "time": self.at, "order_id": 1}], None, self.config)
        self.assertEqual(result["tax"], 300)
        self.assertEqual(result["commission"], 171 + 154)

    def test_unknown_residual_mark_is_not_zero_or_completed_pnl(self):
        result = self.replay._mark("SHORT", 100, 2000, 1000,
                                  [{"price": 99, "quantity": 1000, "time": self.at}], None, self.config)
        self.assertIsNone(result["marked_net"])
        self.assertIsNone(result["unrealized_pnl"])
        self.assertNotEqual(result["realized_pnl"], 0)

    def test_cache_cannot_reuse_conditional_fill_when_strict_gate_changes(self):
        signal = self.signal()
        day = {"20261002": {"index": {}}}
        cache = {}

        def simulate(s, _data, _index, _equity, config, _exit):
            return (None, []) if config.get("strict_evidence") else (self.mock_trade(s), [])

        with patch.object(self.replay, "simulate_trade", side_effect=simulate):
            first = self.replay.run_portfolio(day, {"20261002": [signal]}, self.config, "B", cache=cache)
            strict = self.replay.run_portfolio(day, {"20261002": [signal]},
                                               {**self.config, "strict_evidence": True}, "B", cache=cache)
        self.assertEqual(len(first["trades"]), 1)
        self.assertEqual(strict["trades"], [])

    def test_same_decision_no_fill_cannot_select_second_candidate_using_future(self):
        first, second = self.signal("first", score=2), self.signal("second", score=1)

        def simulate(s, *_args):
            return (None, []) if s["signal_id"] == "first" else (self.mock_trade(s), [])

        with patch.object(self.replay, "simulate_trade", side_effect=simulate) as mocked:
            result = self.replay.run_portfolio({"20261002": {"index": {}}},
                                              {"20261002": [first, second]}, self.config, "B")
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(result["trades"], [])

    def test_future_partial_cover_cannot_reduce_prior_event_exposure(self):
        books = [self.book(0), self.book(1), self.book(1.5, ask=120), self.book(3), self.book(4)]
        data = {"day": "20261002", "metadata": {"TEST": "TEST"}, "events": books}
        index = self.replay.index_day(data)

        class Engine:
            def __init__(self, *_args, **_kwargs):
                pass
            def _derive_initial_stop_price(self, _position):
                return 101
            def opposite_signal(self, *_args):
                return False
            def record_exit_quote(self, *_args, **_kwargs):
                pass
            def evaluate_exit(self, *_args, **_kwargs):
                return SimpleNamespace(reason="MOCK_EXIT")

        calls = 0

        def execution(_action, quantity, at, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            fills = ([{"time": self.at + timedelta(seconds=1), "price": 100, "quantity": 2000}]
                     if calls == 1 else
                     [{"time": self.at + timedelta(seconds=3), "price": 105, "quantity": 1000}]
                     if calls == 2 else [])
            filled = sum(row["quantity"] for row in fills)
            return {"fills": fills, "filled_qty": filled, "remaining_qty": quantity - filled, "issues": []}

        with patch.object(self.replay, "LiveDirectionEngine", Engine), \
             patch.object(self.replay, "feed_engine"), \
             patch.object(self.replay, "target_quantity", return_value=(2000, "OK")), \
             patch.object(self.replay, "execute_ioc", side_effect=execution):
            trade, _ = self.replay.simulate_trade(self.signal(), data, index, 190000, self.config)
        prior = [point for point in trade["path"] if point["time"] < self.at + timedelta(seconds=3)]
        self.assertTrue(prior)
        self.assertTrue(all(point["remaining_quantity"] == 2000 for point in prior), prior)
        self.assertEqual(trade["remaining_quantity"], 1000)
        self.assertIsNone(trade["net_pnl"])

    def test_incomplete_account_full_drawdown_is_unknown(self):
        t = self.mock_trade(self.signal())
        t.update(remaining_quantity=1000, net_pnl=None, gross_pnl=None)
        daily = [{"account_complete": False, "net_pnl": None, "running_max_drawdown_twd": 500}]
        summary = self.replay.summarize([t], daily, [])
        self.assertIsNone(summary["net_pnl"])
        self.assertIsNone(summary["max_drawdown_twd"])
        self.assertIsNone(summary["max_drawdown_fraction"])

    def test_zero_evaluable_days_are_not_a_zero_return(self):
        summary = self.replay.summarize([], [], [])
        self.assertIsNone(summary["net_pnl"])
        self.assertIsNone(summary["gross_pnl"])
        self.assertIsNone(summary["max_drawdown_twd"])

    def test_insufficient_sample_never_selects_a_winner(self):
        scenario = "BASE_ASSUMED_LOTS"
        states = {(scenario, "ENTRY_" + name): self.runner.empty_state()
                  for name in self.frozen_config["selection"]["eligible_entries"]}
        result = self.runner.choose_entry(states, scenario, self.frozen_config)
        self.assertEqual(result["entry"], "S1_NONE")
        self.assertEqual(result["status"], "NO_WINNER_FIXED_REFERENCE_NOT_SELECTED")
        self.assertEqual(result["exit"], "E0")
        self.assertTrue(all(not row["enough_sample"] for row in result["diagnostics"]))

    def test_strict_output_is_na_not_zero_performance(self):
        selected = {"entry": "S1_NONE", "status": "NO_WINNER_FIXED_REFERENCE_NOT_SELECTED"}
        rows = self.runner.build_experiment_rows({}, self.frozen_config, selected)
        strict = [row for row in rows if row["scenario"] == "STRICT_EXECUTABLE"]
        self.assertEqual(len(strict), 4)
        for row in strict:
            self.assertIsNone(row["entries"])
            self.assertIsNone(row["closed_trades"])
            self.assertIsNone(row["net_pnl"])
            self.assertIsNone(row["max_drawdown_twd"])

    def test_mode_d_does_not_look_ahead_to_a_later_long_signal(self):
        short, long = self.signal("early_short"), self.signal("later_long", side="LONG")
        long["time"] += timedelta(minutes=30)
        with patch.object(self.replay, "simulate_trade", side_effect=lambda s, *_: (self.mock_trade(s), [])):
            result = self.replay.run_portfolio({"20261002": {"index": {}}},
                                              {"20261002": [short, long]}, self.config, "D")
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["side"], "SHORT")
        self.assertEqual(result["dispositions"][1]["reason"], "SHORT_USED_DAILY_BUDGET_MISSED_LONG")

    def test_holdout_drawdown_is_recomputed_not_inherited_from_training(self):
        state = self.runner.empty_state()
        for day, net, starting, ending in (("20261016", -10000, 190000, 180000),
                                            ("20261027", 1000, 180000, 181000)):
            trade = self.mock_trade(self.signal())
            trade.update(day=day, net_pnl=net, gross_pnl=net + 190,
                         path=[{"time": self.at.replace(day=int(day[-2:])), "net": net}])
            state["trades"].append(trade)
            state["daily"].append({"day": day, "starting_equity": starting, "equity": ending,
                                   "net_pnl": net, "account_complete": True,
                                   "running_max_drawdown_twd": 10000})
        selected = {"entry": "S1_NONE", "status": "NO_WINNER_FIXED_REFERENCE_NOT_SELECTED"}
        rows = self.runner.build_experiment_rows({("BASE_ASSUMED_LOTS", "A"): state},
                                                 self.frozen_config, selected)
        holdout = next(row for row in rows if row.get("data_splits") == ["retrospective_holdout"])
        self.assertEqual(holdout["max_drawdown_twd"], 0)


if __name__ == "__main__":
    unittest.main()
