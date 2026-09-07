from __future__ import annotations

import ast
from dataclasses import replace
from datetime import date, timedelta
import math
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np


# ``stock-strategy`` deliberately is not a Python package (the directory name
# contains a dash).  Keep this file runnable directly and through discovery.
STOCK_STRATEGY_ROOT = Path(__file__).resolve().parents[2]
if str(STOCK_STRATEGY_ROOT) not in sys.path:
    sys.path.insert(0, str(STOCK_STRATEGY_ROOT))

from extension_entry_study_v01 import pipeline as extension_pipeline  # noqa: E402
from extension_entry_study_v01.analysis import (  # noqa: E402
    bucket_analysis_rows,
    cluster_bootstrap_rows,
    shape_diagnostic_rows,
    year_direction_consistency_rows,
)
from extension_entry_study_v01.config import (  # noqa: E402
    CFG,
    COHORTS,
    FEATURE_NAMES,
    PERIODS,
)
from extension_entry_study_v01.features import (  # noqa: E402
    assert_momentum_selection_parity,
    atr20_price,
    bucket_number,
    build_extension_features,
    frozen_quantile_boundaries,
    full_feature_vector,
    gap_bucket_number,
    momentum_scores,
    observed_entry_gap,
)
from extension_entry_study_v01.main import (  # noqa: E402
    TRACKED_ARTIFACTS,
    _assert_outputs_absent,
)
from extension_entry_study_v01.pipeline import (  # noqa: E402
    COHORT_BIT,
    ObservationStore,
    OUTCOME_FIELDS,
    _outcome_digest_values,
)
from multi_setup_study_v01.config import CFG as MULTI_CFG  # noqa: E402
from multi_setup_study_v01.main import _momentum_selected  # noqa: E402
from multi_setup_study_v01.outcomes import (  # noqa: E402
    cost_adjusted_return,
    evaluate_unified_outcome,
)
from multi_setup_study_v01.setup_detectors import (  # noqa: E402
    is_compact_retest as shared_is_compact_retest,
)
from surge_event_study_v01.data import prepare_stocks  # noqa: E402
from surge_event_study_v01.features import build_signal_observation  # noqa: E402
from surge_event_study_v01.models import Bar, SignalObservation  # noqa: E402
from v21.backtest import net_return as v21_net_return  # noqa: E402


FEATURE_INDEX = {name: index for index, name in enumerate(FEATURE_NAMES)}
OUTCOME_INDEX = {name: index for index, name in enumerate(OUTCOME_FIELDS)}


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
        max(open_value, close) + 1.0 if high is None else high,
        min(open_value, close) - 1.0 if low is None else low,
        close,
    )


def bars_from_closes(
    dates: list[str],
    closes: list[float],
    *,
    code: str = "1234",
    overrides: dict[int, dict] | None = None,
) -> list[Bar]:
    special = overrides or {}
    rows: list[Bar] = []
    for index, (day, close) in enumerate(zip(dates, closes)):
        values = dict(special.get(index, {}))
        values.setdefault("open_", closes[index - 1] if index else close)
        rows.append(make_bar(day, code, close, **values))
    return rows


def prepared_fixture(rows: list[Bar]):
    benchmark_rows = [make_bar(row.date, "0050", 100.0) for row in rows]
    stocks, benchmark, _ = prepare_stocks(
        {rows[0].code: rows}, benchmark_rows, CFG
    )
    if len(stocks) != 1:
        raise AssertionError("synthetic fixture did not produce one stock")
    return stocks[0], benchmark


def outcome_fixture() -> tuple[object, int]:
    dates = trading_dates(100)
    closes = [100.0] * len(dates)
    signal_index = 70
    forward = [101.0, 108.0, 94.0, 102.0, 104.0, 105.0, 99.0, 106.0, 97.0, 103.0]
    closes[signal_index + 1 : signal_index + 11] = forward
    rows = bars_from_closes(
        dates,
        closes,
        overrides={signal_index + 1: {"open_": 100.0, "high": 130.0, "low": 99.0}},
    )
    stock, _ = prepared_fixture(rows)
    return stock, signal_index


def synthetic_analysis_arrays() -> dict[str, np.ndarray]:
    """Every cluster contains every decile, keeping all L/M/H cells defined."""

    dates = (20200102, 20200103, 20200203, 20200204)
    rows = len(dates) * 10
    meta = np.zeros(
        rows,
        dtype=np.dtype(
            [
                ("signal_date", "i4"),
                ("stock_code", "i4"),
                ("cohort_mask", "u1"),
                ("momentum_strength_quintile", "u1"),
                ("entry_gap_bucket", "u1"),
                ("outcome_evaluable", "?"),
            ]
        ),
    )
    features = np.zeros((rows, len(FEATURE_NAMES)), dtype=np.float64)
    bins = np.zeros((rows, len(FEATURE_NAMES)), dtype=np.uint8)
    outcomes = np.zeros((rows, len(OUTCOME_FIELDS)), dtype=np.float64)
    cursor = 0
    for date_number, signal_date in enumerate(dates):
        for decile in range(1, 11):
            meta[cursor] = (
                signal_date,
                1000 + cursor,
                COHORT_BIT["ALL_ELIGIBLE"],
                (decile - 1) // 2 + 1,
                (decile - 1) % 6 + 1,
                True,
            )
            features[cursor, :] = decile + date_number / 10.0
            bins[cursor, :] = decile
            gross = (decile - 5.5) / 100.0 + (date_number - 1.5) / 1000.0
            mfe10 = max(gross, 0.01) + 0.005
            mae10 = min(gross, -0.01) - 0.005
            values = {
                "primary_success": float(gross >= 0.08),
                "day1_close_return": gross / 4.0,
                "day3_close_return": gross / 2.0,
                "day5_close_return": gross * 0.75,
                "day10_close_return": gross,
                "mfe_5d": mfe10 - 0.002,
                "mfe_10d": mfe10,
                "mae_5d": mae10 + 0.002,
                "mae_10d": mae10,
                "mfe_abs_mae": mfe10 / abs(mae10),
                "gross_return": gross,
                "net_return": gross - 0.005,
            }
            outcomes[cursor] = tuple(values[name] for name in OUTCOME_FIELDS)
            cursor += 1
    return {
        "meta": meta,
        "features": features,
        "feature_bins": bins,
        "outcomes": outcomes,
    }


class ConfigurationAndSafetyTests(unittest.TestCase):
    def test_fixed_research_contract_has_no_execution_path(self):
        self.assertEqual(CFG.execution_mode, "RESEARCH_ONLY_NO_BROKER_NO_ORDER")
        self.assertEqual(CFG.primary_target, 0.08)
        self.assertEqual(CFG.primary_stop, -0.05)
        self.assertEqual(CFG.primary_horizon, 10)
        self.assertGreaterEqual(CFG.bootstrap_iterations, 5_000)
        self.assertEqual(CFG.discovery_start, "20200101")
        self.assertEqual(CFG.discovery_end, "20221231")
        self.assertEqual(CFG.retrospective_start, "20230101")
        self.assertEqual(CFG.retrospective_end, "20241231")
        self.assertEqual(CFG.stress_start, "20250101")
        self.assertEqual(CFG.stress_end, "20251231")
        self.assertEqual(len(FEATURE_NAMES), 15)
        self.assertEqual(
            tuple(label for label, _, _ in PERIODS),
            (
                "HISTORICAL_DISCOVERY",
                "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS",
                "STRESS_PREVALENCE_SEEN_NOT_BLIND",
            ),
        )

        package = Path(__file__).resolve().parents[1]
        imported_modules: list[str] = []
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.extend(alias.name.lower() for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules.append(node.module.lower())
        forbidden = ("yuanta", "spark", "broker", "order_api")
        self.assertFalse(
            [name for name in imported_modules if any(word in name for word in forbidden)]
        )

    def test_output_guard_refuses_to_overwrite_any_study_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            _assert_outputs_absent(output)
            existing = output / TRACKED_ARTIFACTS[0]
            existing.write_text("immutable prior result\n", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, existing.name):
                _assert_outputs_absent(output)
            self.assertEqual(existing.read_text(encoding="utf-8"), "immutable prior result\n")


class CausalFeatureTests(unittest.TestCase):
    def test_t_features_are_unchanged_when_t_plus_1_and_later_are_mutated(self):
        dates = trading_dates(95)
        closes = [90.0 + index * 0.08 for index in range(len(dates))]
        signal_index = 70
        ordinary = bars_from_closes(
            dates,
            closes,
            overrides={signal_index + 1: {"open_": closes[signal_index] * 1.01}},
        )
        mutated_closes = list(closes)
        for index in range(signal_index + 1, len(mutated_closes)):
            mutated_closes[index] = closes[signal_index] * (1.08 if index % 2 else 0.92)
        mutated = bars_from_closes(
            dates,
            mutated_closes,
            overrides={signal_index + 1: {"open_": closes[signal_index] * 1.08}},
        )
        stock_a, _ = prepared_fixture(ordinary)
        stock_b, _ = prepared_fixture(mutated)

        self.assertEqual(
            build_extension_features(stock_a, signal_index),
            build_extension_features(stock_b, signal_index),
        )
        self.assertEqual(
            full_feature_vector(stock_a, signal_index)[:-1],
            full_feature_vector(stock_b, signal_index)[:-1],
        )
        # Entry gap is intentionally a separate T+1 execution diagnostic.
        self.assertNotEqual(
            observed_entry_gap(stock_a, signal_index),
            observed_entry_gap(stock_b, signal_index),
        )

    def test_exact_feature_math_and_bias20_shared_feature_parity(self):
        dates = trading_dates(100)
        closes = [80.0 + index * 0.2 for index in range(len(dates))]
        rows = [
            make_bar(
                day,
                "1234",
                close,
                open_=closes[index - 1] if index else close,
                high=close + 1.0,
                low=close - 1.0,
            )
            for index, (day, close) in enumerate(zip(dates, closes))
        ]
        stock, benchmark = prepared_fixture(rows)
        index = 70
        actual = build_extension_features(stock, index)

        def mean(window: int, at: int = index) -> float:
            return sum(closes[at - window + 1 : at + 1]) / window

        close = closes[index]
        manual_true_ranges = []
        for cursor in range(index - 19, index + 1):
            previous = closes[cursor - 1]
            manual_true_ranges.append(
                max(2.0, abs((closes[cursor] + 1.0) - previous), abs((closes[cursor] - 1.0) - previous))
            )
        manual_atr = sum(manual_true_ranges) / 20.0
        bias20 = close / mean(20) - 1.0
        bias10 = close / mean(10) - 1.0
        bias20_t3 = closes[index - 3] / mean(20, index - 3) - 1.0
        bias10_t3 = closes[index - 3] / mean(10, index - 3) - 1.0
        expected = {
            "bias_5": close / mean(5) - 1.0,
            "bias_10": bias10,
            "bias_20": bias20,
            "bias_60": close / mean(60) - 1.0,
            "atr_adjusted_bias20": (close - mean(20)) / manual_atr,
            "atr_adjusted_bias10": (close - mean(10)) / manual_atr,
            "return_3": close / closes[index - 3] - 1.0,
            "return_5": close / closes[index - 5] - 1.0,
            "return_10": close / closes[index - 10] - 1.0,
            "return_20": close / closes[index - 20] - 1.0,
            "bias20_change_3d": bias20 - bias20_t3,
            "bias10_change_3d": bias10 - bias10_t3,
            "distance_to_20d_high": 0.0,
            "distance_to_60d_high": 0.0,
        }
        self.assertEqual(set(actual), set(expected))
        for feature, value in expected.items():
            with self.subTest(feature=feature):
                self.assertAlmostEqual(actual[feature], value, places=14)
        self.assertAlmostEqual(atr20_price(stock, index), manual_atr, places=14)

        shared = build_signal_observation(stock, index, benchmark, CFG)
        self.assertIsNotNone(shared)
        self.assertAlmostEqual(
            actual["bias_20"], shared.features["close_vs_sma20"], places=14
        )


class FrozenBoundaryTests(unittest.TestCase):
    def test_discovery_quantiles_are_linear_and_boundary_equality_stays_lower(self):
        source = {
            feature: [float(value) for value in range(100)]
            for feature in FEATURE_NAMES
        }
        deciles = frozen_quantile_boundaries(source, 10)
        quintiles = frozen_quantile_boundaries(source, 5)
        for feature in FEATURE_NAMES:
            with self.subTest(feature=feature):
                self.assertEqual(len(deciles[feature]), 9)
                self.assertEqual(len(quintiles[feature]), 4)
                self.assertAlmostEqual(deciles[feature][0], 9.9)
                self.assertAlmostEqual(deciles[feature][-1], 89.1)
                self.assertEqual(quintiles[feature], deciles[feature][1::2])
                for edge_index, edge in enumerate(deciles[feature], start=1):
                    self.assertEqual(bucket_number(edge, deciles[feature]), edge_index)
                    self.assertEqual(
                        bucket_number(math.nextafter(edge, math.inf), deciles[feature]),
                        edge_index + 1,
                    )
        self.assertEqual(bucket_number(math.nan, deciles[FEATURE_NAMES[0]]), 0)

    def test_entry_gap_buckets_have_fixed_half_open_edges(self):
        values = (-0.010001, -0.01, 0.0, 0.01, 0.02, 0.03)
        self.assertEqual([gap_bucket_number(value) for value in values], [1, 2, 3, 4, 5, 6])
        self.assertEqual(gap_bucket_number(None), 0)
        self.assertEqual(gap_bucket_number(math.nan), 0)


class SharedContractParityTests(unittest.TestCase):
    def test_optional_mfe_mae_ratio_is_stored_as_nan_without_crashing(self):
        stock, signal_index = outcome_fixture()
        # All future closes equal the entry Open, so MAE=0 and the shared
        # outcome intentionally emits a missing MFE/abs(MAE) ratio.
        dates = trading_dates(100)
        rows = bars_from_closes(dates, [100.0] * len(dates))
        flat_stock, _ = prepared_fixture(rows)
        outcome = evaluate_unified_outcome(flat_stock, signal_index, CFG)
        self.assertIsNone(outcome["mfe_abs_mae"])
        store = ObservationStore(chunk_size=2)
        store.append(
            signal_date=flat_stock.bars[signal_index].date,
            stock_code=flat_stock.code,
            cohort_mask=COHORT_BIT["ALL_ELIGIBLE"],
            momentum_strength_quintile=3,
            entry_gap_bucket=3,
            feature_values=tuple(0.0 for _ in FEATURE_NAMES),
            outcome=outcome,
        )
        arrays = store.finalize()
        self.assertTrue(
            math.isnan(arrays["outcomes"][0, OUTCOME_INDEX["mfe_abs_mae"]])
        )
        self.assertIn("NA", _outcome_digest_values(outcome))

    def test_unified_outcome_and_cost_are_the_exact_shared_contract(self):
        self.assertIs(extension_pipeline.evaluate_unified_outcome, evaluate_unified_outcome)
        stock, signal_index = outcome_fixture()
        through_extension = extension_pipeline.evaluate_unified_outcome(stock, signal_index, CFG)
        through_shared = evaluate_unified_outcome(stock, signal_index, CFG)
        self.assertEqual(through_extension, through_shared)
        self.assertEqual(through_extension["outcome_status"], "EVALUABLE")
        self.assertAlmostEqual(through_extension["entry_gap"], observed_entry_gap(stock, signal_index))
        self.assertAlmostEqual(through_extension["gross_return"], 0.08)
        self.assertAlmostEqual(
            through_extension["net_return"],
            v21_net_return(100.0, 108.0, CFG.slippage_one_way),
        )
        self.assertAlmostEqual(
            cost_adjusted_return(100.0, 108.0, CFG),
            v21_net_return(100.0, 108.0, CFG.slippage_one_way),
        )
        self.assertIs(through_extension["is_actual_order"], False)
        self.assertIs(through_extension["is_actual_fill"], False)

    def test_momentum_mirror_matches_frozen_multi_setup_selection_including_ties(self):
        contexts = []
        feature_names = tuple(MULTI_CFG.momentum_score_features)
        for index in range(37):
            feature_values = {
                feature_names[0]: float(index % 5),
                feature_names[1]: float((index * 7) % 11),
                feature_names[2]: float((index * 3) % 13),
                feature_names[3]: float((index * 5) % 7),
            }
            observation = SignalObservation(
                signal_date="20200102",
                calendar_index=1,
                code=str(1000 + index),
                name=f"測試{index}",
                signal_close=100.0,
                average_volume_20=1_000_000.0,
                average_turnover_proxy_20=100_000_000.0,
                features=feature_values,
            )
            contexts.append((observation, None, index))

        scores, strength, top_indices = momentum_scores(contexts)
        assert_momentum_selection_parity(contexts, top_indices)
        expected_codes = [item["observation"].code for item in _momentum_selected(contexts)]
        actual_codes = [contexts[index][0].code for index in top_indices]
        self.assertEqual(actual_codes, expected_codes)
        self.assertEqual(len(top_indices), CFG.momentum_daily_selection_count)
        self.assertEqual(len(scores), len(contexts))
        self.assertEqual(len(strength), len(contexts))
        self.assertTrue(all(0.0 < value < 1.0 for value in strength))

    def test_n_compact_annotation_is_exactly_the_shared_predicate(self):
        self.assertIs(extension_pipeline.is_compact_retest, shared_is_compact_retest)
        cases = (
            ({"pivot_separation_sessions": 7, "bottom_difference": 1e-12}, True),
            ({"pivot_separation_sessions": 8, "bottom_difference": 1e-12}, False),
            ({"pivot_separation_sessions": 7, "bottom_difference": 0.0}, False),
            ({"pivot_separation_sessions": 6, "bottom_difference": -1e-12}, False),
        )
        for geometry, expected in cases:
            with self.subTest(geometry=geometry):
                self.assertIs(shared_is_compact_retest(geometry, MULTI_CFG), expected)


class ClusterBootstrapTests(unittest.TestCase):
    def test_cluster_bootstrap_is_deterministic_and_never_trade_iid(self):
        arrays = synthetic_analysis_arrays()
        fixed = replace(CFG, bootstrap_iterations=5_000, bootstrap_seed=12345)
        first = cluster_bootstrap_rows(arrays, fixed)
        second = cluster_bootstrap_rows(arrays, fixed)

        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertEqual({row["bootstrap_reps"] for row in first}, {5_000})
        self.assertEqual(
            {row["cluster_unit"] for row in first},
            {"signal_date", "calendar_month"},
        )
        self.assertEqual(
            {row["clusters"] for row in first if row["cluster_unit"] == "signal_date"},
            {4},
        )
        self.assertEqual(
            {row["clusters"] for row in first if row["cluster_unit"] == "calendar_month"},
            {2},
        )
        for row in first:
            self.assertIn(row["cohort"], COHORTS)
            # PF is correctly undefined when a region has no sampled losses;
            # mean/success/MFE/MAE intervals must always remain finite here.
            if row["metric"] != "gross_profit_factor":
                self.assertIsNotNone(row["ci_low"])
                self.assertIsNotNone(row["ci_high"])
                self.assertLessEqual(row["ci_low"], row["ci_high"])

    def test_sparse_frozen_regions_fail_closed_instead_of_crashing(self):
        arrays = synthetic_analysis_arrays()
        # Leave only one low-decile row in the sparse N Compact cohort and no
        # rows at all in its later periods/high regions.
        arrays["meta"]["cohort_mask"] = COHORT_BIT["ALL_ELIGIBLE"]
        arrays["meta"][0]["cohort_mask"] |= COHORT_BIT[
            "N_COMPACT_RETEST_HYPOTHESIS"
        ] | COHORT_BIT["N_RETEST"]
        edges10 = {feature: tuple(float(value) for value in range(1, 10)) for feature in FEATURE_NAMES}
        edges5 = {feature: tuple(float(value) for value in (2, 4, 6, 8)) for feature in FEATURE_NAMES}
        buckets = bucket_analysis_rows(arrays, edges10, edges5)
        bootstrap = cluster_bootstrap_rows(arrays, CFG)
        shapes = shape_diagnostic_rows(arrays, buckets, bootstrap, CFG)
        years = year_direction_consistency_rows(arrays)
        compact_shapes = [
            row for row in shapes if row["cohort"] == "N_COMPACT_RETEST_HYPOTHESIS"
        ]
        compact_years = [
            row for row in years if row["cohort"] == "N_COMPACT_RETEST_HYPOTHESIS"
        ]
        self.assertTrue(compact_shapes)
        self.assertTrue(compact_years)
        self.assertTrue(
            all(row["discovery_shape"] == "NO_STABLE_RELATION" for row in compact_shapes)
        )
        self.assertTrue(
            all(not row["gross_direction_consistent_2020_2025"] for row in compact_years)
        )


if __name__ == "__main__":
    unittest.main()
