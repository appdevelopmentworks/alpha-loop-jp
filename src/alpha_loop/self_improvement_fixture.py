"""Authored synthetic multi-day loop, including releases and automatic rollback."""
import copy
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from .common import read_json, write_json
from .csv_input import derive_ranking
from .pipeline import run, evaluate_run
from .reporting import qwen_retrospective_report
from .retrospective import retrospective
from . import self_improvement as loop


class SyntheticReasoner:
    model_id = 'synthetic-loop-reasoner'
    model_revision = 'authored-v1'
    runtime_config = {'runtime_config_version': 'synthetic-v1'}

    def __init__(self, conditions=None):
        self.conditions = conditions or ['ret5_le_0_05', 'volume_ge_2']
        self.facts = None

    def ask(self, facts):
        import json
        self.facts = facts
        return {'model': self.model_id, 'choices': [{'message': {'content': json.dumps({'hypotheses': [
            {'condition_ids': self.conditions, 'reason_code': 'avoid_overheat'}]})}, 'finish_reason': 'stop'}]}


def calendar():
    result, d = [], date(2026, 5, 1)
    while d <= date(2026, 12, 31):
        if d.weekday() < 5 and d.isoformat() not in ('2026-09-21', '2026-09-22', '2026-09-23'):
            result.append(d.isoformat())
        d += timedelta(days=1)
    return result


def input_day(root: Path, session: str, *, bad=False):
    """Independent authored cross-section each day; never real-market evidence."""
    cal = calendar()
    directory = root / 'input' / session
    fetched = session + 'T19:30:00+09:00'
    instruments, bars, future = [], [], []
    following = cal[cal.index(session) + 1]
    for i in range(1, 23):
        iid = f'TSE:{i:04d}'
        instruments.append({'instrument_id': iid, 'symbol': f'{i:04d}', 'raw_code': f'{i:04d}', 'name': f'自己改善合成{i}',
                            'market': 'Standard' if i <= 10 else 'Growth', 'security_type': 'common',
                            'listing_status': 'listed', 'effective_date': cal[0], 'fetched_at': fetched})
        close, volume = (102., 3000000) if i <= 5 else (108., 2500000) if i <= 10 else (99., 2000000) if i <= 15 else (101., 1500000) if i <= 20 else (100., 1000000)
        for day in [d for d in cal if d <= session]:
            c, v = (close, volume) if day == session else (100., 1000000)
            missing = i == 22 and day == session
            bars.append({'instrument_id': iid, 'session_date': day, 'bar_status': 'missing' if missing else 'ok',
                         'open': None if missing else c, 'high': None if missing else max(101., c), 'low': None if missing else min(98., c),
                         'close': None if missing else c, 'volume': v, 'turnover_jpy': None,
                         'adj_open': None if missing else c, 'adj_high': None if missing else max(101., c),
                         'adj_low': None if missing else min(98., c), 'adj_close': None if missing else c,
                         'adj_volume': v, 'adjustment_basis': 'split_only', 'fetched_at': fetched})
        hit = (i <= 5 or 11 <= i <= 15) != bad
        future.append({'instrument_id': iid, 'session_date': following, 'bar_status': 'ok', 'adj_open': close,
                       'adj_high': close * (1.12 if hit else 1.04), 'adj_low': close * .98, 'adj_close': close * 1.01,
                       'adjustment_basis': 'split_only', 'prior_close_rebase_factor': 1.,
                       'execution_status': 'unfilled' if i == 1 else 'unknown' if i == 2 else 'proxy_only'})
    for name, value in (('calendar', cal), ('instruments', instruments), ('bars', bars), ('disclosures', []),
                        ('source', {'data_grade': 'synthetic', 'fetched_at': fetched, 'available_at': fetched}),
                        ('future', {'calendar': cal, 'data_grade': 'synthetic', 'bars': future})):
        write_json(directory / (name + '.json'), value)
    return directory, following


def synthetic_policy(root: Path, path: Path):
    policy = copy.deepcopy(read_json(path))
    policy.update(mode='simulation', tuning_sessions=1, holdout_sessions=5)
    policy['criteria'].update(min_days=5, min_valid=10, min_unique_instruments=5, min_event_clusters=5,
                              bootstrap_samples=400, confidence=.90)
    policy['monitor'].update(min_days=1, min_selected=1, window_days=3)
    write_json(root / 'policy.json', policy)
    write_json(root / 'data/self_improvement/SIMULATION.json', {'data_grade': 'synthetic', 'purpose': 'authored acceptance demo only'})
    return root / 'policy.json'


def issue(root, baseline, policy, session, conditions=None, bad=False):
    directory, following = input_day(root, session, bad=bad)
    at = session + 'T20:00:00+09:00'
    with patch('alpha_loop.pipeline.now_iso', return_value=at), patch('alpha_loop.self_improvement.now_iso', return_value=at):
        manifest = run(root, directory, baseline, session, at)
        ranked = derive_ranking(root, directory, session, .05, 50)
        study = retrospective(root, Path(ranked['ranking_input']), directory, baseline)
        provider = SyntheticReasoner(conditions)
        report = qwen_retrospective_report(root, study['study_id'], provider, loop.learning_facts(root, session))
        result = loop.day(root, manifest['run_id'], policy, Path(report['report_path']))
    return manifest, result, following, provider


def demo(root: Path, baseline: Path, policy_source: Path):
    from .research import compare, prepare_batch
    root.mkdir(parents=True, exist_ok=False)
    policy = synthetic_policy(root, policy_source)
    entries, forecasts = [], []
    days = ['2026-08-24', '2026-08-25', '2026-08-26', '2026-08-27', '2026-08-28', '2026-08-31', '2026-09-01']
    for session in days:
        loop.feedback(root, entries, session)
        manifest, forecast, following, provider = issue(root, baseline, policy, session)
        evaluated = evaluate_run(root, manifest['run_id'], root / 'input' / session, following)
        entries.append({'run_id': manifest['run_id'], 'evaluation_id': evaluated['evaluation_id'],
                        'future_ref': f'input/{session}/future.json'})
        forecasts.append(forecast)
        entries = [e for e in entries if read_json(root / 'outputs' / e['run_id'] / 'run_manifest.json')['target_session'] < session]
        # The next iteration ingests just the newly completed preceding day.
        entries.append({'run_id': manifest['run_id'], 'evaluation_id': evaluated['evaluation_id'], 'future_ref': f'input/{session}/future.json'})
        entries = list({e['run_id']: e for e in entries}.values())
    loop.feedback(root, entries, '2026-09-02')
    registration = loop.status(root)['registrations'][0]
    comparison = compare(root, registration['experiment_id'], prepare_batch(root, registration['experiment_id']))
    with patch('alpha_loop.self_improvement.now_iso', return_value='2026-09-02T20:00:00+09:00'):
        release = loop.activate(root, registration['experiment_id'], policy, reviewer='synthetic-demo-human', human_reviewed=True)
    # Feedback-aware reasoner selects another finite condition combination.
    changed_conditions = ['ret5_le_0_05', 'volume_ge_2', 'ma25_gap_le_0_15']
    m, changed, following, reasoner = issue(root, baseline, policy, '2026-09-03', changed_conditions)
    assert changed['new_versions'] and reasoner.facts['learning_feedback']['feedback']
    evaluated = evaluate_run(root, m['run_id'], root / 'input/2026-09-03', following)
    new_entries = [{'run_id': m['run_id'], 'evaluation_id': evaluated['evaluation_id'], 'future_ref': 'input/2026-09-03/future.json'}]
    # A bad post-release day triggers the frozen monitoring rule and restores baseline.
    m, bad_forecast, following, _ = issue(root, baseline, policy, '2026-09-04', changed_conditions, bad=True)
    evaluated = evaluate_run(root, m['run_id'], root / 'input/2026-09-04', following)
    loop.feedback(root, [{'run_id': m['run_id'], 'evaluation_id': evaluated['evaluation_id'], 'future_ref': 'input/2026-09-04/future.json'}], following)
    monitored = loop.monitor(root, policy, following)
    assert monitored['status'] == 'ROLLED_BACK'
    for session in ['2026-09-07', '2026-09-08', '2026-09-09', '2026-09-10', '2026-09-11']:
        loop.feedback(root, new_entries, session)
        m, _, following, _ = issue(root, baseline, policy, session, changed_conditions)
        evaluated = evaluate_run(root, m['run_id'], root / 'input' / session, following)
        new_entries.append({'run_id': m['run_id'], 'evaluation_id': evaluated['evaluation_id'], 'future_ref': f'input/{session}/future.json'})
    loop.feedback(root, new_entries, '2026-09-14')
    registration2 = next(r for r in loop.status(root)['registrations'] if r['version_id'] == changed['new_versions'][0])
    compare(root, registration2['experiment_id'], prepare_batch(root, registration2['experiment_id']))
    with patch('alpha_loop.self_improvement.now_iso', return_value='2026-09-14T20:00:00+09:00'):
        promoted = loop.promote_ready(root, policy)
    assert loop.status(root)['simulation']['version_id'] == changed['new_versions'][0]
    _, final_forecast, _, _ = issue(root, baseline, policy, '2026-09-14', changed_conditions)
    state = loop.status(root)
    paired = next(f for f in state['feedback'] if f['source_session'] == bad_forecast['session'])
    proof = {'data_grade': 'synthetic', 'forecast_days': len(state['days']), 'versions': len(state['versions']),
             'candidate_csv': bad_forecast['candidate_csv'], 'latest_candidate_csv': final_forecast['candidate_csv'],
             'operational_candidate_csv': final_forecast['operational_candidate_csv'],
             'outcomes_csv': str(root / paired['directory'] / 'outcomes.csv'),
             'next_generation_from_feedback': bool(reasoner.facts['learning_feedback']['feedback']),
             'synthetic_comparison': comparison, 'simulation_release': release, 'automatic_rollback': monitored,
             'subsequent_automatic_promotion': promoted,
             'production_state': state['production'], 'network_used': False, 'gpu_used': False,
             'market_performance_validated': False}
    write_json(root / 'acceptance.json', proof)
    return proof
