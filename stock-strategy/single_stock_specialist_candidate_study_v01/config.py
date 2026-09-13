from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


CANDIDATES = (
    ("2408", "南亞科"),
    ("2344", "華邦電"),
    ("3231", "緯創"),
    ("3017", "奇鋐"),
    ("2368", "金像電"),
)

PERIODS = (
    ("HISTORICAL_DISCOVERY", "20200101", "20221231"),
    ("RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", "20230101", "20241231"),
    ("STRESS_PREVALENCE_SEEN_NOT_BLIND", "20250101", "20251231"),
)

COMPONENT_WEIGHTS = {
    "volatility_opportunity_component": 0.30,
    "liquidity_component": 0.25,
    "continuity_component": 0.20,
    "behavior_stability_component": 0.15,
    "gap_safety_component": 0.10,
}


@dataclass(frozen=True)
class Config:
    study_id: str = "SINGLE_STOCK_SPECIALIST_CANDIDATE_STUDY_V0_1"
    execution_mode: str = "RESEARCH_ONLY_NO_EXECUTION"
    benchmark_symbol: str = "0050"
    discovery_start: str = "20200101"
    discovery_end: str = "20221231"
    maximum_input_date: str = "20251231"
    atr_window: int = 14
    realized_vol_window: int = 20
    efficiency_window: int = 20
    forward_sessions: int = 10
    annualization_sessions: int = 252
    stability_minimum_coverage: float = 0.90
    stability_minimum_sessions: int = 180
    retention_minimum: float = 0.60
    retention_maximum: float = 1.80
    maximum_gap_multiple: float = 2.00
    actual_orders: int = 0
    actual_fills: int = 0
    broker_connections: int = 0
    model_fit_count: int = 0
    stage_a_refit_count: int = 0

    @property
    def candidates(self) -> tuple[str, ...]:
        return tuple(code for code, _ in CANDIDATES)

    def snapshot(self) -> dict:
        payload = asdict(self)
        payload["candidates"] = [
            {"code": code, "name": name} for code, name in CANDIDATES
        ]
        payload["periods"] = [
            {"label": label, "start": start, "end": end}
            for label, start, end in PERIODS
        ]
        payload["component_weights"] = COMPONENT_WEIGHTS
        payload["future_outcomes_used_in_candidate_score"] = False
        return payload

    def fingerprint(self) -> str:
        raw = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


CFG = Config()
