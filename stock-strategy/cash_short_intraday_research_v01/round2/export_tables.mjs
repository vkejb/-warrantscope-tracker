// Exact typed analytical CSV authoring; no strategy or performance recalculation.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {Workbook} from '@oai/artifact-tool';

if(!process.argv[2])throw new Error('Usage: node export_tables.mjs <completed-round2-output>');
const output=path.resolve(process.argv[2]);
const tablesBytes=await fs.readFile(path.join(output,'tables.json'));
const tables=JSON.parse(tablesBytes);
const cfg=JSON.parse(await fs.readFile(new URL('./config.json',import.meta.url),'utf8'));
const originalCfg=JSON.parse(await fs.readFile(new URL('../config.json',import.meta.url),'utf8'));
const signalBytes=await fs.readFile(path.join(output,'signals_enriched.json'));
const originalSignals=new Map(JSON.parse(signalBytes).map(s=>[s.signal_id,s]));
const checksum=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const minimum={
 experiments:['phase','scope','variant','scenario','statistics_scope','split','effective_days','trade_days','original_s1_candidate_count','evaluable_signal_count','unavailable_signal_count','condition_rejected_count','entries','closed_trades','gross_pnl','net_pnl','max_drawdown_twd','incremental_net_vs_R0','incremental_vs_cash'],
 trades:['phase','scope','variant','scenario','day','symbol','signal_id','signal_time','entry_time','entry_price','quantity','exit_time','exit_price','gross_pnl','commission','sell_tax','net_pnl','net_R','initial_price_R','mfe_net_twd','mae_net_twd','exit_reason','remaining_quantity','valuation_path_id'],
 signals:['phase','scope','variant','scenario','day','symbol','signal_id','time','scope_included','evaluable','pass','status','reasons'],
 daily_results:['phase','scope','variant','scenario','day','starting_equity','net_pnl','equity','end_quantity','account_complete','running_max_drawdown_twd'],
 execution_issues:['phase','scope','variant','scenario','day','symbol','signal_id','time','code'],
 fills:['phase','scope','variant','scenario','day','symbol','signal_id','fill_phase','time','price','quantity','book_sequence'],
 evaluation_mask:['day','symbol','signal_id','time','common_evaluable','unavailable_by_variant'],
 market_decision_mask:['day','time','market_available','long_market_evaluable','unavailable_reasons'],
 blocked_pairs:['scenario','variant','day','symbol','signal_id','evaluable','decision','reason','original_outcome','round1_R0_original_net_pnl'],
 paired_exits:['scenario','exit_model','day','symbol','signal_id','quantity','entry_fill_parity','e0_net_pnl','net_pnl','incremental_vs_E0','remaining_quantity'],
 round1_short_diagnostics:['day','symbol','signal_id','signal_time','entry_time','entry_price','quantity','exit_time','exit_price','gross_pnl','commission','sell_tax','net_pnl','horizon_1m_gross_quote_mark_twd','horizon_3m_gross_quote_mark_twd','horizon_5m_gross_quote_mark_twd'],
 portfolio_dispositions:['phase','scope','variant','scenario','day','symbol','signal_id','time','reason'],
 valuation_path_inventory:['path_id','day','symbol','signal_id','scenario','exit_model','quantity','points'],
 diagnostic_fill_components:['day','symbol','signal_id','phase','time','quantity','fill_price','observed_level_price','known_extra_tick_penalty_twd','status'],
 grouped_diagnostics:['phase','scope','variant','scenario','group_by','group','entries','completed','net_pnl','wins','losses'],
 round1_references:['experiment','scenario','study_status','data_splits','entries','closed_trades','net_pnl'],
 data_audit:['day','evaluation_grade','split','canonical_run_id','candidate_count','exclusion_reasons'],
 round1_cd_comparison:['scenario','C_count','D_count','identical_full_trade_rows_except_experiment','same_signal_ids','C_only_signal_ids','D_only_signal_ids'],
};
tables.diagnostic_fill_components=tables.round1_short_diagnostics.flatMap(t=>(t.fill_components||[]).map(f=>({day:t.day,symbol:t.symbol,signal_id:t.signal_id,...f})));
const encode=v=>{
 const s=typeof v==='object'?JSON.stringify(v):String(v);
 return /[",\r\n]/.test(s)?'"'+s.replaceAll('"','""')+'"':s;
};
const checks=[];
for(const [name,sourceRows] of Object.entries(tables)){
 const rows=sourceRows.map(original=>{
  const r={...original};
  if(name==='signals'){
   const old=originalSignals.get(r.signal_id);
   if(!old)throw new Error('Missing original causal signal '+r.signal_id);
   for(const [key,value] of Object.entries(old.features||{}))r['original_'+key]=value;
  }
  if(r.round2_features){for(const [key,value] of Object.entries(r.round2_features))r['feature_'+key]=value;delete r.round2_features;}
  if(name==='experiments'){
   const scenario=originalCfg.stress_scenarios.find(s=>s.name===r.scenario);
   Object.assign(r,{config_version:cfg.version,capital_twd:cfg.capital_twd,maximum_positions:cfg.maximum_positions,
    maximum_trades_per_day:cfg.maximum_trades_per_day,risk_budget_twd:cfg.risk_per_trade_twd,
    rs_300_threshold:r.variant==='R2'||r.variant==='R5'?cfg.rs_300_max:null,
    vwap_slope_seconds:r.variant==='R3'?cfg.vwap_slope_seconds:null,
    entry_distance_threshold:r.variant==='R4'?cfg.maximum_entry_distance_R4:originalCfg.maximum_entry_distance_fraction,
    latency_seconds:scenario?.latency_seconds??null,extra_slippage_ticks:scenario?.extra_slippage_ticks??null,
    depth_unit_multiplier:scenario?.depth_unit_multiplier??null,depth_participation:originalCfg.depth_participation,
    commission_rate:originalCfg.commission_rate,minimum_commission:originalCfg.minimum_commission,
    sell_tax_rate:originalCfg.sell_tax_rate,evidence_status:r.scenario==='STRICT_EXECUTABLE'?'UNAVAILABLE_NOT_ZERO':'CONDITIONAL_UNVERIFIED_NOT_CERTIFIED_EXECUTABLE',
    all_dates_previously_seen:true});
  }
  if(name==='round1_short_diagnostics'){
   // Expanded horizon/flag columns already retain all scalar observations.
   delete r.horizons;delete r.cause_flags;delete r.fill_components;
   r.fill_components_reference='diagnostic_fill_components.csv';
  }
  if(name==='data_audit'&&r.per_symbol_coverage)r.per_symbol_coverage='SEE_ROUND1_REFERENCE_PER_SYMBOL_DATA_AUDIT';
  return r;
 });
 const columns=[...new Set([...(minimum[name]||[]),...rows.flatMap(r=>Object.keys(r))])];
 if(!columns.length)columns.push('status');
 const matrix=[columns,...rows.map(r=>columns.map(k=>r[k]===undefined||r[k]===null?'NA':typeof r[k]==='object'?JSON.stringify(r[k]):r[k]))];
 const prefix='CSV_TEXT:';
 const authored=matrix.map(row=>row.map(v=>typeof v==='string'?prefix+v:v));
 const wb=Workbook.create();const sheet=wb.worksheets.add(name.slice(0,31));
 const range=sheet.getRangeByIndexes(0,0,matrix.length,columns.length);
 range.values=authored;
 sheet.getRangeByIndexes(0,0,1,columns.length).format={fill:'#243746',font:{name:'Arial',bold:true,color:'#FFFFFF'}};
 sheet.showGridLines=false;sheet.freezePanes.freezeRows(1);wb.recalculate();
 const values=range.values;
 for(let i=0;i<authored.length;i++)for(let j=0;j<columns.length;j++)if(values[i][j]!==authored[i][j])throw new Error(`Typed table mismatch ${name}/${i}/${j}`);
 const exact=values.map(row=>row.map(v=>typeof v==='string'&&v.startsWith(prefix)?v.slice(prefix.length):v));
 const inspection=await wb.inspect({kind:'table',range:`${sheet.name}!A1:F3`,include:'values',tableMaxRows:3,tableMaxCols:6,maxChars:1200});
 await fs.writeFile(path.join(output,name+'.csv'),'\uFEFF'+exact.map(row=>row.map(encode).join(',')).join('\r\n')+'\r\n');
 checks.push({table:name,rows:rows.length,columns:columns.length,exact_typed_roundtrip:'PASS',inspection:inspection.ndjson});
 console.log(name,rows.length,'rows',columns.length,'columns');
}
await fs.writeFile(path.join(output,'csv_validation.json'),JSON.stringify({authoring_engine:'@oai/artifact-tool',checks,
 missing:'NA_NOT_ZERO',iso_timestamp_handling:'EXACT_TEXT_TIMEZONE_MICROSECONDS_PRESERVED',
 source_tables_sha256:checksum(tablesBytes),source_signals_sha256:checksum(signalBytes),
 exporter_sha256:checksum(await fs.readFile(new URL(import.meta.url))),
 transforms:['flatten_original_and_round2_causal_features','expose_all_parameters','split_diagnostic_fill_components','no_performance_recalculation']},null,2)+'\n');
