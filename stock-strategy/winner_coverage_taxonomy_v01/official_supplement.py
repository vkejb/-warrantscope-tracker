from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile

from shadow_daily_runner.normalize import COLUMNS, parse_twse
from shadow_daily_runner.sources import SourceSnapshot


TWSE_MI_INDEX = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_twse_supplement(
    inputs: list[tuple[str, Path]], output: Path, metadata_path: Path
) -> dict:
    """Parse immutable official snapshots into the established OHLCV schema."""

    rows: list[dict[str, str]] = []
    sources: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for day, path in sorted(inputs):
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        retrieved = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        url = f"{TWSE_MI_INDEX}?date={day}&type=ALLBUT0999&response=json"
        snapshot = SourceSnapshot(
            source="twse_mi_index",
            request_url=url,
            retrieved_at_utc=retrieved,
            sha256=digest,
            path=path,
            payload=payload,
        )
        parsed = parse_twse(snapshot, day)
        if parsed.errors:
            raise RuntimeError(f"unresolved TWSE rows for {day}: {parsed.errors[:3]}")
        for row in parsed.rows:
            key = (row["date"], row["code"])
            if key in seen:
                raise RuntimeError(f"duplicate official row: {key}")
            seen.add(key)
            rows.append(dict(row))
        sources.append(
            {
                **parsed.source_metadata,
                "valid_price_rows": len(parsed.rows),
                "resolved_nontradable_rows": len(parsed.excluded),
                "unresolved_rows": len(parsed.errors),
            }
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=output.parent, delete=False
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda row: (row["date"], row["code"])))
        temporary = Path(handle.name)
    temporary.replace(output)

    metadata = {
        "artifact": str(output),
        "artifact_sha256": _sha256(output),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "schema": list(COLUMNS),
        "row_count": len(rows),
        "duplicate_code_date": 0,
        "unresolved_invalid_rows": 0,
        "sources": sources,
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", required=True, metavar="DATE=PATH")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()
    pairs: list[tuple[str, Path]] = []
    for item in args.input:
        day, separator, path = item.partition("=")
        if not separator or len(day) != 8 or not day.isdigit():
            raise SystemExit(f"invalid --input {item!r}; expected YYYYMMDD=PATH")
        pairs.append((day, Path(path)))
    metadata = build_twse_supplement(pairs, args.output, args.metadata)
    print(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
