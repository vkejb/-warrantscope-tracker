from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from shadow_daily_runner.live_data_sync import (
    configured_targets,
    sync_sealed_day,
)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class LiveDataSyncTests(unittest.TestCase):
    def _fixture(self, root: Path, target: str = "20260929") -> Path:
        strategy = root / "source"
        content = {
            "schema_version": "1",
            "signal_date": target,
            "setup": "FROZEN_STAGE_A_TOP30",
            "mode": "SHADOW_ONLY",
            "stocks": [
                {"rank": index, "stock_id": str(1000 + index), "stock_name": "測試", "score": 0.1}
                for index in range(1, 31)
            ],
            "model_hash": "model",
            "model_spec_hash": "spec",
            "config_hash": "config",
            "input_hash": "input",
            "eligible_stock_count": 30,
        }
        seal = {
            **content,
            "seal_hash": hashlib.sha256(canonical_bytes(content)).hexdigest(),
            "status": "SEALED",
        }
        seal_path = strategy / "stage_a_prospective_watchlist_v01/runtime/seals" / f"{target}.json"
        seal_path.parent.mkdir(parents=True)
        seal_path.write_text(json.dumps(seal, ensure_ascii=False), encoding="utf-8")

        sources = []
        for market, suffix in (("twse", "json"), ("tpex", "csv")):
            payload = f"{market}-{target}".encode()
            digest = hashlib.sha256(payload).hexdigest()
            path = strategy / f"shadow_daily_runner/runtime/raw/{market}_eod_{target}/{digest}.{suffix}"
            path.parent.mkdir(parents=True)
            path.write_bytes(payload)
            sources.append(
                {
                    "source": f"{market}_eod_{target}",
                    "response_date": target,
                    "path": str(path),
                    "sha256": digest,
                    "request_url": "https://example.invalid",
                    "retrieved_at_utc": "2026-09-29T08:00:00Z",
                }
            )
        audit_path = strategy / "shadow_daily_runner/runtime/audit" / f"official_eod_through_{target}.json"
        audit_path.parent.mkdir(parents=True)
        audit_path.write_text(json.dumps({"archives": [{"sources": sources}]}), encoding="utf-8")
        runtime = strategy / "shadow_daily_runner/runtime"
        (runtime / "trading_calendar.csv").write_text("date\n2026-09-29\n2026-09-30\n", encoding="utf-8")
        (runtime / "trading_calendar.metadata.json").write_text("{}\n", encoding="utf-8")
        return strategy

    def test_syncs_seal_audit_sources_and_calendar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._fixture(root)
            destination = root / "destination"
            result = sync_sealed_day(source, destination, "20260929")
            self.assertEqual("SYNCED", result["status"])
            self.assertEqual(2, len(result["official_sources"]))
            seal = destination / "stage_a_prospective_watchlist_v01/runtime/seals/20260929.json"
            self.assertTrue(seal.is_file())
            audit = json.loads(
                (destination / "shadow_daily_runner/runtime/audit/official_eod_through_20260929.json").read_text()
            )
            target_sources = audit["archives"][0]["sources"]
            self.assertTrue(all(str(destination) in row["path"] for row in target_sources))
            self.assertTrue(all(Path(row["path"]).is_file() for row in target_sources))
            self.assertTrue((destination / "shadow_daily_runner/runtime/trading_calendar.csv").is_file())

    def test_refuses_to_replace_different_immutable_seal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._fixture(root)
            destination = root / "destination"
            seal = destination / "stage_a_prospective_watchlist_v01/runtime/seals/20260929.json"
            seal.parent.mkdir(parents=True)
            seal.write_text("different", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
                sync_sealed_day(source, destination, "20260929")

    def test_reads_unique_configured_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            target = runtime / "target"
            (runtime / "live_data_sync_targets.json").write_text(
                json.dumps({"schema_version": "1", "targets": [str(target)]}),
                encoding="utf-8",
            )
            self.assertEqual([target.resolve()], configured_targets(runtime))


if __name__ == "__main__":
    unittest.main()
