from __future__ import annotations

from .config import CFG, Config
from .study import PATTERNS


def validate_research_run(
    data_audit: dict,
    discovery: dict,
    validation: dict,
    feature_diagnostic: dict,
    validation_decision: dict,
    oos: dict | None,
    cfg: Config = CFG,
) -> dict:
    checks = {
        "strategy_is_research_only": cfg.execution_mode
        == "RESEARCH_ONLY_NO_BROKER_NO_ORDER",
        "no_broad_source_gap": not data_audit.get("broad_source_gap_dates"),
        "discovery_read_boundary": discovery.get("maximum_signal_date_read") is not None
        and discovery["maximum_signal_date_read"] <= cfg.discovery_end,
        "validation_read_boundary": validation.get("maximum_signal_date_read") is not None
        and validation["maximum_signal_date_read"] <= cfg.validation_end,
        "features_are_diagnostic_only": feature_diagnostic.get("policy")
        == "No diagnostic feature filters or reranks V0.1 trades."
        and all(
            not row.get("selected_for_v01_trading")
            for row in feature_diagnostic.get("selection_rows", [])
        ),
        "signals_marked_non_actual": all(
            row.get("is_actual_order") is False and row.get("is_actual_fill") is False
            for period in (discovery, validation)
            for row in period.get("signal_rows", [])
        ),
        "known_patterns_only": all(
            row.get("pattern") in PATTERNS
            for period in (discovery, validation)
            for row in period.get("signal_rows", [])
        ),
        "oos_gate_respected": (
            oos is None
            and not validation_decision.get("patterns_allowed_into_2025")
        )
        or (
            oos is not None
            and bool(validation_decision.get("patterns_allowed_into_2025"))
            and {
                row["pattern"] for row in oos.get("signal_rows", [])
            }.issubset(set(validation_decision["patterns_allowed_into_2025"]))
        ),
    }
    failures = [name for name, passed in checks.items() if not passed]
    return {"passed": not failures, "checks": checks, "failures": failures}
