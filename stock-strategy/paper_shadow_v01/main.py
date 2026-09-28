from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runner import DEFAULT_RUNTIME_DIR, publish_paper_day


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish one read-only paper shadow day")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--session-manifest", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--capital", type=int, default=190_000)
    args = parser.parse_args()
    session = json.loads(args.session_manifest.read_text(encoding="utf-8"))
    result = publish_paper_day(
        args.run_dir, session,
        runtime_dir=args.runtime_dir,
        capital_twd=args.capital,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
