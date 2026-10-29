"""Synthetic runner contracts: exact drawdown, scope, cohorts and no PnL editing."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import ast
import unittest
from zoneinfo import ZoneInfo

from cash_short_intraday_research_v01.round2 import run as r
from cash_short_intraday_research_v01.round2 import SAFETY


AT = datetime(2026, 10, 2, 10, tzinfo=ZoneInfo('Asia/Taipei'))
CONFIG = {'capital_twd': 190000, 'split': {
    'audit_only': [],
    'train': {'start': '20260924', 'end': '20261016'},
    'validation': {'start': '20261019', 'end': '20261022'},
    'retrospective_holdout': {'start': '20261027', 'end': '20261029'},
}}


def daily(day='20261002', start=190000, values=(), net=0, split='train', complete=True):
    path = [{'net': n, 'time': AT + timedelta(seconds=i)} for i, n in enumerate(values)]
    extrema = r.day_extrema([{'path': path}] if path else [])
    return {'day': day, 'split': split, 'starting_equity': start,
            'equity': start + net if net is not None else None,
            'net_pnl': net, 'account_complete': complete,
            'running_max_drawdown_twd': 0, **extrema}


def state(days=()):
    return {'trades': [], 'daily': list(days), 'issues': [], 'dispositions': []}


def enriched(*, identity='s1-test', market=True, rs=-.003):
    return {'day': '20261002', 'symbol': 'TEST', 'signal_id': identity,
            'entry_model': 'S1', 'round2_features': {
                'market_available': market is not None, 'market_weak': market,
                'market_unavailable_reasons': ['UNKNOWN_0050'] if market is None else [],
                'rs_available': rs is not None, 'rs_300': rs,
                'rs_unavailable_reasons': ['UNKNOWN_RS'] if rs is None else [],
                'vwap_slope_available': True, 'vwap_down_180s': True,
                'entry_distance_fraction': .002,
            }}


class DrawdownContracts(unittest.TestCase):
    def test_no_trade_day_has_zero_observed_path_not_fake_points(self):
        out = r.day_extrema([])
        self.assertEqual(out['daily_valuation_points'], 0)
        self.assertEqual(out['daily_path_max_net'], 0)
        self.assertEqual(out['daily_path_min_net'], 0)
        self.assertEqual(out['daily_path_local_drawdown_twd'], 0)
        self.assertIsNone(out['daily_peak_time'])

    def test_min_before_max_is_not_fictitious_max_minus_min_drawdown(self):
        d = daily(start=1000, values=(-100, 200), net=200)
        self.assertEqual(d['daily_path_local_drawdown_twd'], 100)
        self.assertEqual(r.recompute_dd([d]), 100)
        self.assertNotEqual(r.recompute_dd([d]), 300)

    def test_max_before_min_counts_full_intraday_giveback(self):
        d = daily(start=1000, values=(200, -100), net=-100)
        self.assertEqual(d['daily_path_local_drawdown_twd'], 300)
        self.assertEqual(r.recompute_dd([d]), 300)

    def test_exact_cross_day_peak_and_new_split_reset(self):
        days = [daily(start=1000, values=(-100, 200), net=200),
                daily(day='20261005', start=1200, values=(-50, 500, 400), net=400),
                daily(day='20261019', start=1600, values=(-300, 0), net=0, split='validation')]
        self.assertEqual(r.recompute_dd(days[:2]), 100)
        self.assertEqual(r.recompute_dd(days), 400)
        self.assertEqual(r.recompute_dd(days[2:]), 300)

    def test_day_end_mark_included_when_not_in_intraday_path(self):
        d = daily(start=1000, values=(200,), net=-100)
        self.assertEqual(r.recompute_dd([d]), 300)

    def test_empty_split_has_unknown_not_zero_drawdown(self):
        self.assertIsNone(r.recompute_dd([]))


class MetricsScopeContracts(unittest.TestCase):
    def test_unknown_false_and_out_of_common_scope_are_separate(self):
        rows = [dict(day='20261002', signal_id='unknown', scope_included=True,
                     evaluable=False, **{'pass': None}),
                dict(day='20261002', signal_id='blocked', scope_included=True,
                     evaluable=True, **{'pass': False}),
                dict(day='20261002', signal_id='outside', scope_included=False,
                     evaluable=True, **{'pass': True})]
        m = r.metrics(state([daily()]), rows, CONFIG)
        self.assertEqual(m['original_s1_candidate_count'], 3)
        self.assertEqual(m['evaluable_signal_count'], 1)
        self.assertEqual(m['unavailable_signal_count'], 1)
        self.assertEqual(m['common_scope_excluded_count'], 1)
        self.assertEqual(m['condition_rejected_count'], 1)
        self.assertEqual(m['passed_condition_count'], 0)
        self.assertEqual(m['effective_dates'], ['20261002'])
        self.assertEqual(m['net_pnl'], 0)
        self.assertIsNone(m['win_rate'])
        self.assertIsNone(m['profit_factor'])

    def test_split_metrics_use_split_starting_equity_and_not_prior_peak(self):
        days = [daily(start=1000, values=(500, 200), net=200),
                daily(day='20261019', start=1200, values=(-100, 0), net=0, split='validation')]
        # This deliberately carries a prior full-account running DD, which
        # must not contaminate fresh split drawdown or its denominator.
        days[1]['running_max_drawdown_twd'] = 9999
        m = r.metrics(state(days), [], CONFIG, split='validation')
        self.assertEqual(m['max_drawdown_twd'], 100)
        self.assertEqual(m['observed_partial_max_drawdown_twd'], 100)
        self.assertAlmostEqual(m['max_drawdown_fraction'], 100/1200)
        self.assertEqual(m['archive_calendar_days'], 1)

    def test_incomplete_unvalued_account_keeps_primary_pnl_and_dd_unknown(self):
        m = r.metrics(state([daily(net=None, complete=False)]), [], CONFIG)
        self.assertIsNone(m['net_pnl'])
        self.assertIsNone(m['max_drawdown_twd'])
        self.assertIsNone(m['max_drawdown_fraction'])
        self.assertEqual(m['observed_partial_max_drawdown_twd'], 0)
        self.assertFalse(m['selection_threshold_met'])

    def test_zero_trades_does_not_meet_selection_or_create_win_rate(self):
        m = r.metrics(state([daily()]), [], CONFIG)
        self.assertFalse(m['selection_threshold_met'])
        self.assertEqual(m['entries'], 0)
        self.assertIsNone(m['win_rate'])
        self.assertIsNone(m['profit_factor'])
        self.assertEqual(m['prospective_validation'], 'PENDING')
        self.assertEqual(m['sample_status'], 'ALL_DATES_SEEN_EXPLORATORY')


class CohortAndPairContracts(unittest.TestCase):
    def test_cohorts_join_only_b_same_scenario_id_and_correct_phase(self):
        trades = [{'experiment': e, 'scenario': s, 'signal_id': 'same-id', 'day': '20261002'}
                  for e, s in [('B', 'BASE'), ('B', 'WORSE'), ('C', 'BASE')]]
        fills = [{'experiment': e, 'scenario': s, 'signal_id': 'same-id',
                  'phase': p, 'quantity': q}
                 for e, s, p, q in [('B', 'BASE', 'ENTRY', 1000), ('B', 'BASE', 'EXIT', 1000),
                                    ('B', 'WORSE', 'ENTRY', 2000), ('B', 'WORSE', 'EXIT', 2000),
                                    ('C', 'BASE', 'ENTRY', 999000)]]
        tables = {'trades': trades, 'fills': fills}
        before = deepcopy(tables)
        out = r.original_cohorts(tables)
        self.assertEqual(out['BASE']['20261002']['entry_fills'][0]['quantity'], 1000)
        self.assertEqual(out['BASE']['20261002']['exit_fills'][0]['quantity'], 1000)
        self.assertEqual(out['WORSE']['20261002']['entry_fills'][0]['quantity'], 2000)
        self.assertEqual(tables, before)

    def test_blocked_pairs_keep_unknown_separate_and_do_not_subtract_loss(self):
        signals = [enriched(identity='loss', market=False),
                   enriched(identity='win-unknown', market=None, rs=None)]
        cohorts = {'BASE': {
            '20261002': {'signal_id': 'loss', 'net_pnl': -1000, 'quantity': 1000},
        }}
        pairs = r.add_pair_diagnostics(signals, cohorts)
        r1 = next(row for row in pairs if row['variant'] == 'R1')
        self.assertEqual(r1['round1_R0_original_net_pnl'], -1000)
        self.assertEqual(r1['original_outcome'], 'LOSS')
        self.assertEqual(r1['decision'], 'BLOCKED_CONDITION')
        self.assertIn('NOT_SUBTRACTION', r1['interpretation'])
        self.assertNotIn('counterfactual_account_pnl', r1)
        # Diagnostic amounts must not enter account metrics or incremental PnL.
        states = {(s, 'ENTRY', 'OWN_EVALUABLE', v): state([daily()])
                  for s, v in [('BASE', 'R0'), ('BASE', 'R1')]}
        decisions = []
        for version in ('R0', 'R1'):
            decisions.append({'scenario': 'BASE', 'phase': 'ENTRY', 'scope': 'OWN_EVALUABLE',
                              'variant': version, 'day': '20261002', 'signal_id': 'loss',
                              'scope_included': True, 'evaluable': True, 'pass': version == 'R0'})
        rows = r.build_experiments(states, decisions, CONFIG, pairs)
        actual = next(x for x in rows if x['variant'] == 'R1' and x['statistics_scope'] == 'FULL_PERIOD')
        self.assertEqual(actual['blocked_original_losses'], 1)
        self.assertEqual(actual['net_pnl'], 0)
        self.assertEqual(actual['incremental_net_vs_R0'], 0)

    def test_unavailable_original_win_is_not_condition_rejected_win(self):
        signal = enriched(market=None, rs=None)
        pairs = r.add_pair_diagnostics([signal], {'BASE': {'20261002': {
            'signal_id': signal['signal_id'], 'net_pnl': 500, 'quantity': 1000}}})
        for variant in ('R1', 'R2', 'R5'):
            row = next(x for x in pairs if x['variant'] == variant)
            self.assertIs(row['evaluable'], False)
            self.assertEqual(row['decision'], 'UNAVAILABLE')
            self.assertEqual(row['original_outcome'], 'WIN')

    def test_cross_scenario_and_cross_scope_r0_references_do_not_mix(self):
        states = {(s, 'ENTRY', scope, v): state([daily(net=net)])
                  for s, scope, v, net in [('BASE', 'OWN', 'R0', 100),
                                            ('BASE', 'OWN', 'R1', 50),
                                            ('BASE', 'COMMON', 'R0', -200),
                                            ('BASE', 'COMMON', 'R1', -20),
                                            ('WORSE', 'OWN', 'R0', -500),
                                            ('WORSE', 'OWN', 'R1', -100)]}
        rows = r.build_experiments(states, [], CONFIG, [])
        full = {(x['scenario'], x['scope'], x['variant']): x
                for x in rows if x['statistics_scope'] == 'FULL_PERIOD'}
        self.assertEqual(full['BASE', 'OWN', 'R1']['incremental_net_vs_R0'], -50)
        self.assertEqual(full['BASE', 'COMMON', 'R1']['incremental_net_vs_R0'], 180)
        self.assertEqual(full['WORSE', 'OWN', 'R1']['incremental_net_vs_R0'], 400)


class RunnerSafetyContracts(unittest.TestCase):
    def test_runner_keeps_all_zero_live_safety_contract(self):
        self.assertEqual(SAFETY['mode'], 'SHADOW_ONLY')
        self.assertIs(SAFETY['NO_LIVE_ORDER'], True)
        for key in ('actual_orders', 'broker_connections', 'market_subscriptions'):
            self.assertEqual(SAFETY[key], 0)

    def test_runner_imports_no_broker_sdk_or_runtime_entrypoint(self):
        tree = ast.parse(Path(r.__file__).read_text())
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(x.name for x in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or '')
        forbidden = ('requests', 'socket', 'broker', 'sdk', 'adapter', 'collector_main',
                     'yuanta_live_runtime_v01.main', 'auto_runner')
        for module in modules:
            self.assertFalse(any(word in module.lower() for word in forbidden), module)


if __name__ == '__main__':
    unittest.main()
