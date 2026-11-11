"""Read-only status summary for a live or completed early-observation run."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any

from .realtime_expanded_shadow import EVENTS_NAME


DEFAULT_RUNS_DIR = (
    Path(__file__).resolve().parent.parent
    / "yuanta_intraday_shadow_v01" / "runtime" / "runs"
)


def latest_run(runs_dir: Path = DEFAULT_RUNS_DIR) -> Path:
    candidates = sorted(
        path for path in runs_dir.iterdir()
        if path.is_dir() and (path / EVENTS_NAME).exists()
    )
    if not candidates:
        raise FileNotFoundError("no realtime expanded shadow run found")
    return candidates[-1]


def summarize(run_dir: Path) -> dict[str, Any]:
    path = Path(run_dir) / EVENTS_NAME
    states: dict[str, dict[str, Any]] = {}
    candidates, selections, continuations, notifications = [], [], [], []
    errors, warnings = [], []
    last_at = None
    actual_orders = actual_fills = broker_order_calls = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            last_at = row.get("at") or last_at
            actual_orders += int(row.get("actual_orders") or 0)
            actual_fills += int(row.get("actual_fills") or 0)
            broker_order_calls += int(row.get("broker_order_calls") or 0)
            event = row.get("event")
            if event == "EARLY_STATE_TRANSITION":
                states[str(row["stock_id"])] = {
                    "stock_id": str(row["stock_id"]),
                    "stock_name": row.get("stock_name"),
                    "state": row.get("state"),
                    "observed_at": row.get("observed_at"),
                    "relative_strength_5m": row.get("relative_strength_5m"),
                    "source_check_count": row.get("source_check_count"),
                    "volume_gate_passed": row.get("volume_gate_passed"),
                    "entry_price": row.get("entry_price"),
                }
            elif event == "CANDIDATE_SEEN":
                candidates.append(row)
            elif event == "SHADOW_SIGNAL_SELECTED":
                selections.append(row)
            elif event == "THREE_MINUTE_CONTINUATION":
                continuations.append(row)
            elif event == "MANUAL_REVIEW_NOTIFICATION":
                notifications.append(row)
            elif event == "OBSERVER_EVENT_ERROR":
                errors.append(row)
            elif event == "CALLBACK_RECEIVE_TIME_REGRESSION":
                warnings.append(row)
    ordered = sorted(
        states.values(),
        key=lambda row: (str(row.get("observed_at") or ""), row["stock_id"]),
    )
    return {
        "run_dir": str(Path(run_dir).resolve()), "as_of": last_at,
        "state_counts": dict(Counter(row["state"] for row in ordered)),
        "early_states": ordered, "candidate_count": len(candidates),
        "candidates": candidates, "selected_count": len(selections),
        "selected": selections[-1] if selections else None,
        "continuation": continuations[-1] if continuations else None,
        "notification": notifications[-1] if notifications else None,
        "observer_error_count": len(errors), "observer_errors": errors,
        "observer_warning_count": len(warnings), "observer_warnings": warnings,
        "actual_orders": actual_orders, "actual_fills": actual_fills,
        "broker_order_calls": broker_order_calls,
        "interpretation": "EARLY_STATES_ARE_TELEMETRY_NOT_ENTRY_APPROVAL",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    args = parser.parse_args()
    run_dir = args.run_dir or latest_run(args.runs_dir)
    print(json.dumps(summarize(run_dir), ensure_ascii=False, indent=2,
                     sort_keys=True))


if __name__ == "__main__":
    main()
