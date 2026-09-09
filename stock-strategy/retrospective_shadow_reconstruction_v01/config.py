from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parent
STOCK_STRATEGY_DIR = MODULE_DIR.parent


@dataclass(frozen=True, slots=True)
class ReconstructionConfig:
    schema_version: str = "1"
    classification: str = "RETROSPECTIVE_RECONSTRUCTION_NOT_PROSPECTIVE"
    allowed_signal_dates: tuple[str, ...] = ("20260907", "20260908")
    output_dir: Path = MODULE_DIR / "output"
    prospective_store_dir: Path = (
        STOCK_STRATEGY_DIR / "prospective_shadow_v01" / "data"
    )


CFG = ReconstructionConfig()
