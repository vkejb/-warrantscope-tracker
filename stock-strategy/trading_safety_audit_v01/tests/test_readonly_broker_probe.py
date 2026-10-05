from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trading_safety_audit_v01 import readonly_broker_probe as probe


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def emit(self, *args):
        for handler in tuple(self.handlers):
            handler(*args)


class API:
    def __init__(self, mark=1):
        self.OnResponse = Event()
        self.calls = []
        self.mark = mark

    def _call(self, name, *args):
        self.calls.append((name, args))
        self.OnResponse.emit(self.mark, 0, name, None, object())
        return True

    def GetRealReport(self, *args): return self._call("GetRealReport", *args)
    def GetRealReportMerge(self, *args): return self._call("GetRealReportMerge", *args)
    def GetStoreSummary(self, *args): return self._call("GetStoreSummary", *args)
    def GetOrderTradeReport(self, *args): return self._call("GetOrderTradeReport", *args)


class ReadonlyBrokerProbeTests(unittest.TestCase):
    def test_only_allowlisted_queries_are_dispatched(self):
        api = API()
        session = SimpleNamespace(api=api, account="S00000000000")
        with patch.object(probe, "_normalise", side_effect=lambda name, _value, _account: name):
            result = probe._query(session, "UTF8", timeout=0.1)
        self.assertEqual([name for name, _args in api.calls], list(probe.QUERIES))
        self.assertEqual(api.calls[-1][1], (False, session.account, "UTF8"))
        self.assertEqual(set(result), set(probe.QUERIES))
        self.assertFalse(hasattr(api, "SendStockOrder"))

    def test_failed_callback_is_not_accepted_as_empty_evidence(self):
        api = API(mark=0)
        session = SimpleNamespace(api=api, account="S00000000000")
        with self.assertRaisesRegex(RuntimeError, "callback failed"):
            probe._query(session, "UTF8", timeout=0.1)


if __name__ == "__main__":
    unittest.main()
