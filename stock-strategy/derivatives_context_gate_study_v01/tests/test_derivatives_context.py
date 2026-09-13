from __future__ import annotations

import unittest
from pathlib import Path
import hashlib
import numpy as np

from derivatives_context_gate_study_v01.analysis import bucket, score_by_date
from derivatives_context_gate_study_v01.config import CFG


class TestDerivativesContext(unittest.TestCase):
    def test_bucket_boundaries_are_frozen_and_ordered(self):
        cuts=[1,2,3,4,5]
        self.assertEqual([bucket(x,cuts) for x in (0,1,2.5,5,6)], [1,2,3,6,6])


    def test_context_score_uses_only_same_date_context(self):
        context={20200102:{"x":1.0},20200103:{"x":-1.0}}
        defs=[{"feature":"x","median":0.0,"adverse_side":"HIGH"}]
        self.assertEqual(score_by_date(context,defs), {20200102:1,20200103:0})


    def test_safety_and_frozen_contract(self):
        self.assertEqual(CFG.primary_population, "FROZEN_STAGE_A_TOP30")
        self.assertEqual(CFG.actual_orders, 0)
        self.assertEqual(CFG.actual_fills, 0)
        self.assertEqual(CFG.broker_connections, 0)

    def test_frozen_stage_a_exact_reuse(self):
        root=Path(__file__).resolve().parents[2]
        up=root/"upside_opportunity_ranking_v01/runtime/ranking_store.npz"
        conditional=root/"conditional_path_quality_ranking_v01/runtime/conditional_store.npz"
        self.assertEqual(hashlib.sha256(up.read_bytes()).hexdigest(),CFG.expected_stage_a_store_sha256)
        self.assertEqual(hashlib.sha256(conditional.read_bytes()).hexdigest(),CFG.expected_conditional_store_sha256)
        a=np.load(up,allow_pickle=False); b=np.load(conditional,allow_pickle=False)
        self.assertTrue(np.array_equal(a["meta"],b["meta"]))
        self.assertTrue(np.array_equal(a["stage_a_ranks"],b["stage_a_ranks"]))
        self.assertTrue(np.array_equal(b["stage_a_pool"],b["stage_a_ranks"]<=30))


if __name__ == "__main__":
    unittest.main()
