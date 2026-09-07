from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import CFG
from .detector import assert_frozen_contract
from .market_data_provider import ExistingDailyDataProvider
from .service import run_daily, update_outcomes
from .storage import ShadowStore


DEFAULT_STORE = Path(__file__).resolve().parent / "data"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Append-only prospective N Compact shadow observer. "
            "It has no broker connection and cannot submit orders."
        )
    )
    parser.add_argument("--store-dir", type=Path, default=DEFAULT_STORE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create and validate empty append-only ledgers")
    commands.add_parser("status", help="verify ledger chains and print status")

    for name in ("run-daily", "update-outcomes"):
        command = commands.add_parser(name)
        command.add_argument("--archives", type=Path, nargs="+", required=True)
        command.add_argument("--supplements", type=Path, nargs="*", default=[])
        command.add_argument(
            "--trading-calendar",
            type=Path,
            required=True,
            help="independent point-in-time CSV with an ascending date column",
        )
        if name == "update-outcomes":
            command.add_argument("--as-of", required=True, help="YYYY-MM-DD")
    return parser


def _provider(args) -> ExistingDailyDataProvider:
    return ExistingDailyDataProvider(
        args.archives,
        supplement_paths=args.supplements,
        trading_calendar_path=args.trading_calendar,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = ShadowStore(args.store_dir, CFG)
    try:
        assert_frozen_contract(CFG)
        if args.command == "init":
            store.initialize()
            result = {"command": "init", "status": store.validate()}
        elif args.command == "status":
            result = {"command": "status", "status": store.validate()}
        elif args.command == "run-daily":
            result = run_daily(_provider(args), store)
        elif args.command == "update-outcomes":
            result = update_outcomes(_provider(args), store, args.as_of)
        else:  # pragma: no cover - argparse enforces this branch.
            raise AssertionError(args.command)
        if args.command in {"run-daily", "update-outcomes"}:
            manifest = store.record_run(args.command, result)
            result["run_manifest"] = str(manifest.relative_to(store.root))
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
        )
        return 0
    except Exception as exc:
        print(f"prospective shadow failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
