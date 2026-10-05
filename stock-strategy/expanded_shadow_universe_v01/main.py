"""CLI for building and inspecting the expanded shadow universe."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .universe import DEFAULT_AUDIT_DIR, DEFAULT_RUNTIME_DIR, build_universe, load_universe


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Expanded read-only shadow universe")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "show"):
        command = sub.add_parser(name)
        command.add_argument("--date", required=True, help="official EOD date YYYYMMDD")
        command.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    sub.choices["build"].add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.command == "build":
        seal = build_universe(
            args.date, runtime_dir=args.runtime_dir.resolve(), audit_dir=args.audit_dir.resolve()
        )
    else:
        seal, _ = load_universe(args.date, runtime_dir=args.runtime_dir.resolve())
    print(json.dumps({
        "signal_date": seal["signal_date"], "status": seal["status"],
        "selected_count": seal["selected_count"], "eligible_before_cap": seal["eligible_before_cap"],
        "seal_hash": seal["seal_hash"], "mode": seal["mode"],
        "actual_orders": seal["actual_orders"], "actual_fills": seal["actual_fills"],
        "broker_order_calls": seal["broker_order_calls"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
