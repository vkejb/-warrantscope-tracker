"""Run frozen S0/S1/S4 in a separate offline process/output, never broker code.

Inject only the fill backend/statistical labeling into the existing research
runner. Preserve the original selector, thresholds, exits, data and source files.
"""
import argparse
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from .. import replay as legacy_replay
from .. import s4
from .. import statistics
from .execution import LotDepthExecution

HERE = Path(__file__).resolve().parent
LEGACY_REPLAY = legacy_replay.replay_trade


def safe_metrics(rows):
    result = statistics.metrics(rows)
    if not result['account_complete']:
        fields = ('profit_factor', 'win_rate', 'avg_winner', 'avg_loser', 'mean_trade_pnl', 'max_daily_realized_drawdown')
        result['completed_subset_statistics'] = {k: result[k] for k in fields}
        for field in fields:
            result[field] = None
    result['performance_scope'] = 'CONDITIONAL_UNIT_ASSUMPTION_NOT_BROKER_VALIDATED'
    return result


def safe_robustness(rows):
    days = sorted({r['date'] for r in rows})
    output = {'leave_one_day_out': [{'removed_day': d, **safe_metrics([r for r in rows if r['date'] != d])} for d in days]}
    scored = [r for r in rows if r['status'] == 'SCORED_TRADE']
    for name, select in (('remove_best_trade', max), ('remove_worst_trade', min)):
        if scored:
            removed = select(scored, key=lambda r: r['net_pnl'])
            output[name] = {'removed_date': removed['date'], 'removed_net_pnl': removed['net_pnl'],
                            **safe_metrics([r for r in rows if r is not removed])}
        else:
            output[name] = None
    return output


def legal_replay(*args):
    models = []
    def factory():
        model = LotDepthExecution()
        models.append(model)
        return model
    with patch.object(legacy_replay, 'DepthExecution', factory):
        row = LEGACY_REPLAY(*args)
    row.update(fill_increment_shares=1000, execution_model='DISPLAYED_DEPTH_LEGAL_ORDINARY_STOCK_LOTS',
               unit_status='UNIT_UNVERIFIED', eligibility='ELIGIBILITY_UNPROVEN',
               remainder_cancel_basis='IMMEDIATE_CANCELLATION_MODEL_ASSUMPTION_NOT_BROKER_EVIDENCE')
    if models and models[0].requests:
        row['target_quantity'] = models[0].requests[0]['quantity']
        row['execution_requests'] = models[0].requests
        row['depth_fill_evidence'] = models[0].fills
        if row.get('entry_quantity', 0) == 0 and row['status'] == 'UNFILLED_ENTRY':
            row['unfilled_requested_quantity'] = row['target_quantity']
    return row


def run(output):
    if output.resolve() in (s4.MODULE / 'results_s4', s4.REFERENCE):
        raise ValueError('OLD_OUTPUT_OVERWRITE_FORBIDDEN')
    audit_dir = s4.MODULE / 'unit_eligibility_audit_v01'
    audit_manifest = json.loads((audit_dir / 'results/validation_manifest.json').read_text())
    for item in audit_manifest['outputs']:
        if hashlib.sha256((audit_dir / item['path']).read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('PRIOR_UNIT_AUDIT_CHANGED:' + item['path'])
    with patch.multiple(s4, replay_trade=legal_replay, metrics=safe_metrics, robustness=safe_robustness):
        s4.run(output)
    contract = json.loads((output / 'contract.json').read_text())
    contract.update(reference_s4_commit='4a659d3', unit_audit_commit='d7da88f', fill_increment_shares=1000,
                    execution_change='ONLY_LEGAL_FILL_INCREMENT_NOT_SIGNAL_THRESHOLD_OR_EXIT_CHANGE',
                    ordinary_stock_market_only=True,
                    scope='CONDITIONAL_CHRONOLOGICAL_ACCOUNT_REPLAY_NOT_LIVE_CERTIFICATION')
    s4.write_json(output / 'contract.json', contract)
    s4.write_json(output / 'provenance.json', {'prior_unit_audit_manifest_sha256': hashlib.sha256((audit_dir / 'results/validation_manifest.json').read_bytes()).hexdigest(),
                    'legacy_s4_output_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (s4.MODULE / 'results_s4').iterdir() if p.is_file()},
                    'unchanged_legacy_code_hashes': {str(p.relative_to(s4.MODULE.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (s4.MODULE / 's4.py', s4.MODULE / 'replay.py', s4.MODULE / 'execution.py', s4.MODULE / 'policy.py',
                                  s4.MODULE / 'statistics.py', s4.MODULE.parent / 'yuanta_live_runtime_v01/strategy.py')},
                    'no_live_changes': True, 'broker_connections': 0, 'actual_orders': 0, 'actual_fills': 0})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=HERE / 'results')
    args = parser.parse_args()
    run(args.output_dir)
