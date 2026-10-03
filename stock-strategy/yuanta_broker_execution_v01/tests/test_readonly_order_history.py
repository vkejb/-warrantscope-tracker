"""GetOrderTradeReport DTO-only evidence tests: no API, store or SDK session."""

import json
from types import SimpleNamespace
import unittest

from yuanta_broker_execution_v01.adapter import (
    BrokerAdapterError,
    _normalise_order_trade_report,
)


MOCK_ACCOUNT = "S00000000000"


def day(year=2026, month=10, date=2):
    return SimpleNamespace(ushtYear=year, bytMon=month, bytDay=date)


def clock(hour=9, minute=15, second=30, millisecond=7):
    return SimpleNamespace(bytHour=hour, bytMin=minute, bytSec=second, ushtMSec=millisecond)


def stamp(year=2026, month=10, date=2, hour=9, minute=15, second=31, millisecond=9):
    return SimpleNamespace(Year=year, Month=month, Day=date, Hour=hour,
                           Minute=minute, Second=second, Millisecond=millisecond)


def order(**changes):
    fields = dict(
        Account=MOCK_ACCOUNT, CompanyNo="3094", OrderNo="MOCK-ORDER", BS="B",
        OrderType=0, Price=70.5, PriceFlag="2", Time_in_Force="0", APCode=0,
        OrderStatus=20, BeforeQty=2000, AfterQty=2000, OkQty=1000,
        OR_QTY=2000, CancelQty=0, BasketNo="MOCK-NATIVE-BASKET",
        TradeDate=day(), AcceptDate=day(), AcceptTime=clock(),
        UpdateDate=day(), UpdateTime=clock(second=31), Channel="UNMAPPED",
        OTax=0, OCharge=20, ODueAmt=-70520, ErrorNo="",
        CancelFlag="Y", ReduceFlag="Y", TraditionFlag="N", TradeCurrency="TWD",
        Order_Success="Y", Reduce_Flag="N", Chg_Prz_Flag="N", TSE_Cancel="N",
    )
    fields.update(changes)
    return SimpleNamespace(**fields)


def trade(**changes):
    fields = dict(Account=MOCK_ACCOUNT, CompanyNo="3094", OrderNo="MOCK-ORDER",
                  BS="B", OrderType=0, OkQty=1000, OPrice=70.5, SPrice=70.4,
                  DateTime=stamp(), Price_Flag="2", Exchange_Code=0, TradeCurrency="TWD")
    fields.update(changes)
    return SimpleNamespace(**fields)


def payload(orders=(), trades=()):
    return SimpleNamespace(StkOrderList=list(orders), StkTradeList=list(trades))


class ReadOnlyOrderHistoryTests(unittest.TestCase):
    def normalise(self, value):
        return _normalise_order_trade_report(value, account=MOCK_ACCOUNT)

    def test_documented_collections_and_fields_with_no_account_leak(self):
        result = self.normalise(payload([order()], [trade()]))
        self.assertEqual(result["source"], "GetOrderTradeReport")
        self.assertNotIn(MOCK_ACCOUNT, json.dumps(result))
        row = result["orders"][0]
        self.assertTrue(row["account_verified"])
        self.assertEqual((row["order_type"], row["ap_code"]), ("0", 0))
        self.assertEqual((row["price_flag"], row["price_type"]), ("2", "LIMIT"))
        self.assertEqual((row["time_in_force_code"], row["time_in_force"]), ("0", "ROD"))
        self.assertEqual((row["after_qty"], row["ok_qty"], row["original_qty"], row["cancel_qty"]),
                         (2000, 1000, 2000, 0))
        self.assertEqual(row["channel"], "UNMAPPED")
        self.assertEqual((row["fees"], row["tax"], row["due_amount"]), ("20", "0", "-70520"))
        self.assertEqual(row["accept_time"], "09:15:30.007")
        self.assertEqual(row["update_time_source"], "UpdateTime")
        self.assertEqual((row["cancel_flag"], row["repriced_flag"]), ("Y", "N"))
        fill = result["trades"][0]
        self.assertEqual((fill["trade_date"], fill["fill_time"]), ("20261002", "09:15:31.009"))
        self.assertEqual((fill["trade_date_source"], fill["fill_time_source"]), ("DateTime", "DateTime"))
        self.assertEqual(fill["fill_price"], "70.4")
        for absent in ("seq_no", "basket_no", "ap_code", "time_in_force"):
            self.assertNotIn(absent, fill)

    def test_native_short_zero_margin_short_and_trade_kind_not_confused(self):
        for kind in (0, 3, 4, 5, 6, 7, 8):
            result = self.normalise(payload([order(OrderType=kind, TradeKind=4)],
                                            [trade(OrderType=kind, TradeKind=0)]))
            self.assertEqual(result["orders"][0]["order_type"], str(kind))
            self.assertEqual(result["trades"][0]["order_type"], str(kind))

    def test_missing_order_type_stays_unknown_not_cash(self):
        values = (order(), trade())
        for value in values:
            del value.OrderType
        result = self.normalise(payload([values[0]], [values[1]]))
        self.assertEqual(result["orders"][0]["order_type"], "")
        self.assertEqual(result["trades"][0]["order_type"], "")

    def test_cancelled_partially_filled_order_keeps_all_quantity_evidence(self):
        row = self.normalise(payload([order(OrderStatus=30, AfterQty=0, CancelQty=1000)]))["orders"][0]
        self.assertEqual(row["terminal_status"], "CANCELED")
        self.assertEqual((row["before_qty"], row["after_qty"], row["ok_qty"], row["cancel_qty"]),
                         (2000, 0, 1000, 1000))

    def test_only_explicit_terminal_status_is_certified(self):
        for status, expected in ((10, "REJECTED"), (24, "EXPIRED"), (25, "EXPIRED"), (30, "CANCELED"),
                                 (0, "UNKNOWN"), (5, "UNKNOWN"), (20, "UNKNOWN"), (99, "UNKNOWN")):
            row = self.normalise(payload([order(OrderStatus=status, Time_in_Force="3",
                                                AfterQty=0, CancelQty=2000)]))["orders"][0]
            self.assertEqual(row["terminal_status"], expected)

    def test_acknowledged_ioc_with_full_fills_is_not_guessed_terminal(self):
        row = self.normalise(payload([order(OrderStatus=20, Time_in_Force="3",
                                            AfterQty=2000, OkQty=2000)]))["orders"][0]
        self.assertEqual(row["terminal_status"], "UNKNOWN")

    def test_history_price_flag_and_tif_contract(self):
        for flag, expected in (("1", "MARKET"), ("2", "LIMIT"), ("H", "LIMIT_UP"),
                               ("L", "LIMIT_DOWN"), ("-", "FLAT")):
            result = self.normalise(payload([order(PriceFlag=flag)], [trade(Price_Flag=flag)]))
            self.assertEqual(result["orders"][0]["price_type"], expected)
            self.assertEqual(result["trades"][0]["price_type"], expected)
        for code, expected in (("0", "ROD"), ("3", "IOC"), ("4", "FOK")):
            self.assertEqual(self.normalise(payload([order(Time_in_Force=code)]))["orders"][0]["time_in_force"], expected)

    def test_unsupported_price_flag_or_tif_fails_closed(self):
        for field, invalid in (("PriceFlag", "M"), ("PriceFlag", "UNKNOWN"),
                               ("Time_in_Force", "IOC"), ("Time_in_Force", "9")):
            with self.subTest(field=field, invalid=invalid), self.assertRaises(BrokerAdapterError):
                self.normalise(payload([order(**{field: invalid})]))
        with self.assertRaises(BrokerAdapterError):
            self.normalise(payload(trades=[trade(Price_Flag="M")]))

    def test_dates_remain_independent_never_accept_date_as_trade_date(self):
        value = order(AcceptDate=day(date=1), UpdateDate=day(date=3))
        del value.TradeDate
        row = self.normalise(payload([value]))["orders"][0]
        self.assertEqual((row["trade_date"], row["trade_date_source"]), ("", ""))
        self.assertEqual((row["accept_date"], row["update_date"]), ("20261001", "20261003"))

    def test_explicit_date_strings_validated_without_integer_date_guess(self):
        for valid in ("20261002", "2026/10/02", "2026-10-02"):
            self.assertEqual(self.normalise(payload([order(TradeDate=valid)]))["orders"][0]["trade_date"], "20261002")
        for invalid in (20261002, "20260230", "2026/1/02", "1121002", 0):
            with self.subTest(invalid=invalid), self.assertRaises(BrokerAdapterError):
                self.normalise(payload([order(TradeDate=invalid)]))

    def test_invalid_native_dates_and_times_fail_without_clock_fallback(self):
        for field, invalid in (("TradeDate", day(2026, 2, 30)), ("AcceptDate", day(112, 10, 2)),
                               ("UpdateDate", day(0, 0, 0)), ("AcceptTime", clock(hour=24)),
                               ("UpdateTime", clock(millisecond=1000))):
            with self.subTest(field=field), self.assertRaises(BrokerAdapterError):
                self.normalise(payload([order(**{field: invalid})]))
        for invalid in (stamp(year=2026, month=2, date=30), stamp(minute=60), stamp(millisecond=1000)):
            with self.assertRaises(BrokerAdapterError):
                self.normalise(payload(trades=[trade(DateTime=invalid)]))

    def test_leap_day_and_exact_millisecond_boundaries(self):
        result = self.normalise(payload([order(TradeDate=day(2024, 2, 29), AcceptTime=clock(23, 59, 59, 999))],
                                        [trade(DateTime=stamp(2024, 2, 29, 0, 0, 0, 0))]))
        self.assertEqual(result["orders"][0]["trade_date"], "20240229")
        self.assertEqual(result["orders"][0]["accept_time"], "23:59:59.999")
        self.assertEqual(result["trades"][0]["fill_time"], "00:00:00.000")

    def test_missing_optional_values_remain_unknown_not_zero(self):
        value = order()
        for field in ("CancelQty", "OR_QTY", "APCode", "OTax", "OCharge", "TradeDate",
                      "AcceptDate", "UpdateDate", "AcceptTime", "UpdateTime", "PriceFlag", "Time_in_Force"):
            delattr(value, field)
        row = self.normalise(payload([value]))["orders"][0]
        self.assertIsNone(row["cancel_qty"])
        self.assertIsNone(row["original_qty"])
        self.assertIsNone(row["ap_code"])
        self.assertEqual((row["tax"], row["fees"], row["price_type"], row["time_in_force"]), ("", "", "", ""))

    def test_missing_fill_datetime_stays_unknown(self):
        value = trade()
        del value.DateTime
        row = self.normalise(payload(trades=[value]))["trades"][0]
        self.assertEqual((row["trade_date"], row["fill_time"], row["trade_date_source"]), ("", "", ""))

    def test_all_rows_must_match_the_exact_account(self):
        for value in (payload([order(Account="S00000000001")]), payload(trades=[trade(Account="S00000000001")])):
            with self.assertRaises(BrokerAdapterError):
                self.normalise(value)

    def test_invalid_identity_and_required_quantities_fail(self):
        for fields in (dict(BS="BUY"), dict(CompanyNo=""), dict(OrderNo=""),
                       dict(AfterQty=-1), dict(OkQty=1.5), dict(OrderStatus=True)):
            with self.subTest(fields=fields), self.assertRaises(BrokerAdapterError):
                self.normalise(payload([order(**fields)]))
        for invalid in (0, -1, True, 0.5, "NaN"):
            with self.assertRaises(BrokerAdapterError):
                self.normalise(payload(trades=[trade(OkQty=invalid)]))

    def test_invalid_optional_numbers_and_prices_fail(self):
        for field, invalid in (("OrderType", True), ("OrderType", 3.5), ("OrderType", 9),
                               ("APCode", "NaN"), ("CancelQty", -1), ("Price", "Infinity"),
                               ("OTax", -1), ("OCharge", True), ("ODueAmt", "NaN")):
            with self.subTest(field=field), self.assertRaises(BrokerAdapterError):
                self.normalise(payload([order(**{field: invalid})]))
        with self.assertRaises(BrokerAdapterError):
            self.normalise(payload(trades=[trade(SPrice=-1)]))

    def test_both_complete_collections_are_required_not_assumed_empty(self):
        for value in (SimpleNamespace(StkOrderList=[]), SimpleNamespace(StkTradeList=[]),
                      SimpleNamespace(StkOrderList=None, StkTradeList=[]),
                      SimpleNamespace(StkOrderList="[]", StkTradeList=[])):
            with self.assertRaises(BrokerAdapterError):
                self.normalise(value)
        self.assertEqual(self.normalise(payload())["orders"], [])

    def test_truncated_dotnet_collection_fails_closed(self):
        class BrokenCollection:
            Count = 2

            def __getitem__(self, index):
                if index == 0:
                    return order()
                raise RuntimeError("MOCK truncated response")

        with self.assertRaises(BrokerAdapterError):
            self.normalise(SimpleNamespace(StkOrderList=BrokenCollection(), StkTradeList=[]))

    def test_no_join_or_basket_overwrite_for_duplicate_order_number(self):
        result = self.normalise(payload([order(BasketNo="BROKER-BASKET", TradeDate=day(date=2)),
                                         order(BasketNo="OTHER-BASKET", TradeDate=day(date=1))], [trade()]))
        self.assertEqual(len(result["orders"]), 2)
        self.assertEqual(result["orders"][0]["basket_no"], "BROKER-BASKET")
        self.assertNotIn("basket_no", result["trades"][0])

    def test_unreadable_provided_fields_fail_not_silently_missing(self):
        def broken(_self):
            raise RuntimeError("MOCK unreadable proof")

        cls = type("UnreadableHistoryOrder", (), {"Time_in_Force": property(broken)})
        value = cls()
        value.__dict__.update(order().__dict__)
        with self.assertRaises(BrokerAdapterError):
            self.normalise(payload([value]))


if __name__ == "__main__":
    unittest.main()
