from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json


INSTITUTIONAL_FEATURES = tuple(
    f"{actor}_{suffix}"
    for actor in ("foreign", "investment_trust", "dealer")
    for suffix in (
        "net_volume_ratio_1d",
        "net_volume_ratio_3d",
        "net_volume_ratio_5d",
        "consecutive_buy_days_5d",
        "sign_persistence_5d",
    )
)
MARGIN_FEATURES = (
    "margin_balance_change_volume_ratio_1d",
    "margin_balance_change_volume_ratio_3d",
    "margin_balance_change_volume_ratio_5d",
    "short_balance_change_volume_ratio_1d",
    "short_balance_change_volume_ratio_3d",
    "short_balance_change_volume_ratio_5d",
    "short_margin_ratio",
    "margin_data_available",
)
CHIP_FEATURES = INSTITUTIONAL_FEATURES + MARGIN_FEATURES
C_CANDIDATES = (0.01, 0.1, 1.0, 10.0)


@dataclass(frozen=True, slots=True)
class Config:
    study_id: str = "CHIP_INCREMENTAL_STUDY_V0_1"
    execution_mode: str = "RESEARCH_ONLY_NO_EXECUTION"
    primary_model: str = "LOGISTIC_RIDGE_PATH_SUCCESS_CHIP"
    primary_top_k: int = 5
    stage_a_pool_size: int = 30
    publication_lag_sessions: int = 1
    extra_lag_sessions: int = 1
    discovery_start: str = "20200101"
    discovery_end: str = "20221231"
    label_purge_sessions: int = 10
    c_candidates: tuple[float, ...] = C_CANDIDATES
    cooldown_sessions: int = 10
    minimum_mfe_retention: float = 0.80
    expected_conditional_model_spec_sha256: str = "1d5587fa7b3153fb40c8c8420821bf4ef4e0b8423b46dda6e2a90725d3c5d1a8"
    expected_conditional_manifest_sha256: str = "6a9c283b96aa491e0fce14d388aa92abc10983818cdb0e934e29e6990432beb5"
    expected_conditional_store_sha256: str = "efefade875e8fc1f2566e2d5de0e8dcb6cfd267e2ece80304d166ff642ef918c"

    def snapshot(self) -> dict:
        payload = asdict(self)
        payload["chip_features"] = list(CHIP_FEATURES)
        payload["pit_rule"] = (
            "source_date <= previous_market_session(signal_date); "
            "historical endpoint queryability does not prove same-day availability"
        )
        return payload

    @lru_cache(maxsize=None)
    def fingerprint(self) -> str:
        raw = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


CFG = Config()
