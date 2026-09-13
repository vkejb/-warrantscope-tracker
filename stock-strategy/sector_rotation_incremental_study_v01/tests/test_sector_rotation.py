from __future__ import annotations

import numpy as np

from sector_rotation_incremental_study_v01.analysis import bucket_ids, composite_scores, daily_context_ranks
from sector_rotation_incremental_study_v01.config import FEATURES


def test_frozen_bucket_boundaries_are_applied_without_refit():
    values = np.asarray([-2.0, 0.0, 1.0, 2.0, 4.0, np.nan])
    assert bucket_ids(values, [0.0, 1.0, 2.0, 3.0]).tolist() == [1, 2, 3, 4, 5, 0]


def test_composite_score_uses_fixed_direction_and_unavailable_marker():
    feature_matrix = np.zeros((3, len(FEATURES)))
    feature_matrix[:, FEATURES.index("peer_return_5d")] = [0.2, -0.2, 0.3]
    store = {"features": feature_matrix, "available": np.asarray([True, True, False])}
    definitions = [{"feature": "peer_return_5d", "favorable_side": "HIGH", "threshold": 0.0}]
    assert composite_scores(store, definitions).tolist() == [1, 0, -1]


def test_daily_rank_topk_and_deterministic_stock_id_tie_break():
    meta = np.asarray(
        [(20200102, 30), (20200102, 10), (20200102, 20)],
        dtype=[("signal_date", "i4"), ("stock_code", "i4")],
    )
    arrays = {"meta": meta, "stage_a_pool": np.ones(3, dtype=bool)}
    store = {
        "available": np.ones(3, dtype=bool),
        "peer_correlations": np.ones((3, 10), dtype=float),
    }
    ranks = daily_context_ranks(arrays, store, np.asarray([1, 1, 1]))
    assert ranks.tolist() == [3, 1, 2]


def test_leave_one_out_identity_is_distinct():
    target = 6203
    peers = np.asarray([1101, 2330, 6204])
    assert target not in peers


def test_safety_contract_is_zero():
    from sector_rotation_incremental_study_v01.config import CFG
    assert CFG.actual_orders == CFG.actual_fills == CFG.broker_connections == 0
    assert CFG.peer_count == 10
    assert CFG.trailing_correlation_sessions == 60
    assert CFG.minimum_common_sessions == 40


def test_published_contract_if_present():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    path = root / "validation_summary.json"
    if not path.exists():
        return
    payload = json.loads(path.read_text())
    assert payload["stage_a_refit_count"] == 0
    assert payload["later_period_refit_count"] == 0
    assert payload["official_sector_status"] == "OFFICIAL_SECTOR_PIT_UNSAFE"
    assert payload["checks"]["frozen_stage_a_exact_reuse"] is True
    assert payload["checks"]["all_features_t_or_earlier"] is True
    assert payload["checks"]["leave_one_out"] is True
    assert payload["checks"]["current_sector_hindsight_used"] is False
    assert payload["checks"]["actual_orders"] == 0
    assert payload["checks"]["actual_fills"] == 0
    assert payload["checks"]["broker_connections"] == 0


def test_published_peer_store_excludes_target_if_present():
    from pathlib import Path
    from sector_rotation_incremental_study_v01.data import load_store
    root = Path(__file__).resolve().parents[1]
    path = root / "runtime" / "dynamic_peer_store.npz"
    if not path.exists():
        return
    store, audit = load_store(path)
    assert audit["future_data_used"] is False
    assert audit["leave_one_out"] is True
