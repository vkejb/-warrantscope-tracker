from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from yuanta_market_context_v01.adapter import (
    YuantaReadOnlyContextAdapter,
    normalize_quote_result,
    normalize_stock_information_result,
)
from yuanta_market_context_v01.guard import PreTradeEvidenceGate
from yuanta_market_context_v01.microstructure import depth_features


class AdapterTests(unittest.TestCase):
    def test_stock_information_is_normalized(self):
        row = SimpleNamespace(
            StockCode="3605", MarketNo="1", Dayoffmark="X", Creditpercent=60,
            Lendpercent=90, Creditremnants=999999, Lendremnants=30,
            LendSellMark="Y", LendQty=20, StockWarning=[], UpdateDate="1150927",
        )
        result = normalize_stock_information_result(SimpleNamespace(StockInformationList=[row]))
        self.assertEqual(result[0].symbol, "3605")
        self.assertEqual(result[0].market, "TWSE")
        self.assertEqual(result[0].lend_remnants, 30)

    def test_quote_is_normalized(self):
        now = datetime(2026, 9, 27, 9, 30, tzinfo=timezone.utc)
        defaults = dict(
            StkCode="3605", MarketNo="1", StkName="宏致", Time=now,
            YstPrice=170, OpenRefPrice=170, UpStopPrice=187, DownStopPrice=153,
            OpenPrice=171, HighPrice=172, LowPrice=169, BuyPrice=170.5,
            SellPrice=171, DealPrice=171, TotalVol=1000, TotalDealAmt=17100,
            TotalOutVol=600, TotalInVol=400, OrderBuyCount=50, OrderBuyQty=800,
            OrderSellCount=40, OrderSellQty=700,
        )
        quote = normalize_quote_result(SimpleNamespace(QueryWatchList=[SimpleNamespace(**defaults)]))[0]
        self.assertGreater(quote.spread_bps, 0)
        self.assertAlmostEqual(quote.cumulative_trade_imbalance, 0.2)

    def test_dotnet_datetime_shape_is_supported(self):
        stamp = SimpleNamespace(
            Year=2026, Month=9, Day=27, Hour=9, Minute=30, Second=1, Millisecond=250
        )
        row = SimpleNamespace(
            StkCode="3605", MarketNo="1", StkName="宏致", Time=stamp,
            YstPrice=170, OpenRefPrice=170, UpStopPrice=187, DownStopPrice=153,
            OpenPrice=171, HighPrice=172, LowPrice=169, BuyPrice=170.5,
            SellPrice=171, DealPrice=171, TotalVol=1000, TotalDealAmt=17100,
            TotalOutVol=600, TotalInVol=400, OrderBuyCount=50, OrderBuyQty=800,
            OrderSellCount=40, OrderSellQty=700,
        )
        quote = normalize_quote_result(SimpleNamespace(QueryWatchList=[row]))[0]
        self.assertEqual(quote.quote_time.isoformat(), "2026-09-27T09:30:01.250000+08:00")

    def test_read_only_requester_can_only_issue_context_queries(self):
        class GenericList(list):
            def Add(self, value):
                self.append(value)

            @classmethod
            def __class_getitem__(cls, _item):
                return cls

        calls = []
        api = SimpleNamespace(
            GetStockInformation=lambda *args: calls.append(("stock", args)) or True,
            GetWatchListAll=lambda *args: calls.append(("quote", args)) or True,
        )
        types = {
            "List": GenericList,
            "StkInfo": SimpleNamespace,
            "Quote": SimpleNamespace,
            "Market": SimpleNamespace(TWSE="TWSE", TWOTC="TWOTC"),
            "Language": SimpleNamespace(UTF8="UTF8"),
        }
        adapter = YuantaReadOnlyContextAdapter(api, types)
        result = adapter.request(
            "TEST_ACCOUNT",
            [SimpleNamespace(stock_id="3605", market="TWSE")],
        )
        self.assertTrue(all(result.values()))
        self.assertEqual([name for name, _args in calls], ["stock", "quote"])
        self.assertFalse(hasattr(adapter, "place_order"))


class GateTests(unittest.TestCase):
    def _rows(self, day_trade="X", lend=5, warning=()):
        now = datetime(2026, 9, 27, 9, 30, tzinfo=timezone.utc)
        security = normalize_stock_information_result(SimpleNamespace(StockInformationList=[SimpleNamespace(
            StockCode="3605", MarketNo="1", Dayoffmark=day_trade,
            Creditpercent=60, Lendpercent=90, Creditremnants=1,
            Lendremnants=lend, LendSellMark="Y", LendQty=lend,
            StockWarning=list(warning), UpdateDate="1150927",
        )]))[0]
        quote = normalize_quote_result(SimpleNamespace(QueryWatchList=[SimpleNamespace(
            StkCode="3605", MarketNo="1", StkName="宏致", Time=now,
            YstPrice=170, OpenRefPrice=170, UpStopPrice=187, DownStopPrice=153,
            OpenPrice=171, HighPrice=172, LowPrice=169, BuyPrice=170.5,
            SellPrice=171, DealPrice=171, TotalVol=1000, TotalDealAmt=17100,
            TotalOutVol=600, TotalInVol=400, OrderBuyCount=50, OrderBuyQty=800,
            OrderSellCount=40, OrderSellQty=700,
        )]))[0]
        return now, security, quote

    def test_short_requires_sell_first_and_inventory(self):
        now, security, quote = self._rows(day_trade="Y", lend=0)
        decision = PreTradeEvidenceGate().evaluate(security, quote, side="SHORT", now=now)
        self.assertFalse(decision.allowed)
        self.assertIn("SELL_FIRST_NOT_ALLOWED", decision.reasons)
        self.assertIn("NO_CONFIRMED_SHORT_INVENTORY", decision.reasons)

    def test_fresh_eligible_long_is_allowed(self):
        now, security, quote = self._rows()
        self.assertTrue(PreTradeEvidenceGate().evaluate(security, quote, side="LONG", now=now).allowed)

    def test_stale_quote_fails_closed(self):
        now, security, quote = self._rows()
        decision = PreTradeEvidenceGate().evaluate(
            security, quote, side="LONG", now=now + timedelta(seconds=6)
        )
        self.assertIn("STALE_QUOTE_CONTEXT", decision.reasons)


class MicrostructureTests(unittest.TestCase):
    def test_full_depth_features_retain_near_touch_information(self):
        features = depth_features(
            [100, 99.5, 99, 98.5, 98], [500, 100, 100, 100, 100],
            [100.5, 101, 101.5, 102, 102.5], [100, 100, 100, 100, 100],
        )
        self.assertGreater(features.l1_imbalance, features.total_imbalance)
        self.assertGreater(features.weighted_imbalance, 0)
        self.assertGreater(features.microprice_edge_bps, 0)

    def test_crossed_book_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "crossed"):
            depth_features([101], [1], [100], [1])


if __name__ == "__main__":
    unittest.main()
