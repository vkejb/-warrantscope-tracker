import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from expanded_shadow_universe_v01.policy import POLICY
from expanded_shadow_universe_v01.subscriptions import batches, deduplicate_items
from expanded_shadow_universe_v01.universe import (
    ExpandedWatchItem,
    _parse_tpex_master,
    _parse_twse_master,
    _select_candidates,
)
from expanded_shadow_universe_v01.paper import load_expanded_candidates, publish_expanded_paper
from yuanta_intraday_shadow_v01.collector import AppendOnlyRun, WatchItem


def _item(symbol):
    return ExpandedWatchItem(symbol, symbol, "TWSE", 1, 10, 4_000_000, 40_000_000, "半導體業", "24")


class UniverseTests(unittest.TestCase):
    def test_official_master_parsers(self):
        twse = _parse_twse_master(json.dumps([
            {"公司代號": "2330", "公司簡稱": "台積電", "產業別": "24"}
        ], ensure_ascii=False).encode())
        tpex = _parse_tpex_master(json.dumps({
            "stat": "ok", "tables": [{"data": [["6488", "環球晶", "99", "半導體業"]]}]
        }, ensure_ascii=False).encode())
        self.assertEqual(twse["2330"]["industry_code"], "24")
        self.assertEqual(tpex["6488"]["industry"], "半導體業")

    def test_policy_filters_price_liquidity_and_excluded_industries(self):
        eod = {
            "1111": {"close": "100", "volume": "400000", "market": "TWSE", "name": "A"},
            "2222": {"close": "191", "volume": "9999999", "market": "TWSE", "name": "B"},
            "3333": {"close": "100", "volume": "100000", "market": "TPEX", "name": "C"},
            "4444": {"close": "50", "volume": "1000000", "market": "TPEX", "name": "D"},
            "0050": {"close": "100", "volume": "1000000", "market": "TWSE", "name": "ETF"},
        }
        masters = {
            "1111": {"stock_name": "A", "industry": "", "industry_code": "24"},
            "2222": {"stock_name": "B", "industry": "", "industry_code": "24"},
            "3333": {"stock_name": "C", "industry": "半導體業", "industry_code": None},
            "4444": {"stock_name": "D", "industry": "生技醫療業", "industry_code": None},
        }
        selected, reasons = _select_candidates(eod, masters)
        self.assertEqual([row["stock_id"] for row in selected], ["1111"])
        self.assertEqual(reasons, {
            "MISSING_OFFICIAL_INDUSTRY": 1,
            "ABOVE_PRICE_CAP": 1,
            "BELOW_TURNOVER_FLOOR": 1,
            "EXCLUDED_INDUSTRY": 1,
        })

    def test_ranking_is_turnover_then_symbol(self):
        eod = {
            "2000": {"close": "100", "volume": "400000", "market": "TWSE"},
            "1000": {"close": "80", "volume": "500000", "market": "TWSE"},
            "3000": {"close": "100", "volume": "500000", "market": "TWSE"},
        }
        masters = {code: {"stock_name": code, "industry": "", "industry_code": "24"} for code in eod}
        selected, _ = _select_candidates(eod, masters)
        self.assertEqual([row["stock_id"] for row in selected], ["3000", "1000", "2000"])

    def test_subscription_deduplication_and_batch_limit(self):
        primary = [_item(str(1000 + number)) for number in range(30)]
        expanded = [_item(str(1000 + number)) for number in range(350)]
        union = deduplicate_items(primary, expanded)
        groups = batches(union)
        self.assertEqual(len(union), 350)
        self.assertEqual([len(group) for group in groups], [200, 150])
        self.assertTrue(all(len(group) <= 200 for group in groups))

    def test_policy_has_zero_execution_contract(self):
        self.assertEqual(POLICY["actual_orders"], 0)
        self.assertEqual(POLICY["actual_fills"], 0)
        self.assertEqual(POLICY["broker_order_calls"], 0)
        self.assertEqual(POLICY["maximum_symbols"], 400)

    def test_expanded_archive_can_be_loaded_for_paper_replay(self):
        stage = [WatchItem(str(1000 + index), str(index), index, 0.1, "TWSE") for index in range(1, 31)]
        expanded = [_item("2330")]
        stage_seal = {"signal_date": "20261005", "seal_hash": "a" * 64}
        expanded_seal = {
            "signal_date": "20261005", "seal_hash": "b" * 64,
            "policy": {"policy_id": "TEST"},
        }
        with tempfile.TemporaryDirectory() as temp:
            archive = AppendOnlyRun(
                Path(temp), stage_seal, stage, {}, expanded_seal=expanded_seal,
                expanded_items=expanded,
            )
            archive.append("expanded_ticks", {
                "received_at": "2026-10-06T01:00:01.000Z", "quote_time": "09:00:01.000",
                "stock_id": "2330", "deal_price": "100", "deal_volume": "1",
                "buy_price": "99.5", "sell_price": "100", "in_out_flag": "1", "serial_no": 1,
            })
            archive.append("expanded_books", {
                "received_at": "2026-10-06T01:00:01.000Z", "stock_id": "2330",
                "buy_prices": ["99.5"], "buy_volumes": ["10"],
                "sell_prices": ["100"], "sell_volumes": ["20"],
            })
            archive.finalize(
                status="COMPLETE", started_at="2026-10-06T01:00:00Z",
                ended_at="2026-10-06T05:30:00Z",
            )
            candidates, coverage = load_expanded_candidates(archive.run_dir)
            self.assertEqual(len(candidates["2330"]["ticks"]), 1)
            self.assertEqual(len(candidates["2330"]["books"]), 1)
            self.assertEqual(coverage["symbols_with_both"], 1)

    def test_paper_publish_builds_full_session_replay_coverage_from_source(self):
        stage = [WatchItem(str(1000 + index), str(index), index, 0.1, "TWSE") for index in range(1, 31)]
        expanded = [_item("2330")]
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as output:
            archive = AppendOnlyRun(
                Path(temp), {"signal_date": "20261005", "seal_hash": "a" * 64},
                stage, {}, expanded_seal={
                    "signal_date": "20261005", "seal_hash": "b" * 64,
                    "policy": {"policy_id": "TEST"},
                }, expanded_items=expanded,
            )
            archive.finalize(
                status="COMPLETE", started_at="2026-10-06T00:59:00Z",
                ended_at="2026-10-06T05:21:00Z",
            )
            captured = {}

            def replay(candidates, context, coverage, **_kwargs):
                captured.update(coverage)
                return {
                    "trade": None, "confirmation_trade": None,
                    "decision_diagnostics": [], "reason": "NO_APPROVED_LONG_ENTRY",
                    "confirmation_reason": "NO_APPROVED_LONG_ENTRY",
                }

            session = {
                "session_date": "20261006", "coverage_status": "FULL_SESSION",
                "analysis_hash": "c" * 64, "actual_orders": 0,
                "actual_fills": 0, "broker_order_calls": 0,
            }
            with patch("expanded_shadow_universe_v01.paper._load_market_context", return_value={"0050": {}}), patch(
                "expanded_shadow_universe_v01.paper.replay_paper_session", side_effect=replay,
            ):
                result = publish_expanded_paper(
                    archive.run_dir, session, runtime_dir=Path(output),
                )
            self.assertEqual(captured["source_statuses"], ["COMPLETE"])
            self.assertEqual(captured["callback_errors"], 0)
            self.assertEqual(result["production_strategy_trade_count"], 0)


if __name__ == "__main__":
    unittest.main()
