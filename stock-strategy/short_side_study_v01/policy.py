"""Predeclared symmetric gate; production base signals and exits remain unchanged."""
from dataclasses import asdict, replace
from datetime import timedelta
import math

from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC, TAIPEI
from yuanta_live_runtime_v01.strategy import (
    ANTI_CHASE_ENTRY_POLICY, LONG_MARKET_REGIME_POLICY,
    LiveDirectionEngine, LiveSignal,
)
from yuanta_intraday_shadow_v01.exploratory_backtest import _tick_size

SHORT_GATE = {
    'BEARISH': {'maximum_relative_strength': 0.0, 'confirmations': 1},
    'NEUTRAL': {'maximum_relative_strength': -0.0025, 'confirmations': 2},
    'BULLISH': {'maximum_relative_strength': -0.005, 'confirmations': 2},
}
VARIANTS = ('S0', 'S1', 'S2', 'S3')


def short_gate(regime, relative_strength):
    rule = SHORT_GATE.get(regime)
    if rule is None or relative_strength is None or not math.isfinite(relative_strength):
        return False, 0
    return relative_strength <= rule['maximum_relative_strength'], rule['confirmations']


class StudyEngine(LiveDirectionEngine):
    """Only the offline entry selector differs; inherited thresholds never change.

    Confirmations retain production semantics: consecutive base signals, then
    the current boundary's market and anti-chase gates. S3 ranks approved signals
    at that boundary only and never examines subsequent PnL.
    """
    def __init__(self, *args, variant, eligibility=None, adverse_exit_ticks=0, **kwargs):
        if variant not in VARIANTS:
            raise ValueError(variant)
        super().__init__(*args, **kwargs)
        self.variant = variant
        self.eligibility = dict(eligibility or {})
        self.adverse_exit_ticks = adverse_exit_ticks
        self.approved_at_boundary = []
        self.simultaneous_cases = []
        self.raw_at_boundary = []
        self.diagnostics_at_boundary = []

    def safe_exit_quote(self, position, now, *, max_age_seconds=3., max_spread_bps=300.):
        quote=super().safe_exit_quote(position,now,max_age_seconds=max_age_seconds,max_spread_bps=max_spread_bps)
        if quote is None or not self.adverse_exit_ticks:
            return quote
        price=quote.price
        for _ in range(self.adverse_exit_ticks):
            step=_tick_size(math.nextafter(price,-math.inf) if position.side=='LONG' else price)
            price += step*(-1 if position.side=='LONG' else 1)
        return replace(quote,price=price)

    def choose_study_entry(self, decision):
        self.raw_at_boundary=[]
        self.diagnostics_at_boundary=[]
        if self.variant == 'S0':
            result = super().choose_entry(decision, allow_short=False)
            self.approved_at_boundary = [] if result is None else [asdict(result)]
            return result
        decision = decision.astimezone(TAIPEI).replace(microsecond=0)
        if self.last_decision is not None and decision <= self.last_decision:
            return None
        self.last_decision = decision
        self.approved_at_boundary = []
        if not self._clock(SPEC['entry_start']) <= decision.time() <= self._clock(SPEC['last_entry_time']):
            return None
        market = self._market_context(decision)
        if self.variant != 'S1' and market is None:
            # Count base signals even when the benchmark is missing; do not
            # update confirmation state or authorize an entry in this branch.
            for symbol in sorted(self._candidate_symbols):
                raw=self._signal_for(symbol,decision)
                if raw is not None:
                    self.raw_at_boundary.append(dict(raw))
                    self.diagnostics_at_boundary.append({'stock_id':symbol,'side':raw['side'],'reason':'BENCHMARK_MISSING_OR_STALE'})
            return None
        interval = timedelta(seconds=int(SPEC['decision_interval_seconds']))
        for symbol in sorted(self._candidate_symbols):
            raw = self._signal_for(symbol, decision)
            previous = self._previous.get(symbol)
            streak = (
                previous[1] + 1 if raw and previous and previous[0] == raw['side']
                and decision - previous[2] == interval else 1 if raw else 0
            )
            self._previous[symbol] = (raw['side'], streak, decision) if raw else ('', 0, decision)
            if raw is None:
                continue
            candidate = dict(raw)
            self.raw_at_boundary.append(dict(candidate))
            if self.variant in ('S1', 'S2') and candidate['side'] != 'SHORT':
                continue
            if candidate['side'] == 'SHORT' and self.eligibility.get(symbol) is False:
                self.diagnostics_at_boundary.append({'stock_id':symbol,'side':'SHORT','reason':'EXCHANGE_INELIGIBLE'})
                continue
            stock_return = candidate.pop('return_5m', None)
            confirmations = int(SPEC['entry_confirmations'])
            if self.variant != 'S1':
                if stock_return is None:
                    continue
                regime = market['market_regime']
                relative = stock_return - market['benchmark_return_5m']
                if candidate['side'] == 'SHORT':
                    passed, confirmations = short_gate(regime, relative)
                else:
                    passed = relative >= LONG_MARKET_REGIME_POLICY[f'{regime.lower()}_min_relative_strength']
                    confirmations = int(LONG_MARKET_REGIME_POLICY[f'{regime.lower()}_confirmations'])
                if not passed:
                    self.diagnostics_at_boundary.append({'stock_id':symbol,'side':candidate['side'],'reason':'RELATIVE_STRENGTH_GATE','market_regime':regime,'relative_strength':relative})
                    continue
                candidate.update(
                    market_regime=regime, relative_strength_5m=relative,
                    benchmark_return_5m=market['benchmark_return_5m'],
                    benchmark_vwap_gap=market['benchmark_vwap_gap'],
                    required_confirmations=confirmations,
                )
            if candidate['directional_opening_extension'] > ANTI_CHASE_ENTRY_POLICY['maximum_directional_opening_extension'] + 1e-12:
                self.diagnostics_at_boundary.append({'stock_id':symbol,'side':candidate['side'],'reason':'ANTI_CHASE_OPENING'})
                continue
            if candidate['directional_vwap_extension'] > ANTI_CHASE_ENTRY_POLICY['maximum_directional_vwap_extension'] + 1e-12:
                self.diagnostics_at_boundary.append({'stock_id':symbol,'side':candidate['side'],'reason':'ANTI_CHASE_VWAP'})
                continue
            if streak < confirmations:
                self.diagnostics_at_boundary.append({'stock_id':symbol,'side':candidate['side'],'reason':'CONFIRMATIONS'})
                continue
            self.approved_at_boundary.append(candidate)
        if not self.approved_at_boundary:
            return None
        selected = max(self.approved_at_boundary, key=lambda x: (x['score'], x['entry_price'] * x['quantity']))
        if len({x['side'] for x in self.approved_at_boundary}) > 1:
            self.simultaneous_cases.append({
                'decision_time': decision.isoformat(),
                'candidates': self.approved_at_boundary.copy(),
                'selected_stock_id': selected['stock_id'], 'selected_side': selected['side'],
            })
        return LiveSignal(**selected)
