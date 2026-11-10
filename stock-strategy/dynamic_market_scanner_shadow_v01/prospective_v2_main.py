from __future__ import annotations

import argparse
import json
from pathlib import Path

from yuanta_intraday_shadow_v01.postprocess import process_run

from .prospective_v2 import DEFAULT_PROSPECTIVE_RUNTIME_DIR, publish_prospective_v2


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish one new-date prospective V2 day")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--runtime-dir", type=Path,
                        default=DEFAULT_PROSPECTIVE_RUNTIME_DIR)
    args = parser.parse_args()
    session = process_run(args.run_dir)["session"]
    result = publish_prospective_v2(args.run_dir, session,
                                    runtime_dir=args.runtime_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
