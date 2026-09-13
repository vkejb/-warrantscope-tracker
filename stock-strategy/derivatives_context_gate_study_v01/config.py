from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


FEATURES = (
    "tx_total_oi",
    "tx_total_oi_change_1",
    "tx_total_oi_change_5",
    "txo_volume_pc_ratio",
    "txo_oi_pc_ratio",
    "txo_volume_pc_change_1",
    "txo_volume_pc_change_5",
    "txo_oi_pc_change_1",
    "txo_oi_pc_change_5",
)

PERIODS = (
    ("HISTORICAL_DISCOVERY", 20200101, 20221231),
    ("RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", 20230101, 20241231),
    ("STRESS_PREVALENCE_SEEN_NOT_BLIND", 20250101, 20251231),
)


@dataclass(frozen=True)
class Config:
    study_id: str = "DERIVATIVES_CONTEXT_GATE_STUDY_V0_1"
    execution_mode: str = "RESEARCH_ONLY_NO_EXECUTION"
    discovery_start: int = 20200101
    discovery_end: int = 20221231
    primary_population: str = "FROZEN_STAGE_A_TOP30"
    primary_outcomes: tuple[str, ...] = ("mae_10d", "downside_first", "primary_success")
    percentile_boundaries: tuple[float, ...] = (0.2, 0.4, 0.5, 0.6, 0.8)
    bootstrap_reps: int = 5000
    bootstrap_seed: int = 20260913
    high_risk_min_points: int = 2
    expected_stage_a_store_sha256: str = "36c1071c2dcdabdbcb8a4a6d7126d819d9b545075473a5639766e0d234a6c0d9"
    expected_conditional_store_sha256: str = "efefade875e8fc1f2566e2d5de0e8dcb6cfd267e2ece80304d166ff642ef918c"
    actual_orders: int = 0
    actual_fills: int = 0
    broker_connections: int = 0

    def snapshot(self) -> dict:
        d = asdict(self)
        d["features"] = list(FEATURES)
        d["periods"] = [{"label": x, "start": s, "end": e} for x, s, e in PERIODS]
        return d

    def fingerprint(self) -> str:
        raw = json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()


CFG = Config()
