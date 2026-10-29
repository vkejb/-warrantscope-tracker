"""Validate completed local outputs and build a path-redacted private ZIP."""
from __future__ import annotations
import argparse
import csv
from datetime import datetime,timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import unittest
import zipfile

from .protocol import HERE,MODULE,ROOT,R1,dump,verify
from .report import write_report
from ..freeze import sha


def validate(output):
    frozen=verify();tables=json.loads((output/'tables.json').read_text())
    count=0
    for t in tables['trades']:
        for key in ['quantity','requested_quantity','entry_unfilled_quantity','closed_quantity','remaining_quantity']:
            if t[key]<0 or t[key]%1000:raise AssertionError('ILLEGAL_BOARD_LOT')
        if t['quantity']!=t['closed_quantity']+t['remaining_quantity']:raise AssertionError('QUANTITY_CONSERVATION')
        if t['quantity']+t['entry_unfilled_quantity']!=t['requested_quantity']:raise AssertionError('ENTRY_CONSERVATION')
        if t['net_pnl'] is not None and abs(t['gross_pnl']-t['commission']-t['sell_tax']-t['net_pnl'])>0.011:raise AssertionError('NET_COST')
        if t['remaining_quantity'] and t['net_pnl'] is not None:raise AssertionError('UNFINISHED_PNL')
        if t['exit_time'] and t['exit_time']<t['entry_time']:raise AssertionError('EXIT_BEFORE_ENTRY')
        count+=1
    strict=[r for r in tables['experiments'] if r.get('scenario')=='STRICT_EXECUTABLE']
    if not strict or any(r[k] is not None for r in strict for k in ['net_pnl','gross_pnl','max_drawdown_twd','entries','closed_trades']):raise AssertionError('STRICT_FABRICATED')
    if len(tables['round1_short_diagnostics'])!=8:raise AssertionError('EIGHT_DIAGNOSTICS_REQUIRED')
    manifest=json.loads((output/'run_manifest.json').read_text())
    if sha(output/'tables.json')!=manifest['tables_sha256']:raise AssertionError('OUTPUT_TABLE_HASH_CHAIN')
    if sha(output/'valuation_paths.jsonl.gz')!=manifest['valuation_paths_sha256']:raise AssertionError('OUTPUT_CURVE_HASH_CHAIN')
    if any(manifest[k]!=0 for k in ['actual_orders','broker_connections','market_subscriptions']):raise AssertionError('OPERATIONAL_ACTIVITY')
    comparisons=0
    # The eighteenth authored table is a lossless flat projection of the eight
    # original diagnostics, and is checked too (not just exporter row counts).
    expected_components=[{'day':t['day'],'symbol':t['symbol'],'signal_id':t['signal_id'],**f}
                         for t in tables['round1_short_diagnostics'] for f in t.get('fill_components',[])]
    with (output/'diagnostic_fill_components.csv').open(encoding='utf-8-sig',newline='') as s:
        authored_components=list(csv.DictReader(s))
    if len(expected_components)!=len(authored_components):raise AssertionError('DIAGNOSTIC_COMPONENT_ROW_COUNT')
    for expected,actual in zip(expected_components,authored_components):
        for key,value in expected.items():
            text='NA' if value is None else str(value).lower() if isinstance(value,bool) else str(value)
            if isinstance(value,(float,int)) and not isinstance(value,bool):
                if abs(float(actual[key])-value)>1e-8:raise AssertionError('COMPONENT_NUMBER')
            elif actual[key]!=text:raise AssertionError('COMPONENT_EXACT')
            comparisons+=1
    enriched={s['signal_id']:s for s in json.loads((output/'signals_enriched.json').read_text())}
    for name,rows in tables.items():
        with (output/(name+'.csv')).open(encoding='utf-8-sig',newline='') as stream:actual=list(csv.DictReader(stream))
        if len(rows)!=len(actual):raise AssertionError('CSV_ROW_COUNT:'+name)
        for original,authored in zip(rows,actual):
            if name=='signals':
                for key,value in enriched[original['signal_id']]['features'].items():
                    text='NA' if value is None else json.dumps(value,ensure_ascii=False,separators=(',',':')) if isinstance(value,(dict,list)) else str(value).lower() if isinstance(value,bool) else str(value)
                    got=authored['original_'+key]
                    if isinstance(value,(float,int)) and not isinstance(value,bool):
                        if got=='NA' or abs(float(got)-value)>1e-8:raise AssertionError('ORIGINAL_SIGNAL_FEATURE_NUMBER')
                    elif isinstance(value,(dict,list)):
                        if json.loads(got)!=value:raise AssertionError('ORIGINAL_SIGNAL_FEATURE_JSON')
                    elif got!=text:raise AssertionError('ORIGINAL_SIGNAL_FEATURE_EXACT')
                    comparisons+=1
            for key,value in original.items():
                if key=='round2_features':
                    for k,v in value.items():
                        expected='NA' if v is None else json.dumps(v,ensure_ascii=False,separators=(',',':')) if isinstance(v,(dict,list)) else str(v).lower() if isinstance(v,bool) else str(v)
                        got=authored['feature_'+k]
                        if isinstance(v,(dict,list)):
                            if json.loads(got)!=v:raise AssertionError('CSV_NESTED_FEATURE')
                        elif isinstance(v,(float,int)) and not isinstance(v,bool):
                            if abs(float(got)-v)>1e-8:raise AssertionError('CSV_NUMERIC_FEATURE')
                        elif got!=expected:raise AssertionError('CSV_FEATURE_EXACT')
                        comparisons+=1
                    continue
                if name=='data_audit' and key=='per_symbol_coverage':continue
                if name=='round1_short_diagnostics' and key in ['horizons','cause_flags','fill_components']:continue
                expected='NA' if value is None else json.dumps(value,ensure_ascii=False,separators=(',',':')) if isinstance(value,(dict,list)) else str(value).lower() if isinstance(value,bool) else str(value)
                got=authored[key]
                if isinstance(value,(dict,list)):
                    if json.loads(got)!=value:raise AssertionError('CSV_JSON:'+name+key)
                elif isinstance(value,(float,int)) and not isinstance(value,bool):
                    if got=='NA' or abs(float(got)-value)>1e-8:raise AssertionError('CSV_NUMBER:'+name+key)
                elif got!=expected:raise AssertionError('CSV_EXACT:'+name+key)
                comparisons+=1
    # Recompute exact intraday DD independently from saved model marks.
    paths={}
    with gzip.open(output/'valuation_paths.jsonl.gz','rt') as stream:
        for line in stream:
            record=json.loads(line)
            encoded=json.dumps(record['path'],sort_keys=True,separators=(',',':')).encode()
            if hashlib.sha256(encoded).hexdigest()!=record['path_id']:raise AssertionError('VALUATION_PATH_ID')
            if record['path_id'] in paths:raise AssertionError('DUPLICATE_CURVE_PATH')
            paths[record['path_id']]=record['path']
    for entry in tables['valuation_path_inventory']:
        if entry['path_id'] not in paths or len(paths[entry['path_id']])!=entry['points']:raise AssertionError('VALUATION_PATH_INVENTORY')
    if any(t['valuation_path_id'] not in paths for t in tables['trades']):raise AssertionError('TRADE_PATH_REFERENCE')
    keys={tuple(d[k] for k in ['scenario','phase','scope','variant']) for d in tables['daily_results']}
    dd_checks=0
    for key in keys:
        matches=lambda r:tuple(r[k] for k in ['scenario','phase','scope','variant'])==key
        ds=sorted((d for d in tables['daily_results'] if matches(d)),key=lambda d:d['day'])
        ts=[t for t in tables['trades'] if matches(t)]
        peak=float(ds[0]['starting_equity']);dd=0.0
        for d in ds:
            for t in ts:
                if t['day']!=d['day']:continue
                for point in paths[t['valuation_path_id']]:
                    value=d['starting_equity']+point['net'];peak=max(peak,value);dd=max(dd,peak-value)
            if d['equity'] is not None:peak=max(peak,d['equity']);dd=max(dd,peak-d['equity'])
        aggregate=next(r for r in tables['experiments'] if matches(r) and r['statistics_scope']=='FULL_PERIOD')
        if aggregate['max_drawdown_twd'] is not None and abs(dd-aggregate['max_drawdown_twd'])>.011:raise AssertionError('INTRADAY_DD_PATH_RECONCILIATION')
        if aggregate['net_pnl'] is not None and abs(sum(d['net_pnl'] for d in ds)-aggregate['net_pnl'])>.011:raise AssertionError('DAILY_AGGREGATE')
        dd_checks+=1
    user_matches={n:sha(ROOT/n)==h for n,h in frozen['user_modified_file_hashes'].items()}
    if not all(user_matches.values()):raise AssertionError('PREEXISTING_USER_MODIFIED_FILE_CHANGED')
    return {'validated_at':datetime.now(timezone.utc).isoformat(),'trade_rows_checked':count,
        'csv_original_field_comparisons':comparisons,'csv_tables_checked':18,'csv_mismatches':0,'intraday_dd_portfolio_checks':dd_checks,
        'quantity_cost_equity_timestamp_and_source_checks':'PASS','first_round_unchanged':True,
        'preexisting_user_modified_hashes_unchanged':user_matches,'actual_orders':0,
        'broker_connections':0,'market_subscriptions':0}


def package(output):
    # Packaging completed evidence is not permission to rerun on drifted code.
    # Require either the original pins or precisely the separately documented
    # post-run external change; any additional drift blocks the delivery.
    frozen=json.loads((HERE/'freeze.json').read_text())
    drift=json.loads((output/'external_change_receipt.json').read_text())
    changed={n:sha(ROOT/n) if (ROOT/n).is_file() else None
             for n,h in frozen['files'].items() if not (ROOT/n).is_file() or sha(ROOT/n)!=h}
    documented={r['path']:r['observed_sha256'] for r in drift['mismatches']}
    if changed and changed!=documented:raise AssertionError('UNDOCUMENTED_POST_RUN_SOURCE_DRIFT')
    for n,key in [('run_manifest.json','completed_manifest_sha256'),
                  ('validation.json','successful_validation_sha256'),('tables.json','unchanged_tables_sha256')]:
        if sha(output/n)!=drift[key]:raise AssertionError('COMPLETED_EVIDENCE_CHANGED:'+n)
    manifest=json.loads((output/'run_manifest.json').read_text())
    if sha(output/'valuation_paths.jsonl.gz')!=manifest['valuation_paths_sha256']:raise AssertionError('CURVE_CHANGED_BEFORE_PACKAGE')
    archive=HERE/'WarrantScope_cash_short_round2_private.zip'
    if archive.exists():raise FileExistsError('Never overwrite existing delivery')
    files={}
    for p in MODULE.glob('*.py'):files['round1_source/'+p.name]=p
    for n in ['config.json','EXPERIMENT_SPEC.md','IMPLEMENTATION_NOTES.md']:
        files['round1_source/'+n]=MODULE/n
    for p in [*HERE.glob('*.py'),*HERE.glob('*.mjs'),*(HERE/'tests').glob('*.py')]:
        files['round2_source/'+str(p.relative_to(HERE))]=p
    for n in ['config.json','EXPERIMENT_SPEC.md','IMPLEMENTATION_NOTES.md','README.md','DATA_DICTIONARY_TEMPLATE.md','.gitignore']:
        files['round2_source/'+n]=HERE/n
    for n in ['freeze.json','freeze_preliminary_no_performance_20261029T142523.json','precheck.json','PRECHECK.md']:
        files['private_provenance/'+n]=HERE/n
    for n in ['experiments.csv','trades.csv','fills.csv','daily_results.csv','per_symbol_data_audit.csv','DATA_AUDIT_NOTES.md','baseline_audit_notes.json']:
        files['round1_reference/'+n]=R1/n
    # Exact output whitelist: never recursively package raw feeds or account DBs.
    names=['tables.json','signals_enriched.json','run_manifest.json','execution_start.json','precheck.json',
      'reuse_receipts.json','valuation_paths.jsonl.gz','csv_validation.json','RESEARCH_REPORT.md',
      'validation.json','tests.txt','round1_tests.txt','publication_receipt.json','independent_review.json',
      'external_change_receipt.json','authoring_receipt.json']
    for n in names:files['results/'+n]=output/n
    for p in output.glob('*.csv'):files['results/'+p.name]=p
    if any(not p.is_file() for p in files.values()):raise FileNotFoundError([str(p) for p in files.values() if not p.is_file()])
    replacements=[(str(ROOT),'<SOURCE_ROOT>'),(str(ROOT.parent/'-warrantscope-tracker'),'<OTHER_CHECKOUT>'),(str(Path.home()),'<LOCAL_USER_HOME>')]
    payloads={};original_hashes={};redacted=[]
    for name,p in files.items():
        payload=p.read_bytes();original_hashes[name]=hashlib.sha256(payload).hexdigest()
        if name.endswith('.gz'):
            # This is model PnL paths only, not a raw quote feed; no local paths.
            with gzip.open(p,'rt') as stream:
                text=stream.read()
            if '/Users/' in text:raise AssertionError('PRIVATE_PATH_IN_MODEL_CURVE')
        else:
            text=payload.decode('utf-8-sig');before=text
            for old,new in replacements:text=text.replace(old,new)
            if before!=text:redacted.append(name)
            payload=(b'\xef\xbb\xbf' if payload.startswith(b'\xef\xbb\xbf') else b'')+text.encode()
        payloads[name]=payload
    payloads['PATH_REDACTION_AND_ORIGINAL_HASHES.json']=(json.dumps({'original_sha256':original_hashes,'redacted_files':redacted,
       'aliases':['<SOURCE_ROOT>','<OTHER_CHECKOUT>','<LOCAL_USER_HOME>'],
       'note':'Only local paths redacted; numerical results/rules unchanged. Frozen hashes refer to original local bytes. Package hashes below refer to redacted copies. No full raw feed, account DB, credentials or Git included.'},ensure_ascii=False,indent=2)+'\n').encode()
    hashes={n:hashlib.sha256(b).hexdigest() for n,b in payloads.items()}
    payloads['FILE_SHA256SUMS.json']=(json.dumps(hashes,indent=2)+'\n').encode()
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for n,b in payloads.items():z.writestr(n,b)
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:raise AssertionError('ZIP_CRC')
        for n,h in hashes.items():
            if hashlib.sha256(z.read(n)).hexdigest()!=h:raise AssertionError('ZIP_SHA')
    print(json.dumps({'path':str(archive),'bytes':archive.stat().st_size,'sha256':sha(archive),'entries':len(payloads),'redacted_files':len(redacted),'crc_and_hashes':'PASS'}))
    return archive


def deliver(output):
    validation=validate(output);dump(output/'validation.json',validation)
    with (output/'tests.txt').open('w') as s:
        result=unittest.TextTestRunner(stream=s,verbosity=2).run(unittest.TestLoader().discover(str(HERE/'tests')))
    if not result.wasSuccessful():raise AssertionError('ROUND2_TESTS_FAILED')
    with (output/'round1_tests.txt').open('w') as s:
        old=unittest.TextTestRunner(stream=s,verbosity=2).run(unittest.TestLoader().discover(str(MODULE/'tests')))
    if not old.wasSuccessful():raise AssertionError('ROUND1_REGRESSION_FAILED')
    write_report(output)
    print(json.dumps({'validation':validation,'round2_tests':result.testsRun,'round1_tests':old.testsRun}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    parser.add_argument('--package',action='store_true',help='package after publication/independent-review receipts exist')
    args=parser.parse_args();package(args.output) if args.package else deliver(args.output)
