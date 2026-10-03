"""Record live wiring and protected price experiments without running market collection."""
import json
import re
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from alpha_loop.common import canonical, digest, now_iso, read_json, write_json
from alpha_loop.material_research import engine_hash as material_hash
from alpha_loop.pipeline import _code_hash, _verified_manifest, config_load
from alpha_loop.provenance import archive_code, verify_code
from alpha_loop.research import engine_hash as price_hash
from alpha_loop.runtime import status


def main():
    previous = read_json(PROJECT / 'data/operations/remaining_deployment.json')
    acceptance = read_json(PROJECT / 'data/operations/material_m5_acceptance.json')
    installed = Path.home() / 'AppData/Local/hermes'
    saved_jobs = read_json(installed / 'cron/jobs.json')
    jobs = saved_jobs.get('jobs', []) if isinstance(saved_jobs, dict) else saved_jobs
    job = next(j for j in jobs if j['id'] == previous['job_id'])
    checks = {'price_code_unchanged': _code_hash() == previous['numerical_code_hash'],
              'price_m5_engine_unchanged': price_hash() == previous['m5_engine_hash'],
              'installed_wrapper_matches': digest((installed / 'scripts/alpha_loop_daily.py').read_bytes()) == digest((PROJECT / 'integrations/hermes/alpha_loop_daily.py').read_bytes()),
              'existing_daily_schedule_unchanged': job['enabled'] is True and job['no_agent'] is True and job['schedule']['expr'] == '0 20 * * 1-5'}
    manifest = _verified_manifest(PROJECT / 'outputs' / previous['real_run_id'])
    baseline_hash = digest(canonical(config_load(PROJECT / 'configs/baseline.json')))
    checks['baseline_unchanged'] = baseline_hash == manifest['config_hash'] and digest((PROJECT / 'configs/baseline.json').read_bytes()) == acceptance['protected_hashes']['baseline']
    checks['real_artifacts_verified'] = True
    con = sqlite3.connect((PROJECT / 'data/state.sqlite').as_uri() + '?mode=ro', uri=True)
    try:
        rows = con.execute('SELECT experiment_id,registration_hash FROM m5_experiments ORDER BY experiment_id').fetchall()
        checks['registered_price_experiments_unchanged'] = [r[0] for r in rows] == previous['registered_experiments']
        for experiment_id, expected in rows:
            path = PROJECT / 'data/research/experiments' / (experiment_id + '.json')
            if digest(path.read_bytes()) != expected:
                raise RuntimeError('price experiment registration changed')
            spec = read_json(path)
            verify_code(PROJECT, spec['code_bundle_id'])
            if spec['engine_hash'] != price_hash():
                raise RuntimeError('registered price engine changed')
    finally:
        con.close()
    state = status(PROJECT)
    checks['no_active_live_batch'] = not state['running'] and not state['owner_unknown']
    log = (PROJECT / 'data/operations/material_m5_regression_stderr.log').read_text(encoding='utf-8-sig')
    count = re.search(r'Ran (\d+) tests in ([\d.]+)s', log)
    checks['tests_passed'] = bool(count and log.rstrip().endswith('OK'))
    checks['stdlib_synthetic_acceptance_passed'] = acceptance['comparison']['status'] == 'COMPLETE' and acceptance['unchanged_price_code_and_baseline']
    wrapper = read_json(PROJECT / 'data/operations/material_m5_wrapper_acceptance.json')
    checks['native_wrapper_synthetic_passed'] = wrapper['status'] == 'SUCCEEDED'
    if not all(checks.values()):
        raise RuntimeError('deployment verification failed: ' + json.dumps(checks))
    proof = {'verified_at': now_iso(), 'checks': checks, 'job_id': job['id'], 'next_run_at': job['next_run_at'],
             'registered_price_experiments': [r[0] for r in rows], 'price_code_hash': _code_hash(),
             'price_m5_engine_hash': price_hash(), 'material_engine_hash': material_hash(), 'code_bundle_id': archive_code(PROJECT),
             'tests': int(count[1]), 'test_seconds': float(count[2]), 'test_log_hash': digest(log.encode('utf-8')),
             'material_performance_validated': False, 'material_active_enabled': False, 'new_live_scheduled_run_verified': False,
             'real_run_id': manifest['run_id'], 'real_artifact_hashes': manifest['artifacts']}
    write_json(PROJECT / 'data/operations/material_m5_deployment.json', proof)
    print(json.dumps(proof, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
