from __future__ import annotations

from datetime import date, timedelta
import math
from pathlib import Path
import sys
import unittest


# ``stock-strategy`` is intentionally not a Python package (its directory name
# contains a dash).  Make the shared research packages importable whether this
# file is run from the repository root or through unittest discovery.
STOCK_STRATEGY_ROOT = Path(__file__).resolve().parents[2]
if str(STOCK_STRATEGY_ROOT) not in sys.path:
    sys.path.insert(0, str(STOCK_STRATEGY_ROOT))

from multi_setup_study_v01.analysis import (  # noqa: E402
    cluster_bootstrap,
    entry_gap_bucket,
    metric_summary,
    signal_clustering_rows,
    winner_dependence_rows,
)
from multi_setup_study_v01.config import CFG, SETUPS, period_label  # noqa: E402
from multi_setup_study_v01.outcomes import (  # noqa: E402
    cost_adjusted_return,
    evaluate_unified_outcome,
)
from multi_setup_study_v01.ownership import UnavailableOwnershipProvider  # noqa: E402
from multi_setup_study_v01.setup_detectors import (  # noqa: E402
    detect_consolidation_breakout_v2,
    detect_trend_pullback,
    is_compact_retest,
)
from surge_event_study_v01.data import prepare_stocks  # noqa: E402
from surge_event_study_v01.models import Bar  # noqa: E402
from v21.backtest import net_return as v21_net_return  # noqa: E402


EXPECTED_SETUPS = (
    "V_REVERSAL",
    "N_RETEST",
    "N_COMPACT_RETEST_HYPOTHESIS",
    "MOMENTUM_DIRECTIONAL",
    "TREND_PULLBACK",
    "CONSOLIDATION_BREAKOUT_V2",
)


def trading_dates(count: int, start: date = date(2019, 9, 2)) -> list[str]:
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
    close: float,
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
        max(open_value, close) + 0.25 if high is None else high,
        min(open_value, close) - 0.25 if low is None else low,
        close,
    )


def bars_from_closes(
    dates: list[str],
    closes: list[float],
    *,
    code: str = "1234",
    special: dict[int, dict] | None = None,
) -> list[Bar]:
    overrides = special or {}
    rows: list[Bar] = []
    for index, (day, close) in enumerate(zip(dates, closes)):
        values = dict(overrides.get(index, {}))
        values.setdefault("open_", closes[index - 1] if index else close)
        rows.append(make_bar(day, code, close, **values))
    return rows


def prepared_fixture(rows: list[Bar]):
    dates = [row.date for row in rows]
    benchmark_rows = [make_bar(day, "0050", 100.0) for day in dates]
    stocks, benchmark, _ = prepare_stocks(
        {rows[0].code: rows}, benchmark_rows, CFG
    )
    return stocks[0], benchmark


def outcome_fixture(forward_closes: list[float]) -> tuple[object, int]:
    if len(forward_closes) != 10:
        raise ValueError("the unified outcome fixture needs exactly ten closes")
    dates = trading_dates(100)
    closes = [100.0] * len(dates)
    signal_index = 70
    closes[signal_index + 1 : signal_index + 11] = forward_closes
    special = {
        # A deliberately extreme intraday High proves that MFE and the fixed
        # barriers use Close, matching the existing reversal study semantics.
        signal_index + 1: {"open_": 100.0, "high": 130.0, "low": 99.0}
    }
    rows = bars_from_closes(dates, closes, special=special)
    stock, _ = prepared_fixture(rows)
    return stock, signal_index


def analysis_row(
    signal_date: str,
    gross_return: float,
    *,
    setup: str = "TREND_PULLBACK",
    primary_success: bool | None = None,
) -> dict:
    success = gross_return >= 0.08 if primary_success is None else primary_success
    return {
        "setup": setup,
        "signal_date": signal_date,
        "outcome_status": "EVALUABLE",
        "primary_success": success,
        "gross_return": gross_return,
        "net_return": gross_return - 0.006,
        "day10_close_return": gross_return,
        "mfe_10d": max(gross_return, 0.01),
        "mae_10d": min(gross_return, -0.01),
    }


class ConfigurationTests(unittest.TestCase):
    def test_registry_and_frozen_research_contract(self):
        self.assertEqual(tuple(SETUPS), EXPECTED_SETUPS)
        self.assertEqual(CFG.primary_target, 0.08)
        self.assertEqual(CFG.primary_stop, -0.05)
        self.assertEqual(CFG.primary_horizon, 10)
        self.assertEqual(CFG.slippage_one_way, 0.001)
        self.assertGreaterEqual(CFG.bootstrap_iterations, 5_000)
        self.assertEqual(CFG.execution_mode, "RESEARCH_ONLY_NO_BROKER_NO_ORDER")
        self.assertEqual(CFG.ownership_status, "NOT_TESTED_DATA_UNAVAILABLE")
        self.assertEqual(CFG.fingerprint(), CFG.fingerprint())

    def test_ownership_provider_fails_closed_without_backfill(self):
        rows = UnavailableOwnershipProvider().load_asof(
            ["2330"], ["2023-01-03T13:30:00+08:00"]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "NOT_TESTED_DATA_UNAVAILABLE")
        self.assertEqual(rows[0].values, {})
        self.assertIsNone(rows[0].known_at)

    def test_period_labels_do_not_misstate_old_data_as_blind(self):
        self.assertEqual(period_label("20221230"), "HISTORICAL_DISCOVERY")
        self.assertEqual(
            period_label("20230103"), "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS"
        )
        self.assertEqual(
            period_label("20241231"), "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS"
        )
        self.assertEqual(
            period_label("20250102"), "STRESS_PREVALENCE_SEEN_NOT_BLIND"
        )
        self.assertNotEqual(
            period_label("20260906"), "PROSPECTIVE_SHADOW_AFTER_2026_09_06"
        )
        self.assertEqual(
            period_label("20260907"), "PROSPECTIVE_SHADOW_AFTER_2026_09_06"
        )

    def test_cost_proxy_is_exactly_the_existing_v21_model(self):
        self.assertAlmostEqual(
            cost_adjusted_return(100.0, 108.0),
            v21_net_return(100.0, 108.0, CFG.slippage_one_way),
        )
        self.assertEqual(CFG.per_trade_notional, 30_000.0)


class DetectorTests(unittest.TestCase):
    def test_compact_retest_is_an_annotation_not_a_new_geometry_search(self):
        compact = {
            "pivot_separation_sessions": 7,
            "bottom_difference": 0.01,
            "confirmation_rebound": 0.04,
        }
        self.assertTrue(is_compact_retest(compact))
        self.assertFalse(
            is_compact_retest({**compact, "pivot_separation_sessions": 8})
        )
        self.assertFalse(is_compact_retest({**compact, "bottom_difference": 0.0}))

    def test_trend_pullback_is_past_only(self):
        dates = trading_dates(110)
        closes = [70.0 + index * 0.35 for index in range(80)] + [100.0] * 30
        closes[74:80] = [107.0, 110.0, 108.0, 106.0, 104.0, 107.0]
        rows = bars_from_closes(dates, closes)
        stock_a, benchmark_a = prepared_fixture(rows)
        detected_a = detect_trend_pullback(stock_a, 79, benchmark_a, CFG)
        self.assertIsNotNone(detected_a)

        changed = list(rows)
        changed[80] = make_bar(
            dates[80], "1234", 180.0, open_=100.0, high=181.0, low=99.0
        )
        stock_b, benchmark_b = prepared_fixture(changed)
        detected_b = detect_trend_pullback(stock_b, 79, benchmark_b, CFG)
        self.assertEqual(detected_a, detected_b)

    def test_consolidation_breakout_is_past_only(self):
        dates = trading_dates(110)
        closes = [100.0] * 110
        for index in range(59, 74):
            closes[index] = 102.0 if index % 2 else 98.0
        closes[74:79] = [100.0] * 5
        closes[79] = 104.0
        special = {
            index: {"high": max(closes[index], closes[index - 1]) + 1.0,
                    "low": min(closes[index], closes[index - 1]) - 1.0}
            for index in range(59, 74)
        }
        special.update(
            {
                index: {"high": 100.1, "low": 99.9}
                for index in range(74, 79)
            }
        )
        rows = bars_from_closes(dates, closes, special=special)
        stock_a, benchmark_a = prepared_fixture(rows)
        detected_a = detect_consolidation_breakout_v2(
            stock_a, 79, benchmark_a, CFG
        )
        self.assertIsNotNone(detected_a)

        changed = list(rows)
        changed[80] = make_bar(
            dates[80], "1234", 160.0, open_=100.0, high=161.0, low=99.0
        )
        stock_b, benchmark_b = prepared_fixture(changed)
        detected_b = detect_consolidation_breakout_v2(
            stock_b, 79, benchmark_b, CFG
        )
        self.assertEqual(detected_a, detected_b)


class UnifiedOutcomeTests(unittest.TestCase):
    def test_all_forward_fields_use_t1_open_and_close_only(self):
        closes = [101.0, 108.0, 94.0, 102.0, 104.0, 105.0, 99.0, 106.0, 97.0, 103.0]
        stock, signal_index = outcome_fixture(closes)
        result = evaluate_unified_outcome(stock, signal_index, CFG)

        self.assertEqual(result["outcome_status"], "EVALUABLE")
        self.assertEqual(result["entry_open_proxy"], 100.0)
        self.assertAlmostEqual(result["day1_close_return"], 0.01)
        self.assertAlmostEqual(result["day3_close_return"], -0.06)
        self.assertAlmostEqual(result["day5_close_return"], 0.04)
        self.assertAlmostEqual(result["day10_close_return"], 0.03)
        self.assertAlmostEqual(result["mfe_5d"], 0.08)
        self.assertAlmostEqual(result["mae_5d"], -0.06)
        self.assertAlmostEqual(result["mfe_10d"], 0.08)
        self.assertAlmostEqual(result["mae_10d"], -0.06)
        self.assertAlmostEqual(result["mfe_abs_mae"], 0.08 / 0.06)
        self.assertIs(result["is_actual_order"], False)
        self.assertIs(result["is_actual_fill"], False)
        # The D1 intraday High is +30%, but neither MFE nor a descriptive
        # label is allowed to treat it as a Close-confirmed event.
        self.assertFalse(result["plus10_before_minus5"])
        self.assertFalse(result["plus15_before_minus5"])

    def test_primary_barrier_order_and_close_rule_return(self):
        cases = (
            (
                [101.0, 108.0, 94.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
                True,
                "TARGET_BEFORE_STOP",
                0.08,
            ),
            (
                [95.0, 110.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
                False,
                "STOP_BEFORE_TARGET",
                -0.05,
            ),
            (
                [101.0, 102.0, 99.0, 100.0, 103.0, 102.0, 101.0, 100.0, 99.0, 102.0],
                False,
                "DAY10_TIMEOUT",
                0.02,
            ),
        )
        for closes, expected_success, expected_path, expected_gross in cases:
            with self.subTest(path=expected_path):
                stock, signal_index = outcome_fixture(closes)
                result = evaluate_unified_outcome(stock, signal_index, CFG)
                self.assertIs(result["primary_success"], expected_success)
                self.assertEqual(result["path_result"], expected_path)
                self.assertAlmostEqual(result["gross_return"], expected_gross)

    def test_descriptive_barriers_do_not_replace_primary_label(self):
        stock, signal_index = outcome_fixture(
            [101.0, 110.0, 115.0, 114.0, 113.0, 112.0, 111.0, 110.0, 109.0, 108.0]
        )
        result = evaluate_unified_outcome(stock, signal_index, CFG)
        self.assertTrue(result["primary_success"])
        self.assertTrue(result["plus10_before_minus5"])
        self.assertTrue(result["plus15_before_minus5"])

    def test_incomplete_forward_window_censors_outcome(self):
        dates = trading_dates(78)
        rows = bars_from_closes(dates, [100.0] * len(dates))
        stock, _ = prepared_fixture(rows)
        result = evaluate_unified_outcome(stock, 70, CFG)
        self.assertEqual(result["outcome_status"], "CENSORED")
        self.assertIsNone(result["primary_success"])


class EntryGapTests(unittest.TestCase):
    def test_gap_bins_are_half_open_and_exhaustive_at_boundaries(self):
        below = entry_gap_bucket(-0.010001)
        minus_one = entry_gap_bucket(-0.01)
        zero = entry_gap_bucket(0.0)
        one = entry_gap_bucket(0.01)
        two = entry_gap_bucket(0.02)
        three = entry_gap_bucket(0.03)

        self.assertEqual(len({below, minus_one, zero, one, two, three}), 6)
        self.assertEqual(entry_gap_bucket(-0.005), minus_one)
        self.assertEqual(entry_gap_bucket(0.005), zero)
        self.assertEqual(entry_gap_bucket(0.015), one)
        self.assertEqual(entry_gap_bucket(0.025), two)
        self.assertEqual(entry_gap_bucket(0.08), three)


class AnalysisTests(unittest.TestCase):
    def test_metric_summary_keeps_signal_edge_separate_from_other_setups(self):
        base = [
            analysis_row("20230103", 0.10),
            analysis_row("20230104", -0.05, primary_success=False),
            analysis_row("20230105", 0.02, primary_success=False),
        ]
        summary = metric_summary(base)
        mixed = metric_summary(
            base + [analysis_row("20230103", -0.90, setup="V_REVERSAL")]
        )
        # metric_summary receives one setup at a time; adding a different setup
        # to the global study must not cause capital competition or reranking.
        selected = metric_summary(
            [row for row in base + [analysis_row("20230103", -0.90, setup="V_REVERSAL")]
             if row["setup"] == "TREND_PULLBACK"]
        )
        self.assertEqual(summary, selected)
        self.assertNotEqual(summary, mixed)
        self.assertAlmostEqual(
            summary["gross_average_return"], (0.10 - 0.05 + 0.02) / 3
        )
        self.assertLess(
            summary["net_average_return"], summary["gross_average_return"]
        )

    def test_winner_dependence_removes_percentages_of_winners_only(self):
        rows = [analysis_row("20230103", 1.0)]
        rows.extend(analysis_row("20230104", 0.01) for _ in range(99))
        rows.extend(
            analysis_row("20230105", -0.015, primary_success=False)
            for _ in range(100)
        )
        result = winner_dependence_rows(
            rows, "TREND_PULLBACK", "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS"
        )
        lookup = {row["tail_case"]: row for row in result}
        top1 = lookup["REMOVE_TOP_1PCT_WINNERS"]
        top5 = lookup["REMOVE_TOP_5PCT_WINNERS"]
        self.assertEqual(top1["removed_winners"], 1)
        self.assertEqual(top5["removed_winners"], 5)
        self.assertLess(top1["gross_profit_factor"], 1.0)
        self.assertTrue(top1["tail_dependent"])

    def test_signal_clustering_removes_the_whole_max_date(self):
        rows = [analysis_row("20230103", 0.10)]
        rows.extend(analysis_row("20230104", -0.10) for _ in range(2))
        rows.extend(analysis_row("20230105", 0.02) for _ in range(7))
        result = signal_clustering_rows(
            rows,
            "TREND_PULLBACK",
            "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS",
            ["20230103", "20230104", "20230105"],
        )[0]
        self.assertEqual(result["active_signal_days"], 3)
        self.assertEqual(result["maximum_daily_count"], 7)
        self.assertEqual(result["max_date"], "20230105")
        self.assertAlmostEqual(result["top5_dates_share"], 1.0)
        # After removing all seven records on the maximum date, PF is
        # 0.10 / abs(-0.20) = 0.5.
        self.assertAlmostEqual(
            result["remove_max_signal_date_gross_profit_factor"], 0.5
        )

    def test_bootstrap_uses_cluster_metadata_and_is_reproducible(self):
        rows = []
        rows.extend(analysis_row("20230103", 0.10) for _ in range(10))
        rows.extend(
            analysis_row("20230104", -0.10, primary_success=False)
            for _ in range(10)
        )
        rows.extend(analysis_row("20230201", 0.02) for _ in range(5))

        date_a = cluster_bootstrap(
            rows, "net_return", "signal_date", reps=40, seed=123
        )
        date_b = cluster_bootstrap(
            rows, "net_return", "signal_date", reps=40, seed=123
        )
        month = cluster_bootstrap(
            rows, "net_return", "month", reps=40, seed=123
        )
        self.assertEqual(date_a, date_b)
        self.assertEqual(date_a["cluster_unit"], "signal_date")
        self.assertEqual(month["cluster_unit"], "month")
        self.assertEqual(date_a["bootstrap_reps"], 40)
        self.assertEqual(month["bootstrap_reps"], 40)
        self.assertEqual(date_a["clusters"], 3)
        self.assertEqual(month["clusters"], 2)
        for result in (date_a, month):
            low, high = result["mean_ci_low"], result["mean_ci_high"]
            self.assertTrue(math.isfinite(low))
            self.assertTrue(math.isfinite(high))
            self.assertLessEqual(low, high)


if __name__ == "__main__":
    unittest.main()
