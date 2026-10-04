"""Bounded hypothesis generations, sealed daily shadow forecasts and release control.

The price engine and registered M5 experiments are reused without modification.
SQLite intents precede file publication so interrupted days retain their choices.
"""
from __future__ import annotations

import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .common import JST, canonical, digest, now_iso, parse_time, read_json, stable_id, write_csv, write_json
from .hypothesis_plans import CATALOG_VERSION, validate_plans
from .pipeline import _code_hash, _verified_manifest, _verified_snapshot
from .provenance import archive_code, verify_code
from .runtime import exclusive

VERSION = 'hypothesis-loop-v1'


def settings(path: Path) -> dict:
    value = read_json(path)
    required = {'schema_version', 'mode', 'max_versions', 'max_new_per_day', 'automatic_promotion',
                'tuning_sessions', 'holdout_sessions', 'criteria', 'monitor'}
    if set(value) != required or value['schema_version'] != 1 or value['mode'] not in ('shadow', 'simulation'):
        raise ValueError('invalid self-improvement policy')
    for key, maximum in (('max_versions', 20), ('max_new_per_day', 2), ('tuning_sessions', 60), ('holdout_sessions', 120)):
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise ValueError('invalid bounded policy: ' + key)
    if type(value['automatic_promotion']) is not bool:
        raise ValueError('automatic_promotion must be boolean')
    monitor = value['monitor']
    if set(monitor) != {'window_days', 'min_days', 'min_selected', 'max_unknown_rate', 'max_hit_rate_drop', 'max_capture_drop'}:
        raise ValueError('incomplete monitoring policy')
    for key in ('window_days', 'min_days', 'min_selected'):
        if type(monitor[key]) is not int or not 1 <= monitor[key] <= 10000:
            raise ValueError('invalid monitoring count')
    if monitor['min_days'] > monitor['window_days']:
        raise ValueError('monitor window too short')
    for key in ('max_unknown_rate', 'max_hit_rate_drop', 'max_capture_drop'):
        if type(monitor[key]) not in (int, float) or not math.isfinite(monitor[key]) or not 0 <= monitor[key] <= 1:
            raise ValueError('invalid monitoring ratio')
    canonical(value)
    from .research import _criteria
    _criteria(value['criteria'], {'max_candidates_per_stage': value['criteria'].get('k_per_stage')})
    if value['holdout_sessions'] < value['criteria']['min_days']:
        raise ValueError('holdout cannot satisfy minimum days')
    return value


@contextmanager
def _db(root: Path):
    directory = root / 'data/self_improvement'
    directory.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(directory / 'state.sqlite', timeout=10)
    try:
        con.execute('CREATE TABLE IF NOT EXISTS objects(kind TEXT,id TEXT,payload TEXT,sha TEXT,PRIMARY KEY(kind,id))')
        con.execute('CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,payload TEXT,sha TEXT)')
        con.commit()
        with con:
            yield con
    finally:
        con.close()


def engine_hash():
    directory = Path(__file__).parent
    return digest(canonical({name: digest((directory / name).read_bytes()) for name in
                             ('self_improvement.py', 'hypothesis_plans.py', 'reporting.py', 'inference.py')}))


def _put(con, kind, key, value):
    raw = canonical(value).decode()
    old = con.execute('SELECT payload,sha FROM objects WHERE kind=? AND id=?', (kind, key)).fetchone()
    if old and old != (raw, digest(raw.encode())):
        raise RuntimeError('immutable loop record changed: ' + kind)
    con.execute('INSERT OR IGNORE INTO objects VALUES(?,?,?,?)', (kind, key, raw, digest(raw.encode())))


def _get(con, kind, key):
    row = con.execute('SELECT payload,sha FROM objects WHERE kind=? AND id=?', (kind, key)).fetchone()
    if not row:
        return None
    if digest(row[0].encode()) != row[1]:
        raise RuntimeError('self-improvement ledger hash mismatch')
    import json
    return json.loads(row[0])


def _all(con, kind):
    return [_get(con, kind, key) for key, in con.execute('SELECT id FROM objects WHERE kind=? ORDER BY id', (kind,)).fetchall()]


def _events(con):
    import json
    previous, events = None, []
    for seq, raw, sha in con.execute('SELECT seq,payload,sha FROM events ORDER BY seq'):
        item = json.loads(raw)
        if digest(raw.encode()) != sha or item['previous_hash'] != previous or seq != len(events) + 1:
            raise RuntimeError('release history chain mismatch')
        previous = sha
        events.append(item)
    return events


def _state(con, mode):
    events = [e for e in _events(con) if e['mode'] == mode]
    return events[-1]['after'] if events else {'version_id': None, 'release_id': None, 'activated_at': None}


def _event(con, mode, action, after, details):
    history = _events(con)
    item = {'mode': mode, 'action': action, 'at': now_iso(), 'before': _state(con, mode), 'after': after,
            'details': details, 'previous_hash': digest(canonical(history[-1])) if history else None}
    con.execute('INSERT INTO events VALUES(?,?,?)', (len(history) + 1, canonical(item).decode(), digest(canonical(item))))
    return item


def _protected(root):
    from .research import _load
    return [_load(root, p.stem)['periods']['holdout'] for p in (root / 'data/research/experiments').glob('*.json')]


def protected(root: Path, session: str) -> bool:
    return any(p['start'] <= session <= p['end'] for p in _protected(root))


def _manifest(root, run_id):
    if not re.fullmatch(r'run-[a-f0-9]{20}', run_id):
        raise ValueError('invalid run id')
    value = _verified_manifest(root / 'outputs' / run_id)
    if not value or value['run_kind'] != 'baseline' or value['code_hash'] != _code_hash():
        raise ValueError('verified current baseline run required')
    snapshot = _verified_snapshot(root, value['snapshot_id'])
    return value, snapshot


def _matches(row, plan):
    reasons = []
    for condition in plan['conditions']:
        value = row.get(condition['feature'])
        if type(value) not in (int, float) or not math.isfinite(value):
            reasons.append(condition['condition_id'] + ':unknown')
        else:
            a, b = Decimal(str(value)), Decimal(str(condition['value']))
            if not (a >= b if condition['operator'] == '>=' else a <= b):
                reasons.append(condition['condition_id'] + ':not_met')
    return not reasons, reasons


def _stats(rows, field):
    chosen = [r for r in rows if r[field]]
    valid = [r for r in chosen if r['discovery_hit'] is not None]
    hits = sum(r['discovery_hit'] for r in valid)
    all_hits = sum(r['discovery_hit'] for r in rows if r['discovery_hit'] is not None)
    return {'selected': len(chosen), 'valid': len(valid), 'hits': hits, 'false_positives': len(valid) - hits,
            'misses': all_hits - hits, 'universe_hits': all_hits, 'unknown': len(chosen) - len(valid),
            'hit_rate': hits / len(valid) if valid else None,
            'capture_rate': hits / all_hits if all_hits else None,
            'unknown_rate': (len(chosen) - len(valid)) / len(chosen) if chosen else None}


def _verify_files(directory, payload):
    for name, sha in payload['artifacts'].items():
        if Path(name).name != name or digest((directory / name).read_bytes()) != sha:
            raise RuntimeError('self-improvement artifact hash mismatch')


def _feedback(root, con, entries, session):
    from .research import _read_entry
    completed = []
    for entry in entries:
        if not re.fullmatch(r'[a-f0-9]{20}', entry.get('evaluation_id', '')):
            raise ValueError('invalid evaluation id')
        if entry.get('future_ref') and not (root / entry['future_ref']).resolve().is_relative_to(root.resolve()):
            raise ValueError('future input must be inside the operation root')
        days = [d for d in _all(con, 'day') if d['run_id'] == entry['run_id']]
        if not days:
            continue
        day = days[0]
        old = _get(con, 'feedback', day['day_id'])
        if old:
            if old['outcome_session'] > session:
                raise ValueError('future results cannot enter daily learning')
            if old['evaluation_id'] != entry['evaluation_id']:
                raise ValueError('first complete evaluation is frozen; cannot substitute')
            _verify_files(root / old['directory'], old)
            completed.append(old)
            continue
        manifest, snapshot = _manifest(root, entry['run_id'])
        spec = {'numerical_code_hash': manifest['code_hash'], 'baseline_config_hash': manifest['config_hash'],
                'baseline_config': manifest['config'], 'calendar': snapshot['data']['calendar']}
        if not entry.get('future_ref'):
            raise ValueError('original future input required for learning feedback')
        evaluation_path = root / 'outputs' / entry['run_id'] / 'evaluation' / entry['evaluation_id']
        evaluation = read_json(evaluation_path / 'evaluation_manifest.json')
        next_day = day['forecast_session']
        if next_day > session or evaluation['through'] > session:
            raise ValueError('future results cannot enter daily learning')
        outcomes_raw = read_json(evaluation_path / 'outcomes.json')
        if any(o['outcome_status'] == 'horizon_incomplete' for o in outcomes_raw):
            continue
        _, decisions, outcomes, _, _, evidence = _read_entry(root, entry, spec)
        _verify_files(root / day['directory'], day)
        selections = read_json(root / day['directory'] / 'selections.json')
        rows, metrics = [], {}
        for version_id, selected in selections.items():
            subset = []
            for row in decisions:
                outcome = outcomes[row['instrument_id']]
                chosen = row['instrument_id'] in selected
                hit = outcome['discovery_hit']
                sample = {'version_id': version_id, 'source_session': day['session'], 'outcome_session': next_day,
                          'instrument_id': row['instrument_id'], 'A_selected': row['selected'], 'B_selected': chosen,
                          'discovery_hit': hit, 'discovery_return': outcome['discovery_return'],
                          'execution_status': outcome['execution_status'], 'gross_proxy_return': outcome['open_close_proxy_return']
                          if outcome['execution_status'] in ('proxy_only', 'cost_unset') else None,
                          'result': 'UNKNOWN' if hit is None else 'HIT' if chosen and hit else 'FALSE_POSITIVE' if chosen else 'MISS' if hit else 'NOT_SELECTED'}
                subset.append(sample)
            metrics[version_id] = {'A': _stats(subset, 'A_selected'), 'B': _stats(subset, 'B_selected')}
            rows.extend(subset)
        directory = 'outputs/self_improvement/' + day['day_id'] + '/evaluation'
        payload = {'day_id': day['day_id'], 'source_session': day['session'], 'outcome_session': next_day,
                   'evaluation_id': entry['evaluation_id'], 'evidence': evidence, 'metrics': metrics,
                   'development_eligible': not protected(root, day['session']) and not protected(root, next_day),
                   'data_grade': day['data_grade'], 'directory': directory, 'artifacts': {}}
        out = root / directory
        write_json(out / 'outcomes.json', rows)
        write_csv(out / 'outcomes.csv', list(rows[0]) if rows else ['version_id', 'instrument_id', 'result'], rows)
        write_json(out / 'metrics.json', metrics)
        payload['artifacts'] = {name: digest((out / name).read_bytes()) for name in ('outcomes.json', 'outcomes.csv', 'metrics.json')}
        _put(con, 'feedback', day['day_id'], payload)
        completed.append(payload)
    return completed


def feedback(root: Path, entries: list[dict], session: str) -> list[dict]:
    with exclusive(root / 'data/self_improvement/.lock'), _db(root) as con:
        return _feedback(root, con, entries, session)


def learning_facts(root: Path, session: str) -> dict:
    with _db(root) as con:
        result = []
        for item in _all(con, 'feedback'):
            if item['outcome_session'] <= session and item['development_eligible'] and not protected(root, item['source_session']) and not protected(root, item['outcome_session']):
                _verify_files(root / item['directory'], item)
                result.append({'source_session': item['source_session'], 'outcome_session': item['outcome_session'],
                               'metrics': item['metrics'], 'evidence_hash': digest(canonical(item))})
        recent = sorted(result, key=lambda x: x['source_session'])[-10:]
        versions = sorted([p for p in _all(con, 'version') if p['source_session'] <= session], key=lambda p: (p['created_at'], p['version_id']))[-8:]
        aggregated = {}
        for version in versions:
            key = version['version_id']
            metrics = [r['metrics'][key] for r in recent if key in r['metrics']]
            if metrics:
                aggregated[key] = {arm: {field: sum(m[arm][field] for m in metrics) for field in ('selected', 'valid', 'hits', 'false_positives', 'misses', 'unknown', 'universe_hits')} for arm in ('A', 'B')}
        return {'version': VERSION, 'cutoff_session': session, 'feedback': aggregated,
                'source_days': [{k: r[k] for k in ('source_session', 'outcome_session', 'evidence_hash')} for r in recent],
                'hypotheses': [{'version_id': p['version_id'], 'condition_ids': p['condition_ids']} for p in versions],
                'note': 'development feedback only; unknown is not negative; hit-rate and capture are both required'}


def _plans(root, path, manifest):
    from .retrospective import _verify
    from .reporting import json_content
    if protected(root, manifest['target_session']):
        return [], None, 'PROTECTED_HOLDOUT'
    if path is None:
        return [], None, 'NO_HYPOTHESIS_EVIDENCE'
    report = read_json(path)
    _, study, _ = _verify(root, report['study_id'])
    if report['data_grade'] != manifest['data_grade'] or study['ranking_session'] != manifest['target_session']:
        raise ValueError('proposal source day/grade mismatch')
    if protected(root, study['ranking_session']) or protected(root, study['feature_session']):
        return [], None, 'PROTECTED_HOLDOUT'
    raw_cache = root / 'data/qwen_cache' / (report['cache_key'] + '.json')
    cache = read_json(raw_cache)
    if digest(canonical(cache['identity'])) != report['cache_key'] or cache['identity']['facts_hash'] != digest(canonical(cache['facts'])):
        raise RuntimeError('Qwen proposal cache hash mismatch')
    output = root / 'outputs/retrospectives' / report['study_id']
    if cache['identity']['manifest_hash'] != digest((output / 'study_manifest.json').read_bytes()):
        raise RuntimeError('proposal study binding mismatch')
    response = cache['response']
    if response['model'] != report['model_id'] or cache['identity']['model_revision'] != report['model_revision']:
        raise ValueError('proposal model mismatch')
    plans = validate_plans(json_content(response['choices'][0]['message']['content']), report['study_id'], report['data_grade'])
    if plans != report['hypothesis_plans'] or report.get('structure_validated') is not True:
        raise RuntimeError('proposal plans differ from saved model response')
    analysis = cache['facts'].get('pre_hypothesis_analysis')
    if not analysis or report.get('inference') != analysis['reference'] or cache['identity'].get('analysis_manifest_hash') != analysis['reference']['manifest_hash']:
        raise ValueError('preceding verified analysis required for a new hypothesis generation')
    from .inference import verify as verify_inference, bind_plans
    inferred = verify_inference(root, analysis['reference'], cache['facts'], read_json(output / 'comparison.json')['groups'], report['model_id'], report['model_revision'], cache['identity']['runtime_config'])
    if inferred['summary'] != analysis['summary']:
        raise RuntimeError('proposal analytical summary binding mismatch')
    if parse_time(inferred['created_at']) > parse_time(now_iso()):
        raise ValueError('analysis was not available at hypothesis time')
    bind_plans(plans, inferred['summary'])
    if cache['facts']['ranking_session'] != manifest['target_session'] or cache['facts']['feature_session'] != study['feature_session']:
        raise ValueError('proposal contains wrong-time evidence')
    source = {'report_path': str(path.resolve()), 'report_hash': digest(path.read_bytes()),
              'cache_hash': digest(raw_cache.read_bytes()), 'study_id': report['study_id'], 'study': study,
              'learning_feedback': cache['facts'].get('learning_feedback'), 'inference': analysis['reference']}
    return plans, source, 'VALIDATED'


def _register(root, con, version, policy):
    from .research import DEFAULT_CRITERIA, _criteria, register
    existing = _get(con, 'registration', version['version_id'])
    if existing:
        from .research import _load
        _load(root, existing['experiment_id'])
        return existing
    if digest(Path(version['source_report']).read_bytes()) != version['source']['report_hash']:
        raise RuntimeError('sealed proposal changed before registration')
    manifest, snapshot = _manifest(root, version['source_run_id'])
    calendar = snapshot['data']['calendar']
    cutoff = max(version['source_session'], parse_time(version['created_at']).astimezone(JST).date().isoformat())
    if version['data_grade'] != 'synthetic':
        cutoff = max(cutoff, parse_time(now_iso()).astimezone(JST).date().isoformat())
    need = policy['tuning_sessions'] + policy['holdout_sessions'] + 5
    if len([d for d in calendar if d > cutoff]) < need and version['data_grade'] != 'synthetic':
        import exchange_calendars as xc
        day = date.fromisoformat(cutoff)
        cal = xc.get_calendar('XTKS', start=calendar[0], end=(day + timedelta(days=365)).isoformat())
        calendar = [d.strftime('%Y-%m-%d') for d in cal.sessions]
    future = [d for d in calendar if d > cutoff]
    if len(future) < need:
        return {'status': 'WAITING_CALENDAR', 'version_id': version['version_id']}
    directory = root / 'data/self_improvement/plans' / version['version_id']
    write_json(directory / 'baseline.json', manifest['config'])
    write_json(directory / 'calendar.json', calendar)
    # One global bounded vocabulary budget, including existing price trials.
    external = [p for p in (root / 'data/research/experiments').glob('*.json') if not read_json(p)['family_id'].startswith('LOOP-')]
    criteria = {**DEFAULT_CRITERIA, **policy['criteria'], 'k_per_stage': manifest['config']['max_candidates_per_stage'],
                'max_trials': policy['max_versions'] + len(external)}
    _criteria(criteria, manifest['config'])
    tuning = policy['tuning_sessions']
    plan = {'schema_version': 1, 'hypothesis_id': version['hypothesis_id'], 'version': 1,
            'family_id': 'LOOP-' + version['version_id'], 'mode': 'synthetic' if version['data_grade'] == 'synthetic' else 'prospective' if version['data_grade'] in ('observed', 'vendor_pit') else 'research',
            'periods': {'discovery': {'start': version['feature_session'], 'end': version['source_session']},
                        'tuning': {'start': future[0], 'end': future[tuning - 1]},
                        'holdout': {'start': future[tuning], 'end': future[tuning + policy['holdout_sessions'] - 1]}},
            'baseline_config_path': str((directory / 'baseline.json').resolve()), 'calendar_path': str((directory / 'calendar.json').resolve()),
            'condition_ids': version['condition_ids'], 'reason_code': version['reason_code'], 'criteria': criteria,
            'proposal_ref': version['source_report']}
    write_json(directory / 'plan.json', plan)
    spec = register(root, directory / 'plan.json')
    result = {'status': 'REGISTERED', 'version_id': version['version_id'], 'experiment_id': spec['experiment_id']}
    _put(con, 'registration', version['version_id'], result)
    return result


def day(root: Path, run_id: str, policy_path: Path, report_path: Path | None = None) -> dict:
    policy = settings(policy_path)
    if policy['mode'] == 'simulation' and not (root / 'data/self_improvement/SIMULATION.json').is_file():
        raise ValueError('simulation requires an explicitly isolated demo root')
    with exclusive(root / 'data/self_improvement/.lock'), _db(root) as con:
        manifest, snapshot = _manifest(root, run_id)
        session = manifest['target_session']
        saved = _get(con, 'day', session)
        intent = _get(con, 'intent', session)
        if intent and (intent['run_id'] != run_id or intent['policy'] != policy):
            raise ValueError('daily input and policy already sealed; cannot substitute')
        if intent and intent['loop_engine_hash'] != engine_hash():
            raise RuntimeError('loop code changed; resume with sealed source revision')
        if saved:
            _verify_files(root / saved['directory'], saved)
            return saved
        if not intent:
            previous = _all(con, 'intent')
            if any(p['session'] > session for p in previous):
                raise ValueError('cannot backfill a past hypothesis forecast')
            plans, source, proposal_status = _plans(root, report_path, manifest)
            calendar = snapshot['data']['calendar']
            following = next((d for d in calendar if d > session), None)
            if following is None:
                raise ValueError('next trading session missing')
            at = now_iso()
            eligible = not manifest['history_only'] and parse_time(at) < parse_time(following + 'T09:00:00+09:00') and parse_time(at) >= parse_time(manifest['completed_at'])
            versions = _all(con, 'version')
            new = []
            for plan in plans[:policy['max_new_per_day']]:
                key = 'hv-' + stable_id(CATALOG_VERSION, plan['condition_ids'], manifest['config_hash'], manifest['code_hash'], engine_hash())
                if any(v['version_id'] == key for v in versions) or len(versions) >= policy['max_versions']:
                    continue
                version = {**plan, 'version_id': key, 'created_at': at, 'source_session': session,
                           'feature_session': source['study']['feature_session'], 'source_run_id': run_id,
                           'source_report': source['report_path'], 'data_grade': manifest['data_grade'],
                           'baseline_config_hash': manifest['config_hash'], 'numerical_code_hash': manifest['code_hash'],
                           'loop_engine_hash': engine_hash(),
                           'code_bundle_id': archive_code(root), 'policy': policy, 'source': source}
                _put(con, 'version', key, version)
                versions.append(version)
                new.append(key)
            active = _state(con, policy['mode'])
            if active['version_id'] and active['version_id'] not in [v['version_id'] for v in versions if v['baseline_config_hash'] == manifest['config_hash'] and v['numerical_code_hash'] == manifest['code_hash'] and v['loop_engine_hash'] == engine_hash()]:
                _rollback(con, policy['mode'], 'active_baseline_config_or_code_changed')
                active = _state(con, policy['mode'])
            intent = {'day_id': 'hd-' + stable_id(run_id, policy, VERSION), 'session': session, 'forecast_session': following,
                      'run_id': run_id, 'manifest_hash': digest((root / 'outputs' / run_id / 'run_manifest.json').read_bytes()),
                      'loop_engine_hash': engine_hash(),
                      'data_grade': manifest['data_grade'], 'created_at': at, 'forecast_eligible': eligible,
                      'policy': policy, 'new_versions': new, 'active': active, 'proposal_status': proposal_status,
                      'version_ids': [v['version_id'] for v in versions if v['baseline_config_hash'] == manifest['config_hash'] and v['loop_engine_hash'] == engine_hash()],
                      'source': source, 'feedback_hash': digest(canonical(_all(con, 'feedback')))}
            _put(con, 'intent', session, intent)
            con.commit()  # Freeze choices BEFORE writing candidate files or registering experiments.
        if intent['manifest_hash'] != digest((root / 'outputs' / run_id / 'run_manifest.json').read_bytes()):
            raise RuntimeError('forecast baseline manifest changed')
        decisions = read_json(root / 'outputs' / run_id / 'decisions.json')
        selections, rows = {}, []
        for version_id in intent['version_ids']:
            version = _get(con, 'version', version_id)
            verify_code(root, version['code_bundle_id'])
            chosen = []
            for decision in decisions:
                passes, reasons = _matches(decision, version)
                selected = bool(decision['selected'] and passes)
                if selected:
                    chosen.append(decision['instrument_id'])
                rows.append({'version_id': version_id, 'hypothesis_id': version['hypothesis_id'],
                             'session': session, 'forecast_session': intent['forecast_session'], 'instrument_id': decision['instrument_id'],
                             'selected': selected, 'baseline_selected': decision['selected'], 'stage': decision['stage'],
                             'reasons': reasons if decision['selected'] else ['not_baseline_candidate'] + reasons,
                             'data_grade': intent['data_grade'], 'forecast_eligible': intent['forecast_eligible']})
            selections[version_id] = sorted(chosen)
        directory = 'outputs/self_improvement/' + intent['day_id']
        out = root / directory
        fields = list(rows[0]) if rows else ['version_id', 'instrument_id', 'selected', 'reasons']
        write_csv(out / 'decisions.csv', fields, rows)
        write_csv(out / 'candidates.csv', fields, [r for r in rows if r['selected']])
        write_json(out / 'selections.json', selections)
        active_rows = [r for r in rows if r['version_id'] == intent['active']['version_id'] and r['selected']]
        write_csv(out / 'active_candidates.csv', fields, active_rows)
        registrations = [_register(root, con, _get(con, 'version', key), intent['policy']) for key in intent['version_ids']]
        completed_at = now_iso()
        eligible = intent['forecast_eligible'] and parse_time(completed_at) < parse_time(intent['forecast_session'] + 'T09:00:00+09:00')
        if eligible != intent['forecast_eligible']:
            for row in rows:
                row['forecast_eligible'] = eligible
            write_csv(out / 'decisions.csv', fields, rows)
            write_csv(out / 'candidates.csv', fields, [r for r in rows if r['selected']])
            write_csv(out / 'active_candidates.csv', fields, active_rows)
        result = {**intent, 'forecast_eligible': eligible, 'completed_at': completed_at,
                  'status': 'SHADOW_SAVED', 'directory': directory, 'candidate_csv': str(out / 'candidates.csv'),
                  'operational_candidate_csv': str(out / 'active_candidates.csv') if intent['active']['version_id'] and eligible else str(root / 'outputs' / run_id / 'candidates.csv'),
                  'registrations': registrations, 'active_count': len(active_rows), 'shadow_count': sum(r['selected'] for r in rows),
                  'artifacts': {name: digest((out / name).read_bytes()) for name in ('decisions.csv', 'candidates.csv', 'selections.json', 'active_candidates.csv')}}
        _put(con, 'day', session, result)
        write_json(out / 'manifest.json', result)
        return result


def _certificate(root, experiment_id):
    from .research import _load, _store, _verify_report
    spec = _load(root, experiment_id)
    con = _store(root)
    try:
        row = con.execute('SELECT report_id FROM m5_reports WHERE experiment_id=?', (experiment_id,)).fetchone()
        if not row:
            raise ValueError('completed frozen M5 comparison required')
        directory = root / 'outputs/research' / experiment_id / row[0]
        summary = _verify_report(directory, con)
    finally:
        con.close()
    verify_code(root, spec['code_bundle_id'])
    if spec['numerical_code_hash'] != _code_hash():
        raise ValueError('release numerical engine changed')
    return spec, summary, digest((directory / 'manifest.json').read_bytes())


def _human_policy_approved(con, policy):
    for event in _events(con):
        if event['mode'] == policy['mode'] and event['action'] == 'ACTIVATE' and event['details']['human_reviewed']:
            release = _get(con, 'release', event['details']['release_id'])
            if release and release['policy'] == policy:
                return True
    return False


def _activate(root, con, experiment_id, policy, reviewer, human_reviewed, automatic):
    spec, summary, report_hash = _certificate(root, experiment_id)
    simulation = policy['mode'] == 'simulation'
    if simulation:
        if spec['mode'] != 'synthetic' or not (root / 'data/self_improvement/SIMULATION.json').is_file() or summary['statistical_decision'] != 'ADOPTION_CANDIDATE':
            raise ValueError('isolated synthetic accepted comparison required')
    elif spec['mode'] != 'prospective' or summary['recommendation'] != 'ADOPTION_CANDIDATE':
        raise ValueError('research/synthetic/HOLD cannot activate production')
    versions = [v for v in _all(con, 'version') if v['condition_ids'] == spec['condition_ids'] and v['baseline_config_hash'] == spec['baseline_config_hash']]
    if len(versions) != 1:
        raise ValueError('one matching sealed hypothesis generation required')
    version = versions[0]
    if version['loop_engine_hash'] != engine_hash():
        raise ValueError('release loop engine changed; validate a new generation')
    registration = _get(con, 'registration', version['version_id'])
    if not registration or registration['experiment_id'] != experiment_id or version['policy'] != policy:
        raise ValueError('release requires the generation own frozen experiment and policy')
    holdout = spec['periods']['holdout']
    expected_days = [d for d in spec['calendar'] if holdout['start'] <= d <= holdout['end']]
    for target in expected_days:
        forecast = _get(con, 'day', target)
        if not forecast or not forecast['forecast_eligible'] or version['version_id'] not in forecast['version_ids'] or forecast['loop_engine_hash'] != version['loop_engine_hash']:
            raise ValueError('complete timely sealed hypothesis forecasts required for holdout')
        _verify_files(root / forecast['directory'], forecast)
        checked, _ = _manifest(root, forecast['run_id'])
        if checked['config_hash'] != spec['baseline_config_hash'] or checked['code_hash'] != spec['numerical_code_hash']:
            raise ValueError('holdout shadow baseline differs from certificate')
    state = _state(con, policy['mode'])
    if state['version_id'] == version['version_id']:
        return state
    history = [e for e in _events(con) if e['mode'] == policy['mode']]
    if any(e['action'] == 'ACTIVATE' and e['after']['version_id'] == version['version_id'] for e in history):
        raise ValueError('previously activated generation cannot oscillate back automatically')
    if any(e['action'] == 'ROLLBACK' and e['before']['version_id'] == version['version_id'] for e in history):
        raise ValueError('retired generation requires new evidence and generation; cannot reactivate')
    if automatic:
        if not policy['automatic_promotion'] or not _human_policy_approved(con, policy):
            raise ValueError('first ACTIVE under each frozen policy requires explicit human review')
    elif human_reviewed is not True or not reviewer or not reviewer.strip():
        raise ValueError('explicit human review and reviewer required')
    release_id = 'lr-' + stable_id(experiment_id, report_hash, version['version_id'], policy)
    release = {'release_id': release_id, 'version_id': version['version_id'], 'experiment_id': experiment_id,
               'report_hash': report_hash, 'previous': state, 'policy': policy, 'activated_at': now_iso(),
               'data_mode': 'synthetic_simulation' if simulation else 'prospective'}
    _put(con, 'release', release_id, release)
    after = {k: release[k] for k in ('version_id', 'release_id', 'activated_at')}
    _event(con, policy['mode'], 'ACTIVATE', after, {'release_id': release_id, 'human_reviewed': human_reviewed,
                                                'reviewer': reviewer, 'automatic': automatic, 'report_hash': report_hash})
    return after


def activate(root: Path, experiment_id: str, policy_path: Path, *, reviewer=None, human_reviewed=False, automatic=False):
    policy = settings(policy_path)
    with exclusive(root / 'data/self_improvement/.lock'), _db(root) as con:
        return _activate(root, con, experiment_id, policy, reviewer, human_reviewed, automatic)


def _rollback(con, mode, reason):
    state = _state(con, mode)
    if not state['release_id']:
        return {'status': 'BASELINE_ALREADY_ACTIVE', 'state': state}
    release = _get(con, 'release', state['release_id'])
    event = _event(con, mode, 'ROLLBACK', release['previous'], {'reason': reason})
    return {'status': 'ROLLED_BACK', 'state': event['after'], 'reason': reason}


def rollback(root: Path, policy_path: Path, reason: str):
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('rollback reason required')
    policy = settings(policy_path)
    with exclusive(root / 'data/self_improvement/.lock'), _db(root) as con:
        return _rollback(con, policy['mode'], reason)


def monitor(root: Path, policy_path: Path, session: str):
    policy = settings(policy_path)
    with exclusive(root / 'data/self_improvement/.lock'), _db(root) as con:
        state = _state(con, policy['mode'])
        if not state['release_id']:
            return {'status': 'BASELINE_ACTIVE'}
        release = _get(con, 'release', state['release_id'])
        if release['policy'] != policy:
            raise ValueError('active monitoring policy is frozen')
        items = []
        for f in _all(con, 'feedback'):
            day_value = _get(con, 'day', f['source_session'])
            if f['outcome_session'] <= session and day_value['forecast_eligible'] and day_value['active']['release_id'] == state['release_id'] and state['version_id'] in f['metrics']:
                _verify_files(root / f['directory'], f)
                items.append(f)
        items = sorted(items, key=lambda x: x['source_session'])[-policy['monitor']['window_days']:]
        a, b = {}, {}
        for arm, target in (('A', a), ('B', b)):
            for key in ('selected', 'valid', 'hits', 'unknown', 'universe_hits'):
                target[key] = sum(f['metrics'][state['version_id']][arm][key] for f in items)
        if len(items) < policy['monitor']['min_days'] or b['selected'] < policy['monitor']['min_selected']:
            return {'status': 'WAITING_MONITOR_SAMPLES', 'days': len(items)}
        reasons = []
        if b['unknown'] / b['selected'] > policy['monitor']['max_unknown_rate']:
            reasons.append('unknown_rate_exceeded')
        if a['valid'] and b['valid'] and b['hits'] / b['valid'] < a['hits'] / a['valid'] - policy['monitor']['max_hit_rate_drop']:
            reasons.append('hit_rate_deteriorated')
        if a['universe_hits'] and b['hits'] / a['universe_hits'] < a['hits'] / a['universe_hits'] - policy['monitor']['max_capture_drop']:
            reasons.append('capture_deteriorated')
        if reasons:
            return _rollback(con, policy['mode'], ','.join(reasons))
        return {'status': 'MONITOR_OK', 'days': len(items), 'A': a, 'B': b}


def promote_ready(root: Path, policy_path: Path):
    policy = settings(policy_path)
    results = []
    with exclusive(root / 'data/self_improvement/.lock'), _db(root) as con:
        for registration in _all(con, 'registration'):
            try:
                spec, summary, _ = _certificate(root, registration['experiment_id'])
                if (summary['recommendation'] == 'ADOPTION_CANDIDATE' or policy['mode'] == 'simulation' and summary['statistical_decision'] == 'ADOPTION_CANDIDATE'):
                    results.append({'experiment_id': spec['experiment_id'], 'state': _activate(root, con, spec['experiment_id'], policy, None, False, True)})
                    break  # At most one transition per tick; accepted old versions never oscillate.
            except ValueError as error:
                results.append({'experiment_id': registration['experiment_id'], 'status': 'HOLD', 'reason': str(error)})
    return {'status': 'CHECKED', 'results': results}


def status(root: Path):
    with _db(root) as con:
        return {'version': VERSION, 'production': _state(con, 'shadow'), 'simulation': _state(con, 'simulation'),
                'versions': _all(con, 'version'), 'days': _all(con, 'day'), 'feedback': _all(con, 'feedback'),
                'registrations': _all(con, 'registration'), 'history': _events(con)}
