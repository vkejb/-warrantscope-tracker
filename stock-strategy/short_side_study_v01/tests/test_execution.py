from datetime import datetime, timedelta, timezone
from unittest import TestCase
from short_side_study_v01.execution import DepthBook, DepthExecution, Exposure


class ExecutionTests(TestCase):
    def setUp(self):
        self.at = datetime(2026,10,2,1,10,tzinfo=timezone.utc)
        self.book = DepthBook(self.at, ((100.,300),(99.9,200)), ((100.1,100),(100.2,200)), 'b1')

    def test_short_sells_bid_and_covers_ask(self):
        model = DepthExecution()
        qty, price = model.fill(self.book, at=self.at, side='SELL', quantity=1000, limit_price=99.9)
        self.assertEqual(qty, 500)
        self.assertAlmostEqual(price, 99.96)
        exposure = Exposure('SHORT', qty, qty)
        cover, price = model.fill(self.book, at=self.at, side='BUY', quantity=qty, limit_price=100.2)
        self.assertEqual(cover, 300)
        self.assertGreater(price, 100.)
        exposure.apply_exit(cover)
        self.assertEqual(exposure.remaining_shares, 200)
        self.assertEqual(exposure.status, 'OPEN_EXPOSURE')

    def test_depth_cannot_be_reused(self):
        model = DepthExecution()
        model.fill(self.book, at=self.at, side='BUY', quantity=300, limit_price=100.2)
        qty, price = model.fill(self.book, at=self.at, side='BUY', quantity=200, limit_price=100.2)
        self.assertEqual((qty, price), (0, None))

    def test_future_or_stale_depth_cannot_fill(self):
        for at in (self.at-timedelta(seconds=1), self.at+timedelta(seconds=4)):
            self.assertEqual(DepthExecution().fill(self.book, at=at, side='SELL', quantity=100, limit_price=99.), (0,None))

    def test_identical_callback_does_not_replenish_consumed_liquidity(self):
        model=DepthExecution()
        first=DepthBook(self.at,((100.,100),),((100.1,100),),'first')
        duplicate=DepthBook(self.at,first.bids,first.asks,'duplicate')
        grown=DepthBook(self.at,first.bids,((100.1,200),),'grown')
        self.assertEqual(model.fill(first,at=self.at,side='BUY',quantity=100,limit_price=100.1)[0],100)
        self.assertEqual(model.fill(duplicate,at=self.at,side='BUY',quantity=100,limit_price=100.1)[0],0)
        self.assertEqual(model.fill(grown,at=self.at,side='BUY',quantity=200,limit_price=100.1)[0],100)
