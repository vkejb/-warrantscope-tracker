"""Validate existing audits and produce a private, explicitly whitelisted ZIP."""
from __future__ import annotations
import argparse
import csv
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile
from .isolation import HERE,ROOT,MODULE,R2,digest,dump
from .forward import verify_profile
from .assemble import unchanged

def load(path):return json.loads(Path(path).read_text())

def validate(output,isolated):
    snapshot=load(output/'session_snapshot.json');preserved=unchanged(snapshot)
    profile,iso=verify_profile(HERE/'forward_freeze.json',isolated)
    tables=load(output/'tables.json');manifest=load(output/'run_manifest.json')
    if digest(output/'tables.json')!=manifest['tables_sha256']:raise ValueError('TABLES_CHANGED')
    for n,h in manifest['input_audit_hashes'].items():
        if digest(output/n)!=h:raise ValueError('INPUT_AUDIT_CHANGED:'+n)
    cells=0
    for name,rows in tables.items():
        with (output/(name+'.csv')).open(encoding='utf-8-sig',newline='') as s:authored=list(csv.DictReader(s))
        if len(rows)!=len(authored):raise ValueError('CSV_ROW_COUNT:'+name)
        for expected,got in zip(rows,authored):
            for k,v in expected.items():
                actual=got[k]
                if isinstance(v,(dict,list)):
                    if json.loads(actual)!=v:raise ValueError('CSV_JSON:'+name+':'+k)
                elif isinstance(v,(float,int)) and not isinstance(v,bool):
                    if actual=='NA' or float(actual)!=float(v):raise ValueError('CSV_NUMBER:'+name+':'+k)
                else:
                    text='NA' if v is None else str(v).lower() if isinstance(v,bool) else str(v)
                    if actual!=text:raise ValueError('CSV_TEXT:'+name+':'+k)
                cells+=1
    for g in tables['account_gap_summary']:
        bridge=sum(r['account_minus_fixed_increment'] for r in tables['account_gap_bridge']
                   if r['scenario']==g['scenario'] and r['variant']==g['variant'])
        if bridge!=g['account_minus_fixed_increment'] or g['unexplained_residual']!=0:raise ValueError('GAP_BRIDGE')
    for d in tables['account_daily']:
        if d['net_pnl'] is not None and d['equity']!=d['starting_equity']+d['net_pnl']:raise ValueError('DAILY_EQUITY')
    if any(r['strict_executable'] is not None or r['strict_pass'] for r in tables['strict_gate_status']):raise ValueError('STRICT_PROMOTED')
    if tables['forward_status'][0]['new_qualifying_date_count']!=0:raise ValueError('UNEXPECTED_UNSEEN_PERFORMANCE')
    if manifest['strategy_replay_count']!=0 or any(manifest[k]!=0 for k in ('actual_orders','broker_connections','market_subscriptions')):raise ValueError('OPERATIONAL_OR_REPLAY_ACTIVITY')
    # Current operational source must still equal initial committed HEAD, not the
    # historical replay version. Never reset it to satisfy an old freeze.
    protected='stock-strategy/yuanta_live_runtime_v01/strategy.py'
    original=subprocess.check_output(['git','-C',str(ROOT),'show',snapshot['HEAD']+':'+protected])
    if hashlib.sha256(original).hexdigest()!=digest(ROOT/protected):raise ValueError('CURRENT_FORMAL_LONG_CHANGED_DURING_TASK')
    return {'validated_at':datetime.now(timezone.utc).isoformat(),'csv_tables':len(tables),
      'exact_original_cells_checked':cells,'csv_mismatches':0,'gap_groups_reconciled':len(tables['account_gap_summary']),
      'all_gap_residuals_zero':True,'old_artifact_and_dirty_hash_checks':preserved,
      'original_pinned_bodies':len(profile['original_pins']),'current_LONG_preserved':True,
      'first_second_round_unchanged':True,'forward_profile_hash':profile['profile_hash'],
      'actual_orders':0,'broker_connections':0,'market_subscriptions':0}

def tests(output,isolated):
    groups=[('followup_tests.txt',ROOT/'stock-strategy','cash_short_intraday_research_v01/evidence_followup_v01/tests'),
            ('round1_isolated_tests.txt',Path(isolated)/'stock-strategy','cash_short_intraday_research_v01/tests'),
            ('round2_isolated_tests.txt',Path(isolated)/'stock-strategy','cash_short_intraday_research_v01/round2/tests')]
    results=[]
    for name,cwd,start in groups:
        p=subprocess.run([sys.executable,'-B','-m','unittest','discover','-s',start,'-v'],cwd=cwd,capture_output=True,text=True)
        text=p.stdout+p.stderr
        with (output/name).open('x') as stream:stream.write(text)
        counts=re.findall(r'Ran (\d+) tests?',text)
        if p.returncode or not counts:raise ValueError('TESTS_FAILED:'+name)
        results.append({'log':name,'tests':int(counts[-1]),'exit_code':p.returncode,'sha256':digest(output/name)})
    dump(output/'test_results.json',results);return results

def package(output,isolated):
    validation=validate(output,isolated)
    archive=HERE/'WarrantScope_short_evidence_followup_v01_private.zip'
    if archive.exists():raise FileExistsError('Never overwrite a completed ZIP')
    files={}
    # No recursive runtime, home, account DB, raw quote stream or Git packaging.
    for p in [*HERE.glob('*.py'),*HERE.glob('*.mjs'),*(HERE/'tests').glob('*.py')]:
        files['source/'+str(p.relative_to(HERE))]=p
    for n in ['.gitignore','README.md','FORWARD_SPEC.md','forward_config.json']:
        files['source/'+n]=HERE/n
    files['private_provenance/forward_freeze.json']=HERE/'forward_freeze.json'
    for n in ['tables.json','evidence_tables.json','eligibility_tables.json','account_tables.json',
      'run_manifest.json','session_snapshot.json','isolation_receipt.json','persistent_isolation_receipt.json','forward_review.json',
      'forward_status.json','csv_validation.json','validation.json','test_results.json',
      'followup_tests.txt','round1_isolated_tests.txt','round2_isolated_tests.txt',
      'FOLLOWUP_REPORT.md','EVIDENCE_AUDIT.md','HISTORICAL_ELIGIBILITY.md','ACCOUNT_DIVERGENCE_REPORT.md',
      'publication_receipt.json','independent_delivery_review.json','authoring_receipt.json']:
        files['results/'+n]=output/n
    for p in output.glob('*.csv'):files['results/'+p.name]=p
    for p in (output/'authoring_history').iterdir():
        if p.is_file():files['authoring_history/'+p.name]=p
    files['original_reference/round2_original_private.zip']=R2/'WarrantScope_cash_short_round2_private.zip'
    for n in ['freeze.json','config.json','EXPERIMENT_SPEC.md']:
        files['original_reference/round1_'+n]=MODULE/n
        files['original_reference/round2_'+n]=R2/n
    pinned=load(HERE/'forward_freeze.json')['original_pins']
    for n in pinned:
        if n.endswith('.py'):
            files['original_source_bodies/'+n]=Path(isolated)/n
    if any(not p.is_file() for p in files.values()):raise FileNotFoundError([n for n,p in files.items() if not p.is_file()])
    replacements=[(str(ROOT),'<SOURCE_ROOT>'),(str(Path(isolated).resolve()),'<PINNED_COPY>'),
                  (load(output/'isolation_receipt.json')['isolated_root'],'<INITIAL_PINNED_COPY>'),
                  (str(Path.home()),'<LOCAL_USER_HOME>'),(Path.home().name,'<LOCAL_USER>')]
    payloads={};original_hashes={};redacted=[]
    for name,p in files.items():
        raw=p.read_bytes();original_hashes[name]=hashlib.sha256(raw).hexdigest()
        if name.endswith('.zip'):payloads[name]=raw;continue
        text=raw.decode('utf-8-sig');before=text
        for old,new in replacements:text=text.replace(old,new)
        if text!=before:redacted.append(name)
        payloads[name]=(b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'')+text.encode()
    payloads['PATH_REDACTION_AND_ORIGINAL_HASHES.json']=(json.dumps({'original_sha256':original_hashes,
      'redacted_files':redacted,'note':'Only local identities/paths masked. Original frozen hashes apply to local original bytes; package checksums apply to these masked copies. Historical data bodies are not all included; the isolated local copy has all pinned bodies.'},indent=2)+'\n').encode()
    checks={n:hashlib.sha256(b).hexdigest() for n,b in payloads.items()}
    payloads['FILE_SHA256SUMS.json']=(json.dumps(checks,indent=2)+'\n').encode()
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for n,b in payloads.items():z.writestr(n,b)
    with zipfile.ZipFile(archive) as z:
        if z.testzip():raise ValueError('ZIP_CRC')
        if any(hashlib.sha256(z.read(n)).hexdigest()!=h for n,h in checks.items()):raise ValueError('ZIP_SHA')
    return {'zip':str(archive),'bytes':archive.stat().st_size,'entries':len(payloads),'sha256':digest(archive),'crc_sha':'PASS'}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--isolated-root',type=Path,required=True);p.add_argument('--package',action='store_true')
    a=p.parse_args()
    if a.package:result=package(a.output,a.isolated_root)
    else:
        result=validate(a.output,a.isolated_root);result['test_suites']=tests(a.output,a.isolated_root)
        dump(a.output/'validation.json',result)
    print(json.dumps(result,ensure_ascii=False))
