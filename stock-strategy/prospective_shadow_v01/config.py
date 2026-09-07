from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json


@dataclass(frozen=True, slots=True)
class Config:
    """Frozen prospective contract. Changing it requires a new module version."""

    strategy_id: str = "PROSPECTIVE_N_COMPACT_SHADOW_V0_1"
    schema_version: str = "1"
    execution_mode: str = "SHADOW_ONLY_NOT_SUBMITTED"
    timezone: str = "Asia/Taipei"
    prospective_start_date: str = "20260907"
    market_close_ready_time: str = "13:35"

    parent_setup: str = "N_RETEST"
    setup: str = "N_COMPACT_RETEST"
    compact_rule: str = (
        "pivot_separation_sessions <= 7 and bottom_difference > 0"
    )
    primary_outcome: str = "+8% before -5% within 10 trading days using Close"
    primary_target: float = 0.08
    primary_stop: float = -0.05
    primary_horizon: int = 10

    # A drift in either source fails closed instead of silently changing the
    # prospective definition after observations have begun.
    expected_multi_setup_config_hash: str = (
        "9f15e6bdaa2186ac3a84a61064b3b71ce1b3532e9085135feb4aef59d3172b5f"
    )
    expected_reversal_config_hash: str = (
        "d0c00ee5b733a37ed0093a764a5f679506b455ff7aaf77d14e952ec4280d7be1"
    )
    expected_reversal_study_hash: str = (
        "6dc42e7bd4ae3ce44f87a809cf905df20c106eb8d1044180a142728bb0589691"
    )
    expected_compact_detector_hash: str = (
        "a025efcd65422e1651eb468b00cc8ebcb4a753b7bf5250df6a2ae5b31b389ec3"
    )

    signals_filename: str = "prospective_signals.csv"
    outcomes_filename: str = "prospective_outcomes.csv"
    scans_filename: str = "prospective_scan_log.csv"
    status_filename: str = "shadow_status.json"
    lock_filename: str = ".prospective_shadow.lock"

    def snapshot(self) -> dict:
        return asdict(self)

    @lru_cache(maxsize=None)
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


CFG = Config()
