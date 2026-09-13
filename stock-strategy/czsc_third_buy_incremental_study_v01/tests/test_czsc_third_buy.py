from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

import numpy as np

from czsc_third_buy_incremental_study_v01.analysis import cohort_masks, metrics
from czsc_third_buy_incremental_study_v01.config import CFG
from czsc_third_buy_incremental_study_v01.data import THIRD_BUY_VALUE, load_frozen_arrays, scan_third_buy
from extension_entry_study_v01.pipeline import OUTCOME_FIELDS


class TestCzscThirdBuy(unittest.TestCase):
    def test_exact_signal_value_all_bi_counts(self):
        for count in (6, 8, 10, 12, 14):
            self.assertEqual(int(THIRD_BUY_VALUE.fullmatch(f"三买_{count}笔_任意_0").group(1)), count)
        self.assertIsNone(THIRD_BUY_VALUE.fullmatch("其他_其他_任意_0"))

    def test_cohort_partition_and_stage_a_unchanged(self):
        arrays = {"stage_a_pool": np.asarray([1, 1, 0, 1], dtype=bool)}
        czsc = np.asarray([1, 0, 1, 1], dtype=bool)
        cohorts = cohort_masks(arrays, czsc)
        self.assertTrue(np.array_equal(cohorts["STAGE_A_AND_CZSC"], [1, 0, 0, 1]))
        self.assertTrue(np.array_equal(cohorts["STAGE_A_WITHOUT_CZSC"], [0, 1, 0, 0]))
        self.assertTrue(np.array_equal(cohorts["STAGE_A_AND_CZSC"] | cohorts["STAGE_A_WITHOUT_CZSC"], arrays["stage_a_pool"]))

    def test_t_plus_1_common_outcome_fields_reused(self):
        self.assertIn("primary_success", OUTCOME_FIELDS)
        self.assertIn("day5_close_return", OUTCOME_FIELDS)
        self.assertIn("day10_close_return", OUTCOME_FIELDS)
        self.assertIn("net_return", OUTCOME_FIELDS)

    def test_metric_definition(self):
        dtype = [("signal_date", "i4"), ("stock_code", "i4"), ("outcome_evaluable", "?")]
        meta = np.array([(20200102, 1101, True), (20200102, 1102, True)], dtype=dtype)
        out = np.zeros((2, len(OUTCOME_FIELDS)))
        oi = {name: i for i, name in enumerate(OUTCOME_FIELDS)}
        out[:, oi["day5_close_return"]] = [0.01, -0.01]
        out[:, oi["day10_close_return"]] = [0.02, 0.01]
        out[:, oi["mfe_5d"]] = [.03, .04]; out[:, oi["mfe_10d"]] = [.08, .02]
        out[:, oi["mae_5d"]] = [-.01, -.03]; out[:, oi["mae_10d"]] = [-.02, -.05]
        out[:, oi["gross_return"]] = [.08, -.05]; out[:, oi["net_return"]] = [.07, -.06]
        result = metrics({"meta": meta, "outcomes": out, "path_class": np.array([1, 2])}, np.ones(2, bool))
        self.assertEqual(result["success_rate"], .5)
        self.assertEqual(result["day5_positive_rate"], .5)
        self.assertEqual(result["day10_positive_rate"], 1.0)

    def test_frozen_safety_contract(self):
        self.assertEqual(CFG.stage_a_top_k, 30)
        self.assertEqual(CFG.actual_orders, 0)
        self.assertEqual(CFG.actual_fills, 0)
        self.assertEqual(CFG.broker_connections, 0)
        self.assertEqual(CFG.upstream_version, "0.9.27")
        self.assertEqual(CFG.signal_function, "cxt_third_buy_V230228")

    def test_frozen_stage_a_exact_top30_reuse(self):
        root = Path(__file__).resolve().parents[2]
        arrays, audit = load_frozen_arrays(
            root / "upside_opportunity_ranking_v01/runtime/ranking_store.npz",
            root / "conditional_path_quality_ranking_v01/runtime/conditional_store.npz",
        )
        self.assertEqual(audit["stage_a_refit_count"], 0)
        self.assertEqual(audit["later_period_refit_count"], 0)
        self.assertTrue(np.array_equal(arrays["stage_a_pool"], arrays["stage_a_ranks"] <= 30))

    def test_scanner_uses_current_prefix_and_resets_segments(self):
        class Bar:
            def __init__(self, date, close):
                self.date = date; self.open = close; self.high = close
                self.low = close; self.close = close; self.volume = 100

        class Stock:
            code = "1101"; name = "測試"
            bars = [Bar("20200102", 10), Bar("20200103", 11), Bar("20200106", 99)]
            segment_ids = [0, 0, 1]

        class Raw:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)

        class Analyzer:
            def __init__(self, bars): self.bars = list(bars)
            def update(self, bar): self.bars.append(bar)

        def signal(c, di):
            # Day two succeeds only if exactly the T-prefix is visible. The future
            # segment must not be visible and must start a new analyzer.
            value = "三买_6笔_任意_0" if len(c.bars) == 2 and c.bars[-1].close == 11 else "其他_其他_任意_0"
            return {"日线_D1_三买辅助V230228": value}

        dtype = [("signal_date", "i4"), ("stock_code", "i4")]
        meta = np.array([(20200102, 1101), (20200103, 1101), (20200106, 1101)], dtype=dtype)
        upstream = {"CZSC": Analyzer, "RawBar": Raw, "Freq": type("Freq", (), {"D": "日线"}), "signal": signal}
        with tempfile.TemporaryDirectory() as temp:
            mask, rows, audit = scan_third_buy([Stock()], meta, upstream, Path(temp) / "checkpoint.jsonl", "test")
        self.assertTrue(np.array_equal(mask, [False, True, False]))
        self.assertEqual(rows[0]["signal_date"], 20200103)
        self.assertEqual(audit["segment_resets"], 2)
        self.assertFalse(audit["future_bars_used_for_signal"])


if __name__ == "__main__":
    unittest.main()
