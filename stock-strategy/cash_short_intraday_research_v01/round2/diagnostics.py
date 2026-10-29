"""Pure offline fixed-entry exits and quote-based round-one diagnostics.

Only the original pure ``simulate_trade`` implementation is reused.  A
per-call function/global-namespace copy injects the archived requested quantity;
the original module globals are never patched.  Entry is still replayed through
the original IOC/depth model and must exactly match the archived entry fills.
This preserves the entry's consumed-depth state for subsequent cover attempts.
No SDK, broker entrypoint, I/O, network, account or schedule is accessible here.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import timedelta
import math
from types import FunctionType

from .. import replay as round1
from ..execution import cost, shift_price
from ..signals import SignalScanner
from .features import _finite, _stock_snapshot


class FixedEntryMismatch(ValueError):
    """Archived entry or E0 cannot be reproduced; do not publish comparison."""


def _stamp(value):
    return round1.stamp(value)


def _same(a, b):
    if a is None or b is None:
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-6)
    return a == b


def _fill_signature(fills):
    return [(_stamp(x['time']), float(x['price']), int(x['quantity']),
             str(x.get('book_sequence')), int(x.get('level', 0))) for x in fills]


def _validate_original(original, signal):
    if original.get('side') != 'SHORT' or signal.get('side') != 'SHORT':
        raise FixedEntryMismatch('FIXED_EXIT_REQUIRES_ORIGINAL_SHORT')
    for name in ('signal_id', 'symbol', 'day'):
        if str(original.get(name)) != str(signal.get(name)):
            raise FixedEntryMismatch('ORIGINAL_SIGNAL_IDENTITY_MISMATCH:' + name)
    fills = original.get('entry_fills')
    if not fills:
        raise FixedEntryMismatch('ARCHIVED_ENTRY_FILLS_REQUIRED')
    qty = sum(int(x['quantity']) for x in fills)
    requested = int(original['requested_quantity'])
    if qty != int(original['quantity']) or qty <= 0 or qty % 1000 or requested < qty or requested % 1000:
        raise FixedEntryMismatch('ARCHIVED_ENTRY_QUANTITY_INVALID')
    if any(int(x['quantity']) <= 0 or int(x['quantity']) % 1000 for x in fills):
        raise FixedEntryMismatch('ARCHIVED_FILL_IS_NOT_WHOLE_BOARD_LOT')
    if max(_stamp(x['time']) for x in fills) != _stamp(original['entry_time']):
        raise FixedEntryMismatch('ARCHIVED_ENTRY_TIME_MISMATCH')
    weighted = sum(float(x['price']) * int(x['quantity']) for x in fills) / qty
    if not _same(weighted, original['entry_price']):
        raise FixedEntryMismatch('ARCHIVED_ENTRY_PRICE_MISMATCH')
    return requested


def assert_e0_parity(original, replayed):
    """Check modeled fill/exit/cost results, not a superficial total-PnL match."""
    timestamps = ('signal_time', 'entry_time', 'exit_time', 'exit_trigger_time')
    names = ('entry_price', 'quantity', 'requested_quantity', 'entry_unfilled_quantity',
             'exit_price', 'closed_quantity', 'remaining_quantity', 'gross_pnl',
             'realized_gross_pnl', 'commission', 'sell_tax', 'net_pnl',
             'realized_pnl', 'unrealized_pnl', 'marked_net_pnl', 'exit_reason',
             'fill_status', 'initial_stop_price', 'initial_price_R', 'net_R')
    for name in names:
        if name in original and not _same(original[name], replayed.get(name)):
            raise FixedEntryMismatch('E0_ARCHIVE_PARITY_FAILED:' + name)
    for name in timestamps:
        old, new = original.get(name), replayed.get(name)
        if (old is None) != (new is None) or (old is not None and _stamp(old) != _stamp(new)):
            raise FixedEntryMismatch('E0_ARCHIVE_PARITY_FAILED:' + name)
    if 'exit_fills' not in original:
        raise FixedEntryMismatch('ARCHIVED_EXIT_FILLS_REQUIRED_FOR_E0_PARITY')
    if _fill_signature(original['exit_fills']) != _fill_signature(replayed['exit_fills']):
        raise FixedEntryMismatch('E0_ARCHIVE_PARITY_FAILED:exit_fills')
    return True


def fixed_trade(original_trade, signal, data, index, config, exit_model='E0'):
    """Replay E0/E1/E2 with one scenario's original R0 entry cohort and size.

    Call separately for each original scenario (BASE/WORSE/SHARES), never pair
    BASE entry fills with a different entry-delay scenario.  A no-fill scenario
    has an empty cohort, not invented fixed entries.  All cover logic, cutoff,
    latency, pending fill accounting, capacity, fees and residual exposure are
    the *unchanged* original function.  Only sizing is locally dependency-
    injected; this is not a module-global monkeypatch or alternate execution.
    """
    if exit_model not in {'E0', 'E1', 'E2'}:
        raise ValueError('ONLY_FROZEN_E0_E1_E2_ARE_ALLOWED')
    requested = _validate_original(original_trade, signal)
    if data.get('known_short_ineligible', {}).get(signal['symbol']) is True:
        raise FixedEntryMismatch('KNOWN_HISTORICAL_SELL_FIRST_SUSPENSION')
    original_function = round1.simulate_trade
    namespace = dict(original_function.__globals__)

    def original_size(_signal, _quote, _equity, _config):
        if _signal is not signal:
            raise FixedEntryMismatch('UNEXPECTED_SIGNAL_IN_LOCAL_SIZING')
        return requested, 'FIXED_ARCHIVED_REQUESTED_QUANTITY'

    namespace['target_quantity'] = original_size
    local_function = FunctionType(original_function.__code__, namespace,
                                  original_function.__name__, original_function.__defaults__,
                                  original_function.__closure__)
    result, issues = local_function(signal, data, index, float(config['capital_twd']), config, exit_model)
    if result is None:
        raise FixedEntryMismatch('ARCHIVED_ENTRY_NO_LONGER_FILLS')
    if _fill_signature(original_trade['entry_fills']) != _fill_signature(result['entry_fills']):
        raise FixedEntryMismatch('FIXED_ENTRY_FILL_PARITY_FAILED')
    for name in ('entry_price', 'quantity', 'requested_quantity', 'entry_unfilled_quantity'):
        if not _same(original_trade[name], result[name]):
            raise FixedEntryMismatch('FIXED_ENTRY_PARITY_FAILED:' + name)
    if _stamp(original_trade['entry_time']) != _stamp(result['entry_time']):
        raise FixedEntryMismatch('FIXED_ENTRY_PARITY_FAILED:entry_time')
    if exit_model == 'E0':
        assert_e0_parity(original_trade, result)
    result['entry_cohort_basis'] = 'SAME_SCENARIO_ORIGINAL_R0_ENTRY_FILLS_AND_REQUESTED_QUANTITY'
    result['entry_fill_parity'] = True
    result['e0_archive_parity'] = True if exit_model == 'E0' else 'NOT_APPLICABLE_DIFFERENT_EXIT'
    result['fixed_entry_sizing_injection'] = 'LOCAL_FUNCTION_NAMESPACE_ONLY_NO_GLOBAL_PATCH'
    return result, issues


def _usable_book(index, symbol, at, max_age=10):
    """Only a book received by target time; no nearest/future interpolation."""
    quote = round1.last_book(index, symbol, at)
    if quote is None:
        return None, 'NO_BOOK_RECEIVED_BY_TARGET'
    age = (at - _stamp(quote['received_at'])).total_seconds()
    if age < 0 or age > max_age:
        return None, 'BOOK_STALE_AT_TARGET'
    if quote.get('out_of_order') or quote.get('quote_valid') is False:
        return None, 'INVALID_OR_OUT_OF_ORDER_TARGET_BOOK'
    exchange = quote.get('exchange_time')
    if exchange is not None:
        exchange_age = (_stamp(quote['received_at']) - _stamp(exchange)).total_seconds()
        target_exchange_age = (at - _stamp(exchange)).total_seconds()
        if (exchange_age > 10 or exchange_age < -.5
                or target_exchange_age > max_age or target_exchange_age < -.5):
            return None, 'STALE_OR_FUTURE_EXCHANGE_TARGET_BOOK'
    return quote, 'CONDITIONAL_BOOK_EXCHANGE_TIME_UNKNOWN' if exchange is None else 'OBSERVED_FRESH_BOOK'


def _mark_at_target(trade, index, config, seconds):
    target = _stamp(trade['entry_time']) + timedelta(seconds=seconds)
    quote, status = _usable_book(index, trade['symbol'], target, config.get('book_stale_seconds', 10))
    px, adjusted = None, None
    if quote is not None:
        levels = quote.get('asks', [])
        if levels:
            try:
                px = float(levels[0][0])
                adjusted = shift_price(px, int(config.get('extra_slippage_ticks', 1)), 1)
            except (ValueError, TypeError, IndexError):
                status, quote = 'INVALID_SHORT_COVER_QUOTE_AT_TARGET', None
    if quote is not None and adjusted is None:
        status = 'NO_SHORT_COVER_ASK_AT_TARGET'
    quantity = int(trade['quantity'])
    exit_fills = [x for x in trade.get('exit_fills', []) if _stamp(x['time']) <= target]
    closed = sum(int(x['quantity']) for x in exit_fills)
    if closed > quantity:
        raise AssertionError('TARGET_COVER_OVERFILL')
    actual = round1._mark('SHORT', float(trade['entry_price']), quantity,
                          quantity - closed, exit_fills, adjusted, config)
    hypothetical = cost('SHORT', trade['entry_price'], adjusted, quantity, config) if adjusted is not None else None
    price_risk = trade.get('initial_price_R')
    return {
        'target_time': target, 'quote_status': status,
        'quote_received_at': quote.get('received_at') if quote is not None else None,
        'quote_age_seconds': (target - _stamp(quote['received_at'])).total_seconds() if quote is not None else None,
        'observed_best_cover_ask': px, 'penalized_quote_mark_price': adjusted,
        'actual_closed_quantity_by_target': closed,
        'actual_remaining_quantity_by_target': quantity - closed,
        'actual_realized_pnl_by_target': actual['realized_pnl'],
        'actual_unrealized_quote_pnl_by_target': actual['unrealized_pnl'],
        'actual_total_marked_pnl_by_target': actual['marked_net'],
        'fixed_original_quantity_hypothetical_quote_gross_pnl': hypothetical['gross'] if hypothetical else None,
        'fixed_original_quantity_hypothetical_quote_net_pnl': hypothetical['net'] if hypothetical else None,
        'hypothetical_price_R': ((float(trade['entry_price']) - adjusted) / float(price_risk)
                                 if adjusted is not None and price_risk else None),
        'hypothetical_net_R': hypothetical['net'] / 3500 if hypothetical else None,
        'basis': 'OBSERVED_BEST_ASK_PLUS_FROZEN_PENALTY_NOT_CAPACITY_TESTED_FILL',
        'already_closed_by_target': closed == quantity,
    }


def _causal_vwap_features(signal, data, config):
    """Reconstruct two received-data VWAP snapshots, never final-day VWAP."""
    enriched = signal.get('round2_features')
    if enriched is not None:
        # build_feature_context already verified shared round-one fields and
        # saved t-180 at its own historical receipt cursor. Do not rescan, or
        # convert its explicit unavailable status into a numerical direction.
        old, new = enriched.get('vwap_180s_ago'), enriched.get('vwap_now')
        available = (enriched.get('vwap_slope_available') is True
                     and _finite(old) and _finite(new) and old > 0 and new > 0)
        return {
            'vwap_at_signal_reconstructed': new,
            'vwap_at_signal_minus_180s': old if available else None,
            'vwap_delta_180s': new - old if available else None,
            'vwap_declining_180s': new < old if available else None,
            'vwap_slope_available': available,
            'vwap_slope_unavailable_reasons': ([] if available else
                                               enriched.get('vwap_slope_unavailable_reasons')
                                               or ['VERIFIED_VWAP_SLOPE_UNAVAILABLE']),
            'vwap_comparison_basis': 'VERIFIED_ROUND2_FEATURE_CONTEXT_TWO_RECEIPT_CURSORS',
            'vwap_t_minus_180_feature_status': enriched.get('vwap_180s_ago_warmup_reason'),
            'reconstructed_benchmark_return_300s': enriched.get('benchmark_return_300s'),
            'reconstructed_relative_strength_300s': enriched.get('rs_300'),
            'reconstructed_market_filter_pass': enriched.get('market_weak'),
        }
    now = _stamp(signal['time'])
    symbol = str(signal['symbol'])
    names = {symbol, '0050'} & set(data['metadata'])
    scanner = SignalScanner({s: data['metadata'][s] for s in names}, {symbol}, config)
    events = data['events']
    cursor, snapshots = 0, []
    for at in (now - timedelta(seconds=180), now):
        while cursor < len(events) and events[cursor]['received_at'] <= at:
            event = events[cursor]
            if event['symbol'] in names:
                scanner.ingest(event)
            cursor += 1
        snapshots.append(_stock_snapshot(scanner, symbol, at))
    previous, current = snapshots
    old, new = previous['features'].get('vwap'), current['features'].get('vwap')
    available = (previous['historical_vwap_available'] and current['historical_vwap_available']
                 and _finite(old) and _finite(new))
    reasons = (list(previous['historical_vwap_unavailable_reasons'])
               + ['CURRENT_' + reason for reason in current['historical_vwap_unavailable_reasons']])
    return {
        'vwap_at_signal_reconstructed': new,
        'vwap_at_signal_minus_180s': old if available else None,
        'vwap_delta_180s': new - old if available else None,
        'vwap_declining_180s': new < old if available else None,
        'vwap_slope_available': bool(available),
        'vwap_slope_unavailable_reasons': reasons if not available else [],
        'vwap_comparison_basis': 'TWO_CAUSAL_RECEIVED_CURSORS_STOCK_SNAPSHOT_AVAILABILITY_NOT_FINAL_DAY_VWAP',
        'vwap_t_minus_180_feature_status': previous['features'].get('warmup_reason'),
        'reconstructed_benchmark_return_300s': current['features'].get('benchmark_return_5m'),
        'reconstructed_relative_strength_300s': current['features'].get('relative_strength_5m'),
        'reconstructed_market_filter_pass': current['features'].get('market_filter_pass'),
    }


def fill_cost_decomposition(trade, signal, index, config):
    """Known extra-tick arithmetic and quote changes, NOT causal total impact.

    Keeping observed quantities/fill times fixed is descriptive only. Removing
    penalties can change IOC eligibility, size and subsequent decisions, so the
    decomposition must never be advertised as a no-slippage strategy replay.
    """
    signal_quote, signal_status = _usable_book(index, trade['symbol'], _stamp(signal['time']))
    signal_best_bid = float(signal_quote['bids'][0][0]) if signal_quote and signal_quote.get('bids') else None
    components = []
    entry_penalty = exit_penalty = 0.0
    entry_latency_move = 0.0
    entry_best_quote_move = entry_depth_level_move = 0.0
    complete = True
    for phase, fills in (('ENTRY', trade.get('entry_fills', [])), ('EXIT', trade.get('exit_fills', []))):
        for fill in fills:
            at = _stamp(fill['time'])
            quotes = index['books'].get(trade['symbol'], [])
            times = index['book_times'].get(trade['symbol'], [])
            begin, end = bisect_left(times, at), bisect_right(times, at)
            matches = quotes[begin:end]
            matched = None
            for q in matches:
                seq = q.get('book_sequence', q.get('sequence', q.get('sequence_id', q.get('serial_no'))))
                if q.get('sequence_known') is False:
                    seq = None
                identity = str(seq) if seq is not None else _stamp(q['received_at']).isoformat()
                if identity == str(fill.get('book_sequence')):
                    matched = q
                    break
            levels = matched.get('bids' if phase == 'ENTRY' else 'asks', []) if matched else []
            level = int(fill.get('level', 0)) - 1
            raw = float(levels[level][0]) if 0 <= level < len(levels) else None
            expected = (shift_price(raw, int(config.get('extra_slippage_ticks', 1)), -1 if phase == 'ENTRY' else 1)
                        if raw is not None else None)
            status = 'EXACT_MATCHED_RECEIVED_BOOK_LEVEL' if raw is not None and _same(expected, fill['price']) else 'UNKNOWN_OR_FILL_BOOK_MISMATCH'
            if status != 'EXACT_MATCHED_RECEIVED_BOOK_LEVEL':
                complete = False
                penalty, move, best_change, level_change = None, None, None, None
            else:
                penalty = ((raw - float(fill['price'])) if phase == 'ENTRY' else (float(fill['price']) - raw)) * int(fill['quantity'])
                move = ((signal_best_bid - raw) * int(fill['quantity'])
                        if phase == 'ENTRY' and signal_best_bid is not None else None)
                best = float(levels[0][0])
                best_change = ((signal_best_bid - best) * int(fill['quantity'])
                               if phase == 'ENTRY' and signal_best_bid is not None else None)
                level_change = ((best - raw) if phase == 'ENTRY' else (raw - best)) * int(fill['quantity'])
                if phase == 'ENTRY':
                    entry_penalty += penalty
                    if move is not None:
                        entry_latency_move += move
                        entry_best_quote_move += best_change
                    entry_depth_level_move += level_change
                else:
                    exit_penalty += penalty
            components.append({
                'phase': phase, 'time': at, 'quantity': int(fill['quantity']),
                'fill_price': float(fill['price']), 'observed_level_price': raw,
                'level': level + 1, 'book_sequence': fill.get('book_sequence'),
                'known_extra_tick_penalty_twd': penalty,
                'signal_best_bid_to_fill_level_change_twd': move,
                'entry_signal_best_bid_to_fill_book_best_bid_change_twd': best_change,
                'fill_book_best_to_fill_level_depth_price_difference_twd': level_change,
                'status': status,
            })
    return {
        'signal_best_bid': signal_best_bid, 'signal_quote_status': signal_status,
        'known_entry_extra_tick_penalty_twd': round(entry_penalty, 6) if complete else None,
        'known_exit_extra_tick_penalty_twd': round(exit_penalty, 6) if complete else None,
        'known_total_extra_tick_penalty_twd': round(entry_penalty + exit_penalty, 6) if complete else None,
        'entry_signal_to_fill_level_change_twd': round(entry_latency_move, 6) if complete and signal_best_bid is not None else None,
        'entry_signal_to_fill_best_bid_change_twd': round(entry_best_quote_move, 6) if complete and signal_best_bid is not None else None,
        'entry_displayed_depth_level_price_difference_twd': round(entry_depth_level_move, 6) if complete else None,
        'decomposition_complete': complete,
        'decomposition_basis': 'FIXED_OBSERVED_FILLS_ARITHMETIC_NOT_NO_SLIPPAGE_COUNTERFACTUAL',
        'causal_total_slippage_effect': None,
        'causal_total_slippage_status': 'UNKNOWN_REQUIRES_SEPARATE_FROZEN_FULL_ACCOUNT_REPLAY',
        'fill_components': components,
    }


def _observed_interval_quality(trade, signal, index):
    """Observed callback gaps, not inferred exchange outages or missing trades."""
    begin = _stamp(signal['time']) - timedelta(seconds=300)
    end = max(_stamp(trade['entry_time']) + timedelta(seconds=300),
              _stamp(trade.get('exit_time') or trade.get('mark_time') or trade['entry_time']))
    rows = [e for e in index['events'].get(trade['symbol'], []) if begin <= e['received_at'] <= end]
    result = {'diagnostic_interval_start': begin, 'diagnostic_interval_end': end,
              'interval_gap_basis': 'OBSERVED_RECEIPT_SPACING_INCLUDING_INTERVAL_EDGES_NOT_PROVEN_OUTAGE'}
    for kind in ('tick', 'book'):
        current = [e for e in rows if e.get('kind') == kind]
        times = [begin, *[e['received_at'] for e in current], end]
        gaps = [(b-a).total_seconds() for a, b in zip(times, times[1:])]
        result[kind + '_events_in_diagnostic_interval'] = len(current)
        result['max_' + kind + '_observed_receipt_gap_seconds'] = max(gaps) if gaps else None
        result[kind + '_observed_intervals_above_60_seconds'] = sum(g > 60 for g in gaps)
        result[kind + '_out_of_order_count_in_interval'] = sum(e.get('out_of_order') is True for e in current)
        result[kind + '_missing_exchange_time_count_in_interval'] = sum(e.get('exchange_time') is None for e in current)
    return result


def _partial_cover_orders(trade):
    """An order is partial relative to remaining exposure, not original size."""
    remaining, partials = int(trade['quantity']), 0
    orders = sorted(round1._group_orders(trade.get('exit_fills', [])).values(),
                    key=lambda rows: min(_stamp(x['time']) for x in rows))
    for rows in orders:
        filled = sum(int(x['quantity']) for x in rows)
        if filled > remaining:
            raise AssertionError('DIAGNOSTIC_COVER_OVERFILL')
        partials += filled < remaining
        remaining -= filled
    return partials


def _path_diagnostics(trade):
    path = [p for p in trade.get('path', []) if p.get('net') is not None]
    observed_peak = max(path, key=lambda p: p['net']) if path else None
    observed_trough = min(path, key=lambda p: p['net']) if path else None
    peak = max(0, observed_peak['net']) if observed_peak else None
    final = trade.get('marked_net_pnl')
    peak_to_final = peak - final if peak is not None and final is not None else None
    return {
        'observed_peak_net_twd': observed_peak['net'] if observed_peak else None,
        'observed_peak_net_time': _stamp(observed_peak['time']) if observed_peak else None,
        'observed_trough_net_twd': observed_trough['net'] if observed_trough else None,
        'observed_trough_net_time': _stamp(observed_trough['time']) if observed_trough else None,
        'positive_peak_net_twd': peak,
        'positive_peak_net_time': (_stamp(observed_peak['time']) if observed_peak and observed_peak['net'] > 0 else None),
        'profit_surrendered_twd': min(peak, max(0, peak_to_final)) if peak_to_final is not None else None,
        'drawdown_peak_to_final_twd': peak_to_final,
        'mfe_net_twd_round1_floor_zero': trade.get('mfe_net_twd'),
        'mae_net_twd_round1_ceiling_zero': trade.get('mae_net_twd'),
        'mfe_time': _stamp(observed_peak['time']) if observed_peak and observed_peak['net'] > 0 else None,
        'mae_time': _stamp(observed_trough['time']) if observed_trough and observed_trough['net'] < 0 else None,
        'path_basis': 'RECEIVED_ASK_QUOTE_NET_MARKS_AND_ACTUAL_MODELED_PARTIAL_COVERS_NOT_GUARANTEED_CAPACITY',
    }


def diagnose(original_trade, replayed_trade, signal, data, index, config):
    """One original R0 short: numerical overlapping flags, unknowns retained."""
    assert_e0_parity(original_trade, replayed_trade)
    trade = replayed_trade
    horizons = {seconds: _mark_at_target(trade, index, config, seconds) for seconds in (60, 180, 300)}
    path = _path_diagnostics(trade)
    features = dict(signal.get('features', {}))
    causal = _causal_vwap_features(signal, data, config)
    decomposition = fill_cost_decomposition(trade, signal, index, config)
    gross1 = horizons[60]['fixed_original_quantity_hypothetical_quote_gross_pnl']
    gross3 = horizons[180]['fixed_original_quantity_hypothetical_quote_gross_pnl']
    gross5 = horizons[300]['fixed_original_quantity_hypothetical_quote_gross_pnl']
    gross, net = trade.get('gross_pnl'), trade.get('net_pnl')
    distance = features.get('entry_distance_fraction')
    flags = {
        'adverse_at_one_minute_before_fees': gross1 < 0 if gross1 is not None else None,
        'adverse_at_one_and_three_minutes_before_fees': (gross1 < 0 and gross3 < 0) if gross1 is not None and gross3 is not None else None,
        'positive_quote_profit_then_surrendered': path['profit_surrendered_twd'] > 0 if path['profit_surrendered_twd'] is not None else None,
        'fees_and_tax_flip_gross_profit_to_net_loss': gross > 0 and net <= 0 if gross is not None and net is not None else None,
        'entry_distance_above_fixed_0_3pct_diagnostic': distance > .003 if distance is not None else None,
        'vwap_not_declining_over_180s': not causal['vwap_declining_180s'] if causal['vwap_declining_180s'] is not None else None,
        'market_not_weak_at_entry': not features['market_filter_pass'] if features.get('market_filter_pass') is not None else None,
        'relative_strength_not_below_minus_0_2pct': features['relative_strength_5m'] > -.002 if features.get('relative_strength_5m') is not None else None,
    }
    row = {name: trade.get(name) for name in (
        'signal_id', 'day', 'symbol', 'stock_name', 'signal_time', 'entry_time', 'entry_price',
        'quantity', 'requested_quantity', 'entry_unfilled_quantity', 'exit_time', 'exit_trigger_time',
        'exit_price', 'gross_pnl', 'commission', 'sell_tax', 'net_pnl', 'net_R', 'initial_price_R',
        'initial_stop_price', 'exit_reason', 'fill_status', 'remaining_quantity', 'holding_seconds')}
    row.update({
        'round1_e0_parity': True,
        'market_filter_pass_at_signal': features.get('market_filter_pass'),
        'benchmark_return_300s': features.get('benchmark_return_5m'),
        'benchmark_vwap_gap': features.get('benchmark_vwap_gap'),
        'market_regime': features.get('market_regime'),
        'stock_return_300s': features.get('stock_return_5m'),
        'relative_strength_300s': features.get('relative_strength_5m'),
        'stock_vwap': features.get('vwap'), 'stock_vwap_gap': features.get('vwap_gap'),
        'stock_price_at_signal': features.get('price'),
        'volume_delta': features.get('volume_delta'), 'large_trade_delta': features.get('large_trade_delta'),
        'book_imbalance_diagnostic_only': features.get('book_imbalance'),
        'entry_distance_fraction': distance,
        'structure_stop_fraction': features.get('structure_stop_fraction'),
        'retest_reference': features.get('retest_reference'), 'retest_high': features.get('retest_high'),
        'signal_tick_age_seconds': features.get('tick_age_seconds'),
        'signal_book_age_seconds': features.get('book_age_seconds'),
        'signal_spread_bps': features.get('spread_bps'),
        'entry_capacity_limited': int(trade['entry_unfilled_quantity']) > 0,
        'entry_partial_cause_status': 'DEPTH_OR_IOC_PRICE_ELIGIBILITY_SEE_EXECUTION_ISSUES_NOT_PROVEN_DEPTH_ONLY',
        'cover_partial_order_count': _partial_cover_orders(trade),
        'cover_incomplete_quantity': trade.get('remaining_quantity'),
        'horizon_missing_count': sum(x['penalized_quote_mark_price'] is None for x in horizons.values()),
        'strict_executable': None, 'strict_status': 'NA_UNVERIFIED_UNITS_ELIGIBILITY_FEES_AND_BOOK_CLOCK',
        'actual_orders': 0,
        'causal_attribution': 'NUMERIC_OVERLAPPING_DIAGNOSTIC_FLAGS_NOT_PROVEN_CAUSES',
        'horizons': horizons, 'cause_flags': flags, **path, **causal, **decomposition,
        **_observed_interval_quality(trade, signal, index),
    })
    for seconds, horizon in horizons.items():
        for key, value in horizon.items():
            row[f't_plus_{seconds}s_{key}'] = value
    row.update({'flag_' + key: value for key, value in flags.items()})
    # These named fields ease hand-auditing without implying tradable fills.
    row['horizon_1m_gross_quote_mark_twd'] = gross1
    row['horizon_3m_gross_quote_mark_twd'] = gross3
    row['horizon_5m_gross_quote_mark_twd'] = gross5
    return row
