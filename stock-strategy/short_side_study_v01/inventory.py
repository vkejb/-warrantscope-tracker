"""Cheap, reproducible source audit before loading multi-million-row archives."""
from datetime import datetime, timezone
import hashlib
import json
import re
import zipfile
from pathlib import Path

from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC, TAIPEI


def discover_directories(runs_dir, extra_roots=()):
    directories={p.resolve() for p in Path(runs_dir).iterdir() if p.is_dir()}
    for root in extra_roots:
        for path in Path(root).rglob('watchlist.json'):
            if any((path.parent/name).is_file() for name in ('ticks.jsonl','ticks.jsonl.gz')):
                directories.add(path.parent.resolve())
    return sorted(directories)


def audit_sources(runs_dir, extra_roots=()):
    rows = []
    for directory in discover_directories(runs_dir,extra_roots):
        if not directory.is_dir():
            continue
        path = directory / 'run_manifest.json'
        row = {'run_id':directory.name, 'source_directory':str(directory), 'eligible':False, 'reasons':[]}
        try:
            stamp=re.search(r'\d{8}T\d{6}\.\d{6}Z',directory.name).group(0)
            row['session_date']=datetime.strptime(stamp,'%Y%m%dT%H%M%S.%fZ').replace(tzinfo=timezone.utc).astimezone(TAIPEI).strftime('%Y%m%d')
        except (ValueError,AttributeError):
            row['session_date']=directory.name[:8] if directory.name[:8].isdigit() else 'UNKNOWN'
        if not path.is_file():
            row['reasons'].append('RUN_MANIFEST_MISSING')
            rows.append(row)
            continue
        try:
            manifest = json.loads(path.read_text())
            unsigned = {k:v for k,v in manifest.items() if k != 'manifest_hash'}
            if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != manifest.get('manifest_hash'):
                row['reasons'].append('MANIFEST_HASH_MISMATCH')
            start = datetime.fromisoformat(manifest['started_at'].replace('Z','+00:00')).astimezone(TAIPEI)
            end = datetime.fromisoformat(manifest['ended_at'].replace('Z','+00:00')).astimezone(TAIPEI)
            row.update(session_date=end.strftime('%Y%m%d'), source_manifest_hash=manifest.get('manifest_hash'), status=manifest.get('status'))
            row.update(started_at=start.isoformat(),ended_at=end.isoformat(),
                       callback_errors=manifest.get('event_counts',{}).get('callback_errors',0))
            if manifest.get('status') != 'COMPLETE':
                row['reasons'].append('STATUS_NOT_COMPLETE')
            if start.date() != end.date() or start.strftime('%H:%M:%S') > SPEC['entry_start']+':00' or end.strftime('%H:%M:%S') < SPEC['hard_exit_time']+':00':
                row['reasons'].append('SESSION_TIME_COVERAGE_INCOMPLETE')
            if manifest.get('event_counts',{}).get('callback_errors',0):
                row['reasons'].append('CALLBACK_ERRORS')
            artifacts = manifest.get('artifacts',{})
            for prefix in ('ticks','books','market_context_ticks','market_context_books'):
                names = [name for name in artifacts if name in (prefix+'.jsonl',prefix+'.jsonl.gz')]
                if len(names) != 1 or not (directory/names[0]).is_file():
                    row['reasons'].append(prefix.upper()+'_MISSING')
            watchlist = json.loads((directory/'watchlist.json').read_text())
            stocks = watchlist.get('stocks',[])
            row['top30_count']=len(stocks)
            if len(stocks)!=30 or len({s['stock_id'] for s in stocks})!=30:
                row['reasons'].append('TOP30_NOT_UNIQUE_30')
            # Hash every referenced artifact for otherwise admissible sources.
            if not row['reasons']:
                for name,digest in artifacts.items():
                    source = directory/name
                    if not source.is_file() or sha256_file(source)!=digest:
                        row['reasons'].append('ARTIFACT_HASH_MISMATCH:'+name)
            row['eligible'] = not row['reasons']
        except Exception as exc:
            row['reasons'].append('AUDIT_ERROR:'+type(exc).__name__+':'+str(exc))
        rows.append(row)
    # Exactly one account replay per date. Prefer a valid full source, never a
    # previously profitable day. No partial-session stitching across gaps.
    for day in sorted({r.get('session_date') for r in rows}):
        candidates=[r for r in rows if r.get('session_date')==day and r['eligible']]
        for duplicate in candidates[1:]:
            duplicate['eligible']=False
            duplicate['reasons'].append('DUPLICATE_FULL_SESSION_DATE')
    return rows


def daily_inventory(rows):
    output=[]
    for day in sorted({r['session_date'] for r in rows}):
        sources=[r for r in rows if r['session_date']==day]
        included=[r['source_directory'] for r in sources if r['eligible']]
        output.append({'session_date':day,'included':bool(included),'selected_sources':included,
                       'excluded_sources':[{'source':r['source_directory'],'reasons':r['reasons']} for r in sources if not r['eligible']]})
    return output


def audit_zip_catalog(roots):
    catalog=[]
    paths=sorted({p.resolve() for root in roots for p in Path(root).rglob('*.zip')})
    for path in paths:
        try:
            with zipfile.ZipFile(path) as archive:
                names=archive.namelist()
            prefixes=sorted({str(Path(name).parent) for name in names if Path(name).name in ('ticks.jsonl','ticks.jsonl.gz')})
            catalog.append({'path':str(path),'quote_archive_prefixes':prefixes,
                            'status':'QUOTE_ARCHIVE_REQUIRES_AUDIT' if prefixes else 'NO_TICK_ARCHIVE_NOT_INTRADAY_INPUT'})
        except (zipfile.BadZipFile,OSError) as exc:
            catalog.append({'path':str(path),'status':'ZIP_UNREADABLE','error':str(exc)})
    return catalog
