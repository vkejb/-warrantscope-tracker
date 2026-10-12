from datetime import datetime,timedelta
from unittest import TestCase
from unittest.mock import patch

from yuanta_intraday_shadow_v01.direction_follow_backtest import TAIPEI
from yuanta_live_runtime_v01.strategy import LiveSignal
from short_side_study_v01.s4 import choose_boundary,chronological_selection,replacements


def signal(side,at,score=.5,symbol='TEST'):
    return LiveSignal(symbol,symbol,side,at,score,.5,.5,.001,.2,10.,100.,1000)


class S4Tests(TestCase):
    def setUp(self):
        self.at=datetime(2026,10,1,9,10,tzinfo=TAIPEI)

    def test_same_boundary_uses_existing_score_then_notional(self):
        long=signal('LONG',self.at,.5,'A')
        short=signal('SHORT',self.at,.6,'B')
        self.assertIs(choose_boundary(long,short),short)
        short=signal('SHORT',self.at,.5,'B')
        self.assertIs(choose_boundary(long,short),long)

    def test_earlier_short_consumes_day_before_later_higher_score_long(self):
        at=self.at
        class FakeEngine:
            def __init__(self,*args,variant,**kwargs):
                self.variant=variant
            def choose_study_entry(self,decision):
                if self.variant=='S1' and decision==at:
                    return signal('SHORT',decision,.1,'SHORT_FIRST')
                if self.variant=='S0' and decision==at+timedelta(seconds=30):
                    return signal('LONG',decision,1.,'LONG_LATER')
                return None
            def _market_context(self,decision):
                return None
        stocks={'TEST':{'ticks':[],'books':[]}}
        with patch('short_side_study_v01.s4.StudyEngine',FakeEngine),patch('short_side_study_v01.s4._feed_until'),patch('short_side_study_v01.s4._decision_times',return_value=[at,at+timedelta(seconds=30)]):
            first,_,_,regime,_=chronological_selection(stocks,{},'20261001',{})
        self.assertEqual(first['S4'].stock_id,'SHORT_FIRST')
        self.assertEqual(first['S0'].stock_id,'LONG_LATER')
        self.assertEqual(regime,'UNKNOWN')

    def test_counterfactual_is_never_added_to_account_pnl(self):
        rows=[]
        for variant,side,pnl in [('S0','LONG',100.),('S1','SHORT',-50.),('S4','SHORT',-50.)]:
            rows.append(dict(date='20261001',variant=variant,scenario='BASE_CURRENT_EXECUTION',volume_multiplier=1000,side=side,net_pnl=pnl,stock_id=variant,signal_time=self.at.isoformat()))
        result=replacements(rows)[0]
        self.assertEqual(result['account_net_pnl'],-50.)
        self.assertEqual(result['displaced_counterfactual_pnl'],100.)
        self.assertEqual(result['increment_vs_s0'],-150.)
        self.assertTrue(result['long_winner_displaced'])

    def test_later_short_cannot_replace_long_even_if_long_loses(self):
        rows=[]
        for variant,side,pnl in [('S0','LONG',-100.),('S1','SHORT',200.),('S4','LONG',-100.)]:
            rows.append(dict(date='20261001',variant=variant,scenario='BASE_CURRENT_EXECUTION',volume_multiplier=1000,side=side,net_pnl=pnl,stock_id=variant,signal_time=self.at.isoformat()))
        result=replacements(rows)[0]
        self.assertEqual(result['kind'],'LONG_BLOCKS_LATER_SHORT')
        self.assertEqual(result['account_net_pnl'],-100.)
        self.assertEqual(result['displaced_counterfactual_pnl'],200.)
