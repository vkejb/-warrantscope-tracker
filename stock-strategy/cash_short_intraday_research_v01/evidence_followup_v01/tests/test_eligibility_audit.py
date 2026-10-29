import base64
import hashlib
import json
import unittest
from unittest.mock import patch

from cash_short_intraday_research_v01.evidence_followup_v01 import eligibility_audit as a


def receipt(source_id, doc):
    body = json.dumps(doc).encode()
    return {"source_id": source_id, "url": "https://www.tpex.org.tw/example?startDate=2026/01/01&endDate=2026/10/29",
            "obtained_at": "2026-10-30T00:00:00Z",
            "http_body_base64": base64.b64encode(body).decode(),
            "http_body_sha256": hashlib.sha256(body).hexdigest()}


class EligibilityTests(unittest.TestCase):
    def test_roc_date(self):
        self.assertEqual(a.roc_day("115/10/05"), "20261005")
        self.assertEqual(a.roc_day("1151005"), "20261005")
        self.assertEqual(a.roc_day("20261005"), "20261005")
        self.assertEqual(a.roc_day("2026/10/05"), "20261005")

    def test_missing_date_unknown(self):
        self.assertIsNone(a.roc_day("-"))
        self.assertIsNone(a.roc_day(None))
        self.assertIsNone(a.roc_day("115/14/05"))
        self.assertIsNone(a.roc_day("20261405"))

    def test_inclusive_interval(self):
        start, end = a.interval("115/10/05～115/10/08")
        self.assertTrue(a.interval_contains("20261005", start, end))
        self.assertTrue(a.interval_contains("20261008", start, end))
        self.assertFalse(a.interval_contains("20261009", start, end))

    def test_missing_interval_not_certified(self):
        self.assertFalse(a.interval_contains("20261005", None, None))

    def test_body_hash_checked(self):
        r = receipt("TEST", {})
        r["http_body_sha256"] = "bad"
        with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
            a.decoded_receipt(r)

    def test_tpex_ignored_historical_arguments_fail_closed(self):
        r = receipt("TPEX_SELL_FIRST_SUSPENSION", {"date": "20261029~20261029", "stat": "ok"})
        valid, inventory = a.validated_sources([r], ["20261002", "20261029"])
        self.assertEqual(valid, {})
        self.assertFalse(inventory[0]["requested_period_verified"])

    def test_tpex_period_verified_not_pit(self):
        r = receipt("TPEX_DISPOSAL", {"date": "20260101~20261029", "stat": "ok", "tables": []})
        valid, inventory = a.validated_sources([r], ["20261029"])
        self.assertIn("TPEX_DISPOSAL", valid)
        self.assertFalse(inventory[0]["current_retrieval_is_historical_PIT_proof"])
        self.assertEqual(inventory[0]["available_to_original_system_before_entry"], "UNKNOWN")

    def test_wrong_daily_date_rejected(self):
        doc = {"date": "20261028", "stat": "OK", "tables": [{"fields": [a.FLAG], "data": []}]}
        self.assertEqual(a.validated_sources([receipt("TWSE_DAY_20261029", doc)], ["20261029"])[0], {})

    def test_empty_twse_halt_query_not_global_certificate(self):
        doc = {"stat": "很抱歉，沒有符合條件的資料!", "title": "期間：115/01/01 到 115/10/29"}
        valid, inventory = a.validated_sources([receipt("TWSE_HALT", doc)], ["20261029"])
        self.assertIn("TWSE_HALT", valid)
        self.assertEqual(a.restrictions(valid), [])
        self.assertFalse(inventory[0]["current_retrieval_is_historical_PIT_proof"])

    def test_disposal_publication_distinct_from_effective_date(self):
        doc = {"fields": ["證券代號", "公布日期", "處置起迄時間"],
               "data": [["1234", "115/10/04", "115/10/05～115/10/08"]]}
        r = a.restrictions({"TWSE_DISPOSAL": doc})[0]
        self.assertEqual(r["publication_date"], "20261004")
        self.assertEqual(r["effective_start"], "20261005")
        self.assertEqual(r["available_to_original_system_at_entry"], "UNKNOWN")

    def test_halt_resume_day_is_overlap_not_assumed_full_day_halt(self):
        e = {"event_type": "HALT_OR_RESUMPTION", "effective_start": "20261005", "effective_end": "20261006"}
        self.assertTrue(a.event_matches("20261006", e))
        self.assertFalse(a.event_matches("20261007", e))

    def test_open_halt_does_not_get_fabricated_resume(self):
        e = {"event_type": "HALT_OR_RESUMPTION", "effective_start": "20261005", "effective_end": None}
        self.assertTrue(a.event_matches("20261029", e))
        self.assertIsNone(e["effective_end"])

    def test_old_archives_never_renamed_prospective(self):
        rows = [{"day": "20261029", "started_at": "2026-10-29T00:50:00Z", "manifest_status": "COMPLETE", "stream_coverage_pass": True}]
        result = a.prospective_status(rows, ["20261029"], "2026-10-29T15:00:00Z")
        self.assertEqual(result["status"], "PENDING_NO_NEW_DATE_ARCHIVE")
        self.assertEqual(result["prospective_approved_count"], 0)

    def test_new_archive_not_automatically_unseen(self):
        rows = [{"day": "20261030", "started_at": "2026-10-30T00:50:00Z", "manifest_status": "COMPLETE", "stream_coverage_pass": True}]
        result = a.prospective_status(rows, ["20261029"], "2026-10-29T15:00:00Z")
        self.assertEqual(result["metadata_candidate_count"], 1)
        self.assertEqual(result["prospective_approved_count"], 0)
        self.assertEqual(result["unseen_result_attestation"], "PENDING")

    def test_freeze_compare_uses_timezone_not_lexicographic_order(self):
        rows = [{"day": "20261030", "started_at": "2026-10-30T08:50:00+08:00", "manifest_status": "COMPLETE", "stream_coverage_pass": True}]
        result = a.prospective_status(rows, ["20261029"], "2026-10-30T01:00:00Z")
        self.assertEqual(result["metadata_candidate_count"], 0)

    def test_no_live_fetch_target(self):
        with self.assertRaisesRegex(ValueError, "ONLY_OFFICIAL"):
            a.fetch_receipt("X", "https://broker.invalid/order")

    def test_all_models_not_only_s1_checked(self):
        rows = [{"day": "20261005", "symbol": "1234", "signal_id": "m0", "entry_model": "M0", "experiment": "ENTRY_M0"},
                {"day": "20261005", "symbol": "1234", "signal_id": "s2", "entry_model": "S2", "experiment": "ENTRY_S2"},
                {"day": "20261006", "symbol": "1234", "signal_id": "s1", "entry_model": "S1", "experiment": "B"}]
        checks = a.impact_checks({"ROUND1": {"signals": rows}}, {("20261005", "1234")})
        self.assertEqual(sum(r["affected_row_count"] for r in checks), 2)
        self.assertEqual({sid for r in checks for sid in r["affected_signal_ids"]}, {"m0", "s2"})

    def test_all_round2_phases_checked(self):
        rows = [{"day": "20261005", "symbol": "1234", "signal_id": phase, "phase": phase, "side": "SHORT"}
                for phase in ("ENTRY", "ACCOUNT_EXIT", "FIXED_EXIT")]
        checks = a.impact_checks({"ROUND2": {"trades": rows}}, {("20261005", "1234")})
        self.assertEqual({r["phase"] for r in checks if r["affected_row_count"]}, {"ENTRY", "ACCOUNT_EXIT", "FIXED_EXIT"})

    def test_name_revision_does_not_silently_become_eligibility_revision(self):
        fields = ["證券代號", "證券名稱", a.FLAG]
        previous = {"sha256": "older_bytes", "raw_response": {"tables": [{"fields": fields, "data": [["0053", "Old", ""]]}]}}
        current = {"tables": [{"fields": fields, "data": [["0053", "New", ""]]}]}
        with patch.object(a, "load", return_value=previous), patch.object(a, "digest", return_value="pinned"):
            result = a.compare_cache_response({"day": "20261008", "path": "old.json", "sha256": "pinned"}, receipt("TWSE_DAY_20261008", current))
        self.assertFalse(result["recorded_original_http_hash_reobserved"])
        self.assertEqual(result["suspension_flag_differences"], [])
        self.assertEqual(result["field_differences"][0]["field"], "證券名稱")
        self.assertFalse(result["historical_PIT_receipt_proved"])


if __name__ == "__main__":
    unittest.main()
