"""Restore actual pinned source bodies to a new copy; never edit live checkout.

Copies existing original bytes or a matching Git blob, not inferred source from
hashes. No strategy runner, SDK or operational entrypoint is imported.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
MODULE = HERE.parent
R2 = MODULE / 'round2'

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def dump(path,value):
    path=Path(path)
    if path.exists():raise FileExistsError('Preserved artifact; choose a new output')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')

def git(*args):
    return subprocess.check_output(['git','-C',str(ROOT),*args],text=True).strip()

def snapshot(output):
    frozen=json.loads((R2/'freeze.json').read_text())
    dirty=git('diff','--name-only').splitlines()
    staged=git('diff','--cached','--name-only').splitlines()
    payload={'captured_at':datetime.now(timezone.utc).isoformat(),'HEAD':git('rev-parse','HEAD'),
      'origin_main':git('rev-parse','origin/main'),'dirty_tracked':dirty,'staged':staged,
      'dirty_file_sha256':{n:digest(ROOT/n) for n in dirty if (ROOT/n).is_file()},
      'round2_source_commit':frozen['source_commit'],'round2_freeze_sha256':digest(R2/'freeze.json'),
      'round2_result_manifest_sha256':digest(R2/'results_round2_v1/run_manifest.json'),
      'round1_result_manifest_sha256':digest(MODULE/'results_round1_v2/run_manifest.json'),
      'protected_old_artifact_hashes':{str(p.relative_to(ROOT)):digest(p)
        for folder in (MODULE/'results_round1_v2',R2/'results_round2_v1')
        for p in sorted(folder.iterdir()) if p.is_file()},
      'old_specs_and_freezes':{str(p.relative_to(ROOT)):digest(p) for p in
        [MODULE/'config.json',MODULE/'EXPERIMENT_SPEC.md',MODULE/'freeze.json',
         R2/'config.json',R2/'EXPERIMENT_SPEC.md',R2/'freeze.json',
         R2/'freeze_preliminary_no_performance_20261029T142523.json']},
      'SHADOW_ONLY':True,'NO_LIVE_ORDER':True,'actual_orders':0,
      'production_mutation_authorized':False}
    dump(output,payload);return payload

def verify_copy(target):
    target=Path(target).resolve()
    frozen=json.loads((R2/'freeze.json').read_text())
    rows=[{'path':n,'expected_sha256':h,'actual_sha256':digest(target/n) if (target/n).is_file() else None}
          for n,h in frozen['files'].items()]
    return {'files':rows,'all_original_pins_match':all(r['actual_sha256']==r['expected_sha256'] for r in rows),
            'pinned_files':len(rows),'isolated_root':str(target)}

def restore(target,receipt,allow_existing_archive=False):
    target=Path(target).resolve()
    if target==ROOT or ROOT in target.parents:raise ValueError('Isolation must be outside active repository')
    if target.exists() and not allow_existing_archive:raise FileExistsError('Fresh isolated directory required')
    target.mkdir(parents=True,exist_ok=True)
    frozen=json.loads((R2/'freeze.json').read_text());revision=frozen['source_commit']
    records=[]
    for name,expected in frozen['files'].items():
        source=ROOT/name;dest=target/name;dest.parent.mkdir(parents=True,exist_ok=True)
        if dest.is_file() and digest(dest)==expected:
            method='EXISTING_ISOLATED_BYTES_MATCH'
        elif source.is_file() and digest(source)==expected:
            shutil.copy2(source,dest);method='ORIGINAL_LOCAL_BYTES_COPIED'
        else:
            blob=subprocess.check_output(['git','-C',str(ROOT),'show',revision+':'+name])
            if hashlib.sha256(blob).hexdigest()!=expected:raise ValueError('ORIGINAL_BODY_NOT_FOUND:'+name)
            # Exact Git restoration, not newly authored or inferred program text.
            dest.write_bytes(blob);method='ORIGINAL_GIT_BLOB_BODY_RESTORED'
        if digest(dest)!=expected:raise ValueError('COPY_HASH_MISMATCH:'+name)
        records.append({'path':name,'sha256':expected,'restoration':method,'body_present':True})
    # The freeze is not self-pinned; preserve its unchanged bytes separately.
    freeze_dest=target/(R2/'freeze.json').relative_to(ROOT)
    shutil.copy2(R2/'freeze.json',freeze_dest)
    verification=verify_copy(target)
    if not verification['all_original_pins_match']:raise ValueError('ISOLATION_INCOMPLETE')
    payload={'created_at':datetime.now(timezone.utc).isoformat(),'baseline_commit':revision,
       'freeze_sha256':digest(R2/'freeze.json'),'source_bodies':records,**verification,
       'restored_original_source_bodies':True,'historical_sdk_process_environment_reconstructed':False,
       'environment_limitation':'Research source/input copies verified. Historical SDK DLL/process/OS/network state not reconstructed; offline research does not instantiate SDK.',
       'research_performance_rerun':False,'actual_orders':0}
    dump(receipt,payload);return payload

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot',type=Path);p.add_argument('--target',type=Path);p.add_argument('--receipt',type=Path)
    p.add_argument('--hydrate-existing-archive',action='store_true')
    a=p.parse_args()
    if a.snapshot:result=snapshot(a.snapshot)
    elif a.target and a.receipt:result=restore(a.target,a.receipt,a.hydrate_existing_archive)
    else:p.error('Choose snapshot, or target and receipt')
    print(json.dumps({k:v for k,v in result.items() if k not in ('files','source_bodies','protected_old_artifact_hashes','old_specs_and_freezes')},ensure_ascii=False))
