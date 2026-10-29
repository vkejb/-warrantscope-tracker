// Author typed, flat analytical CSVs using Artifact Tool as the table engine.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { Workbook } from '@oai/artifact-tool';

const output=path.resolve(process.argv[2]||'');
if (!process.argv[2]) throw new Error('Usage: node export_tables.mjs <completed-run-directory>');
const tables=JSON.parse(await fs.readFile(path.join(output,'tables.json'),'utf8'));
const config=JSON.parse(await fs.readFile(new URL('./config.json',import.meta.url),'utf8'));
const missedMatches=tables.missed_longs.map(m=>({m,a:tables.trades.find(t=>t.experiment==='A'&&t.scenario===m.scenario&&t.day===m.day&&t.signal_id===m.signal_id)}));
const dayInRowSplits=(day,row)=>!row.data_splits?.length||row.data_splits.some(split=>{
  const interval=config.split[split];
  return interval&&interval.start<=day&&day<=interval.end;
});
let quoteInventory=null,quoteInventorySha=null;
try{
  const bytes=await fs.readFile(path.join(output,'quote_reference_inventory.json'));
  quoteInventory=JSON.parse(bytes);quoteInventorySha=crypto.createHash('sha256').update(bytes).digest('hex');
}catch(error){if(error.code!=='ENOENT')throw error;}
const quoteReferences=new Map((quoteInventory?.rows||[]).map(r=>[r.day+'|'+r.symbol,r]));
const minimum={
  DATA_AUDIT:['day','evaluation_grade','split','canonical_run_id','candidate_count','exclusion_reasons'],
  experiments:['experiment','scenario','status','effective_days','signal_count','entries','closed_trades','net_pnl','max_drawdown_twd'],
  trades:['experiment','scenario','day','symbol','side','entry_model','entry_time','entry_price','quantity','exit_time','exit_price','commission','sell_tax','net_pnl','net_R','mfe_net_twd','mae_net_twd','exit_reason','fill_status','remaining_quantity'],
  signals:['experiment','scenario','record_type','day','signal_id','symbol','time','entry_model','reason','score'],
  daily_results:['experiment','scenario','day','net_pnl','equity','drawdown_twd','realized_pnl','unrealized_pnl','cash','end_quantity'],
  execution_issues:['experiment','scenario','day','signal_id','symbol','time','code','remaining_quantity'],
  grouped_diagnostics:['experiment','scenario','group_by','group','entries','completed','net_pnl'],
  fills:['experiment','scenario','day','symbol','signal_id','phase','time','price','quantity','book_sequence'],
  missed_longs:['experiment','scenario','day','signal_id','symbol','time','reason'],
  baseline_reference:['day','hash_verified','net_pnl','trade_count','source_path','execution_model'],
  post_exit_diagnostics:['experiment','scenario','day','symbol','exit_time','exit_reason','best_post_exit_quote_net','post_exit_opportunity_not_fill','status'],
  per_symbol_data_audit:['day','symbol','regular_tick_events','regular_book_events','first_regular_tick_received_at','last_regular_tick_received_at'],
};
const encode=value=>{
  if(value===null||value===undefined)return 'NA';
  const text=typeof value==='object'?JSON.stringify(value):String(value);
  return /[",\r\n]/.test(text)?'"'+text.replaceAll('"','""')+'"':text;
};
const checks=[];
for (const [name,rawRows] of Object.entries(tables)) {
  const rows=rawRows.map(row=>{
    const r={...row};
    if(name==='missed_longs'){
      const a=missedMatches.find(x=>x.m===row)?.a;
      Object.assign(r,{counterfactual_A_model_trade_exists:!!a,counterfactual_A_entry_time:a?.entry_time??null,
        counterfactual_A_quantity:a?.quantity??null,counterfactual_A_net_pnl:a?.net_pnl??null,
        attribution_type:a?'MISSED_MODELED_LONG_UNDER_A':'QUALIFIED_SIGNAL_ONLY_NOT_A_FILLED_TRADE',
        must_not_add_counterfactual_to_account_pnl:true});
    }
    if(name==='trades'){
      r.mfe_mae_basis='OBSERVED_NET_QUOTE_MARKS_NOT_GUARANTEED_EXECUTABLE_CAPACITY';
      r.last_exit_evaluation_time_basis='LAST_MARK_TIME_PROXY_SEE_EXIT_TRIGGER_AND_FILL_TIMES';
      if(r.marked_net_pnl===null)r.maximum_profit_giveback_twd=null;
    }
    if(name==='per_symbol_data_audit'){
      const ref=quoteReferences.get(r.day+'|'+r.symbol);
      r.quote_reference_inventory_status=ref?'MATCHED_PREFIX_ONLY':'NOT_IN_OPTIONAL_TOP30_PREFIX_INVENTORY';
      if(ref)for(const key of ['first_regular_trade_price_in_archive','first_regular_trade_time_in_archive',
        'first_regular_trade_received_at','first_regular_trade_source_line','observed_open_proxy_price',
        'observed_open_proxy_time','observed_open_proxy_received_at','observed_open_proxy_status',
        'official_open_price','official_open_price_verified','reference_price','reference_price_status',
        'upper_limit','upper_limit_status','lower_limit','lower_limit_status','tick_size_at_proxy',
        'tick_size_status','aggressor_verified','in_out_flag_interpretation'])r[key]=ref[key];
    }
    if(name==='experiments'){
      const scenario=config.stress_scenarios.find(x=>x.name===r.scenario);
      Object.assign(r,{config_version:config.version,capital_twd:config.capital_twd,risk_limit_twd:config.risk_per_trade_twd,
        maximum_trades_daily:config.maximum_trades_per_day,maximum_positions:config.maximum_positions,
        latency_seconds:scenario?.latency_seconds??null,extra_slippage_ticks:scenario?.extra_slippage_ticks??null,
        depth_unit_multiplier:scenario?.depth_unit_multiplier??null,depth_participation:config.depth_participation,
        commission_rate:config.commission_rate,minimum_commission:config.minimum_commission,sell_tax_rate:config.sell_tax_rate,
        maximum_spread_bps:config.maximum_spread_bps,retest_tolerance_fraction:config.retest_tolerance_fraction,
        confirmation_min_seconds:config.confirmation_min_seconds,confirmation_max_seconds:config.confirmation_max_seconds,
        maximum_entry_distance_fraction:config.maximum_entry_distance_fraction,cover_start:config.cover_start,
        strict_executable_evidence:r.scenario==='STRICT_EXECUTABLE'?'INSUFFICIENT':'ASSUMPTIONS_ONLY'});
      r.missed_filled_A_long_trades=r.scenario==='STRICT_EXECUTABLE'?null:new Set(missedMatches.filter(x=>x.a&&x.m.scenario===r.scenario&&x.m.experiment===r.experiment&&dayInRowSplits(x.m.day,r)).map(x=>x.m.day+'|'+x.m.signal_id)).size;
    }
    if(r.per_symbol_coverage)r.per_symbol_coverage='SEE_per_symbol_data_audit.csv';
    if(r.features&&typeof r.features==='object'){
      for(const [key,value] of Object.entries(r.features))r['feature_'+key]=value;
      delete r.features;
    }
    return r;
  });
  const columns=[...new Set([...(minimum[name]||[]),...rows.flatMap(row=>Object.keys(row))])];
  const matrix=[columns,...rows.map(row=>columns.map(key=>{
    const value=row[key];
    return value===undefined||value===null?'NA':typeof value==='object'?JSON.stringify(value):value;
  }))];
  const workbook=Workbook.create();const sheet=workbook.worksheets.add(name.slice(0,31));
  // Prevent automatic ISO date conversion from losing timezone/microseconds.
  // Prefix is internal only and is removed after an exact typed roundtrip.
  const textPrefix='CSV_TEXT:';
  const authored=matrix.map(row=>row.map(value=>typeof value==='string'?textPrefix+value:value));
  sheet.getRangeByIndexes(0,0,matrix.length,columns.length).values=authored;
  sheet.getRangeByIndexes(0,0,1,columns.length).format={fill:'#243746',font:{name:'Arial',bold:true,color:'#FFFFFF'}};
  sheet.showGridLines=false;sheet.freezePanes.freezeRows(1);
  workbook.recalculate();
  const actual=sheet.getRangeByIndexes(0,0,matrix.length,columns.length).values;
  if(actual.length!==matrix.length)throw new Error('Row count changed: '+name);
  for(let i=0;i<matrix.length;i++)for(let j=0;j<columns.length;j++){
    if(actual[i][j]!==authored[i][j])throw new Error(`Typed table mismatch ${name} ${i} ${j}`);
  }
  const exact=actual.map(row=>row.map(value=>typeof value==='string'&&value.startsWith(textPrefix)?value.slice(textPrefix.length):value));
  const check=await workbook.inspect({kind:'table',range:`${sheet.name}!A1:F3`,include:'values',tableMaxRows:3,tableMaxCols:6,maxChars:1500});
  await fs.writeFile(path.join(output,name+'.csv'),'\uFEFF'+exact.map(row=>row.map(encode).join(',')).join('\r\n')+'\r\n','utf8');
  checks.push({table:name,rows:rows.length,columns:columns.length,typed_roundtrip:'PASS',inspection:check.ndjson});
  console.log(name,rows.length,'rows',columns.length,'columns');
}
await fs.writeFile(path.join(output,'csv_validation.json'),JSON.stringify({authoring_engine:'@oai/artifact-tool',missing_value:'NA',text_preservation:'Internal prefix removed after exact roundtrip; ISO timezone and microseconds preserved',checks},null,2)+'\n');
const manifestFile=path.join(output,'run_manifest.json');
const originalManifest=await fs.readFile(manifestFile,'utf8');
try{await fs.writeFile(path.join(output,'run_manifest_pre_export.json'),originalManifest,{flag:'wx'});}catch(error){if(error.code!=='EEXIST')throw error;}
const manifest=JSON.parse(originalManifest);
manifest.csv_export_provenance={exporter_sha256:crypto.createHash('sha256').update(await fs.readFile(new URL(import.meta.url))).digest('hex'),
  optional_quote_reference_inventory_sha256:quoteInventorySha,
  engine:'@oai/artifact-tool',tables:checks.map(x=>({table:x.table,rows:x.rows,columns:x.columns})),
  transformations:['typed_flat_tables','explicit_NA','features_flattened','same_signal_A_counterfactual_join_not_added_to_PnL','optional_open_reference_prefix_join_not_used_by_trading_engine'],
  trading_rules_changed:false};
await fs.writeFile(manifestFile,JSON.stringify(manifest,null,2)+'\n');
