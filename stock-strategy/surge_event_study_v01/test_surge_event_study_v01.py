from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import csv
import tempfile
import unittest
from pathlib import Path
import zipfile

from .analysis import (
    benjamini_hochberg,
    percentile_quintile,
    percentile_ranks,
    score_period,
)
from .backtest import commission, simulate_portfolio
from .config import CFG
from .data import load_ohlcv, prepare_stocks
from .features import build_signal_observation, evaluate_outcome
from .models import Bar
from .validation import validate_research_run


def trading_dates(count: int, start: date = date(2019, 10, 1)) -> list[str]:
    result = []
    cursor = start
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return result


def bar(day: str, code: str, close: float = 100.0, *, open_: float | None = None, volume: int = 3_000_000) -> Bar:
    open_value = close if open_ is None else open_
    return Bar(
        day,
        code,
        "測試股" if code != "0050" else "元大台灣50",
        volume,
        open_value,
        max(open_value, close) + 0.5,
        min(open_value, close) - 0.5,
        close,
    )


def prepared_fixture(count: int = 100, stock_bars: list[Bar] | None = None, missing_0050: set[str] | None = None):
    dates = trading_dates(count)
    benchmark = [
        bar(day, "0050", 100.0 + index * 0.02)
        for index, day in enumerate(dates)
        if day not in (missing_0050 or set())
    ]
    stocks = {"1234": stock_bars or [bar(day, "1234") for day in dates]}
    prepared, prepared_benchmark, _ = prepare_stocks(stocks, benchmark, CFG)
    return dates, prepared[0], prepared_benchmark


class PercentileTests(unittest.TestCase):
    def test_ties_receive_average_rank(self):
        ranks = percentile_ranks([1.0, 2.0, 2.0, 4.0])
        self.assertEqual(ranks[1], ranks[2])
        self.assertAlmostEqual(ranks[0], 0.125)
        self.assertAlmostEqual(ranks[1], 0.5)
        self.assertAlmostEqual(ranks[3], 0.875)
        self.assertEqual(percentile_quintile(ranks[0]), 1)
        self.assertEqual(percentile_quintile(ranks[3]), 5)

    def test_bh_is_monotone_and_bounded(self):
        adjusted = benjamini_hochberg({"a": 0.001, "b": 0.02, "c": 0.5})
        self.assertLessEqual(adjusted["a"], adjusted["b"])
        self.assertLessEqual(adjusted["b"], adjusted["c"])
        self.assertTrue(all(0 <= value <= 1 for value in adjusted.values()))


class CausalityTests(unittest.TestCase):
    def test_future_prices_do_not_change_t_features(self):
        dates = trading_dates(100)
        first = [bar(day, "1234", 100.0) for day in dates]
        changed = list(first)
        changed[70] = bar(dates[70], "1234", 109.0, open_=100.0)
        _, stock_a, benchmark_a = prepared_fixture(stock_bars=first)
        _, stock_b, benchmark_b = prepared_fixture(stock_bars=changed)
        observation_a = build_signal_observation(stock_a, 60, benchmark_a, CFG)
        observation_b = build_signal_observation(stock_b, 60, benchmark_b, CFG)
        self.assertIsNotNone(observation_a)
        self.assertEqual(observation_a.features, observation_b.features)
        self.assertEqual(observation_a.signal_close, observation_b.signal_close)

    def test_outcome_uses_t_plus_1_open_and_exact_day10(self):
        dates = trading_dates(100)
        rows = [bar(day, "1234", 100.0) for day in dates]
        rows[61] = bar(dates[61], "1234", 101.0, open_=100.0)
        rows[62] = bar(dates[62], "1234", 108.0, open_=101.0)
        rows[63] = bar(dates[63], "1234", 115.0, open_=108.0)
        rows[64] = bar(dates[64], "1234", 110.0, open_=115.0)
        for index in range(65, 71):
            rows[index] = bar(dates[index], "1234", 110.0)
        _, stock, benchmark = prepared_fixture(stock_bars=rows)
        self.assertIsNotNone(build_signal_observation(stock, 60, benchmark, CFG))
        outcome = evaluate_outcome(stock, 60, CFG)
        self.assertEqual(outcome.status, "EVALUABLE")
        self.assertEqual(outcome.entry_open, 100.0)
        self.assertEqual(outcome.first_primary_hit_day, 3)
        self.assertAlmostEqual(outcome.close_return_10, 0.10)
        self.assertTrue(outcome.primary_event)

    def test_missing_future_bar_does_not_remove_signal_but_censors_label(self):
        dates = trading_dates(100)
        rows = [bar(day, "1234") for day in dates if day != dates[65]]
        _, stock, benchmark = prepared_fixture(stock_bars=rows)
        local_index = stock.calendar_indices.index(60)
        self.assertIsNotNone(build_signal_observation(stock, local_index, benchmark, CFG))
        outcome = evaluate_outcome(stock, local_index, CFG)
        self.assertEqual(outcome.status, "NOT_EVALUABLE")
        self.assertEqual(
            outcome.reason, "MISSING_OR_DISCONTINUOUS_PRIMARY_FORWARD_WINDOW"
        )

    def test_future_discontinuity_censors_outcome_not_signal(self):
        dates = trading_dates(100)
        rows = [bar(day, "1234") for day in dates]
        rows[64] = bar(dates[64], "1234", 80.0, open_=80.0)
        for index in range(65, 100):
            rows[index] = bar(dates[index], "1234", 80.0)
        _, stock, benchmark = prepared_fixture(stock_bars=rows)
        self.assertIsNotNone(build_signal_observation(stock, 60, benchmark, CFG))
        self.assertEqual(evaluate_outcome(stock, 60, CFG).status, "NOT_EVALUABLE")

    def test_past_discontinuity_breaks_sixty_session_history(self):
        dates = trading_dates(100)
        rows = [bar(day, "1234") for day in dates]
        rows[30] = bar(dates[30], "1234", 80.0, open_=80.0)
        for index in range(31, 100):
            rows[index] = bar(dates[index], "1234", 80.0)
        _, stock, benchmark = prepared_fixture(stock_bars=rows)
        self.assertIsNone(build_signal_observation(stock, 60, benchmark, CFG))

    def test_market_calendar_does_not_compress_0050_suspension(self):
        dates = trading_dates(100)
        _, _, benchmark = prepared_fixture(missing_0050={dates[50]})
        self.assertEqual(len(benchmark.calendar), 100)
        self.assertIsNone(benchmark.normalized_closes[50])
        self.assertNotEqual(benchmark.segment_ids[49], benchmark.segment_ids[51])

    def test_top30_is_ranked_before_future_outcome_completeness(self):
        dates = trading_dates(85)
        stocks = {}
        for number in range(31):
            code = f"{1000 + number}"
            rows = [bar(day, code, 100.0) for day in dates]
            if number == 30:
                for index in range(40, 61):
                    close = 100.0 + (index - 39) * 0.45
                    previous = rows[index - 1].close
                    rows[index] = bar(dates[index], code, close, open_=previous)
                rows = [item for item in rows if item.date != dates[65]]
            stocks[code] = rows
        benchmark_rows = [bar(day, "0050", 100.0) for day in dates]
        prepared, benchmark, _ = prepare_stocks(stocks, benchmark_rows, CFG)
        rule = {
            "features": [
                {"feature": "return_20", "family": "momentum", "direction": "HIGH"}
            ]
        }
        result = score_period(
            prepared, benchmark, rule, dates[60], dates[60], "test", CFG
        )
        selected = {row["code"]: row for row in result["signal_rows"]}
        self.assertIn("1030", selected)
        self.assertEqual(selected["1030"]["daily_rank"], 1)
        self.assertEqual(selected["1030"]["outcome_status"], "NOT_EVALUABLE")


class LoaderTests(unittest.TestCase):
    def test_invalid_suspension_row_is_skipped_and_supplement_overlays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "sample.zip"
            csv_text = (
                "date,code,name,volume,open,high,low,close\n"
                "20200102,0050,元大台灣50,1000,100,101,99,100\n"
                "20200102,1234,測試,3000000,100,101,99,100\n"
                "20200103,1234,測試,0,,,,\n"
            )
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("sample.csv", csv_text)
            supplement = root / "supplement.csv"
            supplement.write_text(
                "date,code,name,volume,open,high,low,close\n"
                "20200102,1234,測試,3000000,101,102,100,101\n",
                encoding="utf-8",
            )
            stocks, _, audit = load_ohlcv(
                [archive_path], supplement_paths=[supplement], cfg=CFG
            )
            self.assertEqual(stocks["1234"][0].close, 101.0)
            self.assertEqual(audit["invalid_rows"], 1)
            self.assertEqual(audit["duplicate_rows_overridden"], 1)


class PortfolioTests(unittest.TestCase):
    def test_quantity_is_fixed_from_t_limit_not_cheaper_t1_open(self):
        dates = trading_dates(80, date(2022, 10, 3))
        stock_rows = [bar(day, "1234", 100.0) for day in dates]
        stock_rows[61] = bar(dates[61], "1234", 45.0, open_=50.0)
        stock_rows[62] = bar(dates[62], "1234", 40.0, open_=40.0)
        for index in range(63, len(stock_rows)):
            stock_rows[index] = bar(dates[index], "1234", 40.0)
        benchmark_rows = [bar(day, "0050", 100.0) for day in dates]
        prepared, benchmark, _ = prepare_stocks(
            {"1234": stock_rows}, benchmark_rows, CFG
        )
        signals = [
            {
                "signal_date": dates[0],
                "daily_rank": 1,
                "code": "1234",
                "name": "測試股",
                "signal_close": 100.0,
            }
        ]
        signals[0]["signal_date"] = dates[60]
        result = simulate_portfolio(
            signals,
            prepared,
            benchmark,
            dates[60],
            dates[-1],
            scenario="baseline",
            cfg=CFG,
        )
        self.assertEqual(result["trades"][0]["shares"], 97)
        self.assertFalse(result["trades"][0]["is_actual_fill"])

    def test_commission_has_minimum(self):
        self.assertEqual(commission(100.0, CFG.commission_rate, 1), 1.0)


class IntegrityTests(unittest.TestCase):
    def test_duplicate_feature_family_fails_validation(self):
        discovery = {
            "selected_rule": {
                "features": [
                    {"feature": "return_5", "family": "momentum", "direction": "HIGH"},
                    {"feature": "return_20", "family": "momentum", "direction": "HIGH"},
                ],
                "does_not_use_validation_or_oos": True,
            }
        }
        result = validate_research_run(
            {"broad_source_gap_dates": []},
            discovery,
            None,
            {"status": "FAIL"},
            None,
            CFG,
        )
        self.assertFalse(result["passed"])
        self.assertIn("one_feature_per_family", result["failures"])

    def test_config_fingerprint_is_stable_and_no_live_mode(self):
        self.assertEqual(CFG.fingerprint(), CFG.fingerprint())
        self.assertEqual(CFG.execution_mode, "RESEARCH_ONLY_NO_BROKER_NO_ORDER")


if __name__ == "__main__":
    unittest.main()
