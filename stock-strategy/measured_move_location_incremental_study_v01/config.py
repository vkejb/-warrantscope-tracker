from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


PERIODS = (
    ("HISTORICAL_DISCOVERY", "20200101", "20221231"),
    ("RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", "20230101", "20241231"),
    ("STRESS_PREVALENCE_SEEN_NOT_BLIND", "20250101", "20251231"),
)

RATIO_BUCKETS = (
    ("B0_R_LT_0", None, 0.0),
    ("B1_R_0_TO_0_5", 0.0, 0.5),
    ("B2_R_0_5_TO_0_8", 0.5, 0.8),
    ("B3_R_0_8_TO_1_0", 0.8, 1.0),
    ("B4_R_1_0_TO_1_2", 1.0, 1.2),
    ("B5_R_1_2_TO_1_5", 1.2, 1.5),
    ("B6_R_GE_1_5", 1.5, None),
)

PRIMARY_GROUPS = (
    ("A_R_LT_0_8", None, 0.8),
    ("B_R_0_8_TO_1_2", 0.8, 1.2),
    ("C_R_GE_1_2", 1.2, None),
)


@dataclass(frozen=True)
class Config:
    study_id: str = "MEASURED_MOVE_LOCATION_INCREMENTAL_STUDY_V0_1"
    execution_mode: str = "RESEARCH_ONLY_NO_EXECUTION"
    maximum_input_date: str = "20251231"
    primary_left: int = 2
    primary_right: int = 2
    sensitivity_left: int = 3
    sensitivity_right: int = 3
    primary_horizon: int = 5
    secondary_horizon: int = 10
    bootstrap_iterations: int = 5_000
    bootstrap_seed: int = 20_260_915
    minimum_coverage: float = 0.30
    minimum_primary_group_observations_per_period: int = 100
    slippage_one_way: float = 0.001
    per_trade_notional: float = 30_000.0
    commission_rate: float = 0.001425 * 0.28
    minimum_commission: float = 1.0
    sell_tax_rate: float = 0.003
    actual_orders: int = 0
    actual_fills: int = 0
    broker_connections: int = 0
    stage_a_refit_count: int = 0
    model_fit_count: int = 0

    def snapshot(self) -> dict:
        payload = asdict(self)
        payload["periods"] = [
            {"label": label, "start": start, "end": end}
            for label, start, end in PERIODS
        ]
        payload["ratio_buckets"] = [list(item) for item in RATIO_BUCKETS]
        payload["primary_groups"] = [list(item) for item in PRIMARY_GROUPS]
        payload["concept_status"] = "CONCEPT_RECONSTRUCTION_NOT_AUTHOR_STRATEGY"
        return payload

    def fingerprint(self) -> str:
        raw = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


CFG = Config()
