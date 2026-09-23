from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import Mock

from yuanta_intraday_shadow_v01.collector import AppendOnlyRun, WatchItem, canonical_bytes
from yuanta_intraday_shadow_v01.collector_main import _connect_and_login


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
