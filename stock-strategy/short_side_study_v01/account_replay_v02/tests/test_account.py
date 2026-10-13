from datetime import datetime, timedelta, timezone
from unittest import TestCase
from short_side_study_v01.execution import DepthBook
from short_side_study_v01.account_replay_v02.execution import LotDepthExecution
from short_side_study_v01.account_replay_v02.run import legal_replay, safe_metrics, safe_robustness


class LegalAccountTests(TestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 1, 1, tzinfo=timezone.utc)

    def book(self, bid, ask, name='first'):
        return DepthBook(self.at, ((100., bid),), ((101., ask),), name)

    def test_no_odd_lot_fill_or_downsize(self):
        model = LotDepthExecution()
        self.assertEqual(model.fill(self.book(47, 47), at=self.at, side='SELL', quantity=1000, limit_price=100), (0, None))
        self.assertEqual(model.requests[0]['quantity'], 1000)

    def test_illegal_requested_quantity_rejected(self):
        with self.assertRaises(ValueError):
            LotDepthExecution().fill(self.book(1000, 1000), at=self.at, side='BUY', quantity=1, limit_price=101)

    def test_short_bid_cover_ask_legal_partial_exposure(self):
        model = LotDepthExecution()
        self.assertEqual(model.fill(self.book(2000, 1000), at=self.at, side='SELL', quantity=3000, limit_price=100), (2000, 100.))
        self.assertEqual(model.fill(self.book(2000, 1000), at=self.at, side='BUY', quantity=2000, limit_price=101), (1000, 101.))

    def test_raw_replenishment_not_rounding_artifact(self):
        model = LotDepthExecution()
        self.assertEqual(model.fill(self.book(2000, 2000), at=self.at, side='BUY', quantity=2000, limit_price=101)[0], 2000)
        self.assertEqual(model.fill(self.book(1500, 1500, 'smaller'), at=self.at, side='BUY', quantity=1000, limit_price=101)[0], 0)
        self.assertEqual(model.fill(self.book(2000, 2000, 'restored'), at=self.at, side='BUY', quantity=1000, limit_price=101)[0], 0)
        self.assertEqual(model.fill(self.book(2500, 2500, 'grown'), at=self.at, side='BUY', quantity=1000, limit_price=101)[0], 1000)

    def test_duplicate_and_stale_no_reuse(self):
        model = LotDepthExecution()
        book = self.book(1000, 1000)
        self.assertEqual(model.fill(book, at=self.at, side='BUY', quantity=1000, limit_price=101)[0], 1000)
        self.assertEqual(model.fill(self.book(1000, 1000, 'duplicate'), at=self.at, side='BUY', quantity=1000, limit_price=101)[0], 0)
        self.assertEqual(model.fill(book, at=self.at + timedelta(seconds=4), side='SELL', quantity=1000, limit_price=100), (0, None))

    def test_incomplete_account_stats_not_completed_subset(self):
        rows = [{'date': '1', 'status': 'SCORED_TRADE', 'net_pnl': 100}, {'date': '2', 'status': 'OPEN_EXPOSURE', 'net_pnl': None}]
        result = safe_metrics(rows)
        for key in ('net_pnl', 'profit_factor', 'win_rate', 'max_daily_realized_drawdown', 'max_quote_mark_account_drawdown'):
            self.assertIsNone(result[key])
        self.assertEqual(result['completed_subset_net_pnl'], 100)
        self.assertIsNone(safe_robustness(rows)['remove_best_trade']['net_pnl'])

    def test_full_replay_keeps_same_order_size_with_no_illegal_small_fill(self):
        from short_side_study_v01.tests.test_replay import ReplayTests
        fixture = ReplayTests()
        fixture.setUp()
        args = list(fixture.data('SHORT'))
        first = args[2]['TEST'][0]
        args[2]['TEST'][0] = DepthBook(first.received_at, ((100., 47),), first.asks, first.snapshot_id)
        row = legal_replay(*args, 'S1', 'BASE_CURRENT_EXECUTION', 1)
        self.assertEqual(row['status'], 'UNFILLED_ENTRY')
        self.assertEqual(row['target_quantity'], 1000)
        self.assertEqual(row['entry_quantity'], 0)
        self.assertEqual(row['depth_fill_evidence'], [])

    def test_full_replay_retains_legal_partial_cover_exposure(self):
        from dataclasses import replace
        from short_side_study_v01.tests.test_replay import ReplayTests
        fixture = ReplayTests()
        fixture.setUp()
        args = list(fixture.data('SHORT', 1000))
        args[0] = replace(args[0], entry_price=90.)
        first = args[2]['TEST'][0]
        args[2]['TEST'][0] = DepthBook(first.received_at, ((100., 2000),), ((100.5, 0),), first.snapshot_id)
        row = legal_replay(*args, 'S1', 'BASE_CURRENT_EXECUTION', 1)
        self.assertEqual(row['status'], 'OPEN_EXPOSURE')
        self.assertEqual(row['target_quantity'], 2000)
        self.assertEqual(row['entry_quantity'], 2000)
        self.assertEqual(row['covered_shares'], 1000)
        self.assertEqual(row['residual_shares'], 1000)
        self.assertIsNone(row['net_pnl'])
