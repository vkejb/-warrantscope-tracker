"""Temporary evidence only; no vendor/runtime constructor or real state access."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from trading_safety_audit_v01 import offline_readiness as checker


class OfflineReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="offline-readiness-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.runtime = self.repo / "stock-strategy" / "yuanta_live_runtime_v01" / "runtime"
        self.runtime.mkdir(parents=True)
        self.seals = self.root / "seals"
        self.seals.mkdir()
        self.plist = self.root / "service.plist"
        self.calendar = self.root / "calendar.csv"
        self.calendar.write_text("date\n2026-10-01\n2026-10-02\n2026-10-05\n", encoding="utf-8")
        self.target = date(2026, 10, 5)
        self.now = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)
        self.seal = {"schema_version": "1", "signal_date": "20261002", "setup": "FROZEN_STAGE_A_TOP30",
                     "mode": "SHADOW_ONLY", "stocks": [{"stock_id": str(1000 + index)} for index in range(30)],
                     "model_hash": "a" * 64, "model_spec_hash": "b" * 64, "config_hash": "c" * 64,
                     "input_hash": "d" * 64, "eligible_stock_count": 60}
        self.write_seal()
        self.baseline = {"3605|0": 1000}
        self.meta = {"version": 1, "trading_date": "2026-10-05", "captured_at": self.now.isoformat(),
                     "account_fingerprint": "0123456789ab"}
        self.heartbeat = {"at": self.now.isoformat(), "pid": 1234, "state": "RUNNING", "submit_live": True}
        self.write_runtime()
        self.write_plist()
        self.database = self.runtime / "live-orders.sqlite"

    def write_seal(self, day="20261002"):
        content = {key: self.seal[key] for key in checker.SEAL_FIELDS}
        self.seal["seal_hash"] = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                                        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        (self.seals / f"{day}.json").write_text(json.dumps(self.seal), encoding="utf-8")

    def write_runtime(self):
        for filename, data in (("position_baseline.json", self.baseline),
                               ("position_baseline.meta.json", self.meta), ("heartbeat.json", self.heartbeat)):
            (self.runtime / filename).write_text(json.dumps(data), encoding="utf-8")

    def write_plist(self, cwd=None, runtime=None):
        self.plist.write_bytes(plistlib.dumps({
            "ProgramArguments": ["/python", "-m", "yuanta_live_runtime_v01.trading_bot_service", "serve",
                                 "--runtime-dir", str(runtime or self.runtime)],
            "WorkingDirectory": str(cwd or self.repo / "stock-strategy"),
            "EnvironmentVariables": {"SECRET_TOKEN": "PLIST_SECRET_MUST_NOT_APPEAR"}}))

    def make_store(self, *, halted=0, status=None, filled=0, side="BUY", kind="0"):
        with sqlite3.connect(self.database) as connection:
            connection.executescript("""
                CREATE TABLE live_control(singleton INTEGER,halted INTEGER,reason TEXT,next_identify INTEGER,updated_at TEXT);
                CREATE TABLE live_orders(client_order_id TEXT,intent_id TEXT,symbol TEXT,side TEXT,quantity INTEGER,
                    order_type TEXT,status TEXT,filled_quantity INTEGER,created_at TEXT,updated_at TEXT);
                CREATE TABLE live_fills(fill_id TEXT,client_order_id TEXT,quantity INTEGER,price TEXT,filled_at TEXT);
            """)
            connection.execute("INSERT INTO live_control VALUES(1,?,'PRIVATE_ACCOUNT_TOKEN',1,'INITIAL')", (halted,))
            if status is not None:
                connection.execute("INSERT INTO live_orders VALUES('PRIVATE_ORDER_ID','PRIVATE_INTENT_ID','3605',?,1000,?,?,?,'NOW','NOW')",
                                   (side, kind, status, filled))
                if filled:
                    connection.execute("INSERT INTO live_fills VALUES('PRIVATE_FILL_ID','PRIVATE_ORDER_ID',?,'73',?)",
                                       (filled, self.now.isoformat()))

    def report(self):
        return checker.build_report(repo_dir=self.repo, runtime_dir=self.runtime, plist=self.plist,
                                    calendar=self.calendar, seal_dir=self.seals, trading_date=self.target, now=self.now)

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def test_all_local_evidence_never_certifies_live(self):
        self.make_store()
        report = self.report()
        self.assertEqual(report["overall"], "NOT_LIVE_CERTIFIED")
        self.assertEqual(report["store"]["code"], "STORE_LOCAL_SNAPSHOT_NO_EXPOSURE_BROKER_UNVERIFIED")
        self.assertEqual(report["heartbeat"]["code"], "HEARTBEAT_FRESH_METADATA_ONLY")
        self.assertEqual(report["baseline"]["code"], "BASELINE_LOCAL_PROVENANCE_VALID_BROKER_UNVERIFIED")
        self.assertEqual(report["seal"]["code"], "SEAL_LOCAL_HASH_AND_DATE_VALID")

    def test_missing_inputs_not_created(self):
        before = self.snapshot()
        result = checker.inspect_store(self.runtime)
        self.assertEqual(result["code"], "STORE_MISSING")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(checker.inspect_heartbeat(self.root / "absent", self.now, 15)["code"], "HEARTBEAT_INPUT_MISSING")

    def test_corrupt_inputs_return_only_sanitized_codes(self):
        for filename in ("heartbeat.json", "position_baseline.json"):
            (self.runtime / filename).write_text("BROKEN PRIVATE_SECRET", encoding="utf-8")
        self.database.write_text("BROKEN PRIVATE_SECRET", encoding="utf-8")
        self.plist.write_text("BROKEN PRIVATE_SECRET", encoding="utf-8")
        report = self.report()
        self.assertNotIn("PRIVATE_SECRET", json.dumps(report))
        self.assertEqual(report["heartbeat"]["code"], "HEARTBEAT_INPUT_MALFORMED")
        self.assertEqual(report["store"]["code"], "STORE_CORRUPT_OR_UNREADABLE")
        self.assertEqual(report["plist"]["code"], "PLIST_MALFORMED")

    def test_json_duplicate_fields_and_nan_refused(self):
        for content in ('{"at":"PRIVATE_SECRET","at":"OTHER"}', '{"at":NaN}'):
            with self.subTest(content=content):
                (self.runtime / "heartbeat.json").write_text(content, encoding="utf-8")
                result = self.report()["heartbeat"]
                self.assertEqual(result["code"], "HEARTBEAT_INPUT_MALFORMED")
                self.assertNotIn("PRIVATE_SECRET", json.dumps(result))

    def test_deeply_nested_json_no_crash_or_private_echo(self):
        content = '{"PRIVATE_SECRET":' + "[" * 2000 + "0" + "]" * 2000 + "}"
        for path in (self.runtime / "heartbeat.json", self.runtime / "position_baseline.json",
                     self.seals / "20261002.json"):
            path.write_text(content, encoding="utf-8")
        report = self.report()
        for name in ("heartbeat", "baseline", "seal"):
            self.assertEqual(report[name]["code"], name.upper() + "_INPUT_MALFORMED")
        self.assertNotIn("PRIVATE_SECRET", json.dumps(report))

    def test_json_depth_limit_exact_boundary_and_string_brackets(self):
        path = self.runtime / "depth-evidence.json"
        path.write_text('{"key":' + "[" * 63 + "0" + "]" * 63 + "}", encoding="utf-8")
        self.assertIn("key", checker._json(path))
        path.write_text('{"key":' + "[" * 64 + "0" + "]" * 64 + "}", encoding="utf-8")
        with self.assertRaisesRegex(checker.EvidenceError, "^INPUT_MALFORMED$"):
            checker._json(path)
        text = '[{"escaped quote": "' + "[" * 1000 + '"\\\\\\"}'
        path.write_text(json.dumps({"text": text}), encoding="utf-8")
        self.assertEqual(checker._json(path)["text"], text)

    def test_utf8_bom_supported_foreign_encodings_safely_rejected(self):
        path = self.runtime / "encoding-evidence.json"
        content = '{"value":"PRIVATE_SECRET"}'
        path.write_bytes(content.encode("utf-8-sig"))
        self.assertEqual(checker._json(path), {"value": "PRIVATE_SECRET"})
        for encoding in ("utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                path.write_bytes(content.encode(encoding))
                with self.assertRaisesRegex(checker.EvidenceError, "^INPUT_MALFORMED$"):
                    checker._json(path)

    def test_calendar_previous_session_relationship_and_missing_target(self):
        result, previous = checker.inspect_calendar(self.calendar, self.target)
        self.assertEqual(previous, date(2026, 10, 2))
        self.assertEqual(result["expected_signal_date"], "2026-10-02")
        self.assertEqual(checker.inspect_calendar(self.calendar, date(2026, 10, 3))[0]["code"], "TARGET_NOT_IN_CALENDAR")
        self.calendar.write_text("date\n2026-10-02\n2026-10-01\n", encoding="utf-8")
        self.assertEqual(checker.inspect_calendar(self.calendar, self.target)[0]["code"], "CALENDAR_MALFORMED")

    def test_invalid_and_duplicate_seal(self):
        self.seal["stocks"][1]["stock_id"] = self.seal["stocks"][0]["stock_id"]
        self.write_seal()
        self.assertEqual(self.report()["seal"]["code"], "SEAL_IDENTITIES_INVALID")
        self.seal["seal_hash"] = "bad"
        (self.seals / "20261002.json").write_text(json.dumps(self.seal), encoding="utf-8")
        self.assertEqual(self.report()["seal"]["code"], "SEAL_HASH_INVALID")

    def test_seal_wrong_date_schema_count_and_calendar_unknown(self):
        self.seal["signal_date"] = "20261001"
        self.write_seal(day="20261001")
        (self.seals / "20261002.json").unlink()
        self.assertEqual(self.report()["seal"]["code"], "SEAL_PREVIOUS_SESSION_MISMATCH")
        self.assertEqual(checker.inspect_seal(self.seals, None)["code"], "SEAL_CALENDAR_UNVERIFIED")
        self.seal["mode"] = "PRODUCTION"
        self.write_seal(day="20261001")
        self.assertEqual(self.report()["seal"]["code"], "SEAL_SCHEMA_INVALID")
        self.seal["stocks"] = []
        self.write_seal(day="20261001")
        self.assertEqual(self.report()["seal"]["code"], "SEAL_COUNT_INVALID")

    def test_heartbeat_stale_future_naive_unknown_and_pid(self):
        for stamp, state, pid, expected in (
            ((self.now - timedelta(seconds=16)).isoformat(), "RUNNING", 1234, "HEARTBEAT_STALE"),
            ((self.now + timedelta(seconds=1)).isoformat(), "RUNNING", 1234, "HEARTBEAT_FUTURE"),
            ("2026-10-05T02:00:00", "RUNNING", 1234, "HEARTBEAT_MALFORMED"),
            (self.now.isoformat(), "PRIVATE_STATE", 1234, "HEARTBEAT_STATE_UNKNOWN"),
            (self.now.isoformat(), "RUNNING", -1, "HEARTBEAT_PID_INVALID"),
            (self.now.isoformat(), "RUNNING", True, "HEARTBEAT_PID_INVALID"),
            (self.now.isoformat(), "STOPPED_UNSAFE", 1234, "HEARTBEAT_STOPPED_METADATA")):
            with self.subTest(expected=expected):
                self.heartbeat.update(at=stamp, state=state, pid=pid)
                self.write_runtime()
                self.assertEqual(self.report()["heartbeat"]["code"], expected)

    def test_exact_heartbeat_stale_boundary_and_invalid_now(self):
        self.heartbeat["at"] = (self.now - timedelta(seconds=15)).isoformat()
        self.write_runtime()
        self.assertEqual(self.report()["heartbeat"]["code"], "HEARTBEAT_FRESH_METADATA_ONLY")
        with self.assertRaises(ValueError):
            checker.build_report(repo_dir=self.repo, runtime_dir=self.runtime, plist=self.plist,
                                 calendar=self.calendar, seal_dir=self.seals, trading_date=self.target,
                                 now=self.now.replace(tzinfo=None))

    def test_baseline_missing_meta_old_future_and_malformed(self):
        (self.runtime / "position_baseline.meta.json").unlink()
        self.assertEqual(self.report()["baseline"]["code"], "BASELINE_INPUT_MISSING")
        self.meta.update(trading_date="2026-10-02", captured_at="2026-10-02T10:00:00+08:00")
        self.write_runtime()
        self.assertEqual(self.report()["baseline"]["code"], "BASELINE_TARGET_DATE_MISMATCH")
        self.meta.update(trading_date="2026-10-05", captured_at=(self.now + timedelta(seconds=1)).isoformat())
        self.write_runtime()
        self.assertEqual(self.report()["baseline"]["code"], "BASELINE_CAPTURE_FUTURE")
        self.baseline["3605|0"] = 1.5
        self.write_runtime()
        self.assertEqual(self.report()["baseline"]["code"], "BASELINE_MALFORMED")

    def test_baseline_provenance_invalid_does_not_echo_fingerprint(self):
        self.meta["account_fingerprint"] = "PRIVATE_ACCOUNT_TOKEN"
        self.write_runtime()
        result = self.report()["baseline"]
        self.assertEqual(result["code"], "BASELINE_PROVENANCE_MALFORMED")
        self.assertNotIn("PRIVATE_ACCOUNT_TOKEN", json.dumps(result))

    def test_markers_presence_only_never_reads_secret_content(self):
        (self.runtime / "EMERGENCY_STOP").write_text("PRIVATE_ACCOUNT_TOKEN", encoding="utf-8")
        result = self.report()["markers"]
        self.assertEqual(result["code"], "PERSISTENT_MARKERS_PRESENT")
        self.assertTrue(result["markers"]["EMERGENCY_STOP"])
        self.assertNotIn("PRIVATE_ACCOUNT_TOKEN", json.dumps(result))

    def test_halt_exposure_and_open_orders_are_local_only(self):
        self.make_store(halted=1, status="PARTIALLY_FILLED", filled=500)
        result = self.report()["store"]
        self.assertEqual(result["code"], "STORE_LOCAL_BLOCKERS_PRESENT")
        self.assertTrue(result["halted"])
        self.assertTrue(result["has_local_exposure"])
        self.assertEqual(result["open_order_count"], 1)
        text = json.dumps(result)
        for private in ("3605", "500", "PRIVATE_ACCOUNT_TOKEN", "PRIVATE_ORDER_ID", "PRIVATE_FILL_ID"):
            self.assertNotIn(private, text)

    def test_unknown_order_is_blocker_and_unrecognized_state_unverified(self):
        self.make_store(status="UNKNOWN")
        self.assertEqual(self.report()["store"]["unknown_order_count"], 1)
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE live_orders SET status='PRIVATE_INVALID_STATE'")
        result = self.report()["store"]
        self.assertEqual(result["code"], "STORE_ORDER_DATA_INVALID")
        self.assertNotIn("PRIVATE_INVALID_STATE", json.dumps(result))

    def test_mismatched_fill_totals_and_schema_refused(self):
        self.make_store(status="FILLED", filled=1000)
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE live_fills SET quantity=500")
        self.assertEqual(self.report()["store"]["code"], "STORE_FILL_TOTAL_MISMATCH")
        with sqlite3.connect(self.database) as connection:
            connection.execute("DROP TABLE live_fills")
        self.assertEqual(self.report()["store"]["code"], "STORE_SCHEMA_UNRECOGNIZED")

    def test_financing_buckets_never_cancel_out_exposure(self):
        self.make_store(status="FILLED", filled=1000)
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO live_orders VALUES('SECOND','SECOND','3605','SELL',1000,'4','FILLED',1000,'NOW','NOW')")
            connection.execute("INSERT INTO live_fills VALUES('SECOND','SECOND',1000,'73',?)", (self.now.isoformat(),))
        self.assertTrue(self.report()["store"]["has_local_exposure"])

    def test_nonfinite_fill_or_illegal_quantity_cannot_appear_safe(self):
        self.make_store(status="FILLED", filled=1000)
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE live_fills SET price='NaN'")
        self.assertEqual(self.report()["store"]["code"], "STORE_FILL_DATA_INVALID")
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE live_orders SET filled_quantity=1001")
        self.assertEqual(self.report()["store"]["code"], "STORE_ORDER_DATA_INVALID")

    def test_impossible_filled_state_cannot_be_called_local_flat(self):
        self.make_store(status="FILLED", filled=0)
        self.assertEqual(self.report()["store"]["code"], "STORE_ORDER_DATA_INVALID")

    def test_nonempty_wal_and_journal_never_connect_or_claim_flat(self):
        self.make_store()
        for suffix, expected in (("-wal", "STORE_WAL_PRESENT_UNVERIFIED"),
                                 ("-journal", "STORE_JOURNAL_PRESENT_UNVERIFIED")):
            with self.subTest(suffix=suffix):
                sidecar = Path(str(self.database) + suffix)
                sidecar.write_bytes(b"PRIVATE_SQLITE_CONTENT")
                before = self.snapshot()
                with patch.object(checker.sqlite3, "connect", side_effect=AssertionError("Must not query WAL main file")):
                    result = self.report()["store"]
                self.assertEqual(result["code"], expected)
                self.assertIsNone(result.get("has_local_exposure"))
                self.assertEqual(before, self.snapshot())
                sidecar.unlink()

    def test_existing_shm_and_zero_wal_are_not_modified(self):
        self.make_store()
        Path(str(self.database) + "-shm").write_bytes(b"SHM_EVIDENCE")
        Path(str(self.database) + "-wal").write_bytes(b"")
        before = self.snapshot()
        self.report()
        self.assertEqual(before, self.snapshot())

    def test_real_wal_pending_writes_are_never_inferred_flat(self):
        self.make_store()
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE live_control SET halted=1")
        connection.commit()
        before = self.snapshot()
        result = self.report()["store"]
        self.assertEqual(result["code"], "STORE_WAL_PRESENT_UNVERIFIED")
        self.assertIsNone(result["halted"])
        self.assertEqual(before, self.snapshot())

    def test_original_sqlite_connection_never_opened_and_runtime_unchanged(self):
        self.make_store()
        before = self.snapshot()
        connect = checker.sqlite3.connect
        observed = []

        def inspect_connection(path, **kwargs):
            self.assertNotIn(str(self.runtime), str(path))
            self.assertTrue(str(path).endswith("?mode=ro&immutable=1"))
            observed.append(path)
            return connect(path, **kwargs)

        with patch.object(checker.sqlite3, "connect", side_effect=inspect_connection):
            self.report()
        self.assertEqual(len(observed), 1)
        self.assertEqual(before, self.snapshot())

    def test_ambient_tempdir_cannot_write_snapshot_inside_runtime(self):
        self.make_store()
        before = self.snapshot()
        factory = tempfile.TemporaryDirectory
        observed = []

        def isolated_temporary_directory(*args, **kwargs):
            self.assertEqual(Path(kwargs["dir"]).resolve(), Path("/tmp").resolve())
            instance = factory(*args, **kwargs)
            self.assertFalse(Path(instance.name).resolve().is_relative_to(self.runtime.resolve()))
            observed.append(instance.name)
            return instance

        with patch.dict(os.environ, {"TMPDIR": str(self.runtime)}), \
                patch.object(checker.tempfile, "gettempdir", return_value=str(self.runtime)), \
                patch.object(checker.tempfile, "TemporaryDirectory", side_effect=isolated_temporary_directory):
            result = self.report()["store"]
        self.assertEqual(result["code"], "STORE_LOCAL_SNAPSHOT_NO_EXPOSURE_BROKER_UNVERIFIED")
        self.assertEqual(len(observed), 1)
        self.assertEqual(before, self.snapshot())

    def test_changed_original_store_rejects_snapshot(self):
        self.make_store()
        original = checker._private_store_summary

        def mutate_private_test_evidence(connection):
            result = original(connection)
            Path(str(self.database) + "-wal").write_bytes(b"MOCK_CONCURRENT_WRITER")
            return result

        with patch.object(checker, "_private_store_summary", side_effect=mutate_private_test_evidence):
            self.assertEqual(self.report()["store"]["code"], "STORE_CHANGED_DURING_INSPECTION_UNVERIFIED")

    def test_plist_source_runtime_split_diagnosed_without_sync(self):
        alias = self.root / "another-checkout" / "stock-strategy" / "yuanta_live_runtime_v01" / "runtime"
        self.write_plist(runtime=alias)
        result = checker.inspect_plist(self.plist, self.repo, alias)
        self.assertTrue(result["source_matches_requested_repo"])
        self.assertTrue(result["runtime_matches_requested_path"])
        self.assertTrue(result["source_and_runtime_different_checkout"])
        self.assertEqual(result["service_running"], "UNVERIFIED")
        self.assertFalse(alias.exists())

    def test_plist_path_symlink_loop_is_sanitized_not_traceback(self):
        loop = self.root / "PRIVATE_SECRET_PATH_LOOP"
        loop.symlink_to(loop)
        self.write_plist(cwd=loop)
        result = checker.inspect_plist(self.plist, self.repo, self.runtime)
        self.assertEqual(result["code"], "PLIST_MALFORMED")
        self.assertNotIn("PRIVATE_SECRET_PATH_LOOP", json.dumps(result))

    def test_symlink_artifact_refused_and_no_account_symbol_or_path_echo(self):
        self.make_store(halted=1, status="FILLED", filled=1000)
        (self.runtime / "heartbeat.json").unlink()
        (self.runtime / "heartbeat.json").symlink_to(self.plist)
        text = json.dumps(self.report())
        for private in ("PRIVATE", "3605", "1000", str(self.root), "0123456789ab", "PLIST_SECRET"):
            self.assertNotIn(private, text)
        self.assertEqual(self.report()["heartbeat"]["code"], "HEARTBEAT_INPUT_UNSAFE_OR_TOO_LARGE")

    def test_cli_always_nonzero_and_configuration_error_sanitized(self):
        args = ["--repo-dir", str(self.repo), "--runtime-dir", str(self.runtime), "--plist", str(self.plist),
                "--calendar", str(self.calendar), "--seal-dir", str(self.seals), "--trading-date", "2026-10-05"]
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(checker.main(args), 3)
        self.assertEqual(json.loads(output.getvalue())["overall"], "NOT_LIVE_CERTIFIED")
        args[-1] = "PRIVATE_BAD_DATE"
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(checker.main(args), 2)
        self.assertNotIn("PRIVATE_BAD_DATE", output.getvalue())


if __name__ == "__main__":
    unittest.main()
