from __future__ import annotations

import argparse
from pathlib import Path

from .analysis import build_report, write_report


def _session(value: str) -> tuple[str, list[Path]]:
    try:
        date, paths = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use YYYYMMDD=RUN_DIR[,RUN_DIR]") from exc
    return date, [Path(path) for path in paths.split(",")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Research-only initial R stop study")
    parser.add_argument("--session", action="append", required=True, type=_session)
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    write_report(build_report(dict(args.session), args.capital), args.output_dir)


if __name__ == "__main__":
    main()
