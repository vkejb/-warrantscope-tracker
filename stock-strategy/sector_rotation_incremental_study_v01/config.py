from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


FEATURE_FAMILIES: tuple[tuple[str, str], ...] = (
    ("peer_return_1d", "RETURN_RS"),
    ("peer_return_3d", "RETURN_RS"),
    ("peer_return_5d", "RETURN_RS"),
    ("peer_return_20d", "RETURN_RS"),
    ("peer_rs_3d_vs_0050", "RETURN_RS"),
    ("peer_rs_5d_vs_0050", "RETURN_RS"),
    ("peer_rs_20d_vs_0050", "RETURN_RS"),
    ("peer_breadth_up", "BREADTH"),
    ("peer_breadth_above_ma20", "BREADTH"),
    ("peer_breadth_ma20_rising", "BREADTH"),
    ("peer_breadth_20d_close_high", "BREADTH"),
    ("peer_turnover_market_share", "ACTIVITY_VOLUME"),
    ("peer_turnover_share_vs_20d", "ACTIVITY_VOLUME"),
    ("peer_turnover_share_change_5d", "ACTIVITY_VOLUME"),
    ("peer_turnover_share_change_20d", "ACTIVITY_VOLUME"),
    ("peer_median_volume_ratio5", "ACTIVITY_VOLUME"),
    ("peer_median_volume_ratio20", "ACTIVITY_VOLUME"),
)

FEATURES = tuple(name for name, _ in FEATURE_FAMILIES)
PERIODS = (
    ("HISTORICAL_DISCOVERY", 20200101, 20221231),
    ("RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", 20230101, 20241231),
    ("STRESS_PREVALENCE_SEEN_NOT_BLIND", 20250101, 20251231),
)


@dataclass(frozen=True)
class Config:
    study_id: str = "SECTOR_ROTATION_INCREMENTAL_STUDY_V0_1"
    execution_mode: str = "RESEARCH_ONLY_NO_EXECUTION"
    primary_population: str = "FROZEN_STAGE_A_TOP30"
    trailing_correlation_sessions: int = 60
    minimum_common_sessions: int = 40
    peer_count: int = 10
    percentile_boundaries: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8)
    pool_sizes: tuple[int, ...] = (15, 10, 5)
    composite_condition_quantile: float = 0.6
    minimum_composite_families: int = 2
    bootstrap_reps: int = 5_000
    bootstrap_seed: int = 20_260_913
    expected_stage_a_store_sha256: str = "36c1071c2dcdabdbcb8a4a6d7126d819d9b545075473a5639766e0d234a6c0d9"
    expected_conditional_store_sha256: str = "efefade875e8fc1f2566e2d5de0e8dcb6cfd267e2ece80304d166ff642ef918c"
    actual_orders: int = 0
    actual_fills: int = 0
    broker_connections: int = 0

    @property
    def family_by_feature(self) -> dict[str, str]:
        return dict(FEATURE_FAMILIES)

    def snapshot(self) -> dict:
        payload = asdict(self)
        payload["feature_families"] = [
            {"feature": feature, "family": family}
            for feature, family in FEATURE_FAMILIES
        ]
        payload["periods"] = [
            {"label": label, "start": start, "end": end}
            for label, start, end in PERIODS
        ]
        return payload

    def fingerprint(self) -> str:
        raw = json.dumps(
            self.snapshot(), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(raw).hexdigest()


CFG = Config()
