from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

import numpy as np


STOCK_STRATEGY = Path(__file__).resolve().parents[2]
if str(STOCK_STRATEGY) not in sys.path:
    sys.path.insert(0, str(STOCK_STRATEGY))

from chip_incremental_study_v01.config import CFG, CHIP_FEATURES  # noqa: E402
from chip_incremental_study_v01.analysis import incremental_rows  # noqa: E402
from chip_incremental_study_v01.data import load_frozen_research, protected_hashes  # noqa: E402
from chip_incremental_study_v01.models import ALL_MODEL_FEATURES, fit_chip_logistic  # noqa: E402
from chip_incremental_study_v01.pit import build_chip_features, needed_codes, prior_session_map  # noqa: E402
from chip_incremental_study_v01.sources import phase0_audit_rows  # noqa: E402


def meta_fixture(days: int = 8):
    dtype = np.dtype([
        ("signal_date", "<i4"), ("stock_code", "<i4"),
        ("cohort_mask", "u1"), ("momentum_strength_quintile", "u1"),
        ("entry_gap_bucket", "u1"), ("outcome_evaluable", "?"),
    ])
    dates = np.asarray([20200102 + index for index in range(days)], dtype=np.int32)
    rows = [(int(date), 2330, 0, 0, 0, True) for date in dates]
    return np.asarray(rows, dtype=dtype)


class TestChipIncrementalStudy(unittest.TestCase):
    def test_phase0_decisions_are_closed_set(self):
        rows = phase0_audit_rows()
        allowed = {"PIT_USABLE", "PIT_USABLE_WITH_LAG", "NOT_TESTED_DATA_UNAVAILABLE", "REJECTED_PIT_UNSAFE"}
        self.assertTrue(rows)
        self.assertTrue(all(row["decision"] in allowed for row in rows))
        self.assertTrue(all(row["required_lag_sessions"] == 1 for row in rows if row["decision"] == "PIT_USABLE_WITH_LAG"))
        required = {"feature_family", "raw_field", "source", "publication_timing", "PIT_status", "decision"}
        self.assertTrue(all(required.issubset(row) for row in rows))

    def test_no_snapshot_backfill_and_tdcc_rejected(self):
        tdcc = next(row for row in phase0_audit_rows() if row["feature_family"] == "TDCC_OWNERSHIP")
        self.assertEqual(tdcc["decision"], "REJECTED_PIT_UNSAFE")
        self.assertFalse(tdcc["usable_on_same_day_T"])

    def test_prior_session_lag_correctness(self):
        dates = np.asarray([20200102, 20200103, 20200106], dtype=np.int32)
        self.assertEqual(prior_session_map(dates, 1)[20200106], 20200103)
        self.assertEqual(prior_session_map(dates, 2)[20200106], 20200102)

    def test_needed_codes_never_uses_signal_date(self):
        meta = meta_fixture()
        pool = np.zeros(len(meta), dtype=bool)
        pool[-1] = True
        needed = needed_codes(meta["signal_date"], meta["stock_code"], pool, 1)
        self.assertTrue(all(date < int(meta["signal_date"][-1]) for date in needed))

    def test_pit_feature_timestamp_and_margin_missingness(self):
        meta = meta_fixture()
        pool = np.ones(len(meta), dtype=bool)
        dtype = np.dtype([
            ("source_date", "<i4"), ("stock_code", "<i4"),
            ("foreign", "<f8"), ("investment_trust", "<f8"), ("dealer", "<f8"),
            ("margin_balance", "<f8"), ("short_balance", "<f8"),
            ("institutional_market", "u1"), ("margin_market", "u1"),
        ])
        chip = np.asarray([
            (int(date), 2330, index + 1, index + 2, index + 3, np.nan, np.nan, 1, 0)
            for index, date in enumerate(meta["signal_date"])
        ], dtype=dtype)
        volumes = {(int(date), 2330): 1000.0 for date in meta["signal_date"]}
        features, audit = build_chip_features(meta, pool, chip, volumes, 1)
        valid = features["chip_valid"]
        self.assertTrue(np.all(features["chip_source_date"][valid] < meta["signal_date"][valid]))
        self.assertTrue(np.all(features["raw_chip_features"][valid, -1] == 0.0))
        self.assertEqual(audit["future_or_same_date_source_rows"], 0)

    def test_regularization_is_preregistered(self):
        x = np.linspace(-1, 1, 240).reshape(40, 6)
        y = (np.arange(40) % 2).astype(float)
        mask = np.ones(40, dtype=bool)
        model = fit_chip_logistic(x, y, mask, 1.0, "TEST", tuple(f"f{i}" for i in range(6)))
        self.assertEqual(model.regularization_c, 1.0)
        with self.assertRaises(ValueError):
            fit_chip_logistic(x, y, mask, 0.5, "TEST", tuple(f"f{i}" for i in range(6)))

    def test_feature_width_contract(self):
        self.assertEqual(len(ALL_MODEL_FEATURES), 41 + len(CHIP_FEATURES))

    def test_frozen_stage_a_and_ohlcv_exact_reuse(self):
        arrays, model, audit = load_frozen_research(STOCK_STRATEGY)
        self.assertEqual(audit["stage_a_refit_count"], 0)
        self.assertEqual(audit["frozen_ohlcv_refit_count"], 0)
        self.assertEqual(len(arrays["meta"]), 704327)
        self.assertEqual(model.name, "LOGISTIC_RIDGE_PATH_SUCCESS")

    def test_no_prospective_observations(self):
        arrays, _model, _audit = load_frozen_research(STOCK_STRATEGY)
        self.assertEqual(int(np.count_nonzero(arrays["meta"]["signal_date"] >= 20260907)), 0)

    def test_n_compact_and_prospective_ledger_unchanged(self):
        before = protected_hashes(STOCK_STRATEGY)
        arrays, _model, _audit = load_frozen_research(STOCK_STRATEGY)
        after = protected_hashes(STOCK_STRATEGY)
        self.assertEqual(before, after)
        self.assertEqual(int(np.count_nonzero(arrays["n_compact"])), 236)

    def test_incremental_metric_and_mfe_retention(self):
        base = {
            "time_slice_type": "PERIOD", "time_slice": "X", "cohort": "OHLCV_TOP5_COMMON_DATES",
            "path_success_rate": .30, "downside_first_rate": .40, "mae10_mean": -.07,
            "mfe10_mean": .08, "net_mean": -.01, "net_profit_factor": .8,
            "auc_on_common_stage_a_pool": .52, "mean_daily_path_ic": .01,
        }
        chip = {**base, "cohort": "CHIP_TOP5", "path_success_rate": .35, "downside_first_rate": .35,
                "mae10_mean": -.06, "mfe10_mean": .09, "net_mean": .01, "net_profit_factor": 1.1,
                "auc_on_common_stage_a_pool": .55, "mean_daily_path_ic": .03}
        stage = {**base, "cohort": "COMMON_STAGE_A_TOP30", "mfe10_mean": .10}
        row = incremental_rows([base, chip, stage])[0]
        self.assertAlmostEqual(row["delta_success_rate"], .05)
        self.assertAlmostEqual(row["mfe_retention_vs_stage_a"], .9)
        self.assertAlmostEqual(row["mae_improvement_vs_stage_a"], .01)

    def test_module_has_no_broker_or_order_path(self):
        root = STOCK_STRATEGY / "chip_incremental_study_v01"
        text = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
        self.assertNotIn("Yuanta", text)
        self.assertNotIn("place_order", text)
        self.assertNotIn("submit_order", text)

    def test_published_safety_counters_when_available(self):
        path = STOCK_STRATEGY / "chip_incremental_study_v01/run_manifest.json"
        if not path.exists():
            self.skipTest("formal publish not run yet")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["actual_orders"], 0)
        self.assertEqual(payload["actual_fills"], 0)
        self.assertEqual(payload["broker_connections"], 0)
        self.assertEqual(payload["pipeline_validation"]["later_period_refit_count"], 0)


if __name__ == "__main__":
    unittest.main()
