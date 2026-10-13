"""Read-only acceptance of the first post-activation SHORT paper day."""
import argparse
from bisect import bisect_right
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch
from zoneinfo import ZoneInfo

from short_side_study_v01 import s4 as selection_module
from short_side_study_v01.policy import StudyEngine
from short_side_study_v01.account_replay_v02.run import safe_metrics
from yuanta_live_runtime_v01.strategy import LiveDirectionEngine
from yuanta_intraday_shadow_v01.live_parity_backtest import _validate_full_session_coverage

from .main import DEFAULT_RUNTIME, STRATEGY, inspect_published, is_prospective, load_activation
from .pipeline import VARIANTS, episodes, load_inputs, observed_replay, sha


TAIPEI = ZoneInfo('Asia/Taipei')
FIRST_DATE = '20261014'


def first_complete_source(source_root, activation):
    """Find the earliest eligible sealed run, retaining rejected run reasons."""
    rejected = []
    candidates = []
    for path in sorted(source_root.glob('*/run_manifest.json')):
        try:
            manifest = json.loads(path.read_text())
            date = datetime.fromisoformat(manifest['started_at'].replace('Z', '+00:00')).astimezone(TAIPEI).strftime('%Y%m%d')
            if date < FIRST_DATE or not is_prospective(manifest, activation):
                continue
            if manifest.get('status') != 'COMPLETE' or manifest.get('event_counts', {}).get('callback_errors') != 0:
                rejected.append({'run_id': manifest.get('run_id', path.parent.name), 'reason': 'INCOMPLETE_OR_CALLBACK_ERROR'})
                continue
            coverage = {'source_statuses': ['COMPLETE'], 'callback_errors': 0, 'session_date': date,
                        'started_at_taipei': manifest['started_at'], 'ended_at_taipei': manifest['ended_at']}
            _validate_full_session_coverage(coverage)
            candidates.append((date, manifest['started_at'], path.parent))
        except Exception as exc:
            rejected.append({'run_id': path.parent.name, 'reason': type(exc).__name__ + ': ' + str(exc)})
    candidates.sort()
    return (candidates[0][2] if candidates else None), rejected


def classify_account(signal, *, source_ok, candidate_data_ok, benchmark_ok):
    if not source_ok or not candidate_data_ok or (benchmark_ok is False):
        return 'DATA_MISSING'
    return 'SIGNAL' if signal is not None else 'NO_SIGNAL'


def replay_raw_selection(stocks, market, date, vetoes):
    """Trace original S1 raw signal calls over the complete archived cohort."""
    scanned = {}
    raw_short = set()
    s1_live_gate_calls = []
    original_live_choose = LiveDirectionEngine.choose_entry

    def traced_live_choose(engine, decision, *, allow_short):
        if getattr(engine, 'variant', None) == 'S1':
            s1_live_gate_calls.append({'decision_time': decision.isoformat(), 'allow_short': allow_short})
        return original_live_choose(engine, decision, allow_short=allow_short)

    class TracedStudyEngine(StudyEngine):
        def _signal_for(self, symbol, decision):
            result = super()._signal_for(symbol, decision)
            if self.variant == 'S1':
                scanned.setdefault(decision.isoformat(), set()).add(symbol)
                if result is not None and result['side'] == 'SHORT':
                    raw_short.add((symbol, decision.isoformat()))
            return result

    with patch.object(selection_module, 'StudyEngine', TracedStudyEngine), \
         patch.object(LiveDirectionEngine, 'choose_entry', traced_live_choose):
        selected, approvals, ties, regime, context = selection_module.chronological_selection(
            stocks, market, date, vetoes)
    candidates = set(stocks)
    scan_complete = bool(scanned) and all(seen == candidates for seen in scanned.values())
    s1 = selected['S1']
    s1_in_raw = s1 is None or (s1.stock_id, s1.decision_time.isoformat()) in raw_short
    trace = {'source': 'ORIGINAL__signal_for_ON_ARCHIVED_TICK_AND_BOOK',
             'candidate_count': len(candidates), 'decision_boundaries_scanned': len(scanned),
             'minimum_candidates_per_boundary': min(map(len, scanned.values()), default=0),
             'all_candidate_symbols_scanned_at_every_boundary': scan_complete,
             'raw_short_candidate_boundary_count': len(raw_short),
             'selected_s1_was_raw_short_candidate': s1_in_raw,
             'live_allow_short_flag_used': bool(s1_live_gate_calls)}
    return (selected, approvals, ties, regime, context), trace


def inspect_mock_ledger(path, *, account, symbol, trade, saved_events):
    """Read persisted paper fills without reopening its writable engine."""
    if not path.is_file():
        return {'issues': ['MOCK_LEDGER_MISSING']}
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        identity_row = db.execute("SELECT value FROM short_mock_meta WHERE key='identity'").fetchone()
        identity = json.loads(identity_row[0]) if identity_row else None
        orders = db.execute('SELECT side, intent, status, idempotency_key, quantity, filled_quantity FROM orders').fetchall()
        fills = db.execute('SELECT o.side, f.quantity FROM fills f JOIN orders o ON o.order_id=f.order_id').fetchall()
        critical = [{'event_type': event, 'payload': payload} for event, payload in db.execute(
            "SELECT event_type,payload FROM events WHERE event_type='SHORT_MOCK_CRITICAL' ORDER BY sequence")]
    sold = sum(quantity for side, quantity in fills if side == 'SELL')
    bought = sum(quantity for side, quantity in fills if side == 'BUY')
    issues = []
    if identity != {'mode': 'MOCK_ONLY', 'account': account, 'symbol': symbol, 'baseline': 0}:
        issues.append('MOCK_LEDGER_IDENTITY_MISMATCH')
    if len({order[3] for order in orders}) != len(orders) or sum(order[1] == 'ENTRY' for order in orders) != 1:
        issues.append('MOCK_LEDGER_DUPLICATE_OR_MULTIPLE_ENTRIES')
    if any(order[2] in ('SUBMITTED', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'CANCEL_PENDING') for order in orders):
        issues.append('MOCK_LEDGER_OPEN_ORDER')
    if any(quantity <= 0 or quantity % 1000 for _, quantity in fills):
        issues.append('MOCK_LEDGER_NONLOT_FILL')
    if sold != trade.get('entry_quantity') or bought != trade.get('covered_shares') or sold - bought != trade.get('residual_shares'):
        issues.append('MOCK_LEDGER_FILL_POSITION_MISMATCH')
    if critical != saved_events:
        issues.append('MOCK_LEDGER_CRITICAL_EVENTS_MISMATCH')
    return {'entry_filled_shares': sold, 'covered_shares': bought, 'remaining_shares': sold - bought,
            'order_count': len(orders), 'critical_event_count': len(critical), 'issues': issues}


def audit_day(source_dir, day_dir, activation):
    issues = []
    publication = inspect_published(day_dir)
    manifest = json.loads((source_dir / 'run_manifest.json').read_text())
    date = datetime.fromisoformat(manifest['started_at'].replace('Z', '+00:00')).astimezone(TAIPEI).strftime('%Y%m%d')
    if date < FIRST_DATE or not is_prospective(manifest, activation):
        issues.append('SOURCE_PRECEDES_FIRST_PROSPECTIVE_DATE_OR_ACTIVATION')
    if publication['session_date'] != date or publication['source_manifest_sha256'] != sha(source_dir / 'run_manifest.json'):
        issues.append('PUBLICATION_SOURCE_IDENTITY_MISMATCH')
    if publication.get('day_status') not in (None, 'PAPER_DAY_COMPLETE'):
        issues.append('PUBLICATION_DAY_NOT_COMPLETE')
    if (day_dir.parent.parent / 'conflicts' / date).exists():
        issues.append('CONFLICTING_SAME_DAY_SOURCE')
    source = json.loads((day_dir / 'source.json').read_text())
    if source.get('observation_scope') != 'PROSPECTIVE_AFTER_ACTUAL_ACTIVATION':
        issues.append('NOT_PROSPECTIVE_PUBLICATION')
    if source.get('unit_status') != 'UNIT_UNVERIFIED':
        issues.append('DEPTH_UNIT_LABEL_LOST')
    if source.get('status') != 'PAPER_DAY_COMPLETE':
        return {'status': 'DATA_MISSING', 'date': date, 'source_status': source.get('status'),
                'issues': issues, 'accounts': {a: {'status': 'DATA_MISSING'} for a in VARIANTS}}

    try:
        stocks, market, depths, raw, replay_manifest, watch, coverage, quality = load_inputs(source_dir)
    except Exception as exc:
        return {'status': 'DATA_MISSING', 'date': date, 'source_run_id': manifest['run_id'],
                'issues': issues + [type(exc).__name__ + ': ' + str(exc)],
                'accounts': {a: {'status': 'DATA_MISSING'} for a in VARIANTS}}
    ids = {str(s['stock_id']) for s in watch['stocks']}
    missing = {s: [name for name, present in (
        ('valid_ticks', s in stocks and bool(stocks[s]['ticks'])),
        ('valid_books', s in stocks and bool(stocks[s]['books'])),
        ('raw_five_level_books', bool(depths[s]))) if not present] for s in sorted(ids)}
    missing = {s: reasons for s, reasons in missing.items() if reasons}
    candidate_data_ok = len(ids) == 30 and not missing and quality['invalid_depth_rows'] == 0
    if not candidate_data_ok:
        issues.append('CANDIDATE_TICK_OR_BOOK_DATA_MISSING')
    benchmark_ok = quality['market_context'] == 'AVAILABLE_DESCRIPTIVE_ONLY' and bool(market.get('0050', {}).get('ticks')) and bool(market.get('0050', {}).get('books'))
    if not benchmark_ok:
        issues.append('BENCHMARK_DATA_MISSING_FOR_S0_AND_S4')
    saved_qualification = json.loads((day_dir / 'qualification.json').read_text())
    if len(saved_qualification.get('rows', [])) != 30 or saved_qualification.get('session_date') != date:
        issues.append('QUALIFICATION_SNAPSHOT_MISSING_OR_WRONG_DATE')
    vetoes = {r['stock_id']: False for r in saved_qualification.get('rows', []) if r.get('sell_first_suspended') is True}
    try:
        (signals, approvals, _, _, _), raw_trace = replay_raw_selection(stocks, market, date, vetoes)
    except Exception as exc:
        return {'status': 'DATA_MISSING', 'date': date, 'source_run_id': manifest['run_id'],
                'issues': issues + ['RAW_REPLAY_FAILED:' + type(exc).__name__ + ': ' + str(exc)],
                'accounts': {a: {'status': 'DATA_MISSING'} for a in VARIANTS}}
    saved_episodes = json.loads((day_dir / 'signal_episodes.json').read_text())
    if saved_episodes != json.loads(json.dumps(episodes(approvals), default=str)):
        issues.append('SIGNAL_EPISODE_REPLAY_MISMATCH')
    if (not raw_trace['all_candidate_symbols_scanned_at_every_boundary']
            or not raw_trace['selected_s1_was_raw_short_candidate']
            or raw_trace['live_allow_short_flag_used']):
        issues.append('RAW_SHORT_FULL_COHORT_TRACE_FAILED')
    accounts = {}
    for account in VARIANTS:
        signal = signals[account]
        account_issues = []
        saved_signal_path = day_dir / account / 'signal.json'
        if signal is None:
            if saved_signal_path.exists():
                account_issues.append('UNEXPECTED_SAVED_SIGNAL')
        elif not saved_signal_path.exists() or json.loads(saved_signal_path.read_text()) != json.loads(json.dumps(asdict(signal), default=str)):
            account_issues.append('RAW_SIGNAL_REPLAY_MISMATCH')
        if signal is not None:
            books = depths[signal.stock_id]
            index = bisect_right([book.received_at for book in books], signal.decision_time) - 1
            expected_book = raw[signal.stock_id][index] if index >= 0 else {'status': 'MISSING_RAW_BOOK'}
            book_path = day_dir / account / 'raw_entry_book.json'
            if not book_path.exists() or json.loads(book_path.read_text()) != expected_book:
                account_issues.append('RAW_ENTRY_BOOK_MISMATCH')
        report = json.loads((day_dir / account / 'report.json').read_text())
        if report.get('logical_signal_samples') != int(signal is not None) or report.get('unit_status') != 'UNIT_UNVERIFIED':
            account_issues.append('REPORT_SIGNAL_OR_UNIT_MISMATCH')
        unit_rows = {}
        for unit in (1000, 1):
            trade = json.loads((day_dir / account / f'unit_{unit}/trade.json').read_text())
            if trade.get('volume_multiplier') != unit or trade.get('variant') != account or trade.get('unit_status') != 'UNIT_UNVERIFIED':
                account_issues.append(f'UNIT_{unit}_IDENTITY_OR_LABEL_MISMATCH')
            if trade.get('signal_sample_count') != int(signal is not None) or trade.get('independent_trade_sample') != bool(trade.get('entry_quantity', 0)):
                account_issues.append(f'UNIT_{unit}_SAMPLE_COUNT_MISMATCH')
            if trade.get('status') == 'PRIOR_FILLED_POSITION_UNRESOLVED_NO_NEW_ENTRY':
                account_issues.append(f'UNIT_{unit}_UNEXPECTED_CARRIED_POSITION_ON_FIRST_DAY')
            else:
                execution_variant = 'S1' if signal and signal.side == 'SHORT' else 'S0'
                expected = json.loads(json.dumps(observed_replay(
                    signal, stocks, depths, replay_manifest, execution_variant,
                    'BASE_CURRENT_EXECUTION', unit), default=str))
                mismatch = sorted(key for key, value in expected.items() if trade.get(key) != value)
                if mismatch:
                    account_issues.append(f'UNIT_{unit}_TRADE_REPLAY_MISMATCH:' + ','.join(mismatch))
            if report.get('conditional_metrics', {}).get(str(unit)) != safe_metrics([trade]):
                account_issues.append(f'UNIT_{unit}_REPORT_METRICS_MISMATCH')
            remaining = trade.get('residual_shares', 0)
            cover = trade.get('cover_safety', {})
            if remaining < 0 or (trade.get('entry_quantity', 0) and remaining > trade['entry_quantity']):
                account_issues.append(f'UNIT_{unit}_POSITION_QUANTITY_INVALID')
            if signal and signal.side == 'SHORT' and trade.get('entry_quantity', 0):
                if cover.get('remaining_filled_short') != remaining or (cover.get('status') == 'FLAT') != (remaining == 0 and not cover.get('pending_orders') and not cover.get('needs_reconcile')):
                    account_issues.append(f'UNIT_{unit}_COVER_OR_POSITION_MISMATCH')
                if remaining or cover.get('pending_orders') or cover.get('needs_reconcile'):
                    account_issues.append(f'UNIT_{unit}_SHORT_EXPOSURE_UNRESOLVED')
                events_path = day_dir / account / f'unit_{unit}/critical_events.json'
                saved_events = json.loads(events_path.read_text()) if events_path.exists() else None
                ledger = inspect_mock_ledger(day_dir / account / f'unit_{unit}/mock_short_witness.sqlite',
                                             account=account, symbol=signal.stock_id, trade=trade,
                                             saved_events=saved_events)
                account_issues.extend(f'UNIT_{unit}_{issue}' for issue in ledger['issues'])
            else:
                ledger = None
            unit_rows[str(unit)] = {'trade_status': trade.get('status'), 'entry_quantity': trade.get('entry_quantity', 0),
                                    'residual_shares': remaining, 'cover_status': cover.get('status'),
                                    'conditional_only': trade.get('unit_status') == 'UNIT_UNVERIFIED',
                                    'mock_ledger': ledger}
        account_status = classify_account(signal, source_ok=True, candidate_data_ok=candidate_data_ok,
                                          benchmark_ok=benchmark_ok if account in ('S0', 'S4') else None)
        if account_issues:
            account_status = 'ACCEPTANCE_FAILED'
        accounts[account] = {'status': account_status, 'signal': None if signal is None else {'stock_id': signal.stock_id,
                              'side': signal.side, 'decision_time': signal.decision_time.isoformat()},
                             'units': unit_rows, 'issues': account_issues}
    issues.extend(f'{a}:{item}' for a, v in accounts.items() for item in v['issues'])
    return {'status': 'ACCEPTED' if not issues and all(a['status'] != 'DATA_MISSING' for a in accounts.values()) else 'NEEDS_REVIEW',
            'date': date, 'source_run_id': manifest['run_id'], 'source_manifest_sha256': sha(source_dir / 'run_manifest.json'),
            'coverage': coverage, 'quality': quality, 'missing_candidate_data': missing, 'raw_short_trace': raw_trace,
            'unit_status': 'UNIT_UNVERIFIED', 'fills_are_not_broker_verified': True,
            'accounts': accounts, 'issues': issues}


def main():
    parser = argparse.ArgumentParser(description='Read-only first prospective raw SHORT paper acceptance')
    parser.add_argument('--runtime-dir', type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument('--source-root', type=Path, default=STRATEGY / 'yuanta_intraday_shadow_v01/runtime/runs')
    args = parser.parse_args()
    activation = load_activation(args.runtime_dir)
    source, rejected = first_complete_source(args.source_root, activation)
    if source is None:
        result = {'status': 'WAITING_FOR_FIRST_COMPLETE_DAY', 'from_date': FIRST_DATE, 'rejected': rejected,
                  'prospective_samples_claimed': 0}
    else:
        manifest = json.loads((source / 'run_manifest.json').read_text())
        date = datetime.fromisoformat(manifest['started_at'].replace('Z', '+00:00')).astimezone(TAIPEI).strftime('%Y%m%d')
        published = args.runtime_dir / 'days' / date
        if published.is_dir():
            try:
                result = audit_day(source, published, activation)
            except Exception as exc:
                result = {'status': 'ACCEPTANCE_FAILED', 'source_run_id': manifest['run_id'], 'date': date,
                          'reason': type(exc).__name__ + ': ' + str(exc)}
        else:
            result = {'status': 'READY_FOR_MANUAL_DRAIN', 'source_run_id': manifest['run_id'], 'date': date,
                      'source_manifest_sha256': sha(source / 'run_manifest.json'), 'rejected': rejected}
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
