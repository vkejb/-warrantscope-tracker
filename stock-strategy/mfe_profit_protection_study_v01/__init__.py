"""Research-only MFE profit-protection overlay.

This package deliberately has no broker imports and is not imported by the live
runtime.  The default feature flag remains disabled.
"""

from .overlay import (
    ENABLE_MFE_PROFIT_PROTECTION,
    EntryFill,
    MFEProtectionState,
    OverlayVariant,
    PositionBasis,
    VARIANTS,
)

__all__ = [
    "ENABLE_MFE_PROFIT_PROTECTION",
    "EntryFill",
    "MFEProtectionState",
    "OverlayVariant",
    "PositionBasis",
    "VARIANTS",
]
