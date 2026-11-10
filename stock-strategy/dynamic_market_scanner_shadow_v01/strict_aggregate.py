from __future__ import annotations

import argparse
import json
from pathlib import Path

from .strict_replay import DEFAULT_RESULTS_DIR, aggregate_result_dirs


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge strict replay date groups")
    parser.add_argument("source_dirs", nargs="+", type=Path)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    args = parser.parse_args()
    print(json.dumps(aggregate_result_dirs(args.source_dirs, args.results_dir),
                     ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
