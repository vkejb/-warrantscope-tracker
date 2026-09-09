from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json

from cross_sectional_alpha_ranking_v01.config import FEATURE_NAMES, PERIODS


CONTEXT_FEATURE_NAMES: tuple[str, ...] = (
    "frozen_stage_a_upside_score_z",
    "frozen_stage_a_rank_percentile",
)
CONDITIONAL_FEATURE_NAMES: tuple[str, ...] = FEATURE_NAMES + CONTEXT_FEATURE_NAMES
C_CANDIDATES: tuple[float, ...] = (0.01, 0.1, 1.0, 10.0)
CONDITIONAL_TOP_K: tuple[int, ...] = (3, 5, 10)
GAP_BUCKETS: tuple[tuple[str, float | None, float | None], ...] = (
    ("LT_MINUS_1PCT", None, -0.01),
    ("MINUS_1_TO_0PCT", -0.01, 0.0),
    ("0_TO_1PCT", 0.0, 0.01),
    ("1_TO_2PCT", 0.01, 0.02),
    ("2_TO_3PCT", 0.02, 0.03),
    ("GE_3PCT", 0.03, None),
)


@dataclass(frozen=True, slots=True)
class Config:
    study_id: str = "CONDITIONAL_PATH_QUALITY_RANKING_V0_1"
    execution_mode: str = "RESEARCH_ONLY_NO_EXECUTION"
    primary_model: str = "LOGISTIC_RIDGE_PATH_SUCCESS"
    benchmark_model: str = "IC_WEIGHTED_PATH_SCORE"
    stage_a_pool_size: int = 30
    primary_top_k: int = 5
    conditional_top_k: tuple[int, ...] = CONDITIONAL_TOP_K
    c_candidates: tuple[float, ...] = C_CANDIDATES
    discovery_start: str = "20200101"
    discovery_end: str = "20221231"
    label_purge_sessions: int = 10
    cooldown_sessions: int = 10
    primary_target: float = 0.08
    primary_stop: float = -0.05
    minimum_mfe_retention: float = 0.80
    expected_mother_rows: int = 704_327
    expected_winner_store_sha256: str = "157339658c60fdb3604dc845a3234107f08f27dab1f39204766852ceaa17f5f8"
    expected_cross_store_sha256: str = "eb88b627a45c9154ddb1aab6eb56cacc5bae32373f54edd171e07c6805ff67c5"
    expected_upside_store_sha256: str = "36c1071c2dcdabdbcb8a4a6d7126d819d9b545075473a5639766e0d234a6c0d9"
    expected_stage_a_model_spec_sha256: str = "95c56676f79a98fb8a48324d2777294f727f76327c815674b96c29810d9d057d"
    expected_stage_a_manifest_sha256: str = "313dd76524d94d69b4ca26c45d229503877fc9cbd298d839bc2f94ee7931066a"
    expected_stage_a_fingerprint: str = "a943b58962b0e19e2830a700cd7ed1d32a8dc338be79652126e13be82b114ccd"

    def snapshot(self) -> dict:
        payload = asdict(self)
        payload["feature_names"] = list(CONDITIONAL_FEATURE_NAMES)
        payload["periods"] = [
            {"label": label, "start": start, "end": end}
            for label, start, end in PERIODS
        ]
        payload["timeout_primary_treatment"] = "NON_SUCCESS"
        payload["gap_buckets"] = [
            {"label": label, "lower": lower, "upper": upper}
            for label, lower, upper in GAP_BUCKETS
        ]
        return payload

    @lru_cache(maxsize=None)
    def fingerprint(self) -> str:
        raw = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


CFG = Config()
