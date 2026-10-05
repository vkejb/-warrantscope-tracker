from pathlib import Path
import hashlib
import json
import tempfile
import unittest
from unittest.mock import Mock

from yuanta_intraday_shadow_v01.collector import (
    AppendOnlyRun,
    WatchItem,
    canonical_bytes,
    subscription_items,
)
from yuanta_intraday_shadow_v01.collector_main import (
    _connect_and_login,
    _is_quote_callback,
    _vendor_subscription_lists,
)
from expanded_shadow_universe_v01.universe import ExpandedWatchItem


class _EventHook:
    def __iadd__(self, callback):
        self.callback = callback
        return self


class _LoginEvent:
    def clear(self):
        pass

    def wait(self, _timeout):
        return True


class _FakeAPI:
    def __init__(self, accepted, login_state):
        self.accepted = accepted
        self.login_state = login_state
        self.OnResponse = _EventHook()
        self.closed = False
        self.disposed = False

    def SetLogType(self, _value):
        pass

    def Open(self, _environment):
        pass

    def Login(self, *_args):
        if self.accepted:
            self.login_state.update({"ok": True, "code": "0001"})
        return self.accepted

    def Close(self):
        self.closed = True

    def Dispose(self):
        self.disposed = True


class CollectorContractTests(unittest.TestCase):
    def _fixture(self):
        stocks = [WatchItem(str(1000 + i), f"股票{i}", i, i / 100.0, "TWSE" if i % 2 else "TPEX") for i in range(1, 31)]
        return {"signal_date": "20260921", "seal_hash": "a" * 64}, stocks

    def test_append_only_run_and_hashes(self):
        seal, stocks = self._fixture()
        with tempfile.TemporaryDirectory() as temp:
            run = AppendOnlyRun(Path(temp), seal, stocks, {"audit_sha256": "b" * 64})
            run.append("ticks", {"stock_id": "1001", "price": "10"})
            manifest = run.finalize(status="COMPLETE", started_at="a", ended_at="b")
            self.assertEqual(manifest["artifacts"]["ticks.jsonl"], hashlib.sha256(run.tick_path.read_bytes()).hexdigest())
            self.assertEqual(manifest["actual_orders"], 0)
            self.assertEqual(manifest["broker_order_calls"], 0)

    def test_expanded_shadow_streams_are_separate_and_hashed(self):
        seal, stocks = self._fixture()
        expanded = [
            ExpandedWatchItem("2330", "台積電", "TWSE", 1, 100.0, 1_000_000, 100_000_000.0, "", "24")
        ]
        expanded_seal = {
            "signal_date": "20260921", "seal_hash": "c" * 64,
            "policy": {"policy_id": "EXPANDED_TEST"},
        }
        with tempfile.TemporaryDirectory() as temp:
            run = AppendOnlyRun(
                Path(temp), seal, stocks, {"audit_sha256": "b" * 64},
                expanded_seal=expanded_seal, expanded_items=expanded,
            )
            run.append("expanded_ticks", {"stock_id": "2330", "deal_price": "100"})
            run.append("expanded_books", {"stock_id": "2330", "buy_prices": ["99"]})
            manifest = run.finalize(status="COMPLETE", started_at="a", ended_at="b")
            self.assertEqual(manifest["expanded_universe_count"], 1)
            self.assertEqual(manifest["expanded_universe_seal_hash"], "c" * 64)
            self.assertIn("expanded_ticks.jsonl", manifest["artifacts"])
            self.assertIn("expanded_books.jsonl", manifest["artifacts"])
            watchlist = json.loads((run.run_dir / "watchlist.json").read_text())
            self.assertEqual(watchlist["expanded_shadow_universe"]["stocks"][0]["stock_id"], "2330")

    def test_live_expanded_archive_does_not_claim_stage_a_subscription(self):
        seal, stocks = self._fixture()
        expanded = [
            ExpandedWatchItem(
                "2330", "台積電", "TWSE", 1, 100.0, 1_000_000,
                100_000_000.0, "", "24",
            )
        ]
        expanded_seal = {
            "signal_date": "20260921", "seal_hash": "c" * 64,
            "policy": {"policy_id": "EXPANDED_TEST"},
        }
        with tempfile.TemporaryDirectory() as temp:
            run = AppendOnlyRun(
                Path(temp), seal, stocks, {"audit_sha256": "b" * 64},
                expanded_seal=expanded_seal, expanded_items=expanded,
                stage_a_quotes_enabled=False,
            )
            self.assertEqual(run.subscription_count, 2)  # 2330 + 0050
            self.assertFalse(run.snapshot["stage_a_quotes_enabled"])
            run.finalize(status="COMPLETE", started_at="a", ended_at="b")

    def test_vendor_lists_are_batched_at_two_hundred(self):
        class _Value:
            pass

        class _VendorList(list):
            def Add(self, value):
                self.append(value)

        class _ListFactory:
            def __class_getitem__(cls, _model):
                return _VendorList

        api_types = {
            "Market": type("Market", (), {"TWSE": 1, "TWOTC": 2}),
            "List": _ListFactory,
            "StockTick": _Value,
            "FiveTickA": _Value,
        }
        items = [
            ExpandedWatchItem(str(1000 + index), str(index), "TWSE", index + 1, 1, 1, 1, "", "24")
            for index in range(401)
        ]
        result = _vendor_subscription_lists(api_types, items, "tick")
        self.assertEqual([len(group) for group in result], [200, 200, 1])

    def test_callback_error_types_are_preserved_in_manifest(self):
        seal, stocks = self._fixture()
        with tempfile.TemporaryDirectory() as temp:
            run = AppendOnlyRun(Path(temp), seal, stocks, {})
            run.callback_error(
                "ValueError",
                callback_name="SubscribeStockTick",
                stock_id="2330",
                phase="STOCK_TICK_PAYLOAD",
            )
            run.callback_error("ValueError")
            run.callback_error("UNKNOWN_WATCHLIST_SYMBOL")
            manifest = run.finalize(status="COMPLETE", started_at="a", ended_at="b")

            self.assertEqual(manifest["event_counts"]["callback_errors"], 3)
            self.assertEqual(
                manifest["callback_error_types"],
                {"UNKNOWN_WATCHLIST_SYMBOL": 1, "ValueError": 2},
            )
            self.assertIn("callback_errors.jsonl", manifest["artifacts"])
            events = [
                json.loads(line)
                for line in run.callback_error_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(events[0]["callback_name"], "SubscribeStockTick")
            self.assertEqual(events[0]["stock_id"], "2330")
            self.assertEqual(events[0]["phase"], "STOCK_TICK_PAYLOAD")
            self.assertNotIn("message", events[0])

    def test_callback_diagnostic_does_not_accept_raw_payload_or_message(self):
        seal, stocks = self._fixture()
        with tempfile.TemporaryDirectory() as temp:
            run = AppendOnlyRun(Path(temp), seal, stocks, {})
            with self.assertRaises(TypeError):
                run.callback_error(
                    "ValueError",
                    message="account=SECRET",
                    raw_payload={"password": "SECRET"},
                )
            run.finalize(status="COMPLETE", started_at="a", ended_at="b")
            self.assertEqual(run.callback_error_path.read_text(), "")

    def test_non_quote_market_callback_is_not_treated_as_quote_error(self):
        self.assertFalse(_is_quote_callback("SubscribeAck"))
        self.assertFalse(_is_quote_callback("Heartbeat"))
        self.assertTrue(_is_quote_callback("SubscribeStockTick"))
        self.assertTrue(_is_quote_callback("SubscribeFiveTickA"))

    def test_subscription_is_top30_plus_permanent_0050(self):
        _seal, stocks = self._fixture()
        subscribed = subscription_items(stocks)
        self.assertEqual(len(stocks), 30)
        self.assertEqual(len(subscribed), 31)
        self.assertEqual(subscribed[-1].stock_id, "0050")
        self.assertEqual(subscribed[-1].market, "TWSE")

    def test_0050_is_archived_separately_without_changing_top30_contract(self):
        seal, stocks = self._fixture()
        with tempfile.TemporaryDirectory() as temp:
            run = AppendOnlyRun(Path(temp), seal, stocks, {})
            run.append("market_context_ticks", {
                "stock_id": "0050", "deal_price": "66.5",
            })
            run.append("market_context_books", {
                "stock_id": "0050", "buy_prices": ["66.4"],
            })
            manifest = run.finalize(
                status="COMPLETE", started_at="a", ended_at="b",
            )
            self.assertEqual(manifest["watchlist_count"], 30)
            self.assertEqual(manifest["subscription_count"], 31)
            self.assertEqual(
                manifest["market_context_event_counts"]["0050"],
                {"ticks": 1, "books": 1},
            )
            self.assertIn("market_context_ticks.jsonl", manifest["artifacts"])
            self.assertIn("market_context_books.jsonl", manifest["artifacts"])
            watch = __import__("json").loads(
                (run.run_dir / "watchlist.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(watch["stocks"]), 30)
            self.assertEqual(watch["market_context"][0]["stock_id"], "0050")

    def test_live_mode_and_execution_counts_are_explicit(self):
        seal, stocks = self._fixture()
        with tempfile.TemporaryDirectory() as temp:
            run = AppendOnlyRun(
                Path(temp),
                seal,
                stocks,
                {"audit_sha256": "b" * 64},
                mode="LIVE_TRADING_QUOTES",
            )
            manifest = run.finalize(
                status="COMPLETE",
                started_at="a",
                ended_at="b",
                actual_orders=2,
                actual_fills=2,
                broker_order_calls=3,
            )
            self.assertEqual(run.snapshot["mode"], "LIVE_TRADING_QUOTES")
            self.assertEqual(manifest["mode"], "LIVE_TRADING_QUOTES")
            self.assertEqual(manifest["actual_orders"], 2)
            self.assertEqual(manifest["actual_fills"], 2)
            self.assertEqual(manifest["broker_order_calls"], 3)

    def test_archive_mode_is_fail_closed(self):
        seal, stocks = self._fixture()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "unsupported archive mode"):
                AppendOnlyRun(
                    Path(temp),
                    seal,
                    stocks,
                    {},
                    mode="UNKNOWN_MODE",
                )

    def test_new_run_never_overwrites_prior_run(self):
        seal, stocks = self._fixture()
        with tempfile.TemporaryDirectory() as temp:
            one = AppendOnlyRun(Path(temp), seal, stocks, {})
            one.finalize(status="COMPLETE", started_at="a", ended_at="b")
            two = AppendOnlyRun(Path(temp), seal, stocks, {})
            two.finalize(status="COMPLETE", started_at="a", ended_at="b")
            self.assertNotEqual(one.run_dir, two.run_dir)

    def test_gzip_run_is_readable_and_hashed(self):
        seal, stocks = self._fixture()
        with tempfile.TemporaryDirectory() as temp:
            run = AppendOnlyRun(Path(temp), seal, stocks, {}, compress=True)
            run.append("ticks", {"stock_id": "1001", "price": "10"})
            manifest = run.finalize(status="COMPLETE", started_at="a", ended_at="b")
            self.assertIn("ticks.jsonl.gz", manifest["artifacts"])
            import gzip
            with gzip.open(run.tick_path, "rt", encoding="utf-8") as handle:
                self.assertEqual(__import__("json").loads(handle.readline())["stock_id"], "1001")

    def test_snapshot_contains_no_credentials(self):
        seal, stocks = self._fixture()
        with tempfile.TemporaryDirectory() as temp:
            run = AppendOnlyRun(Path(temp), seal, stocks, {})
            run.finalize(status="COMPLETE", started_at="a", ended_at="b")
            text = "\n".join(path.read_text() for path in run.run_dir.iterdir())
            for forbidden in ("password", "token", "pfx_password", "trading_password", "account"):
                self.assertNotIn(forbidden, text.lower())

    def test_canonical_json_is_deterministic(self):
        self.assertEqual(canonical_bytes({"b": 2, "a": 1}), canonical_bytes({"a": 1, "b": 2}))

    def test_no_order_api_in_collector_sources(self):
        root = Path(__file__).parents[1]
        source = (root / "collector.py").read_text() + (root / "collector_main.py").read_text()
        for forbidden in ("SendStockOrder", "SendFutureOrder", "StockOrder(", "FutureOrder("):
            self.assertNotIn(forbidden, source)

    def test_login_rebuilds_connection_and_retries_three_times(self):
        outcomes = iter((False, False, False, True))
        login_state = {"ok": False, "code": ""}
        apis = []

        def trader():
            api = _FakeAPI(next(outcomes), login_state)
            apis.append(api)
            return api

        sleeps = []
        result = _connect_and_login(
            {
                "Trader": trader,
                "LogType": Mock(NONE="NONE"),
                "Environment": Mock(PROD="PROD"),
            },
            on_response=lambda *_: None,
            pfx=Path("certificate.pfx"),
            pfx_password="secret-a",
            account="S12341234567",
            trading_password="secret-b",
            login_event=_LoginEvent(),
            login_state=login_state,
            sleep=sleeps.append,
        )

        self.assertIs(result, apis[-1])
        self.assertEqual(len(apis), 4)
        self.assertTrue(all(api.closed and api.disposed for api in apis[:3]))
        self.assertFalse(apis[-1].closed)
        self.assertEqual(sleeps, [5, 5, 5, 10, 5, 20, 5])

    def test_login_fails_closed_after_bounded_retries(self):
        login_state = {"ok": False, "code": ""}
        apis = []

        def trader():
            api = _FakeAPI(False, login_state)
            apis.append(api)
            return api

        with self.assertRaisesRegex(RuntimeError, "已重試3次"):
            _connect_and_login(
                {
                    "Trader": trader,
                    "LogType": Mock(NONE="NONE"),
                    "Environment": Mock(PROD="PROD"),
                },
                on_response=lambda *_: None,
                pfx=Path("certificate.pfx"),
                pfx_password="secret-a",
                account="S12341234567",
                trading_password="secret-b",
                login_event=_LoginEvent(),
                login_state=login_state,
                sleep=lambda _seconds: None,
            )

        self.assertEqual(len(apis), 4)
        self.assertTrue(all(api.closed and api.disposed for api in apis))


if __name__ == "__main__":
    unittest.main()
