"""One-shot paper CLI. Activation is a paper record, not process/launchd startup."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from zoneinfo import ZoneInfo

from short_side_study_v01.account_replay_v02.run import safe_metrics
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC
from yuanta_live_runtime_v01.strategy import ANTI_CHASE_ENTRY_POLICY, LIVE_EXIT_POLICY
from .pipeline import HERE, VARIANTS, process_day, sha, write

DEFAULT_RUNTIME = HERE / 'runtime'
STRATEGY = HERE.parent


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def source_hashes():
    names = ('short_side_study_v01/policy.py', 'short_side_study_v01/s4.py', 'short_side_study_v01/replay.py',
             'short_side_study_v01/account_replay_v02/run.py', 'short_side_study_v01/account_replay_v02/execution.py',
             'yuanta_live_runtime_v01/strategy.py', 'intraday_paper_execution_v01/engine.py',
             'raw_short_shadow_v01/main.py', 'raw_short_shadow_v01/pipeline.py', 'raw_short_shadow_v01/safety.py')
    return {name: sha(STRATEGY / name) for name in names}


def contract():
    return {'id': 'RAW_SHORT_S1_PAPER_FROZEN_V1', 'mode': 'PAPER_ONLY_POST_SESSION_EXISTING_ARCHIVES',
            'pool': 'EXISTING_PRIOR_SESSION_SEALED_STAGE_A_TOP30_NO_EXPANDED_POOL',
            'accounts': list(VARIANTS), 'accounts_not_added_or_ex_post_ranked': True,
            'base_spec': SPEC, 'anti_chase': ANTI_CHASE_ENTRY_POLICY, 'exit_policy': LIVE_EXIT_POLICY,
            'capital_twd': 190000, 'daily_attempts_per_account': 1, 'short_variant': 'S1',
            'short_0050_gate': False, '0050_descriptive_only_for_s1': True,
            'capacity_hypotheses_shares_per_raw_unit': [1000, 1], 'unit_status': 'UNIT_UNVERIFIED',
            'unit_unknown_is_not_long_order_fault': True, 'permissions_supply_quota': 'UNKNOWN',
            'twenty_entries': 'REVIEW_NODE_ONLY_NEVER_ENABLE_LIVE', 'real_short_routing': False,
            'frozen_source_hashes': source_hashes()}


def owned_runtime(path):
    path = path.resolve()
    if not path.is_relative_to(DEFAULT_RUNTIME.resolve()):
        raise ValueError('ONLY_ISOLATED_RAW_SHORT_RUNTIME_ALLOWED')
    return path


def activate(runtime):
    runtime = owned_runtime(runtime)
    path = runtime / 'activation.json'
    if path.exists():
        return load_activation(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    frozen = contract()
    stamp = now()
    value = {'enabled_at': stamp, 'frozen_at': stamp, 'contract': frozen, 'contract_hash': digest(frozen),
             'scope': 'MANUAL_ONE_SHOT_POST_SESSION_PAPER_PROCESSING', 'scheduler_installed_by_this_task': False,
             'collector_process_changed': False, 'starting_prospective_trades': 0, 'no_backdating': True}
    with path.open('x') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')
    return value


def load_activation(runtime):
    value = json.loads((runtime / 'activation.json').read_text())
    if value['contract_hash'] != digest(value['contract']) or value['contract'] != contract():
        raise ValueError('FROZEN_PAPER_CONTRACT_OR_SOURCE_CHANGED')
    return value


def is_prospective(manifest, activation):
    start = datetime.fromisoformat(manifest['started_at'].replace('Z', '+00:00'))
    enabled = datetime.fromisoformat(activation['enabled_at'])
    return start >= enabled


def inspect_published(target):
    manifest = json.loads((target / 'publication.json').read_text())
    for name, expected in manifest['output_hashes'].items():
        if sha(target / name) != expected:
            raise ValueError('PAPER_DAY_OUTPUT_HASH_MISMATCH:' + name)
    return manifest


def carried_positions(runtime):
    carried = {}
    for previous in sorted((runtime / 'days').glob('*')):
        inspect_published(previous)
        for account in VARIANTS:
            for unit in (1000,1):
                trade = previous / account / f'unit_{unit}/trade.json'
                if trade.exists():
                    row = json.loads(trade.read_text())
                    if row.get('residual_shares',0):
                        carried[(account,unit)] = row
    return carried


def run_day(directory, runtime, *, cache_dir=None, fetch_public=False):
    runtime = owned_runtime(runtime)
    activation = load_activation(runtime)
    source = json.loads((directory / 'run_manifest.json').read_text())
    date = datetime.fromisoformat((source.get('ended_at') or source['started_at']).replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Taipei')).strftime('%Y%m%d')
    identity = sha(directory / 'run_manifest.json')
    if not is_prospective(source, activation):
        record = {'status': 'EXCLUDED_PRE_ACTIVATION', 'source_run_id': source['run_id'], 'source_started_at': source['started_at'],
                  'enabled_at': activation['enabled_at'], 'prospective_samples_added': 0}
        write(runtime / 'excluded' / f'{source["run_id"]}.json', record)
        return record
    target = runtime / 'days' / date
    if target.exists():
        old = inspect_published(target)
        if old['source_manifest_sha256'] == identity:
            return {'status': 'ALREADY_PUBLISHED_NO_NEW_SAMPLE', 'session_date': date}
        write(runtime / 'conflicts' / date / f'{source["run_id"]}.json', {'status': 'DATA_CONFLICT_DO_NOT_COUNT', 'old_source': old['source_manifest_sha256'], 'new_source': identity})
        return {'status': 'DATA_CONFLICT_DO_NOT_COUNT', 'session_date': date}
    stage = Path(tempfile.mkdtemp(prefix='paper-day-', dir=runtime))
    carried = carried_positions(runtime)
    result = process_day(directory, stage, cache_dir=cache_dir, fetch_public=fetch_public,
                         observation_scope='PROSPECTIVE_AFTER_ACTUAL_ACTIVATION', carried_positions=carried)
    publication = {'session_date': date, 'source_manifest_sha256': identity, 'source_run_id': source['run_id'],
                   'activation_hash': digest(activation), 'published_at': now(), 'day_status': result['status'],
                   'output_hashes': {str(p.relative_to(stage)): sha(p) for p in stage.rglob('*') if p.is_file()}}
    write(stage / 'publication.json', publication)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage.rename(target)
    return {'status': 'PUBLISHED', 'session_date': date, 'day_status': result['status']}


def status(runtime):
    runtime = owned_runtime(runtime)
    activation = load_activation(runtime) if (runtime / 'activation.json').exists() else None
    accounts = {v: {} for v in VARIANTS}
    included, unresolved = [], []
    for path in sorted((runtime / 'days').glob('*')):
        try:
            inspect_published(path)
            if (runtime / 'conflicts' / path.name).exists():
                raise ValueError('CONFLICTING_SAME_DAY_SOURCE')
        except Exception as exc:
            unresolved.append({'date': path.name, 'reason': str(exc)})
            continue
        included.append(path.name)
        for account in VARIANTS:
            for unit in (1000, 1):
                trade = path / account / f'unit_{unit}/trade.json'
                if trade.exists():
                    row = json.loads(trade.read_text())
                else:
                    row = {'date': path.name, 'status': 'DATA_UNAVAILABLE', 'net_pnl': None}
                accounts[account].setdefault(str(unit), []).append(row)
    totals = {}
    for account in VARIANTS:
        totals[account] = {}
        for unit in (1000, 1):
            rows = accounts[account].get(str(unit), []) + [{'date': r['date'], 'status': 'DATA_UNAVAILABLE', 'net_pnl': None} for r in unresolved]
            m = safe_metrics(rows)
            totals[account][str(unit)] = {**m, 'review_node_reached': m['entered_trades'] >= 20,
                                         'automatic_live_enable': False, 'not_added_to_other_unit_or_account': True}
    return {'activation': activation, 'included_dates': included, 'unresolved_days': unresolved,
            'accounts': totals, 'unit_status': 'UNIT_UNVERIFIED', 'eligibility': 'ELIGIBILITY_UNPROVEN',
            'scheduler_gap': 'INSTALLATION_PLAN_ONLY_NOT_INSTALLED_BY_THIS_TASK',
            'live_send_available': False, 'broker_connections': 0, 'actual_orders': 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('activate', 'status', 'run-day', 'replay', 'drain'))
    parser.add_argument('--runtime-dir', type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--source-root', type=Path, default=STRATEGY / 'yuanta_intraday_shadow_v01/runtime/runs')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--qualification-cache', type=Path)
    parser.add_argument('--fetch-public', action='store_true', help='public TWSE only; never broker login')
    args = parser.parse_args()
    if args.action == 'activate':
        result = activate(args.runtime_dir)
    elif args.action == 'status':
        result = status(args.runtime_dir)
    elif args.action == 'replay':
        if not args.source or not args.output or not args.output.resolve().is_relative_to((HERE / 'validation').resolve()):
            raise ValueError('SOURCE_AND_FRESH_ISOLATED_VALIDATION_OUTPUT_REQUIRED')
        if args.output.exists():
            raise ValueError('READONLY_REPLAY_MUST_NOT_OVERWRITE_EXISTING_OUTPUT')
        result = process_day(args.source, args.output, cache_dir=args.qualification_cache, fetch_public=args.fetch_public)
    elif args.action == 'run-day':
        if not args.source:
            raise ValueError('SOURCE_REQUIRED')
        result = run_day(args.source, args.runtime_dir, cache_dir=args.qualification_cache, fetch_public=args.fetch_public)
    else:
        activation = load_activation(owned_runtime(args.runtime_dir))
        result = []
        for path in sorted(args.source_root.glob('*/run_manifest.json')):
            source = json.loads(path.read_text())
            if source.get('status') == 'COMPLETE' and is_prospective(source, activation):
                result.append(run_day(path.parent, args.runtime_dir, cache_dir=args.qualification_cache, fetch_public=args.fetch_public))
        if not result:
            result = {'status': 'NO_NEW_COMPLETE_ARCHIVE_AFTER_ACTIVATION', 'no_samples_backfilled': True}
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
