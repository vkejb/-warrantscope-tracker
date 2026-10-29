"""Local-only validation and sanitized delivery ZIP; no upload or broker access."""
from __future__ import annotations
import argparse
import csv
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import unittest
import zipfile

from .freeze import MODULE,sha,verify_freeze
from .report import write_report


def validate(output):
    tables=json.loads((output/'tables.json').read_text())
    checks=[]
    for trade in tables['trades']:
        for key in ['requested_quantity','quantity','closed_quantity','remaining_quantity','entry_unfilled_quantity']:
            if trade[key]%1000:raise AssertionError('ILLEGAL_BOARD_LOT:'+key)
        if trade['closed_quantity']+trade['remaining_quantity']!=trade['quantity']:raise AssertionError('QUANTITY_NOT_CONSERVED')
        if trade['net_pnl'] is not None:
            if abs(trade['gross_pnl']-trade['commission']-trade['sell_tax']-trade['net_pnl'])>0.011:
                raise AssertionError('TRADE_COST_RECONCILIATION')
        if trade['remaining_quantity'] and trade['net_pnl'] is not None:raise AssertionError('OPEN_PNL_FABRICATED')
        if trade['exit_time'] and trade['exit_time']<trade['entry_time']:raise AssertionError('EXIT_BEFORE_ENTRY')
        if trade['exit_trigger_time'] and trade['exit_time'] and trade['exit_time']<trade['exit_trigger_time']:raise AssertionError('EXIT_BEFORE_TRIGGER')
        if trade['entry_time']<trade['signal_time']:raise AssertionError('ENTRY_BEFORE_SIGNAL')
        checks.append(trade['signal_id'])
    grouped={}
    for row in tables['daily_results']:
        key=(row['experiment'],row['scenario']);grouped.setdefault(key,[]).append(row)
    for key,days in grouped.items():
        previous=190000
        for row in sorted(days,key=lambda x:x['day']):
            if abs(row['starting_equity']-previous)>0.011:raise AssertionError('EQUITY_ROLL_FORWARD')
            if row['account_complete'] and row['net_pnl'] is not None:
                if abs(row['equity']-row['starting_equity']-row['net_pnl'])>0.011:raise AssertionError('DAILY_PNL_RECONCILIATION')
                previous=row['equity']
    for path in output.glob('*.csv'):
        with path.open(encoding='utf-8-sig',newline='') as f:
            reader=csv.DictReader(f);rows=list(reader)
            if path.stem in tables and len(rows)!=len(tables[path.stem]):raise AssertionError('CSV_ROW_COUNT:'+path.name)
    manifest=json.loads((output/'run_manifest.json').read_text())
    if manifest['actual_orders'] or manifest['broker_connections'] or manifest['market_subscriptions']:
        raise AssertionError('NONZERO_OPERATIONAL_ACTIVITY')
    verify_freeze(Path(manifest['source_root']),MODULE/'freeze.json')
    return {'trade_rows_checked':len(checks),'unique_signal_ids':len(set(checks)),
            'portfolio_scenarios':len(grouped),'quantity_cost_equity_causality_checks':'PASS',
            'actual_orders':0,'broker_connections':0,'market_subscriptions':0,
            'completed_at':datetime.now(timezone.utc).isoformat()}


def deliver(output):
    for required in ['DATA_AUDIT.csv','experiments.csv','trades.csv','signals.csv','daily_results.csv','execution_issues.csv']:
        if not (output/required).is_file():raise FileNotFoundError(required+'; run export_tables.mjs first')
    validation=validate(output)
    (output/'validation.json').write_text(json.dumps(validation,ensure_ascii=False,indent=2)+'\n')
    suite=unittest.defaultTestLoader.discover(str(MODULE/'tests'))
    with (output/'tests.txt').open('w',encoding='utf-8') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    if not result.wasSuccessful():raise AssertionError('SYNTHETIC_TESTS_FAILED')
    write_report(output)
    archive=MODULE/'cash_short_intraday_research_v01_round1_delivery.zip'
    if archive.exists():raise FileExistsError('Delivery already exists; do not overwrite it.')
    source_files=[p for p in MODULE.glob('*.py')]+list((MODULE/'tests').glob('*.py'))
    source_files += [MODULE/n for n in ['config.json','EXPERIMENT_SPEC.md','IMPLEMENTATION_NOTES.md','README.md','export_tables.mjs','freeze.json']]
    result_files=list(output.glob('*.csv'))+[output/n for n in ['RESEARCH_REPORT.md','run_manifest.json','implementation_freeze.json',
              'eligibility_input_freeze.json','selection_before_holdout.json','event_cases.json','signal_counts.json','state_transitions.json',
              'tests.txt','validation.json','csv_validation.json']]
    files=source_files+result_files
    # Whitelist packaging only; never traverse runtime/source archives, Git or account files.
    for path in files:
        if not path.is_file():raise FileNotFoundError(path)
        if path.suffix.lower() in {'.env','.db','.sqlite','.sqlite3','.pem','.key','.pfx'}:raise ValueError('FORBIDDEN_DELIVERY_FILE')
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as package:
        hashes={}
        for path in files:
            name='source/'+str(path.relative_to(MODULE)) if path in source_files else 'results/'+path.name
            package.write(path,name);hashes[name]=sha(path)
        package.writestr('FILE_SHA256SUMS.json',json.dumps(hashes,ensure_ascii=False,indent=2)+'\n')
    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None:raise AssertionError('ZIP_CRC_FAILED')
        expected=json.loads(package.read('FILE_SHA256SUMS.json'))
        for name,value in expected.items():
            if hashlib.sha256(package.read(name)).hexdigest()!=value:raise AssertionError('ZIP_SHA_FAILED')
    print(json.dumps({'delivery':str(archive),'bytes':archive.stat().st_size,'sha256':sha(archive),
                      'tests':result.testsRun,'validation':validation},ensure_ascii=False))
    return archive


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    deliver(parser.parse_args().output.resolve())
