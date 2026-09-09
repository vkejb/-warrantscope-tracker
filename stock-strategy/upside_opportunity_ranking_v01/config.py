from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json

from cross_sectional_alpha_ranking_v01.config import FEATURE_NAMES, PERIODS


STAGE_A_TOP_K: tuple[int, ...] = (10, 20, 30, 50)
TWO_STAGE_TOP_K: tuple[int, ...] = (3, 5, 10)
ALPHA_CANDIDATES: tuple[float, ...] = (0.1, 1.0, 10.0, 100.0)
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
    study_id: str = "UPSIDE_OPPORTUNITY_RANKING_V0_1"
    execution_mode: str = "RESEARCH_ONLY_NO_EXECUTION"
    stage_a_primary_model: str = "LINEAR_RIDGE_MFE10"
    stage_a_benchmark_model: str = "IC_WEIGHTED_MFE_RANK"
    stage_a_primary_top_k: int = 30
    two_stage_primary_top_k: int = 5
    alpha_candidates: tuple[float, ...] = ALPHA_CANDIDATES
    stage_a_top_k: tuple[int, ...] = STAGE_A_TOP_K
    two_stage_top_k: tuple[int, ...] = TWO_STAGE_TOP_K
    discovery_start: str = "20200101"
    discovery_end: str = "20221231"
    label_purge_sessions: int = 10
    cooldown_sessions: int = 10
    bootstrap_iterations: int = 5_000
    bootstrap_seed: int = 20_260_910
    expected_winner_store_sha256: str = "157339658c60fdb3604dc845a3234107f08f27dab1f39204766852ceaa17f5f8"
    expected_cross_store_sha256: str = "eb88b627a45c9154ddb1aab6eb56cacc5bae32373f54edd171e07c6805ff67c5"
    expected_stage_b_model_spec_sha256: str = "1548094c67eb4afb85622cd649ab122b82e4f1de24a20feb7b1b748d43ea217e"
    expected_stage_b_fingerprint: str = "5e3e78aef9d986bbbcf0e1e40857f962a5e4aec7d3a1b6df245123345cb6507a"
    expected_mother_rows: int = 704_327

    def snapshot(self) -> dict:
        value = asdict(self)
        value["feature_names"] = list(FEATURE_NAMES)
        value["periods"] = [
            {"label": label, "start": start, "end": end}
            for label, start, end in PERIODS
        ]
        value["gap_buckets"] = [
            {"label": label, "lower": lower, "upper": upper}
            for label, lower, upper in GAP_BUCKETS
        ]
        return value

    @lru_cache(maxsize=None)
    def fingerprint(self) -> str:
        raw = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


CFG = Config()
