from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from shadow_daily_runner.calendar import build_trading_calendar
from shadow_daily_runner.config import RunnerConfig
from shadow_daily_runner.sources import OfficialSourceClient, SourceSnapshot


class CalendarYearFallbackTests(unittest.TestCase):
    def test_wrong_year_never_turns_holidays_into_sessions(self):
        fresh = b'[{"Name":"next","Date":"1160101","Description":"holiday"}]'
        snapshot = SourceSnapshot("twse_calendar", "official", "now", hashlib.sha256(fresh).hexdigest(), Path("fresh"), fresh)
        with self.assertRaisesRegex(RuntimeError, "no rows for 2026"):
            build_trading_calendar(snapshot, 2026)

    def test_exact_request_hashed_2026_cache_used_when_endpoint_switches_to_2027(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = RunnerConfig(runtime_dir=Path(directory))
            source_dir = cfg.raw_dir / "twse_calendar"
            source_dir.mkdir(parents=True)
            old = b'[{"Name":"holiday","Date":"1150101","Description":"closed"}]'
            digest = hashlib.sha256(old).hexdigest()
            (source_dir / f"{digest}.json").write_bytes(old)
            (source_dir / f"{digest}.metadata.json").write_text(json.dumps({
                "source": "twse_calendar", "request_url": cfg.twse_calendar_url,
                "retrieved_at_utc": "2026-09-08T00:39:12Z", "sha256": digest,
                "bytes": len(old),
            }))
            fresh = b'[{"Name":"next","Date":"1160101","Description":"holiday"}]'
            client = OfficialSourceClient(cfg)
            with patch("shadow_daily_runner.sources.subprocess.run", return_value=subprocess.CompletedProcess([], 0, fresh, b"")):
                selected = client.calendar(year=2026)
            self.assertEqual(selected.sha256, digest)
            self.assertEqual(len(build_trading_calendar(selected, 2026)[1]["official_schedule_rows"]), 1)


if __name__ == "__main__":
    unittest.main()
