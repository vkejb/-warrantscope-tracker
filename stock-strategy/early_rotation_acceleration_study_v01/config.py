from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


FEATURE_FAMILIES = (
    ("peer_rs_3d_vs_0050", "RS"),
    ("peer_rs_20d_vs_0050", "RS"),
    ("peer_rs_acceleration_3v20", "RS"),
    ("peer_turnover_share_change_1d", "TURNOVER"),
    ("peer_turnover_share_change_3d", "TURNOVER"),
    ("peer_turnover_acceleration", "TURNOVER"),
    ("peer_breadth_up_change_3d", "BREADTH"),
    ("peer_breadth_ma20_change_3d", "BREADTH"),
    ("peer_breadth_newhigh_change_3d", "BREADTH"),
    ("peer_volume_expansion_change_3d", "VOLUME"),
)
FEATURES = tuple(name for name, _ in FEATURE_FAMILIES)
COMPOSITE_CANDIDATES = (
    "peer_rs_acceleration_3v20",
    "peer_turnover_acceleration",
    "peer_breadth_up_change_3d",
    "peer_breadth_ma20_change_3d",
    "peer_breadth_newhigh_change_3d",
    "peer_volume_expansion_change_3d",
)
LEVEL_FEATURES = (
    "peer_rs_20d_vs_0050",
    "peer_breadth_ma20_rising",
    "peer_turnover_market_share",
    "peer_turnover_share_vs_20d",
)
PERIODS = (
    ("HISTORICAL_DISCOVERY", 20200101, 20221231),
    ("RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", 20230101, 20241231),
    ("STRESS_PREVALENCE_SEEN_NOT_BLIND", 20250101, 20251231),
)


@dataclass(frozen=True)
class Config:
    study_id: str = "EARLY_ROTATION_ACCELERATION_STUDY_V0_1"
    execution_mode: str = "RESEARCH_ONLY_NO_EXECUTION"
    discovery_start: int = 20200101
    discovery_end: int = 20221231
    percentile_boundaries: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8)
    level_acceleration_boundaries: tuple[float, ...] = (1 / 3, 2 / 3)
    peer_count: int = 10
    correlation_window: int = 60
    minimum_common_sessions: int = 40
    minimum_composite_families: int = 2
    composite_high_quantile: float = 0.6
    pool_sizes: tuple[int, ...] = (15, 10, 5)
    lead_time_sessions: int = 10
    bootstrap_reps: int = 5_000
    bootstrap_seed: int = 20_260_914
    expected_stage_a_store_sha256: str = "36c1071c2dcdabdbcb8a4a6d7126d819d9b545075473a5639766e0d234a6c0d9"
    expected_conditional_store_sha256: str = "efefade875e8fc1f2566e2d5de0e8dcb6cfd267e2ece80304d166ff642ef918c"
    expected_peer_store_sha256: str = "83b40ae5e339cb9229523d188559c8ce6f280bc6ea87250921ee948e07ca4b7d"
    expected_peer_content_digest: str = "85c16a99df334abe4bba7b2d91f2a32b5524f7162225d7d45383913dddd79919"
    expected_sector_commit: str = "065bece347139e610d9f1d1f5913a3a89fa74322"
    actual_orders: int = 0
    actual_fills: int = 0
    broker_connections: int = 0

    @property
    def family_by_feature(self):
        return dict(FEATURE_FAMILIES)

    def snapshot(self):
        payload = asdict(self)
        payload["feature_families"] = [
            {"feature": feature, "family": family}
            for feature, family in FEATURE_FAMILIES
        ]
        payload["periods"] = [
            {"label": label, "start": start, "end": end}
            for label, start, end in PERIODS
        ]
        payload["turnover_acceleration_definition"] = (
            "log(share_T/share_T-3)/3 - log(share_T-3/share_T-20)/17"
        )
        payload["rs_acceleration_definition"] = (
            "same-day Stage-A Top30 percentile(peer_rs_3d) minus percentile(peer_rs_20d)"
        )
        return payload

    def fingerprint(self):
        raw = json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()


CFG = Config()
