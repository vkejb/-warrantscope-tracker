from __future__ import annotations

from collections import Counter
from datetime import datetime

from .config import CFG, Config
from .models import ShadowDecision, Signal, SignalEvaluation, UniverseMember


def validate_results(
    universe: list[UniverseMember],
    evaluations: list[SignalEvaluation],
    raw_signals: list[Signal],
    selected_signals: list[Signal],
    forward_rows: list[dict],
    shadow_outcomes: list[ShadowDecision],
    *,
    total_evaluation_rows: int | None = None,
    cfg: Config = CFG,
) -> dict:
    failures: list[str] = []
    warnings: list[str] = []
    evaluation_row_count = (
        len(evaluations) if total_evaluation_rows is None else total_evaluation_rows
    )
    if cfg.execution_mode != "SHADOW_ONLY_NOT_SUBMITTED":
        failures.append("execution_mode is not permanently shadow-only")

    universe_keys = [(row.trade_date, row.market, row.symbol) for row in universe]
    if not universe:
        warnings.append("NO_ELIGIBLE_UNIVERSE_ROWS")
    if universe and evaluation_row_count == 0:
        failures.append("universe exists but no signal evaluations were produced")
    if len(universe_keys) != len(set(universe_keys)):
        failures.append("duplicate universe member")
    ranks = Counter((row.trade_date, row.universe_rank) for row in universe)
    if any(count > 1 for count in ranks.values()):
        failures.append("duplicate universe rank within date")

    raw_keys = [(row.trade_date, row.market, row.symbol) for row in raw_signals]
    if len(raw_keys) != len(set(raw_keys)):
        failures.append("more than one raw signal for a stock-date")
    selected_dates = [row.trade_date for row in selected_signals]
    if len(selected_dates) != len(set(selected_dates)):
        failures.append("more than one selected signal per date")
    if any(not row.selected for row in selected_signals):
        failures.append("selected signal missing selected flag")
    if not set((row.trade_date, row.market, row.symbol) for row in selected_signals).issubset(
        set(raw_keys)
    ):
        failures.append("selected signal is not a raw signal")

    first = datetime.strptime(cfg.first_signal_bar_end, "%H:%M").time()
    last = datetime.strptime(cfg.last_signal_bar_end, "%H:%M").time()
    if any(not (first <= row.signal_time.time() <= last) for row in raw_signals):
        failures.append("signal outside configured completed-bar window")
    passed_evaluations = {
        (row.trade_date, row.market, row.symbol, row.bar_end)
        for row in evaluations
        if row.passed
    }
    if any(
        (row.trade_date, row.market, row.symbol, row.signal_time)
        not in passed_evaluations
        for row in raw_signals
    ):
        failures.append("raw signal lacks matching passed evaluation")

    expected_forward_rows = len(selected_signals) * (len(cfg.forward_minutes) + 1)
    if len(forward_rows) != expected_forward_rows:
        failures.append("forward row count does not match selected signals and horizons")
    if any("NOT_EXECUTABLE_FILL" not in row["reference_basis"] for row in forward_rows):
        failures.append("forward row is not clearly marked as a non-executable reference")

    if len(shadow_outcomes) not in {0, len(selected_signals)}:
        failures.append("shadow outcome count does not match selected signals")
    if any(row.is_actual_order or row.is_actual_fill for row in shadow_outcomes):
        failures.append("shadow output falsely claims an actual order or fill")
    if universe and not selected_signals:
        warnings.append("NO_SELECTED_SIGNALS")

    return {
        "passed": not failures,
        "result_state": (
            "INVALID"
            if failures
            else "VALID_NO_ELIGIBLE_UNIVERSE"
            if not universe
            else "VALID_NO_SIGNAL"
            if not selected_signals
            else "VALID_WITH_SIGNALS"
        ),
        "failure_count": len(failures),
        "failures": failures,
        "warning_count": len(warnings),
        "warnings": warnings,
        "checks": {
            "config_hash": cfg.fingerprint(),
            "universe_rows": len(universe),
            "evaluation_rows": evaluation_row_count,
            "retained_evaluation_rows": len(evaluations),
            "passed_evaluations": len(passed_evaluations),
            "raw_signals": len(raw_signals),
            "selected_signals": len(selected_signals),
            "forward_rows": len(forward_rows),
            "shadow_outcomes": len(shadow_outcomes),
            "actual_orders": sum(row.is_actual_order for row in shadow_outcomes),
            "actual_fills": sum(row.is_actual_fill for row in shadow_outcomes),
        },
    }
