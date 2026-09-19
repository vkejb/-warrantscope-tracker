from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from stage_a_t1_extreme_upside_study_v01.entry_state import classify
from stage_a_t1_extreme_upside_study_v01.outcomes import episode, evaluate, next_session_bar, rank_bucket
from stage_a_t1_extreme_upside_study_v01.official_limits import _parse_twse
from stage_a_t1_extreme_upside_study_v01.entry_state import digest
from stage_a_t1_outcomes_v01.ledger import verify
from stage_a_t1_extreme_upside_study_v01.analysis import _random_null, _csv_bytes
from surge_event_study_v01.models import Bar


def bars(prices):
    return [Bar(f"202609{i:02d}", "1234", "測試", 1000, price, price, price, price) for i, price in enumerate(prices)]


class ContractTest(unittest.TestCase):
    def test_t1_only_not_t2(self):
        stock = type("Stock", (), {"bars": bars([10, 11, 12])})()
        self.assertEqual(next_session_bar(stock, "20260900", ["20260900", "20260901", "20260902"]).close, 11)

    def test_official_limit_not_percentage_proxy(self):
        bar = Bar("20260902", "1234", "測試", 1000, 10, 10.95, 9.9, 10.95)
        result = evaluate(10, bar, 10.95)
        self.assertTrue(result["t1_close_at_upper_limit"])
        self.assertTrue(result["t1_high_touched_upper_limit"])
        self.assertEqual(result["close_limit_open_bucket"], "OPEN_LT_3")
        with self.assertRaises(ValueError):
            evaluate(10, bar, 10.9)

    def test_capturable_fixed(self):
        bar = Bar("20260902", "1234", "測試", 1000, 10.4, 11.0, 10.1, 10.8)
        value = evaluate(10, bar, 11)
        self.assertTrue(value["capturable_3"])
        self.assertTrue(value["capturable_5"])
        self.assertFalse(value["capturable_close_5"])

    def test_rank_buckets(self):
        self.assertEqual([rank_bucket(x) for x in (1, 5, 6, 10, 11, 20, 21, 30)], ["RANK_1_5", "RANK_1_5", "RANK_6_10", "RANK_6_10", "RANK_11_20", "RANK_11_20", "RANK_21_30", "RANK_21_30"])
        with self.assertRaises(ValueError):
            rank_bucket(31)

    def test_episode_streak(self):
        self.assertEqual([episode(x) for x in (0, 1, 2)], [("NEW_ENTRY", 1), ("CONTINUING_DAY_2", 2), ("CONTINUING_DAY_3_PLUS", 3)])

    def test_random_null_preserves_30_per_day(self):
        metrics = ("t1_close_at_upper_limit", "t1_high_touched_upper_limit", "t1_return_ge_8", "t1_return_ge_5", "capturable_5")
        rows = [{name: i == 0 for name in metrics} for i in range(40)]
        result = _random_null({"day1": rows, "day2": rows}, rows[:30] * 2, reps=100)
        self.assertEqual(result[0]["observed_hits"], 2)
        self.assertEqual(result[0]["simulations"], 100)
        self.assertEqual(result, _random_null({"day1": rows, "day2": rows}, rows[:30] * 2, reps=100))

    def test_csv_output_deterministic(self):
        self.assertEqual(_csv_bytes([{"a": 1, "b": 2}]), _csv_bytes([{"a": 1, "b": 2}]))

    def test_overheated_has_priority(self):
        prices = [10.0] * 24 + [10.8]
        self.assertEqual(classify(bars(prices), "20260924").classification, "OVERHEATED")

    def test_ready_requires_uptrend_and_moderate_extension(self):
        prices = [10 + i * .02 for i in range(25)]
        synthetic = [Bar(f"202609{i:02d}", "1234", "測試", 1000, price, price + .2, price - .2, price) for i, price in enumerate(prices)]
        self.assertEqual(classify(synthetic, "20260924").classification, "READY")

    def test_watch_is_not_discarded(self):
        prices = [10 + i * .02 for i in range(25)]
        self.assertEqual(classify(bars(prices), "20260924").classification, "WATCH")

    def test_cooling_has_priority(self):
        self.assertEqual(classify(bars([10.0 - i * .01 for i in range(25)]), "20260924").classification, "COOLING_BUT_WEAK")

    def test_no_future_state(self):
        with self.assertRaises(ValueError):
            classify(bars([10.0] * 26), "20260924")

    def test_twse_all_market_limit_parser(self):
        payload = {"stat": "OK", "date": "20260918", "fields": ["證券代號", "漲停價"], "data": [["006203", "15.30"], ["6203", "53.4"]]}
        # Production coverage gate must reject a truncated market response.
        with self.assertRaisesRegex(RuntimeError, "coverage too low"):
            _parse_twse(json.dumps(payload).encode(), "20260918")

    def test_9_18_seal_predates_9_21(self):
        path = Path(__file__).resolve().parents[1] / "runtime/seals/20260918.json"
        if not path.exists():
            self.skipTest("local prospective seal absent")
        value = json.loads(path.read_text())
        self.assertLess(datetime.fromisoformat(value["created_at"]), datetime(2026, 9, 21, tzinfo=ZoneInfo("Asia/Taipei")))
        self.assertEqual(len(value["stocks"]), 30)
        self.assertNotIn("t1_open", value)
        self.assertEqual(value["actual_orders"], 0)
        self.assertEqual(value["actual_fills"], 0)
        self.assertEqual(value["broker_connections"], 0)

    def test_outcome_chain_detects_mutation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            content = {"signal_date": "20260916", "stock_id": "1234", "previous_hash": "GENESIS", "t1_close": 10.0}
            path.write_text(json.dumps({**content, "outcome_hash": digest(content)}) + "\n")
            self.assertEqual(verify(path)[2], 1)
            path.write_text(path.read_text().replace("10.0", "11.0"))
            with self.assertRaises(RuntimeError):
                verify(path)


if __name__ == "__main__":
    unittest.main()
