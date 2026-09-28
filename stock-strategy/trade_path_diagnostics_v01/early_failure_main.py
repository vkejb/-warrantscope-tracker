from __future__ import annotations

import argparse
from pathlib import Path

from .early_failure import build_report, write_report
from .main import _session


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest-only early failure grid")
    parser.add_argument("--session", action="append", required=True, type=_session)
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    write_report(build_report(dict(args.session), args.capital), args.output_dir)


if __name__ == "__main__":
    main()
