from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


PERIODS = (
    ("HISTORICAL_DISCOVERY", 20200101, 20221231),
    ("RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", 20230101, 20241231),
    ("STRESS_PREVALENCE_SEEN_NOT_BLIND", 20250101, 20251231),
)

COHORTS = (
    "CZSC_ONLY_ALL",
    "STAGE_A_TOP30",
    "STAGE_A_AND_CZSC",
    "STAGE_A_WITHOUT_CZSC",
)


@dataclass(frozen=True)
class Config:
    study_id: str = "CZSC_THIRD_BUY_INCREMENTAL_STUDY_V0_1"
    upstream_repo: str = "https://github.com/waditu/czsc"
    upstream_version: str = "0.9.27"
    upstream_commit: str = "2d676f987a93cbcc9067513753fe21dad8638c9f"
    upstream_source_path: str = "czsc/signals/cxt.py"
    upstream_source_sha256: str = "7918f9f1c583f16c14d5fad4be35b14a0a2880b1c2a2a1983c8c2159876cd0b4"
    signal_definition_sha256: str = "3a388a0e1a8b0f38a492f526071d16fc13280eca6a95c8ba46567bc385c41326"
    signal_function: str = "cxt_third_buy_V230228"
    signal_di: int = 1
    stage_a_top_k: int = 30
    bootstrap_reps: int = 5_000
    bootstrap_seed: int = 20_260_913
    minimum_later_period_intersection: int = 50
    minimum_intersection_signal_dates: int = 50
    minimum_intersection_signals_per_month: float = 1.0
    minimum_mfe_retention: float = 0.80
    expected_stage_a_store_sha256: str = "36c1071c2dcdabdbcb8a4a6d7126d819d9b545075473a5639766e0d234a6c0d9"
    expected_conditional_store_sha256: str = "efefade875e8fc1f2566e2d5de0e8dcb6cfd267e2ece80304d166ff642ef918c"
    actual_orders: int = 0
    actual_fills: int = 0
    broker_connections: int = 0

    def snapshot(self) -> dict:
        payload = asdict(self)
        payload["periods"] = [
            {"label": label, "start": start, "end": end}
            for label, start, end in PERIODS
        ]
        payload["cohorts"] = list(COHORTS)
        return payload

    def fingerprint(self) -> str:
        raw = json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()


CFG = Config()
