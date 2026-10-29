// Exact typed CSV export of completed audits, never a strategy calculation.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {Workbook} from '@oai/artifact-tool';
if(!process.argv[2])throw new Error('Usage: node export_tables.mjs <completed-followup-output>');
const output=path.resolve(process.argv[2]);
const bytes=await fs.readFile(path.join(output,'tables.json'));
const tables=JSON.parse(bytes),only=process.argv[3]||null;
if(only&&!Object.hasOwn(tables,only))throw new Error('Unknown table');
const prior=only?JSON.parse(await fs.readFile(process.argv[4],'utf8')):null;
const checks=prior?prior.checks.filter(c=>c.table!==only):[];
const encode=v=>{const s=String(v);return /[",\r\n]/.test(s)?'"'+s.replaceAll('"','""')+'"':s;};
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
for(const [name,rows] of Object.entries(tables)){
 if(only&&name!==only)continue;
 const target=path.join(output,name+'.csv');
 try{await fs.stat(target);throw new Error('Never overwrite '+name);}catch(error){if(error.code!=='ENOENT')throw error;}
 const columns=[...new Set(rows.flatMap(r=>Object.keys(r)))];if(!columns.length)columns.push('status');
 const matrix=[columns,...rows.map(r=>columns.map(k=>r[k]===undefined||r[k]===null?'NA':typeof r[k]==='object'?JSON.stringify(r[k]):r[k]))];
 const prefix='CSV_TEXT:';const authored=matrix.map(row=>row.map(v=>typeof v==='string'?prefix+v:v));
 const wb=Workbook.create(),sheet=wb.worksheets.add(name.slice(0,31));
 const range=sheet.getRangeByIndexes(0,0,matrix.length,columns.length);range.values=authored;
 sheet.showGridLines=false;sheet.freezePanes.freezeRows(1);wb.recalculate();
 const got=range.values;
 for(let i=0;i<authored.length;i++)for(let j=0;j<columns.length;j++)if(got[i][j]!==authored[i][j])throw new Error(`Roundtrip mismatch ${name}/${i}/${j}`);
 const exact=got.map(row=>row.map(v=>typeof v==='string'&&v.startsWith(prefix)?v.slice(prefix.length):v));
 const inspect=await wb.inspect({kind:'table',range:`${sheet.name}!A1:F3`,include:'values',maxChars:1000,tableMaxRows:3,tableMaxCols:6});
 await fs.writeFile(target,'\uFEFF'+exact.map(row=>row.map(encode).join(',')).join('\r\n')+'\r\n',{flag:'wx'});
 checks.push({table:name,rows:rows.length,columns:columns.length,typed_roundtrip:'PASS',inspection:inspect.ndjson});
 console.log(name,rows.length,columns.length);
}
await fs.writeFile(path.join(output,'csv_validation.json'),JSON.stringify({engine:'@oai/artifact-tool',checks,
 source_tables_sha256:sha(bytes),exporter_sha256:sha(await fs.readFile(new URL(import.meta.url))),
 authoring_history:only?'One reference table gained original source-row/split labels; prior draft preserved, no numerical or performance changes.':null,
 exact_timestamp_text:true,missing:'NA_NOT_ZERO',performance_recalculated:false},null,2)+'\n',{flag:'wx'});
