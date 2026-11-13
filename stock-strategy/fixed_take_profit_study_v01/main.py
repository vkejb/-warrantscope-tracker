from __future__ import annotations

import argparse
from pathlib import Path

from .study import RESULTS_DIR, run


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed take-profit shadow study")
    parser.add_argument("--output-root", type=Path, default=RESULTS_DIR)
    args = parser.parse_args()
    print(run(args.output_root))


if __name__ == "__main__":
    main()
