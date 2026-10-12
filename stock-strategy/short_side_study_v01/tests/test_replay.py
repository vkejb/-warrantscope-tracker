from datetime import datetime, timedelta
from unittest import TestCase
from short_side_study_v01.execution import DepthBook
from short_side_study_v01.replay import replay_trade
from short_side_study_v01.statistics import metrics,robustness,correlation
from yuanta_intraday_shadow_v01.direction_follow_backtest import TAIPEI
from yuanta_live_runtime_v01.strategy import LiveSignal, ManagedPosition, LiveDirectionEngine


class ReplayTests(TestCase):
    def setUp(self):
        self.at=datetime(2026,10,2,9,10,tzinfo=TAIPEI)
        self.hard=self.at.replace(hour=13,minute=20)

    def data(self,side,cover_quantity=1000):
        signal=LiveSignal('TEST','TEST',side,self.at,.8,-.5,-.5,-.001,-.5,10.,99.5 if side=='SHORT' else 101.,1000)
        stamps=[self.at,self.hard]
        ticks=[dict(time=t,received_at=t,exchange_time=t,price=100.,volume=10.,bid=100.,ask=100.5,flag='0',serial=i+1) for i,t in enumerate(stamps)]
        books=[dict(time=t,best_bid=100.,best_ask=100.5,buy_volume=10.,sell_volume=10.) for t in stamps]
        stocks={'TEST':{'ticks':ticks,'books':books}}
        depths={'TEST':[DepthBook(self.at,((100.,1000),),((100.5,1000),),'entry'),
                         DepthBook(self.hard,((100.,cover_quantity),),((100.5,cover_quantity),),'exit')]}
        manifest={'run_id':'TEST_OFFLINE','ended_at':(self.hard+timedelta(minutes=15)).isoformat()}
        return signal,stocks,depths,manifest

    def test_short_hard_exit_and_costs(self):
        args=self.data('SHORT')
        row=replay_trade(*args,'S2','BASE_CURRENT_EXECUTION',1)
        self.assertEqual(row['status'],'SCORED_TRADE')
        self.assertEqual(row['exit_reason'],'HARD_EXIT')
        self.assertEqual(row['exit_price'],100.5)
        self.assertLess(row['net_pnl'],row['gross_pnl'])
        self.assertEqual(row['residual_shares'],0)

    def test_partial_cover_never_fabricates_flat_pnl(self):
        row=replay_trade(*self.data('SHORT',100),'S2','BASE_CURRENT_EXECUTION',1)
        self.assertEqual(row['status'],'OPEN_EXPOSURE')
        self.assertEqual(row['residual_shares'],900)
        self.assertIsNone(row['net_pnl'])

    def test_delay_cannot_use_pre_delay_quote(self):
        row=replay_trade(*self.data('SHORT'),'S2','DELAY_5_SECONDS',1)
        self.assertEqual(row['status'],'ENTRY_DELAY_DATA_UNAVAILABLE')

    def test_stats_exposure_is_not_zero(self):
        rows=[dict(date='1',status='SCORED_TRADE',net_pnl=100),dict(date='2',status='SCORED_TRADE',net_pnl=-50),dict(date='3',status='OPEN_EXPOSURE',net_pnl=None)]
        m=metrics(rows)
        self.assertEqual(m['profit_factor'],2.)
        self.assertEqual(m['max_daily_realized_drawdown'],50.)
        self.assertFalse(m['account_complete'])
        self.assertIsNone(robustness(rows)['remove_best_trade']['net_pnl'])
        self.assertEqual(robustness(rows)['remove_best_trade']['completed_subset_net_pnl'],-50)
        self.assertEqual(correlation(rows,rows),1.)

    def test_long_short_fixed_3500_risk_basis(self):
        engine=LiveDirectionEngine({'TEST':'TEST'})
        for side in ('LONG','SHORT'):
            position=ManagedPosition('TEST','TEST',side,1000,100.,'OFFLINE',self.at)
            price=engine._derive_initial_stop_price(position)
            self.assertLess(price,100.) if side=='LONG' else self.assertGreater(price,100.)
            self.assertAlmostEqual(engine.projected_net(position,price),-3500.,delta=.02)

    def test_short_pnl_direction_and_sell_tax(self):
        from yuanta_intraday_shadow_v01.direction_follow_backtest import _projected_net_pnl
        profit=_projected_net_pnl('SHORT',100.,90.,1000)
        loss=_projected_net_pnl('SHORT',100.,110.,1000)
        self.assertEqual(profit[0],10000.)
        self.assertEqual(loss[0],-10000.)
        self.assertEqual(profit[2],loss[2])
        self.assertLess(profit[3],10000.)

    def test_quote_path_metrics_are_reported_without_changing_exit(self):
        row=replay_trade(*self.data('SHORT'),'S2','BASE_CURRENT_EXECUTION',1)
        self.assertIn('mfe_net_pnl',row)
        self.assertIn('mae_net_pnl',row)
        self.assertGreaterEqual(row['mfe_net_pnl'],row['net_pnl'])
        self.assertGreaterEqual(row['giveback_net_pnl'],0.)
