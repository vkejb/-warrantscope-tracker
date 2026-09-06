from __future__ import annotations

from collections import Counter

from .config import CFG, Config


def validate_research_run(
    data_audit: dict,
    discovery: dict,
    validation_period: dict | None,
    validation_decision: dict,
    oos_period: dict | None,
    cfg: Config = CFG,
) -> dict:
    failures: list[str] = []
    selected = discovery["selected_rule"].get("features", [])
    families = [row["family"] for row in selected]
    checks = {
        "discovery_read_boundary": discovery["selected_rule"].get(
            "does_not_use_validation_or_oos"
        )
        is True,
        "at_most_four_features": len(selected) <= cfg.maximum_selected_features,
        "one_feature_per_family": all(
            count == 1 for count in Counter(families).values()
        ),
        "no_broad_source_gap_dates": not data_audit.get("broad_source_gap_dates"),
        "validation_not_run_without_rule": bool(selected)
        or validation_period is None,
        "oos_gate_enforced": (
            validation_decision.get("status") == "PASS" and oos_period is not None
        )
        or (validation_decision.get("status") != "PASS" and oos_period is None),
    }
    if validation_period is not None:
        checks["top30_bound"] = all(
            row["selected_signal_count"] <= cfg.daily_selection_count
            for row in validation_period["daily_rows"]
        )
        checks["no_actual_signal_fills"] = all(
            row.get("is_actual_order") is False and row.get("is_actual_fill") is False
            for row in validation_period["signal_rows"]
        )
    for name, passed in checks.items():
        if not passed:
            failures.append(name)
    return {
        "passed": not failures,
        "checks": checks,
        "failures": failures,
        "result_status": cfg.result_status,
    }
