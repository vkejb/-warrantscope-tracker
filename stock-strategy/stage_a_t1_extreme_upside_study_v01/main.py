from __future__ import annotations

import argparse
import json
from pathlib import Path

from .seal import seal_from_active_inputs
from .analysis import run


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage A entry-state shadow seal; no broker functions")
    commands = parser.add_subparsers(dest="command", required=True)
    seal = commands.add_parser("seal-from-active-inputs")
    seal.add_argument("--active-inputs", type=Path, required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--active-inputs", type=Path, required=True)
    publish.add_argument("--direct-official-audit", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "seal-from-active-inputs":
        result = seal_from_active_inputs(args.active_inputs)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "publish":
        print(json.dumps(run(args.active_inputs, args.direct_official_audit), ensure_ascii=False, sort_keys=True))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
