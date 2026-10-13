from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zoneinfo import ZoneInfo

from intraday_paper_execution_v01.engine import InvalidTransition, ReconciliationError, DuplicateOrderConflict
from raw_short_shadow_v01.safety import ShortCoverSafety


class CoverSafetyTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'mock_short_account.sqlite'
        self.at = datetime(2026, 10, 13, 9, 10, tzinfo=ZoneInfo('Asia/Taipei'))
        self.s = ShortCoverSafety(self.path, account='S1_MOCK', symbol='TEST', baseline=5000)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def entered(self, requested=3000, filled=2000):
        order = self.s.entry('entry', requested, 100, self.at)
        self.s.fill(order.order_id, 'entry-fill', filled, 100)
        order = self.s.engine.get_order(order.order_id)
        if order.remaining_quantity:
            self.s.cancel(order.order_id)
            self.s.cancel_ack(order.order_id)
            self.s.reconcile(**self.s.mock_snapshot({'entry': 'CANCELED'}))
        else:
            self.s.reconcile(**self.s.mock_snapshot())
        return order

    def cover(self, key='cover', at=None, ask=101., quantity=5000, quote_at=None):
        at = at or self.at
        return self.s.cover(key, at=at, ask=ask, ask_quantity=quantity, quote_at=quote_at or at)

    def settle(self, key):
        self.s.reconcile(**self.s.mock_snapshot({key: 'CANCELED'}))

    def test_partial_sell_manages_only_filled_not_requested(self):
        self.entered(3000, 1000)
        self.assertEqual(self.s.remaining(), 1000)
        self.assertEqual(self.cover().quantity, 1000)

    def test_partial_cover_reserves_remaining_no_second_cover(self):
        self.entered()
        order = self.cover()
        self.s.fill(order.order_id, 'cover-fill', 1000, 101)
        self.s.reconcile(**self.s.mock_snapshot())
        self.assertEqual(self.s.remaining(), 1000)
        self.assertIsNone(self.cover('second'))
        self.assertNotEqual(self.s.snapshot()['status'], 'FLAT')

    def test_duplicate_fill_and_same_intent_do_not_add_shares(self):
        self.entered()
        order = self.cover()
        self.assertEqual(self.cover().order_id, order.order_id)
        self.s.fill(order.order_id, 'duplicate', 1000, 101)
        self.s.fill(order.order_id, 'duplicate', 1000, 101)
        self.assertEqual(self.s.remaining(), 1000)
        with self.assertRaises(DuplicateOrderConflict):
            self.s.fill(order.order_id, 'duplicate', 1000, 102)

    def test_rejection_cannot_leave_flat_and_retry_needs_reconcile(self):
        self.entered()
        order = self.cover()
        self.s.failure(order.order_id, 'REJECTED')
        self.assertIsNone(self.cover('retry-before-reconcile'))
        self.assertEqual(self.s.remaining(), 2000)
        self.settle('cover')
        self.assertEqual(self.cover('retry-after-reconcile').quantity, 2000)

    def test_partial_fill_then_failure_rescues_real_remainder(self):
        self.entered()
        order = self.cover()
        self.s.fill(order.order_id, 'partial', 1000, 101)
        self.s.failure(order.order_id, 'REJECTED')
        self.settle('cover')
        self.assertEqual(self.cover('rescue').quantity, 1000)

    def test_cancel_pending_then_fill_does_not_overbuy(self):
        self.entered()
        order = self.cover()
        self.s.cancel(order.order_id)
        self.s.cancel_ack(order.order_id)
        self.s.fill(order.order_id, 'while-cancel-pending', 1000, 101)
        self.assertIsNone(self.cover('too-soon'))
        self.settle('cover')
        rescue = self.cover('after-final-cancel')
        self.assertEqual(rescue.quantity, 1000)
        self.s.fill(rescue.order_id, 'rescue-fill', 1000, 101)
        self.s.reconcile(**self.s.mock_snapshot())
        self.assertEqual(self.s.snapshot()['status'], 'FLAT')

    def test_cancel_ack_before_final_snapshot_keeps_reservation(self):
        self.entered()
        order = self.cover()
        self.s.cancel(order.order_id)
        self.s.cancel_ack(order.order_id)
        self.assertIsNone(self.cover('duplicate-rescue'))
        self.assertTrue(self.s.snapshot()['pending_orders'])

    def test_cancel_failure_and_unknown_keep_exposure(self):
        self.entered()
        order = self.cover()
        for reason in ('CANCEL_FAILURE', 'UNKNOWN'):
            self.s.failure(order.order_id, reason)
            snapshot = self.s.mock_snapshot()
            snapshot['orders']['cover']['status'] = 'UNKNOWN'
            with self.assertRaises(ReconciliationError):
                self.s.reconcile(**snapshot)
            self.assertIsNone(self.cover(reason))
            self.assertEqual(self.s.remaining(), 2000)

    def test_restart_restores_fills_and_reservations_requires_snapshot(self):
        self.entered()
        order = self.cover()
        self.s.fill(order.order_id, 'partial-before-crash', 1000, 101)
        self.s.close()
        self.s = ShortCoverSafety(self.path, account='S1_MOCK', symbol='TEST', baseline=5000)
        self.assertEqual(self.s.remaining(), 1000)
        self.assertIsNone(self.cover('restart-before-reconcile'))
        self.s.reconcile(**self.s.mock_snapshot())
        self.assertIsNone(self.cover('still-reserved'))

    def test_reconcile_lost_callback_before_releasing_cancel(self):
        self.entered()
        order = self.cover()
        self.s.cancel(order.order_id)
        self.s.cancel_ack(order.order_id)
        snapshot = self.s.mock_snapshot({'cover': 'CANCELED'})
        snapshot['orders']['cover'].update(filled=1000, fill_price=101)
        snapshot['inventory'] = 4000
        self.s.reconcile(**snapshot)
        self.assertEqual(self.cover('remaining-only').quantity, 1000)

    def test_manual_cover_before_rescue_preserves_baseline(self):
        self.entered()
        self.s.manual_cover('manual', 1000, 101)
        self.s.manual_cover('manual', 1000, 101)
        self.s.reconcile(**self.s.mock_snapshot())
        self.assertEqual(self.cover().quantity, 1000)
        with self.assertRaises(ReconciliationError):
            self.s.manual_cover('excess-manual', 2000, 101)

    def test_manual_cover_with_open_buy_cannot_mark_flat(self):
        self.entered()
        order = self.cover()
        self.s.manual_cover('manual-all', 2000, 101)
        self.s.reconcile(**self.s.mock_snapshot())
        self.assertIsNone(self.cover('extra'))
        self.assertNotEqual(self.s.snapshot()['status'], 'FLAT')
        self.s.cancel(order.order_id)
        self.s.cancel_ack(order.order_id)
        self.settle('cover')
        self.assertEqual(self.s.snapshot()['status'], 'FLAT')

    def test_limit_up_no_ask_stays_uncovered(self):
        self.entered()
        self.assertIsNone(self.cover(ask=None, quantity=0))
        self.assertEqual(self.s.remaining(), 2000)
        self.assertNotEqual(self.s.snapshot()['status'], 'FLAT')

    def test_cross_1320_no_entry_but_cover_remains_required(self):
        self.entered()
        after = self.at.replace(hour=13, minute=21)
        self.assertEqual(self.cover(at=after).quantity, 2000)
        with self.assertRaises(InvalidTransition):
            self.s.entry('late-entry', 1000, 100, after)

    def test_utc_clock_converts_to_taipei_hard_exit(self):
        after = self.at.replace(hour=13, minute=21).astimezone(ZoneInfo('UTC'))
        with self.assertRaises(InvalidTransition):
            self.s.entry('late-utc-entry', 1000, 100, after)

    def test_stale_ask_does_not_use_last_price(self):
        self.entered()
        self.assertIsNone(self.cover(quote_at=self.at - timedelta(seconds=4)))
        self.assertEqual(self.s.remaining(), 2000)

    def test_no_filled_entry_no_cover_intent(self):
        order = self.s.entry('entry', 3000, 100, self.at)
        self.s.reconcile(**self.s.mock_snapshot())
        self.assertIsNone(self.cover())
        self.assertNotEqual(self.s.snapshot()['status'], 'FLAT')

    def test_cannot_overbuy_on_fill_or_change_baseline(self):
        self.entered()
        order = self.cover()
        with self.assertRaises(ReconciliationError):
            self.s.fill(order.order_id, 'overbuy', 3000, 101)
        with self.assertRaises(ValueError):
            ShortCoverSafety(self.path, account='S1_MOCK', symbol='TEST', baseline=6000)

    def test_real_snapshot_and_real_database_names_forbidden(self):
        with self.assertRaises(ValueError):
            self.s.reconcile(mode='BROKER_LIVE', inventory=5000, orders={})
        with self.assertRaises(ValueError):
            ShortCoverSafety(Path(self.tmp.name) / 'live.sqlite', account='S1', symbol='TEST')
