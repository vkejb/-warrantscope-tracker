"""Pure DTO evidence regressions; no SDK, login, store or order execution."""

from datetime import datetime
from types import SimpleNamespace
import unittest

from yuanta_broker_execution_v01.adapter import (
    BrokerAdapterError,
    TAIPEI,
    YuantaSparkExecutionAdapter,
    _normalise_merge_report,
    _normalise_real_report,
)
from yuanta_broker_execution_v01.models import Side


NORMALISERS = (_normalise_real_report, _normalise_merge_report)


def native_date(year=2026, month=10, day=2):
    return SimpleNamespace(ushtYear=year, bytMon=month, bytDay=day)


def native_time(hour=9, minute=15, second=30, milliseconds=7):
    return SimpleNamespace(
        bytHour=hour, bytMin=minute, bytSec=second, ushtMSec=milliseconds
    )


def row(**changes):
    values = dict(
        Account="S00000000000", RptType=51, OrderNo="MOCK-ORDER",
        CompanyNo="3094", BS="B", Price=70, OrderQty=1000,
        OrderStatus=8, OkQty=1000, LastOrderStatus=8,
        BasketNo="MOCK-BASKET", SeqNo=1,
    )
    values.update(changes)
    return SimpleNamespace(**values)


class BrokerReportDateTests(unittest.TestCase):
    def assert_temporal(self, value, **expected):
        for normalise in NORMALISERS:
            with self.subTest(normaliser=normalise.__name__):
                report = normalise(value)
                for key, answer in expected.items():
                    self.assertEqual(report[key], answer)

    def assert_rejected(self, value):
        for normalise in NORMALISERS:
            with self.subTest(normaliser=normalise.__name__):
                with self.assertRaises(BrokerAdapterError):
                    normalise(value)

    def test_documented_native_date_and_time(self):
        self.assert_temporal(
            row(OrderDate=native_date(), OrderTime=native_time()),
            trade_date="20261002", trade_date_source="OrderDate",
            order_time="09:15:30.007",
        )

    def test_native_order_type_is_preserved_without_trade_kind_inference(self):
        # RealReport.TradeKind is an action (BUY/SELL/cancel), not financing.
        # The optional native OrderType remains the only source for this field.
        for order_type, trade_kind in (("0", 4), ("3", 0), ("4", 1)):
            with self.subTest(order_type=order_type, trade_kind=trade_kind):
                self.assert_temporal(
                    row(OrderType=order_type, TradeKind=trade_kind),
                    order_type=order_type,
                )

    def test_absent_or_empty_order_type_remains_unknown(self):
        self.assert_temporal(row(TradeKind=0), order_type="")
        for empty in (None, "", "   "):
            self.assert_temporal(row(OrderType=empty, TradeKind=0), order_type="")

    def test_unreadable_order_type_remains_unknown(self):
        def broken(_self):
            raise RuntimeError("MOCK unreadable order type")

        cls = type("UnreadableOrderTypeRow", (), {"OrderType": property(broken)})
        value = cls()
        value.__dict__.update(row(TradeKind=0).__dict__)
        self.assert_temporal(value, order_type="")

    def test_native_date_fallback_when_explicit_date_blank_or_null(self):
        for empty in ("", "  ", None):
            with self.subTest(empty=empty):
                self.assert_temporal(
                    row(TradeDate=empty, OrderDate=native_date()),
                    trade_date="20261002", trade_date_source="OrderDate",
                )

    def test_explicit_trade_date_formats_are_validated_and_preserved(self):
        for text in ("20261002", "2026/10/02", "2026-10-02"):
            self.assert_temporal(
                row(TradeDate=text), trade_date=text,
                trade_date_source="TradeDate", order_time="",
            )

    def test_matching_explicit_and_native_dates_record_both_sources(self):
        self.assert_temporal(
            row(TradeDate="2026/10/02", OrderDate=native_date()),
            trade_date="2026/10/02", trade_date_source="TradeDate+OrderDate",
        )

    def test_conflicting_dates_rejected(self):
        self.assert_rejected(row(TradeDate="20261001", OrderDate=native_date()))

    def test_invalid_explicit_date_never_falls_back_to_valid_native_date(self):
        for text in ("20260230", "2026/1/02", "2026-10/02", "20261002junk",
                     "NaN", "00000000", "1121002"):
            with self.subTest(text=text):
                self.assert_rejected(row(TradeDate=text, OrderDate=native_date()))

    def test_invalid_native_date_rejected_even_with_valid_explicit_date(self):
        self.assert_rejected(
            row(TradeDate="20261002", OrderDate=native_date(2026, 2, 30))
        )

    def test_legacy_absent_dates_remain_unknown_not_today(self):
        self.assert_temporal(
            row(), trade_date="", trade_date_source="", order_time="",
        )

    def test_blank_or_null_optional_native_fields_remain_unknown(self):
        for empty in (None, "", "   "):
            self.assert_temporal(
                row(TradeDate=empty, OrderDate=empty, OrderTime=empty),
                trade_date="", trade_date_source="", order_time="",
            )

    def test_leap_years_and_modern_gregorian_boundaries(self):
        for year, month, day, expected in (
            (1900, 1, 1, "19000101"), (2000, 2, 29, "20000229"),
            (2024, 2, 29, "20240229"), (9999, 12, 31, "99991231"),
        ):
            self.assert_temporal(
                row(OrderDate=native_date(year, month, day)), trade_date=expected,
            )
        for year, month, day in (
            (1900, 2, 29), (2023, 2, 29), (2026, 0, 2), (2026, 13, 1),
            (2026, 10, 0), (2026, 4, 31), (0, 0, 0), (112, 10, 2),
            (10000, 1, 1),
        ):
            self.assert_rejected(row(OrderDate=native_date(year, month, day)))

    def test_native_date_components_must_be_finite_integral_not_bool(self):
        for field in ("ushtYear", "bytMon", "bytDay"):
            for bad in (True, -1, 1.5, "NaN", "Infinity", None, "bad"):
                date = native_date()
                setattr(date, field, bad)
                self.assert_rejected(row(OrderDate=date))

    def test_missing_native_components_and_nonempty_wrong_type_rejected(self):
        for bad in (SimpleNamespace(ushtYear=2026, bytMon=10), "20261002", 0):
            self.assert_rejected(row(OrderDate=bad))

    def test_documented_time_boundaries_and_milliseconds(self):
        for value, expected in (
            (native_time(0, 0, 0, 0), "00:00:00.000"),
            (native_time(23, 59, 59, 999), "23:59:59.999"),
        ):
            self.assert_temporal(row(OrderTime=value), order_time=expected)

    def test_invalid_native_time_rejected(self):
        for field, bad in (
            ("bytHour", 24), ("bytMin", 60), ("bytSec", 60),
            ("ushtMSec", 1000), ("bytHour", -1), ("bytMin", 1.5),
            ("bytSec", True), ("ushtMSec", "NaN"),
        ):
            value = native_time()
            setattr(value, field, bad)
            self.assert_rejected(row(OrderTime=value))
        for bad in (SimpleNamespace(bytHour=9, bytMin=0, bytSec=0), "09:15:00"):
            self.assert_rejected(row(OrderTime=bad))

    def test_time_evidence_does_not_invent_missing_date(self):
        self.assert_temporal(
            row(OrderTime=native_time()), trade_date="", trade_date_source="",
            order_time="09:15:30.007",
        )

    def test_unreadable_provided_temporal_fields_rejected(self):
        for name in ("TradeDate", "OrderDate", "OrderTime"):
            for error in (RuntimeError, AttributeError):
                def broken(_self, error=error):
                    raise error("MOCK unreadable temporal field")

                cls = type("UnreadableRow", (), {name: property(broken)})
                value = cls()
                value.__dict__.update(row().__dict__)
                self.assert_rejected(value)

    def test_native_day_is_used_by_order_identity_validation(self):
        adapter = YuantaSparkExecutionAdapter.__new__(YuantaSparkExecutionAdapter)
        adapter.account = "S00000000000"
        local = SimpleNamespace(
            created_at=datetime(2026, 10, 2, 9, tzinfo=TAIPEI).isoformat(),
            symbol="3094", side=Side.BUY, basket_no="MOCK-BASKET",
            broker_order_no="MOCK-ORDER",
        )
        for normalise in NORMALISERS:
            self.assertTrue(adapter._identity_matches(
                local, normalise(row(OrderDate=native_date()))
            ))
            self.assertFalse(adapter._identity_matches(
                local, normalise(row(OrderDate=native_date(2026, 10, 1)))
            ))


if __name__ == "__main__":
    unittest.main()
