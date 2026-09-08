from __future__ import annotations

import argparse
import json
import sys

from .config import CFG
from .pipeline import prepare_inputs, public_result
from .runner import attempt, runner_status


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Official TWSE/TPEx EOD preparation and fail-closed external runner. "
            "No broker connection or order capability exists."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare", help="prepare/audit today's data without sealing a scan")
    commands.add_parser("attempt", help="scheduled current-day attempt; no date override exists")
    commands.add_parser("status", help="show runner and prospective-ledger counts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            prepared = prepare_inputs(cfg=CFG)
            result = public_result(prepared)
            code = 0 if prepared.ready or not prepared.trading_day else 3
        elif args.command == "attempt":
            result = attempt(cfg=CFG)
            code = 0 if result["status"] in {
                "SUCCESS",
                "ALREADY_SUCCEEDED_NO_OP",
                "NON_TRADING_DAY_NO_OP",
            } else 3
        elif args.command == "status":
            result = runner_status(CFG)
            code = 0
        else:  # pragma: no cover
            raise AssertionError(args.command)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
        return code
    except Exception as exc:
        print(
            f"shadow daily runner failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
