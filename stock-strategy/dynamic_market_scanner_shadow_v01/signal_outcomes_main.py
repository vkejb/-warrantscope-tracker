from __future__ import annotations

import argparse
import json
from pathlib import Path

from .signal_outcomes import DEFAULT_OUTCOME_RESULTS_DIR, run_outcomes
from .strict_replay import DEFAULT_RESULTS_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description="Build fixed-horizon signal outcomes")
    parser.add_argument("--ledger", type=Path,
                        default=DEFAULT_RESULTS_DIR / "candidate_ledger.csv")
    parser.add_argument("--results-dir", type=Path,
                        default=DEFAULT_OUTCOME_RESULTS_DIR)
    args = parser.parse_args()
    print(json.dumps(run_outcomes(args.ledger, args.results_dir),
                     ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
