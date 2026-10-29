import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr

from cash_short_intraday_research_v01.evidence_followup_v01.evidence_audit import (
    build_evidence_rows, extract_sdk_version_candidates, fee_db_aggregate, main,
    quantity_provenance, scan_stream, sdk_evidence_description,
    timestamp_provenance, write_report,
)


class QuantityProvenanceTests(unittest.TestCase):
    def test_unknown_never_converts(self):
        x = quantity_provenance("123")
        self.assertEqual(x["raw_value"], "123")
        self.assertIsNone(x["converted_value"])
        self.assertFalse(x["conversion_applied"])

    def test_scenario_not_certified(self):
        x = quantity_provenance("12", multiplier=1000)
        self.assertEqual(x["scenario_value"], "12000")
        self.assertIsNone(x["converted_value"])
        self.assertFalse(x["conversion_applied"])

    def test_certified_explicit_lots(self):
        x = quantity_provenance("2", raw_unit="BOARD_LOTS", certified=True,
                                basis="OFFICIAL_VERSIONED_DEFINITION", basis_sha256="a" * 64)
        self.assertEqual(x["converted_value"], "2000")
        self.assertEqual(x["conversion_multiplier"], "1000")

    def test_certified_shares_not_multiplied_again(self):
        x = quantity_provenance("2000", raw_unit="SHARES", certified=True,
                                basis="OFFICIAL_VERSIONED_DEFINITION", basis_sha256="b" * 64)
        self.assertEqual(x["converted_value"], "2000")
        with self.assertRaisesRegex(ValueError, "DOUBLE_CONVERSION"):
            quantity_provenance(x, multiplier=1000)

    def test_conflicting_multiplier_rejected(self):
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            quantity_provenance("2000", raw_unit="SHARES", multiplier=1000,
                                certified=True, basis="x", basis_sha256="b" * 64)

    def test_no_certification_from_just_multiplier(self):
        with self.assertRaises(ValueError):
            quantity_provenance("2", certified=True, multiplier=1000)

    def test_certification_requires_basis(self):
        with self.assertRaisesRegex(ValueError, "BASIS"):
            quantity_provenance("2", raw_unit="BOARD_LOTS", certified=True)
        with self.assertRaisesRegex(ValueError, "BASIS"):
            quantity_provenance("2", raw_unit="BOARD_LOTS", certified=True,
                                basis="x", basis_sha256="z" * 64)

    def test_invalid_raw_rejected(self):
        for x in (None, True, "NaN", "Infinity", -1, {}, []):
            with self.subTest(x=x), self.assertRaises(ValueError):
                quantity_provenance(x)


class TimeSchemaTests(unittest.TestCase):
    def test_receipt_is_not_exchange_or_write(self):
        x = timestamp_provenance({"received_at": "2026-10-29T01:00:00Z"})
        self.assertIsNone(x["exchange_time_raw"])
        self.assertIsNone(x["write_at"])
        self.assertFalse(x["transport_minus_exchange_seconds_is_order_latency"])

    def test_sequences_separate(self):
        x = timestamp_provenance({"serial_no": 4, "ingest_sequence": 101})
        self.assertEqual(x["serial_no"], 4)
        self.assertEqual(x["ingest_sequence"], 101)
        self.assertIn("NOT_CROSS_CHANNEL", x["serial_scope"])

    def test_scanner_preserves_unknown_and_no_outage_claim(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ticks.jsonl.gz"
            rows = [
                {"received_at": "2026-10-29T01:00:01Z", "stock_id": "1234", "quote_time": "09:00:01.000", "serial_no": 1, "in_out_flag": "0", "deal_volume": "100"},
                {"received_at": "2026-10-29T01:00:02Z", "stock_id": "1234", "quote_time": "09:00:02.000", "serial_no": 3, "in_out_flag": "0", "deal_volume": "2"},
            ]
            with gzip.open(p, "wt") as f:
                f.write("\n".join(json.dumps(x) for x in rows) + "\n")
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
            result = scan_stream(p, day="20261029", role="ticks", expected_sha256=sha)
            self.assertEqual(result["native_volume_message_changes"]["down"], 1)
            self.assertEqual(result["tick_serial_steps_per_symbol_original_file_order"]["gap"], 1)
            self.assertFalse(result["serial_gaps_prove_network_outage"])
            self.assertFalse(result["decreasing_native_volume_proves_single_execution_quantity"])
            self.assertEqual(result["unit_certification"], "UNKNOWN")
            self.assertNotIn("ingest_sequence", result["field_present_counts"])
            with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
                scan_stream(p, day="20261029", role="ticks", expected_sha256="0" * 64)

    def test_evidence_does_not_promote_strict_or_change_model(self):
        rows = build_evidence_rows({"x": {}})
        self.assertTrue(all(not x["model_parameter_change_supported"] for x in rows))
        self.assertEqual(rows[-1]["confirmation_level"], "NA_UNCHANGED")


class DynamicSdkAndReportTests(unittest.TestCase):
    def test_candidate_extraction_accepts_synthetic_future_release(self):
        text = "noise 8.0.0.0\nFileVersion: 3.2031.1224.7\n2031.12.24.99123\n0.0015\n3.2031.1224.7"
        self.assertEqual(extract_sdk_version_candidates(text),
                         ["2031.12.24.99123", "3.2031.1224.7"])

    def test_missing_version_stays_unknown(self):
        self.assertEqual(extract_sdk_version_candidates("normal text 8.0.0.0"), [])
        description = sdk_evidence_description({})
        self.assertIn("deps宣告UNKNOWN", description)
        self.assertIn("版本候選UNKNOWN", description)

    def test_a07_uses_observed_metadata_not_one_sdk_release(self):
        sources = {
            "sdk_deps": {"declared_package_versions": ["YuantaSparkAPI/2031.12.24.99123"]},
            "sdk_dll": {"visible_version_candidates": ["3.2031.1224.7"]},
            "source_manifests": {"count": 2, "sdk_version_field_present_count": 1,
                                  "collector_source_hash_field_present_count": 0},
        }
        row = next(x for x in build_evidence_rows(sources) if x["item_id"] == "A07_SDK_VERSION")
        self.assertIn("2031.12.24.99123", row["actual_evidence"])
        self.assertIn("3.2031.1224.7", row["actual_evidence"])
        self.assertIn("2份manifest", row["actual_evidence"])
        self.assertIn("欄存在1份", row["actual_evidence"])

    def test_report_day_stream_counts_and_sdk_come_from_synthetic_payload(self):
        daily = []
        for day in ("20311223", "20311224"):
            daily.append({"day": day, "tick_rows": 4, "book_rows": 6,
                          "book_explicit_exchange_timestamp_present": 0, "book_quote_time_present": 0,
                          "tick_ingest_sequence_present": 0, "book_ingest_sequence_present": 0,
                          "callback_received_at_present": 0, "explicit_write_timestamp_present": 0,
                          "raw_unit_metadata_present": 0})
        payload = {
            "evidence_daily_coverage": daily,
            "raw_stream_schema_audit": [{"source_hash_matches_round2_precheck": True} for _ in range(3)],
            "source_manifest_schema_audit": [{"sdk_version_field_present": False,
                                              "collector_source_hash_field_present": False}],
            "sources": {"sdk_deps": {"declared_package_versions": ["YuantaSparkAPI/2031.12.24.99123"]},
                        "sdk_dll": {"visible_version_candidates": ["3.2031.1224.7"]}},
            "evidence_audit": [],
        }
        with tempfile.TemporaryDirectory() as d:
            write_report(payload, d)
            report = (Path(d) / "EVIDENCE_AUDIT.md").read_text()
        self.assertIn("2日、3個凍結原始串流", report)
        self.assertIn("ticks 8筆，五檔 12筆", report)
        self.assertIn("相符3/3個", report)
        self.assertIn("2031.12.24.99123", report)
        self.assertIn("3.2031.1224.7", report)
        self.assertIn("1份manifest", report)


class ReadOnlyFeeTests(unittest.TestCase):
    def test_augment_requires_explicit_private_launchagent_input(self):
        with patch("cash_short_intraday_research_v01.evidence_followup_v01.evidence_audit.augment_existing_fee_evidence") as augment:
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                main(["--root", "/tmp/synthetic-project", "--output", "/tmp/synthetic-output", "--augment-fee-existing"])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("requires --fee-launchagent PATH", err.getvalue())
            augment.assert_not_called()

    def test_augment_passes_explicit_private_path_without_reading_it(self):
        with patch("cash_short_intraday_research_v01.evidence_followup_v01.evidence_audit.augment_existing_fee_evidence") as augment:
            main(["--root", "/tmp/synthetic-project", "--output", "/tmp/synthetic-output", "--augment-fee-existing", "--fee-launchagent", "/tmp/synthetic-service.plist"])
            augment.assert_called_once_with(Path("/tmp/synthetic-project"), Path("/tmp/synthetic-output"),
                                            fee_launchagent=Path("/tmp/synthetic-service.plist"))

    def test_reads_committed_wal_without_exporting_payload(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.sqlite"
            writer = sqlite3.connect(p)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE live_fills(fill_id TEXT,price TEXT)")
            writer.execute("CREATE TABLE live_events(payload TEXT)")
            writer.execute("INSERT INTO live_fills VALUES('PRIVATE_ORDER_NO','99')")
            writer.execute("INSERT INTO live_events VALUES(?)", (json.dumps({"account": "PRIVATE_ACCOUNT", "nested": {"fees": 17, "tax": 10}}),))
            writer.commit()
            self.assertTrue(Path(str(p) + "-wal").exists())
            result = fee_db_aggregate(p)
            self.assertEqual(result["record_counts"]["live_fills"], 1)
            self.assertTrue(result["actual_fee_evidence_found"])
            encoded = json.dumps(result)
            self.assertNotIn("PRIVATE_ACCOUNT", encoded)
            self.assertNotIn("PRIVATE_ORDER_NO", encoded)
            self.assertNotIn('"fees": 17', encoded)
            self.assertTrue(result["persistent_database_and_wal_bytes_unchanged_during_read"])
            writer.close()

    def test_no_fees_stays_unknown_even_with_fill(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.sqlite"
            writer = sqlite3.connect(p)
            writer.execute("CREATE TABLE live_fills(fill_id TEXT,price TEXT)")
            writer.execute("INSERT INTO live_fills VALUES('PRIVATE','42')")
            writer.commit()
            writer.close()
            result = fee_db_aggregate(p)
            self.assertEqual(result["record_counts"]["live_fills"], 1)
            self.assertFalse(result["actual_fee_evidence_found"])
            self.assertIn("UNKNOWN", result["actual_account_fee"])


if __name__ == "__main__":
    unittest.main()
