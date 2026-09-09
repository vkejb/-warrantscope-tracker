from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import CFG
from .reconstruct import reconstruct_date


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Isolated reconstruction of the missed 2026-09-07/08 frozen N Compact "
            "scans. This command cannot write prospective ledgers or place orders."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("reconstruct")
    command.add_argument(
        "--signal-date",
        action="append",
        required=True,
        help="repeat for 2026-09-07 and 2026-09-08; no other date is allowed",
    )
    command.add_argument("--archives", type=Path, nargs="+", required=True)
    command.add_argument("--trading-calendar", type=Path, required=True)
    command.add_argument("--output-dir", type=Path, default=CFG.output_dir)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command != "reconstruct":  # pragma: no cover
            raise AssertionError(args.command)
        results = [
            reconstruct_date(
                signal_date,
                args.archives,
                args.trading_calendar,
                output_dir=args.output_dir,
            )
            for signal_date in args.signal_date
        ]
        print(
            json.dumps(
                {"command": "reconstruct", "results": results},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
        )
        return 0
    except Exception as exc:
        print(
            f"retrospective reconstruction failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
