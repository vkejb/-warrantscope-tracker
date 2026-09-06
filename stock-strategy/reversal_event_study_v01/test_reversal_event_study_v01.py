from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import tempfile
import unittest
from pathlib import Path

from surge_event_study_v01.data import prepare_stocks
from surge_event_study_v01.models import Bar

from .analysis import benjamini_hochberg, cluster_bootstrap, feature_diagnostic
from .backtest import commission, simulate_portfolio
from .config import CFG
from .main import _assert_fresh_output
from .study import (
    build_pattern_observation,
    detect_n_pattern,
    detect_v_pattern,
    evaluate_outcome,
    rank_portfolio_signals,
    scan_period,
)
from .validation import validate_research_run


def trading_dates(count: int, start: date = date(2019, 10, 1)) -> list[str]:
    result: list[str] = []
    cursor = start
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return result


def make_bar(
    day: str,
    code: str,
    close: float = 100.0,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: int = 3_000_000,
) -> Bar:
    open_value = close if open_ is None else open_
    return Bar(
        day,
        code,
        "測試股" if code != "0050" else "元大台灣50",
        volume,
        open_value,
        max(open_value, close) + 0.5 if high is None else high,
        min(open_value, close) - 0.5 if low is None else low,
        close,
    )


def bars_from_closes(dates: list[str], code: str, closes: list[float]) -> list[Bar]:
    result: list[Bar] = []
    for index, (day, close) in enumerate(zip(dates, closes)):
        open_ = closes[index - 1] if index else close
        result.append(make_bar(day, code, close, open_=open_))
    return result


def prepared_fixture(stock_rows: list[Bar], dates: list[str] | None = None):
    actual_dates = dates or [row.date for row in stock_rows]
    benchmark_rows = [make_bar(day, "0050", 100.0) for day in actual_dates]
    prepared, benchmark, _ = prepare_stocks(
        {stock_rows[0].code: stock_rows}, benchmark_rows, CFG
    )
    return prepared[0], benchmark


def v_fixture(count: int = 100):
    dates = trading_dates(count)
    closes = [100.0] * count
    closes[55:61] = [98.0, 95.0, 92.0, 90.0, 88.0, 91.0]
    rows = bars_from_closes(dates, "1234", closes)
    rows[60] = make_bar(
        dates[60], "1234", 91.0, open_=88.0, high=92.0, low=87.0
    )
    return dates, rows


def n_fixture(count: int = 110):
    dates = trading_dates(count)
    closes = [100.0] * count
    sequence = {
        40: 98.0,
        41: 95.0,
        42: 92.0,
        43: 90.0,
        44: 89.0,
        45: 88.0,
        46: 89.0,
        47: 91.0,
        48: 94.0,
        49: 96.0,
        50: 95.0,
        51: 94.0,
        52: 93.0,
        53: 92.0,
        54: 92.0,
        55: 91.5,
        56: 91.0,
        57: 90.5,
        58: 90.0,
        59: 89.0,
        60: 92.0,
    }
    for index, value in sequence.items():
        closes[index] = value
    rows = bars_from_closes(dates, "1234", closes)
    rows[60] = make_bar(
        dates[60], "1234", 92.0, open_=89.0, high=93.0, low=88.0
    )
    return dates, rows


class PatternTests(unittest.TestCase):
    def test_v_pattern_is_causal_and_detected(self):
        dates, rows = v_fixture()
        stock, benchmark = prepared_fixture(rows, dates)
        geometry = detect_v_pattern(stock, 60, CFG)
        self.assertIsNotNone(geometry)
        self.assertEqual(geometry["pivot_index"], 59)
        observation = build_pattern_observation(stock, 60, benchmark, CFG)
        self.assertEqual(observation.pattern, "V_REVERSAL")

    def test_future_bars_do_not_change_t_pattern_or_features(self):
        dates, rows = v_fixture()
        changed = list(rows)
        for index in range(70, 80):
            changed[index] = make_bar(
                dates[index], "1234", 160.0, open_=160.0, high=161.0, low=159.0
            )
        stock_a, benchmark_a = prepared_fixture(rows, dates)
        stock_b, benchmark_b = prepared_fixture(changed, dates)
        first = build_pattern_observation(stock_a, 60, benchmark_a, CFG)
        second = build_pattern_observation(stock_b, 60, benchmark_b, CFG)
        self.assertEqual(first.pattern, second.pattern)
        self.assertEqual(first.geometry, second.geometry)
        self.assertEqual(first.features, second.features)

    def test_n_retest_uses_latest_prior_local_low(self):
        dates, rows = n_fixture()
        stock, benchmark = prepared_fixture(rows, dates)
        geometry = detect_n_pattern(stock, 60, CFG)
        self.assertIsNotNone(geometry)
        self.assertEqual(geometry["first_pivot_index"], 45)
        self.assertEqual(geometry["pivot_index"], 59)
        observation = build_pattern_observation(stock, 60, benchmark, CFG)
        self.assertEqual(observation.pattern, "N_RETEST")

    def test_n_priority_removes_raw_overlap(self):
        dates, rows = n_fixture()
        # Make the last five-session selloff large enough for V while retaining N.
        closes = [row.close for row in rows]
        closes[46:60] = [
            89.0,
            90.0,
            91.0,
            92.0,
            93.0,
            94.0,
            95.0,
            96.0,
            96.0,
            94.0,
            92.0,
            90.0,
            89.0,
            88.5,
        ]
        rows = bars_from_closes(dates, "1234", closes)
        rows[60] = make_bar(
            dates[60], "1234", 92.0, open_=88.5, high=93.0, low=88.0
        )
        stock, benchmark = prepared_fixture(rows, dates)
        self.assertIsNotNone(detect_v_pattern(stock, 60, CFG))
        self.assertIsNotNone(detect_n_pattern(stock, 60, CFG))
        observation = build_pattern_observation(stock, 60, benchmark, CFG)
        self.assertEqual(observation.pattern, "N_RETEST")
        self.assertTrue(observation.raw_overlap)


class OutcomeTests(unittest.TestCase):
    def test_target_before_stop_is_success_using_t1_open(self):
        dates, rows = v_fixture()
        rows[61] = make_bar(dates[61], "1234", 102.0, open_=100.0)
        rows[62] = make_bar(dates[62], "1234", 108.0, open_=102.0)
        rows[63] = make_bar(dates[63], "1234", 94.0, open_=108.0)
        for index in range(64, 71):
            rows[index] = make_bar(dates[index], "1234", 103.0, open_=103.0)
        stock, _ = prepared_fixture(rows, dates)
        result = evaluate_outcome(stock, 60, CFG)
        self.assertEqual(result.entry_open, 100.0)
        self.assertTrue(result.primary_success)
        self.assertEqual(result.first_target_day, 2)
        self.assertEqual(result.first_stop_day, 3)
        self.assertEqual(result.path_result, "TARGET_BEFORE_STOP")

    def test_stop_before_later_target_is_failure(self):
        dates, rows = v_fixture()
        rows[61] = make_bar(dates[61], "1234", 94.0, open_=100.0)
        rows[62] = make_bar(dates[62], "1234", 109.0, open_=94.0)
        for index in range(63, 71):
            rows[index] = make_bar(dates[index], "1234", 100.0, open_=100.0)
        stock, _ = prepared_fixture(rows, dates)
        result = evaluate_outcome(stock, 60, CFG)
        self.assertFalse(result.primary_success)
        self.assertEqual(result.path_result, "STOP_BEFORE_TARGET")

    def test_missing_future_market_session_censors_but_not_signal(self):
        dates, rows = v_fixture()
        rows = [row for row in rows if row.date != dates[65]]
        stock, benchmark = prepared_fixture(rows, dates)
        local_index = stock.calendar_indices.index(60)
        self.assertIsNotNone(build_pattern_observation(stock, local_index, benchmark, CFG))
        self.assertEqual(evaluate_outcome(stock, local_index, CFG).status, "CENSORED")


class ScanAndStatisticsTests(unittest.TestCase):
    def test_scan_cooldown_is_past_only(self):
        dates, rows = v_fixture(115)
        # Install a second independent V setup 10 market sessions later.
        closes = [row.close for row in rows]
        closes[65:71] = [98.0, 95.0, 92.0, 90.0, 88.0, 91.0]
        rows = bars_from_closes(dates, "1234", closes)
        rows[60] = make_bar(dates[60], "1234", 91.0, open_=88.0, high=92.0, low=87.0)
        rows[70] = make_bar(dates[70], "1234", 91.0, open_=88.0, high=92.0, low=87.0)
        stock, benchmark = prepared_fixture(rows, dates)
        result = scan_period([stock], benchmark, dates[60], dates[70], "test", CFG)
        included = [row for row in result["signal_rows"] if row["cooldown_included"]]
        self.assertGreaterEqual(len(included), 2)

    def test_period_boundary_uses_preperiod_cooldown_seed(self):
        dates, rows = v_fixture(110)
        closes = [row.close for row in rows]
        closes[62:69] = [93.0, 90.0, 87.0, 85.0, 83.0, 82.0, 85.0]
        rows = bars_from_closes(dates, "1234", closes)
        rows[60] = make_bar(dates[60], "1234", 91.0, open_=88.0, high=92.0, low=87.0)
        rows[68] = make_bar(dates[68], "1234", 85.0, open_=82.0, high=86.0, low=81.0)
        stock, benchmark = prepared_fixture(rows, dates)
        result = scan_period([stock], benchmark, dates[68], dates[68], "test", CFG)
        matching = [row for row in result["signal_rows"] if row["signal_date"] == dates[68]]
        self.assertEqual(len(matching), 1)
        self.assertFalse(matching[0]["cooldown_included"])
        self.assertGreaterEqual(result["cooldown_seed_count"], 1)

    def test_ranking_uses_t_fields_and_is_deterministic(self):
        rows = [
            {
                "signal_date": "20230103",
                "code": "2222",
                "pattern": "V_REVERSAL",
                "cooldown_included": True,
                "signal_return_1": 0.04,
                "average_turnover_proxy_20": 60_000_000.0,
            },
            {
                "signal_date": "20230103",
                "code": "1111",
                "pattern": "V_REVERSAL",
                "cooldown_included": True,
                "signal_return_1": 0.05,
                "average_turnover_proxy_20": 50_000_000.0,
            },
        ]
        ranked = rank_portfolio_signals(rows, "V_REVERSAL")
        self.assertEqual([row["code"] for row in ranked], ["1111", "2222"])
        self.assertEqual([row["daily_rank"] for row in ranked], [1, 2])

    def test_bh_bounded(self):
        adjusted = benjamini_hochberg({"a": 0.001, "b": 0.02, "c": 0.5})
        self.assertTrue(all(0 <= value <= 1 for value in adjusted.values()))
        self.assertLessEqual(adjusted["a"], adjusted["b"])

    def test_feature_diagnostic_never_selects_v01_trades(self):
        self.assertEqual(
            feature_diagnostic([], [], CFG)["policy"],
            "No diagnostic feature filters or reranks V0.1 trades.",
        )

    def test_cluster_bootstrap_is_month_clustered(self):
        rows = []
        for index in range(10):
            rows.append(
                {
                    "signal_date": f"202301{index + 2:02d}",
                    "pattern": "V_REVERSAL",
                    "cooldown_included": True,
                    "outcome_status": "EVALUABLE",
                    "primary_success": index % 2 == 0,
                    "day10_close_return": 0.01,
                    "gross_close_rule_return": 0.01,
                }
            )
        result = cluster_bootstrap(rows, "V_REVERSAL", CFG, iterations=20)
        self.assertEqual(result["cluster"], "calendar month resampled within year")


class PortfolioAndIntegrityTests(unittest.TestCase):
    def test_quantity_is_frozen_from_t_limit(self):
        dates = trading_dates(90, date(2022, 10, 3))
        closes = [100.0] * 90
        rows = bars_from_closes(dates, "1234", closes)
        rows[61] = make_bar(dates[61], "1234", 50.0, open_=50.0)
        for index in range(62, 90):
            rows[index] = make_bar(dates[index], "1234", 50.0, open_=50.0)
        stock, benchmark = prepared_fixture(rows, dates)
        signals = [
            {
                "signal_date": dates[60],
                "daily_rank": 1,
                "code": "1234",
                "name": "測試股",
                "pattern": "V_REVERSAL",
                "signal_close": 100.0,
            }
        ]
        result = simulate_portfolio(
            signals, [stock], benchmark, dates[60], dates[-1], scenario="baseline", cfg=CFG
        )
        self.assertEqual(result["trades"][0]["shares"], 97)
        self.assertFalse(result["trades"][0]["is_actual_fill"])

    def test_t_plus_1_segment_change_is_rejected(self):
        dates = trading_dates(90, date(2022, 10, 3))
        rows = bars_from_closes(dates, "1234", [100.0] * 90)
        rows[61] = make_bar(dates[61], "1234", 50.0, open_=50.0)
        for index in range(62, 90):
            rows[index] = make_bar(dates[index], "1234", 50.0, open_=50.0)
        stock, benchmark = prepared_fixture(rows, dates)
        signals = [
            {
                "signal_date": dates[60],
                "daily_rank": 1,
                "code": "1234",
                "name": "測試股",
                "pattern": "V_REVERSAL",
                "signal_close": 100.0,
                "signal_segment_id": stock.segment_ids[60],
            }
        ]
        result = simulate_portfolio(
            signals, [stock], benchmark, dates[60], dates[-1], scenario="baseline", cfg=CFG
        )
        self.assertEqual(result["summary"]["completed_trades"], 0)
        self.assertEqual(result["order_audit"]["t_plus_1_discontinuity_rejected"], 1)

    def test_commission_minimum_and_no_live_mode(self):
        self.assertEqual(commission(100.0, CFG.commission_rate, 1), 1.0)
        self.assertEqual(CFG.execution_mode, "RESEARCH_ONLY_NO_BROKER_NO_ORDER")

    def test_fresh_output_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileExistsError):
                _assert_fresh_output(Path(directory))

    def test_failed_validation_cannot_open_oos(self):
        empty_period = {
            "maximum_signal_date_read": "20221230",
            "signal_rows": [],
        }
        validation_period = {
            "maximum_signal_date_read": "20241231",
            "signal_rows": [],
        }
        diagnostic = {
            "policy": "No diagnostic feature filters or reranks V0.1 trades.",
            "selection_rows": [],
        }
        decision = {"patterns_allowed_into_2025": []}
        result = validate_research_run(
            {"broad_source_gap_dates": []},
            empty_period,
            validation_period,
            diagnostic,
            decision,
            None,
            CFG,
        )
        self.assertTrue(result["passed"])


if __name__ == "__main__":
    unittest.main()
