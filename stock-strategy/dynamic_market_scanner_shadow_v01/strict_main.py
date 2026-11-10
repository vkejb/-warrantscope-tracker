from __future__ import annotations

import argparse
import json
from pathlib import Path

from .strict_replay import DEFAULT_RESULTS_DIR, run_all


def main() -> int:
    parser = argparse.ArgumentParser(description="Run frozen STRICT_MARKET_WIDE_V1 offline replay")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--session-date", action="append", default=[])
    args = parser.parse_args()
    dates = set(args.session_date) if args.session_date else None
    print(json.dumps(run_all(args.results_dir, dates), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
