"""Freeze protocol and read-only source provenance BEFORE calculating performance."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

MODULE = Path(__file__).resolve().parent


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def freeze(root: Path, target: Path) -> dict:
    if target.exists():
        raise FileExistsError('Freeze exists. Do not overwrite a pre-performance protocol.')
    study = root / 'stock-strategy'
    protected = [
        'yuanta_live_runtime_v01/strategy.py', 'yuanta_live_runtime_v01/main.py',
        'yuanta_live_runtime_v01/risk_manager.py', 'paper_shadow_v01/runner.py',
        'yuanta_intraday_shadow_v01/direction_follow_backtest.py',
        'yuanta_intraday_shadow_v01/collector_main.py',
        'yuanta_intraday_shadow_v01/auto_runner.py',
    ]
    changed = git(root, 'diff', '--name-only').splitlines()
    payload = {
        'frozen_at': datetime.now(timezone.utc).isoformat(),
        'phase': 'BEFORE_PERFORMANCE', 'mode': 'SHADOW_ONLY',
        'NO_LIVE_ORDER': True, 'actual_orders': 0,
        'root': str(root), 'commit': git(root, 'rev-parse', 'HEAD'),
        'branch': git(root, 'branch', '--show-current'),
        'git_status': git(root, 'status', '--porcelain=v1'),
        'existing_modified_file_hashes': {n: sha(root / n) for n in changed if (root/n).is_file()},
        'protocol_hashes': {n: sha(MODULE / n) for n in ['config.json', 'EXPERIMENT_SPEC.md']},
        'protected_source_hashes': {n: sha(study / n) for n in protected if (study/n).is_file()},
        'source_manifest_hashes': {str(p.relative_to(root)): sha(p) for p in sorted(
            (study / 'yuanta_intraday_shadow_v01/runtime/runs').glob('*/run_manifest.json'))},
        'selection_data_not_yet_evaluated': True,
        'historical_holdout_not_prospective': True,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return payload


def verify_freeze(root: Path, target: Path) -> dict:
    payload = json.loads(target.read_text(encoding='utf-8'))
    for name, expected in payload['protocol_hashes'].items():
        if sha(MODULE / name) != expected:
            raise ValueError('FROZEN_PROTOCOL_CHANGED: ' + name)
    for name, expected in payload['protected_source_hashes'].items():
        if sha(root / 'stock-strategy' / name) != expected:
            raise ValueError('PROTECTED_SOURCE_CHANGED: ' + name)
    for name, expected in payload['source_manifest_hashes'].items():
        if sha(root / name) != expected:
            raise ValueError('FROZEN_SOURCE_MANIFEST_CHANGED: ' + name)
    return payload


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=MODULE.parents[1])
    parser.add_argument('--output', type=Path, default=MODULE / 'freeze.json')
    args = parser.parse_args()
    payload = freeze(args.root.resolve(), args.output.resolve())
    print(json.dumps({'frozen_at': payload['frozen_at'], 'protocol_hashes': payload['protocol_hashes'],
                      'source_manifests': len(payload['source_manifest_hashes']), 'actual_orders': 0}))
