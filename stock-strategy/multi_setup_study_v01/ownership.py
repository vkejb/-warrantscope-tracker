from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .config import CFG


@dataclass(frozen=True, slots=True)
class AsOfFeatureResult:
    security_id: str
    decision_timestamp: str
    status: str
    known_at: str | None
    values: dict[str, float]
    source: str | None
    payload_sha256: str | None


class OwnershipProvider(Protocol):
    """Future providers must perform a point-in-time as-of join internally."""

    def load_asof(
        self, security_ids: list[str], decision_timestamps: list[str]
    ) -> list[AsOfFeatureResult]: ...


class MarginShortProvider(Protocol):
    """Future providers must enforce their conservative available_from date."""

    def load_asof(
        self, security_ids: list[str], decision_timestamps: list[str]
    ) -> list[AsOfFeatureResult]: ...


class UnavailableOwnershipProvider:
    """Fail-closed V0.1 provider: no snapshot is silently backfilled."""

    def load_asof(
        self, security_ids: list[str], decision_timestamps: list[str]
    ) -> list[AsOfFeatureResult]:
        return [
            AsOfFeatureResult(
                security_id=security_id,
                decision_timestamp=timestamp,
                status=CFG.ownership_status,
                known_at=None,
                values={},
                source=None,
                payload_sha256=None,
            )
            for security_id in security_ids
            for timestamp in decision_timestamps
        ]
