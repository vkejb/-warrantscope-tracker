"""Fail-closed authorization gate for production broker submission."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping


@dataclass(frozen=True, slots=True)
class LiveTradingGate:
    """Immutable snapshot of the three independent LIVE controls.

    The command-line parser owns ``cli_live``.  Capturing the environment once
    prevents a later mutation of ``os.environ`` from silently enabling sends.
    """

    execution_mode: str
    enable_live_trading: str
    cli_live: bool

    @classmethod
    def from_environment(
        cls,
        *,
        cli_live: bool,
        environ: Mapping[str, str] | None = None,
    ) -> "LiveTradingGate":
        values = os.environ if environ is None else environ
        return cls(
            execution_mode=str(values.get("EXECUTION_MODE", "DRY_RUN")).strip().upper(),
            enable_live_trading=str(values.get("ENABLE_LIVE_TRADING", "NO")).strip().upper(),
            cli_live=bool(cli_live),
        )

    @property
    def authorized(self) -> bool:
        return (
            self.execution_mode == "LIVE"
            and self.enable_live_trading == "YES"
            and self.cli_live
        )

    def public_snapshot(self) -> dict[str, object]:
        return {
            "execution_mode": self.execution_mode,
            "enable_live_trading": self.enable_live_trading,
            "cli_live": self.cli_live,
            "authorized": self.authorized,
        }
