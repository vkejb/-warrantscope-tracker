from __future__ import annotations

import argparse
import json
from pathlib import Path

from .seal_store import latest_seal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Frozen Stage A Top30 prospective shadow watchlist")
    commands = parser.add_subparsers(dest="command", required=True)
    seal = commands.add_parser("seal-current")
    seal.add_argument("--archives", nargs="+", type=Path, required=True)
    seal.add_argument("--trading-calendar", type=Path, required=True)
    seal.add_argument("--expected-input-hash", required=True)
    commands.add_parser("status")
    args = parser.parse_args(argv)
    try:
        if args.command == "seal-current":
            from .watchlist import seal_current
            result = seal_current(args.archives, args.trading_calendar, expected_input_hash=args.expected_input_hash)
        else:
            result = {"latest_seal": latest_seal()}
        code = 0
    except Exception as exc:
        result = {"status": "FAILED", "error_type": type(exc).__name__, "reason": str(exc), "actual_orders": 0, "actual_fills": 0, "broker_connections": 0}
        code = 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
