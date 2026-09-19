from __future__ import annotations

import argparse
import json
from pathlib import Path

from .ledger import update, verify, RUNTIME


def main() -> int:
    parser = argparse.ArgumentParser(description="Separate read-only-input Stage A T+1 outcome updater")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("update-outcomes")
    command.add_argument("--active-inputs", required=True, type=Path)
    command.add_argument("--direct-official-audit", required=True, type=Path)
    commands.add_parser("verify")
    args = parser.parse_args()
    result = update(args.active_inputs, args.direct_official_audit) if args.command == "update-outcomes" else dict(zip(("keys", "last_hash", "rows"), verify(RUNTIME / "outcomes.jsonl")))
    if "keys" in result:
        result["keys"] = len(result["keys"])
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
