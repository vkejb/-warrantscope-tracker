"""Immutable local protocol/input pins. No network or operational API."""
from __future__ import annotations
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from ..freeze import sha

HERE=Path(__file__).resolve().parent
MODULE=HERE.parent
ROOT=MODULE.parents[1]
R1=MODULE/'results_round1_v2'


def dump(path,value):
    def serial(v):
        if isinstance(v,datetime):return v.isoformat()
        raise TypeError(type(v).__name__)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=serial,allow_nan=False)+'\n',encoding='utf-8')


def code_files():
    # Reporting/packaging authoring is distinct from frozen performance logic.
    names=['__init__.py','protocol.py','features.py','diagnostics.py','run.py','precheck.py']
    return [HERE/n for n in names]+sorted((HERE/'tests').glob('*.py'))


def freeze():
    target=HERE/'freeze.json'
    if target.exists():raise FileExistsError('Round2 freeze exists; never overwrite')
    precheck=json.loads((HERE/'precheck.json').read_text())
    if precheck['hash_failures']:raise ValueError('ROUND1_PRECHECK_FAILED')
    # Never bless changed inputs merely because an earlier precheck passed.
    for row in precheck['code_protocol_and_frozen_input_checks']:
        if not Path(row['path']).is_file() or sha(Path(row['path']))!=row['actual_sha256']:
            raise ValueError('INPUT_CHANGED_SINCE_PRECHECK:'+row['path'])
    for row in precheck['round1_result_artifact_hash_inventory']:
        if not Path(row['path']).is_file() or sha(Path(row['path']))!=row['sha256']:
            raise ValueError('ROUND1_RESULT_CHANGED_SINCE_PRECHECK:'+row['path'])
    from .precheck import source_check
    for row in precheck['source_checks']:
        if not source_check({**row,'sha256':row['expected_sha256'],
                             'expected_content_sha256':row.get('content_sha256')})['match']:
            raise ValueError('SOURCE_CHANGED_SINCE_PRECHECK:'+row['path'])
    protected=json.loads((MODULE/'freeze.json').read_text())['protected_source_hashes']
    paths=[HERE/'config.json',HERE/'EXPERIMENT_SPEC.md',HERE/'IMPLEMENTATION_NOTES.md',HERE/'precheck.json',*code_files()]
    paths+=list(MODULE.glob('*.py'))+[MODULE/'config.json',MODULE/'EXPERIMENT_SPEC.md',MODULE/'IMPLEMENTATION_NOTES.md']
    paths+=sorted(p for p in R1.iterdir() if p.is_file())
    paths+=[ROOT/'stock-strategy'/n for n in protected]
    # Canonical hash evidence is checked by precheck; pin exact existing bytes too.
    paths+=[Path(r['path']) for r in precheck['source_checks']]
    unique=sorted(set(paths),key=str)
    if any(not p.is_file() for p in unique):raise FileNotFoundError('FROZEN_INPUT_MISSING')
    modified=subprocess.check_output(['git','-C',str(ROOT),'diff','--name-only'],text=True).splitlines()
    payload={'version':'CASH_SHORT_ROUND2_FROZEN_V01','frozen_at':datetime.now(timezone.utc).isoformat(),
       'phase':'BEFORE_ROUND2_REAL_PERFORMANCE','source_commit':subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip(),
       'source_root':str(ROOT),'original_results_immutable':True,
       'files':{str(p.relative_to(ROOT)):sha(p) for p in unique},
       'user_modified_file_hashes':{n:sha(ROOT/n) for n in modified if (ROOT/n).is_file()},
       'scope':'SIX_FIXED_ENTRY_FILTERS_SEPARATE_FIXED_AND_ACCOUNT_EXITS_ALL_DATES_SEEN',
       'actual_orders':0,'broker_connections':0,'market_subscriptions':0}
    dump(target,payload);return payload


def verify():
    payload=json.loads((HERE/'freeze.json').read_text())
    failures=[n for n,h in payload['files'].items() if not (ROOT/n).is_file() or sha(ROOT/n)!=h]
    if failures:raise ValueError('FROZEN_ROUND2_INPUT_OR_CODE_CHANGED:'+','.join(failures))
    return payload


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify',action='store_true',help='read-only verify the existing frozen inputs')
    args=parser.parse_args()
    p=verify() if args.verify else freeze()
    print(json.dumps({'frozen_at':p['frozen_at'],'files':len(p['files']),'actual_orders':0}))
