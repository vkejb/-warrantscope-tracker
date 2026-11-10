from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from yuanta_intraday_shadow_v01.collector import DEFAULT_RUNTIME_DIR, canonical_bytes
from yuanta_intraday_shadow_v01.session_analysis import publish_session_analysis

from .prospective_v2 import DEFAULT_PROSPECTIVE_RUNTIME_DIR, publish_prospective_v2


def load_or_publish_session(run_dir: Path, collector_runtime_dir: Path) -> dict:
    """Reuse a sealed session analysis without repeating notifications."""
    source = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    session_dir = collector_runtime_dir / "session_analyses" / str(source["run_id"])
    session_path = session_dir / "session_manifest.json"
    if not session_path.exists():
        session_dir = publish_session_analysis(
            run_dir, collector_runtime_dir / "session_analyses"
        )
        session_path = session_dir / "session_manifest.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in session.items() if key != "analysis_hash"}
    if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != session.get("analysis_hash"):
        raise RuntimeError(f"session analysis hash mismatch: {session_path}")
    if session.get("source_run_id") != source.get("run_id"):
        raise RuntimeError(f"session analysis source run mismatch: {session_path}")
    if session.get("source_manifest_hash") != source.get("manifest_hash"):
        raise RuntimeError(f"session analysis source manifest mismatch: {session_path}")
    return session


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish one new-date prospective V2 day")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--runtime-dir", type=Path,
                        default=DEFAULT_PROSPECTIVE_RUNTIME_DIR)
    parser.add_argument(
        "--collector-runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR,
        help="collector runtime containing immutable session_analyses",
    )
    args = parser.parse_args()
    session = load_or_publish_session(args.run_dir, args.collector_runtime_dir)
    result = publish_prospective_v2(args.run_dir, session,
                                    runtime_dir=args.runtime_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
