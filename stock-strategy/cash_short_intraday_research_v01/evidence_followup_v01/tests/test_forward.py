"""Synthetic chronology, isolation, residual-state and safe-entry contracts."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cash_short_intraday_research_v01.evidence_followup_v01 import forward as f


class ForwardContracts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);(self.base/'archive').mkdir()
        (self.base/'attestation.txt').write_text('SYNTHETIC explicit unseen assertion')
        self.profile={'frozen_at':'2026-10-29T15:00:00+00:00',
                      'known_exploratory_dates':['20261029'],'profile_hash':'synthetic',
                      'original_source_commit':'SYNTHETIC_NOT_A_REAL_COMMIT',
                      'variant_hashes':{'R0':'a','R2':'b','R5':'c'}}
        self.manifest={'schema':'CASH_SHORT_FORWARD_MANIFEST_V01',
          'unseen_attestation':{'outcomes_inspected':False,'basis':'SYNTHETIC_FIXTURE_NOT_MARKET_EVIDENCE',
           'evidence_path':'attestation.txt','evidence_sha256':f.sha(self.base/'attestation.txt')},
          'runs':[{'day':'20261030','path':'archive','eligibility':[]}]}

    def errors(self):return f.validate_manifest(self.manifest,self.profile,self.base)
    def test_new_date_accepted_only_with_body_and_hash(self):self.assertEqual(self.errors(),[])
    def test_known_date_rejected(self):
        self.manifest['runs'][0]['day']='20261029'
        self.assertTrue(any('KNOWN_EXPLORATORY' in x for x in self.errors()))
    def test_old_unlisted_date_also_rejected(self):
        self.manifest['runs'][0]['day']='20261028'
        self.assertTrue(any('NOT_STRICTLY' in x for x in self.errors()))
    def test_taipei_local_freeze_not_utc_date(self):
        self.profile['frozen_at']='2026-10-29T17:00:00+00:00'
        self.assertTrue(any('NOT_STRICTLY' in x for x in self.errors()))
    def test_invalid_calendar_date(self):
        self.manifest['runs'][0]['day']='20261301'
        self.assertTrue(any('INVALID_DAY' in x for x in self.errors()))
    def test_duplicates_not_independent_days(self):
        self.manifest['runs']*=2
        self.assertTrue(any('DUPLICATE_DATE' in x for x in self.errors()))
    def test_seen_outcome_rejected(self):
        self.manifest['unseen_attestation']['outcomes_inspected']=True
        self.assertIn('UNSEEN_ATTESTATION_REQUIRED',self.errors())
    def test_unspecified_unseen_status_rejected(self):
        self.manifest['unseen_attestation']['outcomes_inspected']=None
        self.assertIn('UNSEEN_ATTESTATION_REQUIRED',self.errors())
    def test_attestation_not_hash_only(self):
        (self.base/'attestation.txt').unlink()
        self.assertIn('ATTESTATION_BODY_OR_HASH_MISSING',self.errors())
    def test_attestation_wrong_hash(self):
        self.manifest['unseen_attestation']['evidence_sha256']='0'*64
        self.assertIn('ATTESTATION_BODY_OR_HASH_MISSING',self.errors())
    def test_no_data_pending_not_zero(self):
        self.manifest['runs']=[]
        self.assertIn('NO_NEW_ARCHIVED_DATES',self.errors())
    def test_latest_forbidden(self):
        (self.base/'latest').mkdir();self.manifest['runs'][0]['path']='latest'
        self.assertTrue(any('LATEST_ALIAS' in x for x in self.errors()))
    def test_known_restriction_requires_real_evidence(self):
        self.manifest['runs'][0]['eligibility']=[{'symbol':'TEST','known_restriction':True,'effective_from':'20261030'}]
        self.assertTrue(any('RESTRICTION_EVIDENCE' in x for x in self.errors()))
    def test_expired_restriction_not_applied(self):
        self.manifest['runs'][0]['eligibility']=[{'symbol':'TEST','known_restriction':True,
          'evidence_path':'attestation.txt','evidence_sha256':f.sha(self.base/'attestation.txt'),
          'effective_from':'20261001','effective_to':'20261029'}]
        self.assertTrue(any('ALREADY_ENDED' in x for x in self.errors()))
    def test_unknown_qualification_not_promoted(self):
        self.manifest['runs'][0]['eligibility']=[{'symbol':'TEST','known_restriction':None}]
        self.assertEqual(self.errors(),[])
        self.assertNotIn('qualification_certified',self.manifest['runs'][0]['eligibility'][0])
    def test_source_quality_missing_not_no_signal(self):
        d={'day':'20261030','point_in_time_pass':True,'source_hash_verified':True,
           'callback_errors':0,'source_status':'COMPLETE','recorded_coverage_status':'PARTIAL_SESSION',
           'recorded_stream_coverage_pass':False}
        self.assertEqual(set(f.source_gate(d,'20261030')),{'COVERAGE_NOT_FULL','STREAM_COVERAGE_FAIL'})
    def test_source_day_mismatch(self):
        d={'day':'20261029','point_in_time_pass':True,'source_hash_verified':True,
           'callback_errors':0,'source_status':'COMPLETE','recorded_coverage_status':'FULL_SESSION',
           'recorded_stream_coverage_pass':True}
        self.assertIn('ARCHIVE_DAY_MISMATCH',f.source_gate(d,'20261030'))
    def test_isolation_needs_source_body_not_just_hash(self):
        p={'original_pins':{'missing.py':'0'*64}}
        with self.assertRaisesRegex(ValueError,'BODY'):f.verify_isolated(p,self.base)
    def test_active_checkout_isolation_forbidden(self):
        with self.assertRaisesRegex(ValueError,'ACTIVE_CHECKOUT'):f.verify_isolated({},f.ROOT)
    def test_no_overwrite(self):
        target=self.base/'result.json';f.dump(target,{'original':True})
        with self.assertRaises(FileExistsError):f.dump(target,{'original':False})
        self.assertTrue(json.loads(target.read_text())['original'])
    def test_profile_tampering_detected_before_import(self):
        p={'profile_hash':'0'*64};path=self.base/'freeze.json';f.dump(path,p)
        with self.assertRaisesRegex(ValueError,'PROFILE_HASH'):f.verify_profile(path,self.base)
    def test_safety_constants_no_orders_or_sdk(self):
        self.assertEqual(f.SAFETY['actual_orders'],0)
        self.assertEqual(f.VARIANTS,('R0','R2','R5'))
        text=Path(f.__file__).read_text()
        self.assertNotIn('import Yuanta',text)
        self.assertNotIn('SendStockOrder',text)

    def test_synthetic_full_worker_zero_signal_dates_and_portfolio_call_contract(self):
        self.synthetic_worker_check()

    def test_cross_day_equity_carry(self):
        self.synthetic_worker_check(first_day_net=-123.25)

    def test_cross_day_residual_exposure_carry(self):
        self.synthetic_worker_check(first_day_net=-123.25,residual=True)

    def test_common_scope_uses_only_three_fixed_variants(self):
        self.synthetic_worker_check(with_signal=True)

    def test_common_missing_benchmark_is_unknown_not_condition_false(self):
        self.synthetic_worker_check(with_signal=True,unavailable_variant='R5')

    def test_common_false_condition_still_evaluable(self):
        self.synthetic_worker_check(with_signal=True,reject_variant='R5')

    def test_invalid_batch_never_calls_strategy_or_writes_zero_pnl(self):
        self.synthetic_worker_check(invalid_batch=True)

    def test_changed_archive_after_preflight_blocks_before_portfolio(self):
        self.synthetic_worker_check(source_changed=True)

    def synthetic_worker_check(self,first_day_net=0,residual=False,with_signal=False,
                               unavailable_variant=None,reject_variant=None,invalid_batch=False,source_changed=False):
        iso=self.base/'iso';cfg=iso/'stock-strategy/cash_short_intraday_research_v01/config.json'
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({'stress_scenarios':[{'name':'BASE_ASSUMED_LOTS'}]}))
        self.manifest['runs'].append({'day':'20261102','path':'archive','eligibility':[]})
        manifest_path=self.base/'manifest.json';manifest_path.write_text(json.dumps(self.manifest))
        counts={'load':0,'portfolio':0}
        def load_day(paths):
            # First two calls are preflight; next two are actual synthetic days.
            day=('20261030','20261102')[counts['load']%2];counts['load']+=1
            return {'day':day,'point_in_time_pass':True,'source_hash_verified':True,'callback_errors':0,
              'source_status':'COMPLETE','recorded_coverage_status':'FULL_SESSION',
              'recorded_stream_coverage_pass':not (invalid_batch and day=='20261102'),
              'metadata':{},'events':[],'candidates':[],
              'sources':[{'changed':True}] if source_changed and counts['load']>2 else []}
        def portfolio(days,signals_by_day,config,mode,short_model,market_filter,exit_model,**kwargs):
            self.assertEqual((mode,short_model,market_filter,exit_model),('B','S1','NONE','E0'))
            self.assertEqual(set(days),set(signals_by_day))
            day=next(iter(days));later=day=='20261102'
            self.assertEqual(kwargs['initial_equity'],190000+first_day_net if later else 190000)
            self.assertEqual(kwargs['prior_residual'],residual if later else False)
            if later:self.assertEqual(kwargs['prior_max_dd'],max(0,-first_day_net))
            which=counts['portfolio']%6
            variant=f.VARIANTS[which%3];common=which>=3
            expected=with_signal and variant not in (unavailable_variant,reject_variant) and not (common and unavailable_variant)
            self.assertEqual(len(next(iter(signals_by_day.values()))),int(bool(expected)))
            counts['portfolio']+=1
            ending=190000+first_day_net
            return {'trades':[],'daily':[{'day':day,'starting_equity':kwargs['initial_equity'],
              'net_pnl':None if later and residual else first_day_net if not later else 0,
              'equity':None if residual else ending,'account_complete':not residual}],
              'issues':[],'dispositions':[],'ending_equity':ending,
              'peak_equity':190000,'max_drawdown':max(0,-first_day_net),'residual':residual}
        def append(state,result):
            for k in ('trades','daily','issues','dispositions'):state[k].extend(result[k])
            for k in ('ending_equity','peak_equity','max_drawdown','residual'):state[k]=result[k]
        def metric(state,signals,config):
            return {'net_pnl':0,'max_drawdown_twd':0,'closed_trades':0,'win_rate':None,'profit_factor':None}
        def scan(d,c):return ([{'signal_id':'synthetic-'+d['day'],'day':d['day'],'symbol':'TEST',
                               'time':d['day'],'entry_model':'S1'}] if with_signal else [],[])
        def evaluate(s,v):
            self.assertIn(v,('R0','R2','R5'))
            return {'evaluable':v!=unavailable_variant,'pass':None if v==unavailable_variant else v!=reject_variant,
                    'reasons':['SYNTHETIC_BENCHMARK_MISSING'] if v==unavailable_variant else []}
        mods=(SimpleNamespace(load_day=load_day),SimpleNamespace(index_day=lambda d:{},scan_day=scan,run_portfolio=portfolio),
          SimpleNamespace(build_feature_context=lambda d,s,c:{'signals':s},evaluate_variant=evaluate),
          SimpleNamespace(append_state=append,day_extrema=lambda t:{},metrics=metric,annotate_path_profit=lambda t:None))
        args=SimpleNamespace(freeze_file=self.base/'unused',isolated_root=iso,manifest=manifest_path,output=self.base/'new_results',check=False)
        with patch.object(f,'verify_profile',return_value=(self.profile,iso)),patch.object(f,'original_modules',return_value=mods):
            if source_changed:
                with self.assertRaisesRegex(ValueError,'SOURCE_CHANGED'):f.worker(args)
                self.assertEqual(counts['portfolio'],0)
                self.assertFalse((args.output/'tables.json').exists());return
            self.assertEqual(f.worker(args),2 if invalid_batch else 0)
        if invalid_batch:
            self.assertEqual(counts['portfolio'],0)
            self.assertFalse((args.output/'tables.json').exists())
            status=json.loads((args.output/'validation_status.json').read_text())
            self.assertEqual(status['status'],'BLOCKED_DATA_INCOMPLETE_NO_PNL_CALCULATED');return
        self.assertEqual(counts['portfolio'],12)
        tables=json.loads((args.output/'tables.json').read_text())
        self.assertEqual(len(tables['daily_results']),12)
        self.assertEqual(len(tables['cash_daily']),2)
        self.assertTrue(all(r['win_rate'] is None for r in tables['experiments']))
        self.assertTrue(all(r['strict_executable'] is None for r in tables['experiments']))

    def test_declared_config_cannot_differ_from_executed_settings(self):
        cfg=json.loads((f.HERE/'forward_config.json').read_text())
        original=json.loads((f.HERE.parent/'config.json').read_text())
        round2=json.loads((f.R2/'config.json').read_text())
        f.config_contract(cfg,original,round2)
        cfg['capital_twd']+=1
        with self.assertRaisesRegex(ValueError,'CONFIG_DIFFER'):f.config_contract(cfg,original,round2)

    def test_declared_rs_cannot_differ_from_executed_constant(self):
        cfg=json.loads((f.HERE/'forward_config.json').read_text())
        original=json.loads((f.HERE.parent/'config.json').read_text())
        round2=json.loads((f.R2/'config.json').read_text())
        cfg['rs_300_max']=round2['rs_300_max']=-0.003
        with self.assertRaisesRegex(ValueError,'RS_CONSTANT'):f.config_contract(cfg,original,round2)

    def test_cannot_remove_original_pins_then_rehash_profile(self):
        path=self.base/'stock-strategy/cash_short_intraday_research_v01/round2/freeze.json'
        path.parent.mkdir(parents=True);path.write_text(json.dumps({'files':{'source.py':'a'*64},'source_commit':'synthetic'}))
        p={'original_pins':{},'original_freeze_sha256':f.sha(path),'original_source_commit':'synthetic'}
        with self.assertRaisesRegex(ValueError,'PIN_SET_INCOMPLETE'):f.verify_isolated(p,self.base)

    def test_restored_original_body_is_actually_compared(self):
        path=self.base/'stock-strategy/cash_short_intraday_research_v01/round2/freeze.json'
        path.parent.mkdir(parents=True);body=self.base/'source.py';body.write_text('SYNTHETIC ORIGINAL BODY')
        pins={'source.py':f.sha(body)};path.write_text(json.dumps({'files':pins,'source_commit':'synthetic'}))
        p={'original_pins':pins,'original_freeze_sha256':f.sha(path),'original_source_commit':'synthetic'}
        self.assertEqual(f.verify_isolated(p,self.base),self.base.resolve())
        body.write_text('SYNTHETIC CHANGED BODY')
        with self.assertRaisesRegex(ValueError,'BODY_OR_INPUT'):f.verify_isolated(p,self.base)


if __name__=='__main__':unittest.main()
