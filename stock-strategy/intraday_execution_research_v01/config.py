from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


SNAPSHOT_TIMES = ("09:05", "09:15", "09:30", "10:00", "11:00", "13:00")


@dataclass(frozen=True, slots=True)
class Config:
    study_id: str = "INTRADAY_EXECUTION_RESEARCH_V0_1"
    classification: str = "INFRASTRUCTURE_READY_NO_REAL_INTRADAY_EVIDENCE"
    schema_version: int = 1
    timezone: str = "Asia/Taipei"
    stage_a_top_k: int = 30
    snapshot_times: tuple[str, ...] = SNAPSHOT_TIMES
    entry_rule: str = "FIRST_VALID_TRADABLE_EVENT_STRICTLY_AFTER_SNAPSHOT"
    mock_seed: int = 20260911
    expected_stage_a_store_sha256: str = "36c1071c2dcdabdbcb8a4a6d7126d819d9b545075473a5639766e0d234a6c0d9"
    expected_stage_a_spec_sha256: str = "95c56676f79a98fb8a48324d2777294f727f76327c815674b96c29810d9d057d"
    expected_stage_a_manifest_sha256: str = "313dd76524d94d69b4ca26c45d229503877fc9cbd298d839bc2f94ee7931066a"
    expected_stage_a_model_fingerprint: str = "a943b58962b0e19e2830a700cd7ed1d32a8dc338be79652126e13be82b114ccd"

    def fingerprint(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()


CFG = Config()
