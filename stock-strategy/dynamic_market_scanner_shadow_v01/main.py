"""CLI for publishing dynamic expanded-universe shadow lifecycle diagnostics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from expanded_shadow_universe_v01.paper import DEFAULT_RUNTIME_DIR as PAPER_RUNTIME

from .scanner import DEFAULT_RUNTIME_DIR, publish_dynamic_scan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--paper-run-id")
    parser.add_argument("--paper-runtime-dir", type=Path, default=PAPER_RUNTIME)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    args = parser.parse_args(argv)
    day_dir = args.paper_runtime_dir.resolve() / "days" / args.session_date
    if args.paper_run_id:
        source = day_dir / args.paper_run_id
    else:
        matches = sorted(path for path in day_dir.iterdir() if (path / "manifest.json").is_file())
        if len(matches) != 1:
            raise RuntimeError("paper run id is required when the session has zero or multiple runs")
        source = matches[0]
    paper = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    result = publish_dynamic_scan(
        paper,
        paper_runtime_dir=args.paper_runtime_dir.resolve(),
        runtime_dir=args.runtime_dir.resolve(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
