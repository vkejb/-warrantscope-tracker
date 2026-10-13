"""Existing completed quote archive -> independent paper S0/S1/S4 day books.

No collector/SDK object, broker adapter, real inventory or account DB. Reuse
frozen selectors, exit management, legal capacity replay and paper execution.
"""
from bisect import bisect_right
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import urllib.request
from unittest.mock import patch

from paper_shadow_v01.runner import _load_market_context
from short_side_study_v01.s4 import chronological_selection
from short_side_study_v01.account_replay_v02 import run as account_replay
from short_side_study_v01.account_replay_v02.run import safe_metrics
from short_side_study_v01.account_replay_v02.execution import LotDepthExecution
from short_side_study_v01.execution import DepthBook
from yuanta_intraday_shadow_v01.direction_follow_backtest import load_session, SPEC
from yuanta_intraday_shadow_v01.live_parity_backtest import _validate_full_session_coverage, _parse_stamp
from .safety import ShortCoverSafety

HERE = Path(__file__).resolve().parent
VARIANTS = ('S0', 'S1', 'S4')


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for value in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(value)
    return h.hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


def load_inputs(directory):
    manifest = json.loads((directory / 'run_manifest.json').read_text())
    watch = json.loads((directory / 'watchlist.json').read_text())
    if len(watch['stocks']) != 30:
        raise ValueError('FROZEN_STAGE_A_TOP30_REQUIRED')
    stocks, coverage = load_session([directory])
    _validate_full_session_coverage(coverage)
    ids = {str(r['stock_id']) for r in watch['stocks']}
    stocks = {k: v for k, v in stocks.items() if k in ids}
    market_status = 'AVAILABLE_DESCRIPTIVE_ONLY'
    try:
        market = _load_market_context(directory, manifest)
    except Exception as exc:
        # Missing 0050 must not veto raw S1. Existing LONG A still has its own
        # original benchmark rule; do not relax it or introduce a SHORT gate.
        market = {'0050': {'ticks': [], 'books': [], 'meta': {'stock_name': '0050'}}}
        market_status = 'UNKNOWN_DESCRIPTIVE_ONLY:' + type(exc).__name__
    depths = {s: [] for s in ids}
    raw_samples = {s: [] for s in ids}
    name = next(n for n in ('books.jsonl.gz', 'books.jsonl') if n in manifest['artifacts'])
    if sha(directory / name) != manifest['artifacts'][name]:
        raise ValueError('SOURCE_BOOK_HASH_MISMATCH')
    invalid = 0
    with (gzip.open(directory / name, 'rt') if name.endswith('.gz') else (directory / name).open()) as stream:
        for sequence, line in enumerate(stream):
            row = json.loads(line)
            sid = str(row.get('stock_id'))
            if sid not in ids:
                continue
            if row.get('index_flag') != 'IndexFlag50':
                invalid += 1
                continue
            try:
                if any(len(row[k]) != 5 for k in ('buy_prices', 'buy_volumes', 'sell_prices', 'sell_volumes')):
                    raise ValueError('INCOMPLETE_DEPTH')
                def qty(value):
                    q = Decimal(str(value))
                    if not q.is_finite() or q < 0 or q != q.to_integral_value():
                        raise ValueError('INVALID_RAW_QUANTITY')
                    return int(q)
                bids = tuple((float(p), qty(q)) for p, q in zip(row['buy_prices'], row['buy_volumes']))
                asks = tuple((float(p), qty(q)) for p, q in zip(row['sell_prices'], row['sell_volumes']))
                book = DepthBook(_parse_stamp(row['received_at']), bids, asks, str(sequence))
                depths[sid].append(book)
                raw_samples[sid].append(row)
            except (ValueError, KeyError, TypeError):
                invalid += 1
    return stocks, market, depths, raw_samples, manifest, watch, coverage, {'market_context': market_status, 'invalid_depth_rows': invalid}


def qualification(watch, date, output, cache_dir=None, fetch_public=False):
    """Archive exact-day public evidence. Unknown is never a broker permission."""
    source, response, error = None, None, None
    path = cache_dir / f'{date}_twse_eligibility.json' if cache_dir else None
    if path and path.is_file():
        candidate = json.loads(path.read_text())
        if candidate.get('session_date') != date or candidate.get('raw_response', {}).get('date') != date or candidate.get('raw_response', {}).get('stat') != 'OK':
            raise ValueError('QUALIFICATION_DATE_MISMATCH')
        response = candidate['raw_response']
        source = {'path': str(path), 'sha256': sha(path), 'date': date, 'url': candidate.get('url')}
        write(output / 'twse_source_snapshot.json', candidate)
    elif fetch_public:
        url = f'https://www.twse.com.tw/exchangeReport/TWTB4U?date={date}&response=json'
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                raw = r.read()
            candidate = json.loads(raw)
            if candidate.get('date') != date or candidate.get('stat') != 'OK':
                raise ValueError('OFFICIAL_RESPONSE_DATE_OR_STATUS_MISMATCH')
            response = candidate
            source = {'url': url, 'date': date, 'http_payload_sha256': hashlib.sha256(raw).hexdigest()}
            write(output / 'twse_source_snapshot.json', {'session_date': date, 'url': url, 'raw_response': candidate,
                                                         'http_payload_sha256': source['http_payload_sha256']})
        except Exception as exc:
            error = type(exc).__name__ + ': ' + str(exc)
    table = next((t for t in response.get('tables', []) if '暫停現股賣出後現款買進當沖註記' in t.get('fields', [])), None) if response else None
    flags = {}
    if table:
        col = table['fields'].index('暫停現股賣出後現款買進當沖註記')
        flags = {str(r[0]): bool(str(r[col]).strip()) for r in table['data']}
    rows, vetoes = [], {}
    for item in watch['stocks']:
        sid, market = str(item['stock_id']), item['market']
        listed = market == 'TWSE' and sid in flags
        suspended = flags[sid] if listed else 'UNKNOWN'
        if suspended is True:
            vetoes[sid] = False  # Reuse S1's existing known-ineligible rule.
        rows.append({'date': date, 'stock_id': sid, 'market': market,
                     'exchange_target': 'LISTED_IN_DATED_DAYTRADE_TABLE' if listed else 'UNKNOWN',
                     'sell_first_suspended': suspended, 'broker_account_permission': 'UNKNOWN',
                     'borrowable_supply': 'UNKNOWN', 'broker_quota': 'UNKNOWN', 'entry_time_restrictions': 'UNKNOWN',
                     'eligibility': 'ELIGIBILITY_UNPROVEN', 'source': source if listed else None})
    result = {'session_date': date, 'source': source, 'source_error': error, 'rows': rows,
              'unknown_not_authorization': True, 'tpex_historical_snapshot': 'UNKNOWN_UNLESS_EXACT_DAY_SOURCE_SUPPLIED'}
    write(output / 'qualification.json', result)
    return result, vetoes


def episodes(approvals):
    output, last = [], {}
    for row in sorted(approvals, key=lambda r: str(r['decision_time'])):
        key = (row['variant'], row['stock_id'], row['side'])
        at = _parse_stamp(str(row['decision_time']))
        if key in last and at - last[key]['last'] == timedelta(seconds=SPEC['decision_interval_seconds']):
            last[key]['record']['end'] = at.isoformat()
            last[key]['record']['boundary_count'] += 1
            last[key]['last'] = at
        else:
            record = {'variant': key[0], 'stock_id': key[1], 'side': key[2], 'start': at.isoformat(),
                      'end': at.isoformat(), 'boundary_count': 1, 'is_trade_sample': False}
            output.append(record)
            last[key] = {'last': at, 'record': record}
    return output


def observed_replay(*args):
    models = []
    class RecordedDepth(LotDepthExecution):
        def __init__(self):
            super().__init__()
            self.attempts = []
            models.append(self)
        def fill(self, book, **kwargs):
            filled, price = super().fill(book, **kwargs)
            self.attempts.append({'at': kwargs['at'].isoformat(), 'side': kwargs['side'],
                  'quantity': kwargs['quantity'], 'limit_price': kwargs['limit_price'], 'filled': filled,
                  'depth_received_at': book.received_at.isoformat(), 'snapshot_id': book.snapshot_id,
                  'displayed_executable_ask_shares': sum(q for p,q in book.asks if 0 < p <= kwargs['limit_price'])})
            return filled, price
    with patch.object(account_replay, 'LotDepthExecution', RecordedDepth):
        row = account_replay.legal_replay(*args)
    row['execution_attempts'] = models[0].attempts if models else []
    return row


def witness_short(row, output, depths):
    if row.get('side') != 'SHORT' or not row.get('execution_requests'):
        return {'status': 'NO_FILLED_SHORT_TO_MANAGE', 'actual_orders': 0}
    safety = ShortCoverSafety(output / 'mock_short_witness.sqlite', account=row['variant'], symbol=row['stock_id'])
    requests = row['execution_attempts']
    at = _parse_stamp(requests[0]['at'])
    entry = safety.entry('paper-entry', requests[0]['quantity'], requests[0]['limit_price'], at)
    qty = row.get('entry_quantity', 0)
    if qty:
        safety.fill(entry.order_id, 'paper-entry-fill', qty, row['entry_price'])
    entry = safety.engine.get_order(entry.order_id)
    if entry.remaining_quantity:
        safety.cancel(entry.order_id)
        safety.cancel_ack(entry.order_id)
        safety.reconcile(**safety.mock_snapshot({'paper-entry': 'CANCELED'}))
    else:
        safety.reconcile(**safety.mock_snapshot())
    for n, req in enumerate(requests[1:]):
        at = _parse_stamp(req['at'])
        visible = req['displayed_executable_ask_shares']
        safety.reconcile(**safety.mock_snapshot())
        order = safety.cover(f'paper-cover-{n}', at=at, ask=req['limit_price'], ask_quantity=visible,
                             quote_at=_parse_stamp(req['depth_received_at']))
        if req['filled'] and not order:
            raise ValueError('PAPER_COVER_WITNESS_COULD_NOT_RESERVE_OBSERVED_FILL')
        if order and req['filled']:
            safety.fill(order.order_id, f'paper-cover-fill-{n}', req['filled'], req['limit_price'])
        if order:
            current = safety.engine.get_order(order.order_id)
            if current.remaining_quantity:
                safety.cancel(order.order_id)
                safety.cancel_ack(order.order_id)
                safety.reconcile(**safety.mock_snapshot({current.idempotency_key: 'CANCELED'}))
            else:
                safety.reconcile(**safety.mock_snapshot())
    if safety.remaining():
        safety.alert('CLOSE_REPORT_HAS_UNCOVERED_SHORT')
    snapshot = safety.snapshot()
    assert snapshot['remaining_filled_short'] == row['residual_shares']
    if row['status'] == 'SCORED_TRADE':
        assert snapshot['status'] == 'FLAT'
    write(output / 'cover_safety.json', snapshot)
    alerts = [dict(r) for r in safety.engine.connection.execute('SELECT event_type,payload FROM events WHERE event_type="SHORT_MOCK_CRITICAL"')]
    write(output / 'critical_events.json', alerts)
    safety.close()
    return snapshot


def process_day(directory, output, *, cache_dir=None, fetch_public=False, observation_scope='ENGINEERING_ACCEPTANCE_NOT_PROSPECTIVE', carried_positions=None):
    output.mkdir(parents=True, exist_ok=True)
    initial = json.loads((directory / 'run_manifest.json').read_text())
    watch = json.loads((directory / 'watchlist.json').read_text())
    date = _parse_stamp(initial.get('ended_at') or initial['started_at']).strftime('%Y%m%d')
    qualified, vetoes = qualification(watch, date, output, cache_dir, fetch_public)
    source = {'run_id': initial['run_id'], 'session_date': date, 'source_directory': str(directory),
              'manifest_sha256': sha(directory / 'run_manifest.json'), 'watchlist_sha256': sha(directory / 'watchlist.json'),
              'started_at': initial['started_at'], 'ended_at': initial['ended_at'], 'observation_scope': observation_scope,
              'unit_status': 'UNIT_UNVERIFIED', 'eligibility': 'ELIGIBILITY_UNPROVEN', 'accounts_are_not_combined': True}
    try:
        stocks, market, depths, raw, manifest, watch, coverage, quality = load_inputs(directory)
        signals, approvals, ties, regime, context = chronological_selection(stocks, market, date, vetoes)
    except Exception as exc:
        error = type(exc).__name__ + ': ' + str(exc)
        write(output / 'source.json', {**source, 'status': 'DATA_UNAVAILABLE', 'error': error})
        for account in VARIANTS:
            write(output / account / 'report.json', {'account': account, 'date': date, 'status': 'DATA_UNAVAILABLE',
                   'net_pnl': None, 'sample_count': 0, 'error': error, 'observation_scope': observation_scope})
        return {**source, 'status': 'DATA_UNAVAILABLE', 'error': error}
    source.update(status='PAPER_DAY_COMPLETE', coverage=coverage, quality=quality,
                  artifact_hashes={n: h for n, h in manifest['artifacts'].items() if n in ('ticks.jsonl.gz','books.jsonl.gz','ticks.jsonl','books.jsonl','market_context_ticks.jsonl.gz','market_context_books.jsonl.gz')})
    write(output / 'source.json', source)
    write(output / 'signal_episodes.json', episodes(approvals))
    write(output / 'diagnostics.json', {'same_boundary_ties': ties, 's1_regime_descriptive_only': regime, 'context': context,
                                      'approved_boundaries_are_not_independent_samples': True})
    for account in VARIANTS:
        signal = signals[account]
        rows = []
        if signal:
            write(output / account / 'signal.json', asdict(signal))
            index = bisect_right([b.received_at for b in depths[signal.stock_id]], signal.decision_time) - 1
            write(output / account / 'raw_entry_book.json', raw[signal.stock_id][index] if index >= 0 else {'status': 'MISSING_RAW_BOOK'})
        for unit in (1000, 1):
            carried = (carried_positions or {}).get((account, unit))
            if carried:
                row = {'date': date, 'variant': account, 'volume_multiplier': unit,
                       'status': 'PRIOR_FILLED_POSITION_UNRESOLVED_NO_NEW_ENTRY', 'net_pnl': None,
                       'entry_quantity': 0, 'residual_shares': carried['residual_shares'],
                       'side': carried['side'], 'stock_id': carried['stock_id'], 'origin_day': carried.get('origin_day', carried['date']),
                       'observation_scope': observation_scope, 'unit_status': 'UNIT_UNVERIFIED',
                       'eligibility': 'ELIGIBILITY_UNPROVEN', 'new_trade_sample': False,
                       'carried_exposure_is_not_reset_at_new_session': True}
                write(output / account / f'unit_{unit}/trade.json', row)
                rows.append(row)
                continue
            execution_variant = 'S1' if signal and signal.side == 'SHORT' else 'S0'
            row = observed_replay(signal, stocks, depths, manifest, execution_variant, 'BASE_CURRENT_EXECUTION', unit)
            row.update(variant=account, observation_scope=observation_scope, account_id=f'{account}:{date}:{unit}',
                       signal_sample_count=1 if signal else 0, independent_trade_sample=bool(row.get('entry_quantity',0)),
                       s1_regime_descriptive_only=regime if signal and signal.side == 'SHORT' else None)
            state_dir = output / account / f'unit_{unit}'
            state_dir.mkdir(parents=True, exist_ok=True)
            row['cover_safety'] = witness_short(row, state_dir, depths)
            write(state_dir / 'trade.json', row)
            rows.append(row)
        write(output / account / 'report.json', {'account': account, 'date': date, 'observation_scope': observation_scope,
              'unit_status': 'UNIT_UNVERIFIED', 'eligibility': 'ELIGIBILITY_UNPROVEN',
              'not_added_to_other_accounts': True, 'conditional_metrics': {str(r['volume_multiplier']): safe_metrics([r]) for r in rows},
              'unconditional_net_pnl': None, 'twenty_trades_is_review_only_not_live_enable': True,
              'logical_signal_samples': 1 if signal else 0, 'strategy_position_source': 'PAPER_FILLED_ONLY',
              'no_real_baseline_read': True, 'broker_permission_supply_quota': 'UNKNOWN'})
    return source
