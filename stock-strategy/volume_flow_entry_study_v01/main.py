"""Run the frozen offline volume-flow study."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .analysis import (
    DEFAULT_RESULTS_DIR,
    build_report,
    refresh_existing_presentation,
    write_results,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument(
        "--refresh-existing-presentation",
        action="store_true",
        help="refresh only report and source hashes from existing numerical CSVs",
    )
    args = parser.parse_args(argv)
    if args.refresh_existing_presentation:
        outputs = refresh_existing_presentation(args.output_dir.resolve())
        print(json.dumps({
            "mode": "PRESENTATION_ONLY_NO_PERFORMANCE_RECOMPUTE",
            "outputs": outputs,
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
        }, ensure_ascii=False, indent=2))
        return 0
    report = build_report()
    outputs = write_results(report, args.output_dir.resolve())
    print(json.dumps({
        "eligible_dates": report["eligible_dates"],
        "config_hash": report["config_hash"],
        "outputs": outputs,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
