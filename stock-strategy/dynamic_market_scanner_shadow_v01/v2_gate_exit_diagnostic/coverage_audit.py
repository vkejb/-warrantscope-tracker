"""Read-only 0050 archive coverage diagnostics for the frozen V2 cohort."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from datetime import datetime, timedelta
import gzip
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from dynamic_market_scanner_shadow_v01.strict_replay import _write_csv
from .cohort_replay import audit_source, sources


TZ = ZoneInfo("Asia/Taipei")


def _channel(path: Path) -> list[datetime]:
    stamps = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if str(row.get("stock_id")) == "0050" and row.get("received_at"):
                stamps.append(datetime.fromisoformat(
                    str(row["received_at"]).replace("Z", "+00:00")
                ).astimezone(TZ))
    return sorted(stamps)


def inspect(day: str, run_dir: Path) -> dict:
    audit, manifest = audit_source(day, run_dir)
    if manifest is None:
        return {"day": day, "status": "NOT_REPLAYABLE", "reason": audit.get("reason")}
    tick = _channel(run_dir / "market_context_ticks.jsonl.gz")
    book = _channel(run_dir / "market_context_books.jsonl.gz")
    combined = sorted(tick + book)
    start = datetime.strptime(day, "%Y%m%d").replace(tzinfo=TZ, hour=9)
    grid = [start + timedelta(minutes=i) for i in range(271)]
    fresh_10s = 0
    missing = 0
    for target in grid:
        index = bisect_right(combined, target) - 1
        if index < 0:
            missing += 1
        elif (target - combined[index]).total_seconds() <= 10:
            fresh_10s += 1
    return {"day": day, "status": "ARCHIVED_0050_COVERAGE_DIAGNOSTIC",
            "tick_rows_0050": len(tick), "book_rows_0050": len(book),
            "first_tick_received_at": tick[0].isoformat() if tick else None,
            "last_tick_received_at": tick[-1].isoformat() if tick else None,
            "first_book_received_at": book[0].isoformat() if book else None,
            "last_book_received_at": book[-1].isoformat() if book else None,
            "minute_grid_09_00_to_13_30": len(grid),
            "grid_fresh_10s": fresh_10s, "grid_no_prior_quote": missing,
            "grid_stale_or_no_prior": len(grid) - fresh_10s,
            "interpretation": "freshness_sample_not_disconnect_proof"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    rows = [inspect(day, path) for day, path in sources().items()]
    _write_csv(args.output, rows)
    print(json.dumps({"days": len(rows), "total_fresh_10s": sum(
        row.get("grid_fresh_10s", 0) for row in rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
