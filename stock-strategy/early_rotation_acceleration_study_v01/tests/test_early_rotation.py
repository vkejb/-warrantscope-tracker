from __future__ import annotations

import json
from pathlib import Path
import numpy as np

from early_rotation_acceleration_study_v01.analysis import _bucket, _empirical_percentile
from early_rotation_acceleration_study_v01.config import CFG, FEATURES


def test_frozen_tercile_and_quintile_bucketing():
    assert _bucket(np.asarray([-.1,.2,.5,.9,np.nan]),[.2,.5]).tolist()==[1,2,3,3,0]


def test_empirical_percentile_uses_discovery_reference_only():
    actual=_empirical_percentile(np.asarray([0.,2.,5.]),np.asarray([0.,1.,2.,3.]))
    assert np.allclose(actual,[.25,.75,1.])


def test_preregistered_feature_set_is_fixed():
    assert len(FEATURES)==10
    assert CFG.peer_count==10 and CFG.correlation_window==60 and CFG.minimum_common_sessions==40


def test_safety_and_no_refit_contract():
    assert CFG.actual_orders==CFG.actual_fills==CFG.broker_connections==0


def test_published_validation_if_present():
    path=Path(__file__).resolve().parents[1]/"validation_summary.json"
    if not path.exists():return
    payload=json.loads(path.read_text())
    assert payload["stage_a_refit_count"]==0
    assert payload["later_period_refit_count"]==0
    assert payload["dynamic_peer_refit_count"]==0
    assert payload["checks"]["all_primary_features_t_or_earlier"] is True
    assert payload["checks"]["future_data_only_in_lead_time_diagnostic"] is True
    assert payload["checks"]["leave_one_out"] is True
    assert payload["checks"]["actual_orders"]==payload["checks"]["actual_fills"]==payload["checks"]["broker_connections"]==0
