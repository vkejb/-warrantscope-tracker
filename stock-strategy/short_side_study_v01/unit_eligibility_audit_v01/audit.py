"""Offline evidence audit only. No strategy selection, replay, broker or runtime writes."""
import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
STRATEGY = STUDY.parent
DATES = ('20260929', '20261001', '20261002', '20261005', '20261006', '20261008')
FLAG_FIELD_LAYOUT = {'IndexFlag20': 'BID_LEVELS_1_TO_5', 'IndexFlag21': 'ASK_LEVELS_1_TO_5',
                     'IndexFlag42': 'BID_LEVELS_6_TO_10', 'IndexFlag43': 'ASK_LEVELS_6_TO_10',
                     'IndexFlag50': 'BOTH_SIDES_LEVELS_1_TO_5', 'IndexFlag51': 'BOTH_SIDES_LEVELS_6_TO_10'}


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def verify_hash(path, expected):
    actual = sha(path)
    if actual != expected:
        raise ValueError(f'SOURCE_HASH_MISMATCH: {path}')
    return actual


def strict_integer(value):
    """Do not silently truncate fractional depth like the legacy int(float(q))."""
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('INVALID_QUANTITY') from exc
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError('INVALID_QUANTITY')
    return int(number)


def legal_quantity(quantity, market='TWSE', lot_size=1000):
    """Frozen Taiwan ordinary-stock contract; never infer an odd-lot market."""
    quantity = strict_integer(quantity)
    if market in ('TWSE', 'TPEX', 'TWOTC'):
        return quantity % lot_size == 0
    if market in ('TWSEODD', 'TWOTCODD'):
        return 0 <= quantity < lot_size
    return None


def dated_exchange_row(payload, date, stock_id):
    """A dated public table flag, not broker entitlement or entry-time approval."""
    if payload['session_date'] != date or payload['raw_response'].get('date') != date:
        raise ValueError('HISTORICAL_DATE_MISMATCH')
    raw = payload['raw_response']
    if raw.get('stat') != 'OK' or payload.get('market') != 'TWSE':
        raise ValueError('HISTORICAL_SOURCE_STATUS_MISMATCH')
    table = next(t for t in raw['tables'] if '暫停現股賣出後現款買進當沖註記' in t['fields'])
    code_col = table['fields'].index('證券代號')
    flag_col = table['fields'].index('暫停現股賣出後現款買進當沖註記')
    found = [r for r in table['data'] if str(r[code_col]) == stock_id]
    if len(found) > 1:
        raise ValueError('DUPLICATE_HISTORICAL_SYMBOL')
    if not found:
        return {'exchange_status': 'HISTORICAL_TARGET_UNPROVEN', 'exchange_flag': None,
                'raw_row_index': None, 'table_fields': table['fields']}
    flag = str(found[0][flag_col]).strip()
    if stock_id not in payload['codes'] or payload['codes'][stock_id] != (flag == ''):
        raise ValueError('HISTORICAL_CACHE_MAPPING_MISMATCH')
    return {'exchange_status': 'HISTORICAL_SELL_FIRST_SUSPENDED' if flag else 'HISTORICAL_TABLE_LISTED_FLAG_CLEAR',
            'exchange_flag': flag, 'raw_row_index': table['data'].index(found[0]), 'table_fields': table['fields']}


def sensitivity_row(row, paired):
    quantity = row.get('entry_quantity')
    fills = row.get('exit_fills', [])
    invalid_exit = [f for f in fills if not legal_quantity(f['shares'])]
    target = row.get('target_quantity')
    comparison_available = target is not None and paired.get('target_quantity') is not None
    return {k: row.get(k) for k in ('date', 'variant', 'scenario', 'volume_multiplier', 'stock_id', 'side', 'status')} | {
        'target_quantity': target, 'entry_quantity': quantity, 'residual_shares': row.get('residual_shares'),
        'paired_target_quantity': paired.get('target_quantity'),
        'same_requested_quantity': target == paired.get('target_quantity') if comparison_available else None,
        'target_comparison_status': 'CHECKED' if comparison_available else 'NO_RECORDED_TARGET',
        'legal_entry_increment': legal_quantity(quantity) if quantity is not None else None,
        'invalid_exit_fills': invalid_exit,
        'filled_notional_twd': quantity * row['entry_price'] if quantity is not None and 'entry_price' in row else None,
        'requested_notional_twd': target * row['entry_price'] if target is not None and 'entry_price' in row else None,
        'legacy_partial_entry': row.get('partial_entry'),
        'legacy_completed_subset_pnl': row.get('net_pnl'),
        'verified_full_strategy_pnl': None,
        'audit_status': 'INVALID_BOARD_LOT_EXECUTION' if (quantity and not legal_quantity(quantity)) or invalid_exit
                        else 'UNIT_UNVERIFIED',
        'risk_basis': 'LEGACY_MANAGED_POSITION_USES_ACTUAL_PARTIAL_QUANTITY_NOT_REQUESTED_QUANTITY',
        'cancel_remainder_evidence': 'MODEL_ASSUMPTION_NO_BROKER_EVIDENCE',
    }


def scan_stream(path, watch_symbols):
    counts, fields, flags, markets = Counter(), Counter(), Counter(), Counter()
    shape_failures = 0
    first_by_pair = {}
    with (gzip.open(path, 'rt', encoding='utf-8') if path.suffix == '.gz' else path.open()) as stream:
        for number, line in enumerate(stream, 1):
            row = json.loads(line)
            sid = str(row.get('stock_id', ''))
            if sid not in watch_symbols:
                raise ValueError('UNEXPECTED_ARCHIVE_SYMBOL')
            counts[sid] += 1
            fields.update(row.keys())
            flag = str(row.get('index_flag', 'NOT_PRESENT'))
            market = str(row.get('market', 'NOT_PRESENT'))
            flags[flag] += 1
            markets[market] += 1
            key = f'{market}/{flag}'
            if key not in first_by_pair:
                first_by_pair[key] = {'line': number, 'stock_id': sid, 'received_at': row.get('received_at'),
                                      'field_names': sorted(row), 'quantity_unit': 'UNIT_UNVERIFIED'}
            if flag == 'IndexFlag50':
                try:
                    if any(len(row[k]) != 5 for k in ('buy_prices', 'buy_volumes', 'sell_prices', 'sell_volumes')):
                        raise ValueError('NOT_FIVE_LEVELS')
                    for k in ('buy_volumes', 'sell_volumes'):
                        for value in row[k]:
                            strict_integer(value)
                except (ValueError, KeyError, TypeError):
                    shape_failures += 1
    return {'path': str(path), 'sha256': sha(path), 'rows': sum(counts.values()), 'symbol_counts': dict(counts),
            'index_flags': dict(flags), 'archived_market_labels': dict(markets), 'field_presence': dict(fields),
            'first_schema_evidence': first_by_pair, 'invalid_flag50_quantity_shapes': shape_failures,
            'raw_callback_market_type_preserved': 'MarketType' in fields or 'market_type' in fields,
            'synchronized_independent_depth_evidence': 'NONE_IN_FROZEN_ARCHIVE_PROVENANCE'}


def source_excerpt(path, first, last, interpretation):
    lines = path.read_text().splitlines()
    return {'path': str(path), 'sha256': sha(path), 'first_line': first, 'last_line': last,
            'source': '\n'.join(lines[first - 1:last]), 'interpretation': interpretation}


def audit():
    inventory_path = STUDY / 'results_expanded/data_inventory.json'
    inventory = [r for r in read(inventory_path) if r.get('eligible')]
    if sorted(r['session_date'] for r in inventory) != list(DATES):
        raise ValueError('FROZEN_COHORT_CHANGED')
    sessions, eligibility, inputs = [], [], [{'path': str(inventory_path), 'sha256': sha(inventory_path)}]
    for record in inventory:
        directory, date = Path(record['source_directory']), record['session_date']
        manifest, watch = read(directory / 'run_manifest.json'), read(directory / 'watchlist.json')
        unsigned = {k: v for k, v in manifest.items() if k != 'manifest_hash'}
        signed_bytes = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        if hashlib.sha256(signed_bytes).hexdigest() != manifest.get('manifest_hash'):
            raise ValueError('SOURCE_MANIFEST_HASH_MISMATCH')
        actual_date = datetime.fromisoformat(manifest['ended_at'].replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Taipei')).strftime('%Y%m%d')
        if actual_date != date or len(watch['stocks']) != 30:
            raise ValueError('SOURCE_DATE_OR_TOP30_MISMATCH')
        for name, expected in manifest['artifacts'].items():
            if name in ('watchlist.json', 'books.jsonl', 'books.jsonl.gz', 'market_context_books.jsonl', 'market_context_books.jsonl.gz'):
                verify_hash(directory / name, expected)
        if manifest.get('manifest_hash') != record['source_manifest_hash']:
            raise ValueError('INVENTORY_MANIFEST_MISMATCH')
        symbols = {str(s['stock_id']) for s in watch['stocks']}
        book_path = next(directory / n for n in ('books.jsonl.gz', 'books.jsonl') if (directory / n).is_file())
        streams = [scan_stream(book_path, symbols)]
        for n in ('market_context_books.jsonl.gz', 'market_context_books.jsonl'):
            if n in manifest['artifacts']:
                streams.append(scan_stream(directory / n, {'0050'}))
                break
        version_keys = [k for k in manifest if any(v in k.lower() for v in ('sdk', 'vendor', 'binary', 'assembly'))]
        sessions.append({'date': date, 'run_id': manifest['run_id'], 'directory': str(directory),
                         'manifest_sha256': sha(directory / 'run_manifest.json'), 'watchlist_sha256': sha(directory / 'watchlist.json'),
                         'manifest_version_keys': version_keys, 'historical_sdk_binding': 'HISTORICAL_SDK_VERSION_UNBOUND',
                         'market_type_provenance': 'WATCHLIST_LABEL_NOT_RAW_CALLBACK_MARKETTYPE', 'streams': streams})
        cache_path = STUDY / 'sources' / f'{date}_twse_eligibility.json'
        payload = read(cache_path)
        inputs.append({'path': str(cache_path), 'sha256': sha(cache_path),
                       'upstream_response_sha256_recorded': payload['sha256'],
                       'upstream_original_bytes_available': False})
        for stock in watch['stocks']:
            sid, market = str(stock['stock_id']), stock['market']
            row = {'date': date, 'stock_id': sid, 'stock_name': stock['stock_name'], 'market': market,
                   'broker_quota_status': 'BROKER_QUOTA_UNPROVEN', 'broker_acceptance_status': 'BROKER_ACCEPTANCE_UNPROVEN',
                   'entry_time_restriction_status': 'ENTRY_TIME_RESTRICTIONS_UNPROVEN', 'fully_proven_short_eligibility': None}
            if market == 'TWSE':
                row.update(dated_exchange_row(payload, date, sid), source=payload['url'], cache_path=str(cache_path),
                           cache_sha256=sha(cache_path), source_date=payload['raw_response']['date'])
            else:
                row.update(exchange_status='TPEX_HISTORICAL_TARGET_AND_SUSPENSION_UNPROVEN', exchange_flag=None,
                           source='https://www.tpex.org.tw/zh-tw/mainboard/trading/day-trading/suspended-historical.html',
                           source_date=None, limitation='Historical form exists; no frozen dated target/flag response available. Current list not used.')
            eligibility.append(row)
    legacy_path = STUDY / 'results_s4/daily_results.json'
    legacy = read(legacy_path)
    pairs = {(r['date'], r['variant'], r['scenario'], r['volume_multiplier']): r for r in legacy}
    sensitivities = [sensitivity_row(r, pairs[(r['date'], r['variant'], r['scenario'], 1000 if r['volume_multiplier'] == 1 else 1)]) for r in legacy]
    short_keys = {(r['date'], r.get('stock_id')) for r in legacy if r.get('side') == 'SHORT'}
    inputs.append({'path': str(legacy_path), 'sha256': sha(legacy_path)})
    affected = []
    for name in ('results', 'results_expanded', 'results_s4'):
        for path in sorted((STUDY / name).glob('*')):
            if path.is_file():
                affected.append({'path': str(path.relative_to(STUDY)), 'sha256': sha(path),
                    'impact': 'CAPACITY_PNL_DRAWDOWN_AND_PARTIAL_FILL_INFERENCE_UNVERIFIED' if path.name in (
                        'daily_results.json', 'summary.json', 'report_zh-TW.txt', 'counterfactual_replacements.json') else
                        'SUPPORTING_EVIDENCE_NOT_A_UNIT_OR_LEGAL_FILL_CERTIFICATION',
                    'action': 'PRESERVED_NOT_OVERWRITTEN_NO_NEW_VERIFIED_PERFORMANCE'})
    trace = [source_excerpt(STRATEGY / 'yuanta_intraday_shadow_v01/collector_main.py', 172, 207,
                           'Raw quantity -> text; no scaling. TWSE/TPEX subscriptions map to TWSE/TWOTC, not ODD.'),
             source_excerpt(STRATEGY / 'yuanta_intraday_shadow_v01/collector_main.py', 312, 359,
                           'Market metadata from watchlist. Raw callback MarketType and SDK binary hash not retained.'),
             source_excerpt(STRATEGY / 'yuanta_intraday_shadow_v01/collector.py', 299, 335,
                           'Canonical JSON and optional gzip: no quantity scaling.'),
             source_excerpt(STRATEGY / 'yuanta_intraday_shadow_v01/direction_follow_backtest.py', 117, 126,
                           'Raw numeric parse; no quantity unit conversion.'),
             source_excerpt(STRATEGY / 'yuanta_intraday_shadow_v01/direction_follow_backtest.py', 178, 196,
                           'Aggregated buy/sell sums for signals, not execution capacity. No unit proof.'),
             source_excerpt(STUDY / 'replay.py', 31, 41,
                           'Depth quantities int(float(raw)); potential fractional truncation. No flag/channel filter.'),
             source_excerpt(STUDY / 'replay.py', 80, 137,
                           'Only capacity scaling q*volume_multiplier once; target shares independently rounded to 1000. Partial fills change position/risk basis.'),
             source_excerpt(STUDY / 'execution.py', 25, 75,
                           'Capacity consumed directly as shares; no 1000-share increment validation at each fill.'),
             source_excerpt(STUDY / 'replay.py', 176, 199,
                           'Arbitrary-share exits and model remainder cancellation. Exposure nonzero returns net_pnl null.'),
             source_excerpt(STRATEGY / 'yuanta_broker_execution_v01/adapter.py', 824, 848,
                           'Separate order boundary shares//1000 for APCode0 NEW; code assertion, not quote-unit proof.'),
             source_excerpt(STRATEGY / 'yuanta_broker_execution_v01/adapter.py', 352, 378,
                           'Report quantities integer-normalized without multiplying1000. Bundled report docs explicitly say shares for stocks.'),
             source_excerpt(STUDY / 'policy.py', 94, 98,
                           'Only explicit False rejects SHORT; missing eligibility can proceed as hypothetical, not broker authorization.')]
    trace.append(source_excerpt(STUDY / 's4.py', 65, 74,
                               'TWSE absent from cached table becomes False in legacy selection; absence is not positive ineligibility proof. TPEx missing mapping remains hypothetical.'))
    trace.append(source_excerpt(STRATEGY / 'yuanta_intraday_shadow_v01/live_parity_backtest.py', 117, 129,
                               'Aggregated signal book quantities passed unchanged into engine, not scaled execution depth.'))
    trace.append(source_excerpt(STRATEGY / 'yuanta_intraday_shadow_v01/main.py', 26, 30,
                               '_safe_text is string conversion only; no scaling.'))
    trace.append(source_excerpt(STRATEGY / 'yuanta_intraday_shadow_v01/main.py', 53, 83,
                               'Read source only, never execute this loader. YuantaOneAPI namespace imported from YuantaSparkAPI.dll. No historical binary binding.'))
    trace.append(source_excerpt(STRATEGY / 'yuanta_live_runtime_v01/strategy.py', 1052, 1069,
                               'Read-only inherited risk formula: projected net and stop inversion use actual position.quantity.'))
    trace.append(source_excerpt(STRATEGY / 'yuanta_live_runtime_v01/strategy.py', 1102, 1117,
                               'Read-only inherited MFE basis refreshes on actual quantity, so tiny simulated partial position changes risk basis.'))
    output = {'audit_status': 'EVIDENCE_INSUFFICIENT_CLOSED_NO_REPLAY', 'unit_status': 'UNIT_UNVERIFIED',
              'frozen_reference_commit': '4a659d3', 'cohort_dates': list(DATES),
              'rerun_performed': False, 'verified_account_pnl': None, 'verified_daily_drawdown': None,
              'verified_intraday_drawdown': None, 'verified_robustness': None,
              'no_zero_or_subset_substitution': True, 'inputs': inputs, 'sessions': sessions,
              'historical_eligibility': eligibility, 'selected_short_eligibility': [r for r in eligibility if (r['date'], r['stock_id']) in short_keys],
              'sensitivity_audit': sensitivities, 'affected_old_artifacts': affected, 'conversion_trace': trace,
              'conversion_conclusion': 'ONE_REPLAY_SCALE_ONLY_NO_OBSERVED_DOUBLE_SCALE_BUT_CORRECT_MULTIPLIER_UNPROVEN',
              'evidence_limitations': ['NO_VERSION_BOUND_EXPLICIT_DEPTH_UNIT_DOCUMENT', 'NO_RAW_MARKETTYPE_OR_HISTORICAL_SDK_HASH',
                                       'NO_INDEPENDENT_TIME_MATCHED_DEPTH_SOURCE', 'TPEX_HISTORICAL_QUALIFICATION_MISSING',
                                       'BROKER_QUOTA_AND_ACCEPTANCE_UNPROVEN'],
              'flag_field_layout': FLAG_FIELD_LAYOUT,
              'operational_actions': {'broker_login': 0, 'orders': 0, 'operational_process_changes': 0,
                                      'live_or_db_or_control_writes': 0, 'push_merge_deploy': 0}}
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': result['audit_status'], 'sessions': len(result['sessions']),
                      'historical_rows': len(result['historical_eligibility']),
                      'illegal_legacy_execution_rows': sum(r['audit_status'] == 'INVALID_BOARD_LOT_EXECUTION' for r in result['sensitivity_audit']),
                      'rerun_performed': False}))


if __name__ == '__main__':
    main()
