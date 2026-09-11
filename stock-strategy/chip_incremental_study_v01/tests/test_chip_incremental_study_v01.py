from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np


STOCK_STRATEGY = Path(__file__).resolve().parents[2]
if str(STOCK_STRATEGY) not in sys.path:
    sys.path.insert(0, str(STOCK_STRATEGY))

from chip_incremental_study_v01.config import CFG, CHIP_FEATURES  # noqa: E402
from chip_incremental_study_v01.analysis import discovery_diagnostics, incremental_rows  # noqa: E402
from chip_incremental_study_v01.data import load_frozen_research, protected_hashes  # noqa: E402
from chip_incremental_study_v01.identity_v2 import (  # noqa: E402
    IDENTITY_SCHEMA_VERSION,
    PARSER_SCHEMA_VERSION,
    parse_raw_source_v2,
)
from chip_incremental_study_v01.models import (  # noqa: E402
    ALL_MODEL_FEATURES,
    fit_chip_logistic,
    frozen_ohlcv_feature_matrix,
)
from chip_incremental_study_v01.pit import build_chip_features, needed_codes, prior_session_map  # noqa: E402
from chip_incremental_study_v01.sources import (  # noqa: E402
    OfficialRateLimitError,
    PUBLIC_USER_AGENT,
    _get,
    download_official_chip_store,
    phase0_audit_rows,
)
from chip_incremental_study_v01.transport_diagnostic import (  # noqa: E402
    Probe,
    _classify,
    _parse_header_chains,
    _validate_body,
)
from chip_incremental_study_v01.notifications import finalize_batch, send_notification  # noqa: E402
import chip_incremental_study_v01.main as chip_main  # noqa: E402
from chip_incremental_study_v01.main import (  # noqa: E402
    EXPECTED_PHASE1_V2_STORE_SHA256,
    acquisition_progress_view,
    run_to_completed_target,
)
from surge_event_study_v01.data import sha256_file  # noqa: E402
from extension_entry_study_v01.pipeline import OUTCOME_FIELDS  # noqa: E402


def meta_fixture(days: int = 8):
    dtype = np.dtype([
        ("signal_date", "<i4"), ("stock_code", "<i4"),
        ("cohort_mask", "u1"), ("momentum_strength_quintile", "u1"),
        ("entry_gap_bucket", "u1"), ("outcome_evaluable", "?"),
    ])
    dates = np.asarray([20200102 + index for index in range(days)], dtype=np.int32)
    rows = [(int(date), 2330, 0, 0, 0, True) for date in dates]
    return np.asarray(rows, dtype=dtype)


class TestChipIncrementalStudy(unittest.TestCase):
    @staticmethod
    def _twse_institutional_raw(rows):
        return json.dumps({
            "stat": "OK", "date": 20200601,
            "fields": [
                "證券代號", "證券名稱", "外陸資買賣超股數(不含外資自營商)",
                "投信買賣超股數", "自營商買賣超股數",
            ],
            "data": rows,
        }, ensure_ascii=False).encode()

    @staticmethod
    def _tpex_institutional_raw(rows):
        fields = ["代號", "名稱"] + [f"f{i}" for i in range(2, 24)]
        return json.dumps({
            "stat": "ok", "date": 20200601,
            "tables": [{"fields": fields, "data": rows}],
        }, ensure_ascii=False).encode()

    def test_v2_preserves_leading_zero_and_filters_etf_before_research_mapping(self):
        raw = self._twse_institutional_raw([
            ["006203", "元大MSCI台灣", "0", "0", "3000"],
            ["2330", "台積電", "10", "2", "-1"],
        ])
        before = bytes(raw)
        parsed, exclusions = parse_raw_source_v2(
            "TWSE_INSTITUTIONAL", 20200601, raw, {"6203", "2330"}, "https://official.invalid"
        )
        self.assertEqual(raw, before)
        self.assertEqual(parsed["schema_version"], PARSER_SCHEMA_VERSION)
        self.assertEqual(parsed["identity_schema_version"], IDENTITY_SCHEMA_VERSION)
        self.assertEqual([row["security_code"] for row in parsed["rows"]], ["2330"])
        self.assertEqual(exclusions[0]["security_code"], "006203")
        self.assertEqual(exclusions[0]["security_type"], "ETF")
        self.assertNotEqual(exclusions[0]["security_code"], "6203")

    def test_v2_tpex_exact_6203_maps_to_common_stock(self):
        row = ["6203", "海韻電"] + ["0"] * 22
        row[10], row[13], row[22] = "-103000", "0", "-62000"
        parsed, exclusions = parse_raw_source_v2(
            "TPEX_INSTITUTIONAL", 20200601,
            self._tpex_institutional_raw([row]), {"6203"}, "https://official.invalid",
        )
        self.assertFalse(exclusions)
        self.assertEqual(parsed["rows"][0]["security_code"], "6203")
        self.assertEqual(parsed["rows"][0]["security_type"], "COMMON_STOCK")
        self.assertEqual(parsed["rows"][0]["market"], "TPEX")

    def test_v2_same_numeric_suffix_has_distinct_canonical_identity(self):
        twse, exclusions = parse_raw_source_v2(
            "TWSE_INSTITUTIONAL", 20200601,
            self._twse_institutional_raw([["006203", "元大MSCI台灣", "0", "0", "0"]]),
            {"6203"}, "https://official.invalid/twse",
        )
        tpex_row = ["6203", "海韻電"] + ["0"] * 22
        tpex, _ = parse_raw_source_v2(
            "TPEX_INSTITUTIONAL", 20200601,
            self._tpex_institutional_raw([tpex_row]), {"6203"}, "https://official.invalid/tpex",
        )
        self.assertFalse(twse["rows"])
        self.assertEqual((exclusions[0]["market"], exclusions[0]["security_code"]), ("TWSE", "006203"))
        self.assertEqual(
            (tpex["rows"][0]["market"], tpex["rows"][0]["security_code"]),
            ("TPEX", "6203"),
        )

    def test_v2_known_etf_suffixes_never_collide_with_research_codes(self):
        raw = self._twse_institutional_raw([
            ["006203", "元大MSCI台灣", "0", "0", "0"],
            ["006204", "永豐臺灣加權", "0", "0", "0"],
            ["006207", "復華滬深", "0", "0", "0"],
        ])
        parsed, exclusions = parse_raw_source_v2(
            "TWSE_INSTITUTIONAL", 20200601, raw,
            {"6203", "6204", "6207"}, "https://official.invalid",
        )
        self.assertFalse(parsed["rows"])
        self.assertEqual(
            [row["security_code"] for row in exclusions],
            ["006203", "006204", "006207"],
        )

    def test_v2_parser_output_is_deterministic(self):
        raw = self._twse_institutional_raw([["2330", "台積電", "10", "2", "-1"]])
        first = parse_raw_source_v2(
            "TWSE_INSTITUTIONAL", 20200601, raw, {"2330"}, "https://official.invalid"
        )
        second = parse_raw_source_v2(
            "TWSE_INSTITUTIONAL", 20200601, raw, {"2330"}, "https://official.invalid"
        )
        self.assertEqual(first, second)

    def test_phase1_v2_checkpoint_contract_when_available(self):
        path = STOCK_STRATEGY / "chip_incremental_study_v01/checkpoints/phase1_acquisition/phase1_acquisition_final_v2.json"
        if not path.exists():
            self.skipTest("Phase 1 v2 rebuild not completed yet")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["counts"]["raw_pairs"], 5864)
        self.assertEqual(payload["counts"]["parsed_v2_pairs"], 5864)
        self.assertEqual(payload["counts"]["complete_dates"], 1466)
        self.assertEqual(payload["counts"]["cross_market_identity_collisions"], 0)
        self.assertEqual(payload["counts"]["etf_exclusions"], 641)
        self.assertTrue(payload["coverage_gate"]["pass"])
        self.assertFalse(payload["safety"]["immutable_raw_modified"])
        self.assertFalse(payload["safety"]["legacy_parsed_modified"])
        self.assertEqual(payload["safety"]["formal_model_run_count"], 0)

    def test_phase1_v2_store_hash_is_exact_preregistered_input(self):
        path = STOCK_STRATEGY / "chip_incremental_study_v01/runtime/chip_daily_store_v2.npz"
        checkpoint = STOCK_STRATEGY / "chip_incremental_study_v01/checkpoints/phase1_acquisition/phase1_acquisition_final_v2.json"
        self.assertTrue(path.exists())
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(payload["final_store_sha256"], EXPECTED_PHASE1_V2_STORE_SHA256)
        self.assertEqual(sha256_file(path), EXPECTED_PHASE1_V2_STORE_SHA256)

    def test_complete_store_maps_to_final_progress_and_notification_counts(self):
        complete = {"status": "COMPLETE", "dates_requested": 1466,
                    "source_date_pairs": 5864, "coverage_gate": {"all_expected_dates_complete": True}}
        progress = acquisition_progress_view(complete)
        self.assertEqual(progress["complete_source_date_pairs"], 5864)
        self.assertEqual(progress["expected_source_date_pairs"], 5864)
        self.assertEqual(progress["missing_source_date_pairs"], 0)
        self.assertIsNone(progress["stop_reason"])

    def test_completed_pair_target_compensates_for_retry_attempt(self):
        calls = []
        results = iter([
            {"status": "PARTIAL", "complete_source_date_pairs": 1999,
             "stop_reason": "BOUNDED_BATCH_LIMIT_REACHED"},
            {"status": "PARTIAL", "complete_source_date_pairs": 2000,
             "stop_reason": "BOUNDED_BATCH_LIMIT_REACHED"},
        ])
        def download_once(cap):
            calls.append(cap)
            return next(results)

        result = run_to_completed_target(download_once, 1003, 2000, 997)
        self.assertEqual(calls, [997, 1])
        self.assertEqual(result["complete_source_date_pairs"], 2000)

    def test_completed_pair_target_stops_on_official_failure(self):
        def download_once(_cap):
            return {"status": "PARTIAL", "complete_source_date_pairs": 1500,
                    "stop_reason": "RATE_LIMITED:TWSE_MARGIN:20210101"}

        result = run_to_completed_target(download_once, 1003, 2000, 997)
        self.assertEqual(result["complete_source_date_pairs"], 1500)
    def test_checkpoint_and_complete_notifications_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with patch("chip_incremental_study_v01.notifications.subprocess.run", return_value=ok) as run:
                progress = {"complete_source_date_pairs": 2000, "expected_source_date_pairs": 5864,
                            "stop_reason": "BOUNDED_BATCH_LIMIT_REACHED"}
                self.assertEqual(finalize_batch(runtime, progress, 2000), "REACHED_TARGET_CHECKPOINT")
                self.assertEqual(finalize_batch(runtime, progress, 2000), "REACHED_TARGET_CHECKPOINT")
                complete = {"complete_source_date_pairs": 5864, "expected_source_date_pairs": 5864,
                            "stop_reason": None}
                self.assertEqual(finalize_batch(runtime, complete, 5864), "ALL_PAIRS_COMPLETE")
            self.assertEqual(run.call_count, 2)

    def test_transport_and_integrity_failure_notifications(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with patch("chip_incremental_study_v01.notifications.subprocess.run", return_value=ok) as run:
                stopped = {"complete_source_date_pairs": 1008, "expected_source_date_pairs": 5864,
                           "stop_reason": "RATE_LIMITED:TWSE_INSTITUTIONAL:20210101"}
                self.assertEqual(finalize_batch(runtime, stopped, 2000), "OFFICIAL_TRANSPORT_STOP")
                self.assertEqual(finalize_batch(runtime, stopped, 2000, ["HASH_MISMATCH"]), "INTEGRITY_FAILURE")
            self.assertEqual(run.call_count, 2)

    def test_notification_failure_does_not_raise_and_incomplete_checkpoint_suppresses_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            with patch("chip_incremental_study_v01.notifications.subprocess.run", side_effect=OSError("denied")):
                self.assertFalse(send_notification(runtime, "TEST", 1, 2, "test"))
            progress = {"complete_source_date_pairs": 2000, "expected_source_date_pairs": 5864,
                        "stop_reason": "BOUNDED_BATCH_LIMIT_REACHED"}
            with patch("chip_incremental_study_v01.notifications.send_notification") as send:
                self.assertIsNone(finalize_batch(runtime, progress, 2000, checkpoint_ok=False))
            send.assert_not_called()

    def test_notification_cli_does_not_download_or_touch_cache_manifest(self):
        with patch.object(chip_main, "test_notification", return_value=True) as notify, \
             patch.object(chip_main, "command_download") as download:
            self.assertEqual(chip_main.main(["test-notification"]), 0)
        notify.assert_called_once()
        download.assert_not_called()
    def test_transport_trace_redacts_cookie_and_preserves_redirect(self):
        chains = _parse_header_chains(
            "HTTP/2 307\r\nLocation: https://official.example/data\r\nSet-Cookie: sid=secret\r\n\r\n"
            "HTTP/2 200\r\nContent-Type: application/json\r\n\r\n"
        )
        self.assertEqual([row["status"] for row in chains], [307, 200])
        self.assertEqual(chains[0]["headers"]["location"], "https://official.example/data")
        self.assertNotIn("secret", chains[0]["headers"]["set-cookie"])

    def test_transport_validation_rejects_html_redirect_body(self):
        probe = Probe(
            "x", "x", "TWSE_T86_RWD", "TWSE", "INSTITUTIONAL", "RWD_HISTORICAL",
            "https://official.example", (), "json", 20200102, True,
        )
        validation = _validate_body(probe, b"<html>redirect</html>", "text/html")
        trace = {"http_code": 307, "body_validation": validation}
        self.assertFalse(validation["valid_market_payload"])
        self.assertEqual(_classify(trace), "REDIRECT_BLOCKED")

    def test_transport_validation_accepts_twse_multisection_margin_csv(self):
        probe = Probe(
            "x", "x", "TWSE_MI_MARGN_RWD", "TWSE", "MARGIN_SHORT", "RWD_HISTORICAL_CSV",
            "https://official.example", (), "csv", 20200102, True,
        )
        body = (
            '"109年01月02日 信用交易統計"\n'
            '"項目","買進","賣出"\n'
            '"109年01月02日 融資融券彙總 (全部)"\n'
            '"股票",,"融資"\n'
            '"代號","名稱","買進"\n'
            '="2330","台積電","10"\n'
        ).encode("cp950")
        validation = _validate_body(probe, body, "text/csv;charset=ms950")
        self.assertTrue(validation["valid_market_payload"])
        self.assertEqual(validation["schema_fields"][:2], ["代號", "名稱"])
        self.assertEqual(validation["row_count"], 1)

    def test_official_downloader_uses_public_reproducible_client_profile(self):
        payload = json.dumps({"stat": "OK"}).encode()
        response = subprocess.CompletedProcess(
            args=["curl"], returncode=0,
            stdout=payload + b"\n__CHIP_HTTP_STATUS__=200", stderr=b"",
        )
        with patch("chip_incremental_study_v01.sources.subprocess.run", return_value=response) as run:
            parsed, _digest, _url, _body = _get("https://official.invalid", {"date": "20200102"})
        self.assertEqual(parsed["stat"], "OK")
        command = run.call_args.args[0]
        self.assertIn("--compressed", command)
        self.assertEqual(command[command.index("-A") + 1], PUBLIC_USER_AGENT)
        self.assertIn("Accept: application/json,text/plain,*/*", command)
        self.assertIn("Accept-Language: zh-TW,zh;q=0.9,en;q=0.8", command)

    @staticmethod
    def _fake_source(source, date, wanted):
        market = "TWSE" if source.startswith("TWSE") else "TPEX"
        family = "INSTITUTIONAL" if source.endswith("INSTITUTIONAL") else "MARGIN_SHORT"
        if market == "TPEX":
            rows = []
        elif family == "INSTITUTIONAL":
            rows = [{"stock_code": 2330, "foreign": 10, "investment_trust": 2, "dealer": -1}]
        else:
            rows = [{"stock_code": 2330, "margin_balance": 100, "short_balance": 5}]
        raw = json.dumps({"source": source, "date": date}).encode()
        import hashlib
        return {
            "schema_version": 1, "source": source, "market": market,
            "feature_family": family, "date": date,
            "source_url": f"https://official.invalid/{source}/{date}",
            "source_row_count": 100, "wanted_code_count": len(wanted),
            "matched_row_count": len(rows), "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "rows": rows,
        }, raw

    def test_resumable_source_cache_skips_completed_pairs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dates = np.asarray([20200102], dtype=np.int32)
            needed = {20200102: {2330}}
            with patch("chip_incremental_study_v01.sources._fetch_source", side_effect=self._fake_source) as fetch:
                first = download_official_chip_store(
                    dates, needed, root / "store.npz", root / "final.json",
                    root / "cache", request_interval_seconds=0,
                    max_attempts=1, initial_backoff_seconds=0, max_source_requests=2,
                )
                self.assertEqual(first["status"], "PARTIAL")
                self.assertEqual(first["complete_source_date_pairs"], 2)
                second = download_official_chip_store(
                    dates, needed, root / "store.npz", root / "final.json",
                    root / "cache", request_interval_seconds=0,
                    max_attempts=1, initial_backoff_seconds=0, max_source_requests=2,
                )
                self.assertEqual(second["status"], "COMPLETE")
                self.assertEqual(fetch.call_count, 4)
            events = [json.loads(line) for line in (root / "cache/download_manifest.jsonl").read_text().splitlines()]
            complete = [event for event in events if event["request_status"] == "COMPLETE"]
            self.assertEqual(len(complete), 4)
            required = {"source", "market", "date", "request_status", "row_count", "retrieval_timestamp_utc", "raw_file_hash", "parsed_file_hash", "retry_count"}
            self.assertTrue(all(required.issubset(event) for event in complete))
            with np.load(root / "store.npz", allow_pickle=False) as payload:
                self.assertEqual(len(payload["chip_daily"]), 1)

    def test_rate_limit_checkpoints_and_exits_without_final_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("chip_incremental_study_v01.sources._fetch_source", side_effect=OfficialRateLimitError("throttled")):
                result = download_official_chip_store(
                    np.asarray([20200102], dtype=np.int32), {20200102: {2330}},
                    root / "store.npz", root / "final.json", root / "cache",
                    request_interval_seconds=0, max_attempts=2,
                    initial_backoff_seconds=0, max_source_requests=2,
                )
            self.assertEqual(result["status"], "PARTIAL")
            self.assertIn("RATE_LIMITED", result["stop_reason"])
            self.assertFalse((root / "store.npz").exists())
            events = [json.loads(line) for line in (root / "cache/download_manifest.jsonl").read_text().splitlines()]
            limited = [event for event in events if event["request_status"] == "RATE_LIMITED"]
            self.assertEqual([event["retry_count"] for event in limited], [0, 1])

    def test_twse_cdn_307_is_rate_limit_not_market_data(self):
        response = subprocess.CompletedProcess(
            args=["curl"], returncode=0,
            stdout=b"<html>blocked</html>\n__CHIP_HTTP_STATUS__=307", stderr=b"",
        )
        with patch("chip_incremental_study_v01.sources.subprocess.run", return_value=response):
            with self.assertRaises(OfficialRateLimitError):
                _get("https://official.invalid", {"date": "20200102"})

    def test_phase0_decisions_are_closed_set(self):
        rows = phase0_audit_rows()
        allowed = {"PIT_USABLE", "PIT_USABLE_WITH_LAG", "NOT_TESTED_DATA_UNAVAILABLE", "REJECTED_PIT_UNSAFE"}
        self.assertTrue(rows)
        self.assertTrue(all(row["decision"] in allowed for row in rows))
        self.assertTrue(all(row["required_lag_sessions"] == 1 for row in rows if row["decision"] == "PIT_USABLE_WITH_LAG"))
        required = {"feature_family", "raw_field", "source", "publication_timing", "PIT_status", "decision"}
        self.assertTrue(all(required.issubset(row) for row in rows))

    def test_no_snapshot_backfill_and_tdcc_rejected(self):
        tdcc = next(row for row in phase0_audit_rows() if row["feature_family"] == "TDCC_OWNERSHIP")
        self.assertEqual(tdcc["decision"], "REJECTED_PIT_UNSAFE")
        self.assertFalse(tdcc["usable_on_same_day_T"])

    def test_prior_session_lag_correctness(self):
        dates = np.asarray([20200102, 20200103, 20200106], dtype=np.int32)
        self.assertEqual(prior_session_map(dates, 1)[20200106], 20200103)
        self.assertEqual(prior_session_map(dates, 2)[20200106], 20200102)

    def test_needed_codes_never_uses_signal_date(self):
        meta = meta_fixture()
        pool = np.zeros(len(meta), dtype=bool)
        pool[-1] = True
        needed = needed_codes(meta["signal_date"], meta["stock_code"], pool, 1)
        self.assertTrue(all(date < int(meta["signal_date"][-1]) for date in needed))

    def test_needed_codes_can_use_2019_warmup_calendar(self):
        signal_dates = np.asarray([20200102, 20200103], dtype=np.int32)
        codes = np.asarray([2330, 2317], dtype=np.int32)
        pool = np.asarray([True, False])
        calendar = np.asarray([20191227, 20191230, 20191231, 20200102, 20200103], dtype=np.int32)
        needed = needed_codes(
            signal_dates, codes, pool, lag_sessions=1, lookback=3,
            calendar_dates=calendar,
        )
        self.assertEqual(set(needed), {20191227, 20191230, 20191231})
        self.assertTrue(all(2330 in wanted for wanted in needed.values()))

    def test_pit_feature_timestamp_and_margin_missingness(self):
        meta = meta_fixture()
        pool = np.ones(len(meta), dtype=bool)
        dtype = np.dtype([
            ("source_date", "<i4"), ("stock_code", "<i4"),
            ("foreign", "<f8"), ("investment_trust", "<f8"), ("dealer", "<f8"),
            ("margin_balance", "<f8"), ("short_balance", "<f8"),
            ("institutional_market", "u1"), ("margin_market", "u1"),
        ])
        chip = np.asarray([
            (int(date), 2330, index + 1, index + 2, index + 3, np.nan, np.nan, 1, 0)
            for index, date in enumerate(meta["signal_date"])
        ], dtype=dtype)
        volumes = {(int(date), 2330): 1000.0 for date in meta["signal_date"]}
        features, audit = build_chip_features(meta, pool, chip, volumes, 1)
        valid = features["chip_valid"]
        self.assertTrue(np.all(features["chip_source_date"][valid] < meta["signal_date"][valid]))
        self.assertTrue(np.all(features["raw_chip_features"][valid, -1] == 0.0))
        self.assertEqual(audit["future_or_same_date_source_rows"], 0)

    def test_regularization_is_preregistered(self):
        x = np.linspace(-1, 1, 240).reshape(40, 6)
        y = (np.arange(40) % 2).astype(float)
        mask = np.ones(40, dtype=bool)
        model = fit_chip_logistic(x, y, mask, 1.0, "TEST", tuple(f"f{i}" for i in range(6)))
        self.assertEqual(model.regularization_c, 1.0)
        with self.assertRaises(ValueError):
            fit_chip_logistic(x, y, mask, 0.5, "TEST", tuple(f"f{i}" for i in range(6)))

    def test_feature_width_contract(self):
        self.assertEqual(len(ALL_MODEL_FEATURES), 41 + len(CHIP_FEATURES))

    def test_frozen_stage_a_and_ohlcv_exact_reuse(self):
        arrays, model, audit = load_frozen_research(STOCK_STRATEGY)
        self.assertEqual(audit["stage_a_refit_count"], 0)
        self.assertEqual(audit["frozen_ohlcv_refit_count"], 0)
        self.assertEqual(len(arrays["meta"]), 704327)
        self.assertEqual(model.name, "LOGISTIC_RIDGE_PATH_SUCCESS")
        matrix = frozen_ohlcv_feature_matrix(arrays, model)
        self.assertEqual(matrix.shape, (704327, 41))

    def test_no_prospective_observations(self):
        arrays, _model, _audit = load_frozen_research(STOCK_STRATEGY)
        self.assertEqual(int(np.count_nonzero(arrays["meta"]["signal_date"] >= 20260907)), 0)

    def test_n_compact_and_prospective_ledger_unchanged(self):
        before = protected_hashes(STOCK_STRATEGY)
        arrays, _model, _audit = load_frozen_research(STOCK_STRATEGY)
        after = protected_hashes(STOCK_STRATEGY)
        self.assertEqual(before, after)
        self.assertEqual(int(np.count_nonzero(arrays["n_compact"])), 236)

    def test_incremental_metric_and_mfe_retention(self):
        base = {
            "time_slice_type": "PERIOD", "time_slice": "X", "cohort": "OHLCV_TOP5_COMMON_DATES",
            "path_success_rate": .30, "downside_first_rate": .40, "mae10_mean": -.07,
            "mfe10_mean": .08, "net_mean": -.01, "net_profit_factor": .8,
            "auc_on_common_stage_a_pool": .52, "mean_daily_path_ic": .01,
        }
        chip = {**base, "cohort": "CHIP_TOP5", "path_success_rate": .35, "downside_first_rate": .35,
                "mae10_mean": -.06, "mfe10_mean": .09, "net_mean": .01, "net_profit_factor": 1.1,
                "auc_on_common_stage_a_pool": .55, "mean_daily_path_ic": .03}
        stage = {**base, "cohort": "COMMON_STAGE_A_TOP30", "mfe10_mean": .10}
        row = incremental_rows([base, chip, stage])[0]
        self.assertAlmostEqual(row["delta_success_rate"], .05)
        self.assertAlmostEqual(row["mfe_retention_vs_stage_a"], .9)
        self.assertAlmostEqual(row["mae_improvement_vs_stage_a"], .01)

    def test_discovery_diagnostic_keeps_empty_quantile_effect_null(self):
        count = 12
        dtype = np.dtype([("signal_date", "<i4"), ("outcome_evaluable", "?")])
        meta = np.asarray([(20200102 + index, True) for index in range(count)], dtype=dtype)
        outcomes = np.zeros((count, len(OUTCOME_FIELDS)), dtype=float)
        arrays = {
            "meta": meta,
            "path_success": (np.arange(count) % 2).astype(float),
            "path_class": np.ones(count, dtype=np.uint8),
            "outcomes": outcomes,
        }
        rows = discovery_diagnostics(
            np.ones((count, 1)), np.full((count, 1), 0.5), ("constant_feature",),
            arrays, np.ones(count, dtype=bool), bootstrap_reps=10,
        )
        self.assertTrue(rows)
        self.assertTrue(all(row["top_minus_bottom_effect"] is None for row in rows))

    def test_module_has_no_broker_or_order_path(self):
        root = STOCK_STRATEGY / "chip_incremental_study_v01"
        text = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
        self.assertNotIn("Yuanta", text)
        self.assertNotIn("place_order", text)
        self.assertNotIn("submit_order", text)

    def test_published_safety_counters_when_available(self):
        path = STOCK_STRATEGY / "chip_incremental_study_v01/run_manifest.json"
        if not path.exists():
            self.skipTest("formal publish not run yet")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["actual_orders"], 0)
        self.assertEqual(payload["actual_fills"], 0)
        self.assertEqual(payload["broker_connections"], 0)
        self.assertEqual(payload["pipeline_validation"]["later_period_refit_count"], 0)
        self.assertTrue(payload["pipeline_validation"]["phase1_v2_coverage_gate_pass"])
        self.assertTrue(payload["pipeline_validation"]["phase1_v2_store_sha256_verified"])
        self.assertEqual(payload["model_fit_count"], 11)


if __name__ == "__main__":
    unittest.main()
