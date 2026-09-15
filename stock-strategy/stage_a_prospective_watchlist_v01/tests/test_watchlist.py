from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np

from winner_coverage_taxonomy_v01.config import FEATURE_NAMES as TAXONOMY_FEATURE_NAMES

from stage_a_prospective_watchlist_v01.watchlist import build_watchlist, frozen_model, seal_current


class WatchlistTests(unittest.TestCase):
    def test_published_model_exact(self):
        model, spec_hash = frozen_model()
        self.assertEqual(model.fingerprint(), "a943b58962b0e19e2830a700cd7ed1d32a8dc338be79652126e13be82b114ccd")
        self.assertEqual(model.regularization_alpha, 0.1)
        self.assertEqual(spec_hash, "95c56676f79a98fb8a48324d2777294f727f76327c815674b96c29810d9d057d")

    def test_snapshot_must_be_truncated_at_T(self):
        snapshot = type("Snapshot", (), {"data_through_date": "20260917"})()
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            build_watchlist(snapshot, "20260916")

    def test_top30_uses_frozen_feature_mapping_and_same_day_rank(self):
        contexts = []
        for code in range(1001, 1033):
            observation = type("Observation", (), {"code": code, "name": f"Stock {code}"})()
            contexts.append((observation, object(), 60))
        snapshot = type("Snapshot", (), {"data_through_date": "20260916", "prepared_stocks": [], "benchmark": object(), "input_manifest_hash": "input"})()
        return3_column = TAXONOMY_FEATURE_NAMES.index("return_3")
        def vector(_stock, index, _benchmark):
            values = [0.0] * len(TAXONOMY_FEATURE_NAMES)
            values[return3_column] = float(len(calls) + 1)
            calls.append(index)
            return tuple(values)
        calls = []
        model = type("Model", (), {"predict": lambda self, transformed: np.asarray(transformed[:, 0], dtype=float), "fingerprint": lambda self: "frozen"})()
        with patch("stage_a_prospective_watchlist_v01.watchlist.iter_signal_dates", return_value=[("20260916", 0, contexts)]), patch("stage_a_prospective_watchlist_v01.watchlist.build_taxonomy_features", side_effect=vector), patch("stage_a_prospective_watchlist_v01.watchlist.frozen_model", return_value=(model, "spec")):
            result = build_watchlist(snapshot, "20260916")
        self.assertEqual(len(result["stocks"]), 30)
        self.assertEqual(result["stocks"][0]["stock_id"], "1032")
        self.assertEqual(result["stocks"][-1]["stock_id"], "1003")
        self.assertEqual(result["eligible_stock_count"], 32)

    def test_no_backfill_or_before_activation(self):
        old = datetime(2026, 9, 15, 15, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        with self.assertRaisesRegex(RuntimeError, "activation"):
            seal_current([Path("unused")], Path("unused"), now=old)

    def test_exclusive_seal_rerun_never_mutates(self):
        now = datetime(2026, 9, 16, 15, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        content = {"schema_version": "1", "signal_date": "20260916", "setup": "FROZEN_STAGE_A_TOP30", "mode": "SHADOW_ONLY", "stocks": [{"rank": i, "stock_id": str(i), "stock_name": "測試", "score": 0.1} for i in range(1, 31)], "model_hash": "m", "model_spec_hash": "s", "config_hash": "c", "input_hash": "i", "eligible_stock_count": 40}
        fake_provider = type("Provider", (), {"load_through": lambda self, date: type("Snapshot", (), {"input_manifest_hash": "i"})()})
        with tempfile.TemporaryDirectory() as directory, patch("stage_a_prospective_watchlist_v01.watchlist.ExistingDailyDataProvider", return_value=fake_provider()), patch("stage_a_prospective_watchlist_v01.watchlist.build_watchlist", return_value=content):
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "manifest differs"):
                seal_current([Path("unused")], Path("unused"), now=now, runtime_dir=root, expected_input_hash="wrong")
            self.assertFalse((root / "seals" / "20260916.json").exists())
            first = seal_current([Path("unused")], Path("unused"), now=now, runtime_dir=root, expected_input_hash="i")
            path = root / "seals" / "20260916.json"
            before = path.read_bytes()
            second = seal_current([Path("unused")], Path("unused"), now=now, runtime_dir=root, expected_input_hash="i")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(second["status"], "ALREADY_SEALED")
            self.assertEqual(first["seal_hash"], second["seal_hash"])
            content["input_hash"] = "drift"
            with self.assertRaisesRegex(RuntimeError, "conflicts"):
                seal_current([Path("unused")], Path("unused"), now=now, runtime_dir=root, expected_input_hash="i")
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
