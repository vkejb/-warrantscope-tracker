from datetime import datetime, timedelta
from unittest import TestCase
from yuanta_intraday_shadow_v01.direction_follow_backtest import TAIPEI
from short_side_study_v01.policy import StudyEngine, short_gate


class PolicyTests(TestCase):
    def test_fixed_symmetric_thresholds(self):
        for regime, threshold, confirmations in [('BEARISH', 0, 1), ('NEUTRAL', -.0025, 2), ('BULLISH', -.005, 2)]:
            self.assertEqual(short_gate(regime, threshold), (True, confirmations))
            self.assertEqual(short_gate(regime, threshold + .000001), (False, confirmations))
        self.assertEqual(short_gate('BEARISH', float('nan')), (False, 0))

    def test_combined_boundary_uses_score_without_pnl(self):
        engine = StudyEngine({'A':'A', 'B':'B'}, variant='S3')
        engine._market_context = lambda _: {'market_regime':'BEARISH', 'benchmark_return_5m':0., 'benchmark_vwap_gap':-.001}
        def signal(symbol, at):
            return dict(stock_id=symbol, stock_name=symbol, side='LONG' if symbol=='A' else 'SHORT',
                        decision_time=at, score=.8 if symbol=='A' else .7,
                        volume_delta=.5, large_trade_delta=.5, vwap_gap=.001,
                        book_imbalance=.5, spread_bps=10., entry_price=100., quantity=1000,
                        return_5m=.006 if symbol=='A' else -.001,
                        directional_opening_extension=.001, directional_vwap_extension=.001,
                        breakout_boundary_price=99.)
        engine._signal_for = signal
        at = datetime(2026,10,2,9,10,tzinfo=TAIPEI)
        self.assertEqual(engine.choose_study_entry(at).side, 'SHORT')
        self.assertEqual(engine.choose_study_entry(at+timedelta(seconds=30)).side, 'LONG')
        self.assertEqual(len(engine.simultaneous_cases), 1)

    def test_anti_chase_blocks_short_and_no_live_flag_needed(self):
        engine = StudyEngine({'A':'A'}, variant='S2')
        engine._market_context = lambda _: {'market_regime':'BEARISH', 'benchmark_return_5m':0., 'benchmark_vwap_gap':-.001}
        engine._signal_for = lambda _, at: dict(side='SHORT', return_5m=-.01, directional_opening_extension=.021, directional_vwap_extension=.001)
        self.assertIsNone(engine.choose_study_entry(datetime(2026,10,2,9,10,tzinfo=TAIPEI)))

    def test_known_ineligible_short_is_filtered(self):
        engine=StudyEngine({'A':'A'},variant='S1',eligibility={'A':False})
        engine._signal_for=lambda _,at: {'side':'SHORT'}
        self.assertIsNone(engine.choose_study_entry(datetime(2026,10,2,9,10,tzinfo=TAIPEI)))

    def test_inherits_base_thresholds_and_symmetric_exit_unchanged(self):
        from yuanta_live_runtime_v01.strategy import LiveDirectionEngine
        self.assertIs(StudyEngine._signal_for,LiveDirectionEngine._signal_for)
        self.assertIs(StudyEngine.evaluate_exit,LiveDirectionEngine.evaluate_exit)

    def test_stale_benchmark_counts_base_signal_but_never_approves(self):
        engine=StudyEngine({'A':'A'},variant='S2')
        engine._market_context=lambda _:None
        engine._signal_for=lambda _,at:dict(stock_id='A',side='SHORT')
        self.assertIsNone(engine.choose_study_entry(datetime(2026,10,2,9,10,tzinfo=TAIPEI)))
        self.assertEqual(len(engine.raw_at_boundary),1)
        self.assertEqual(engine.approved_at_boundary,[])
        self.assertEqual(engine._previous,{})
