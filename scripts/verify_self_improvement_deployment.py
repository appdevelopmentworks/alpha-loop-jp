"""Deploy the reviewed Hermes entry point and verify it on isolated synthetic input."""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from alpha_loop.common import digest, now_iso, read_json, write_json
from alpha_loop.fixture import create
from alpha_loop.material_research import engine_hash as material_hash
from alpha_loop.pipeline import _code_hash, _verified_manifest
from alpha_loop.provenance import archive_code, verify_code
from alpha_loop.research import engine_hash as price_hash
from alpha_loop.runtime import exclusive, status
from alpha_loop.self_improvement import engine_hash as loop_hash


def main():
    previous = read_json(PROJECT / 'data/operations/material_m5_deployment.json')
    acceptance = read_json(PROJECT / 'data/operations/self_improvement_acceptance.json')
    log = (PROJECT / 'data/operations/self_improvement_regression_stderr.log').read_text(encoding='utf-8-sig')
    count = re.search(r'Ran (\d+) tests in ([\d.]+)s', log)
    if not count or int(count[1]) != 222 or not log.rstrip().endswith('OK'):
        raise RuntimeError('full 222-test regression required before deployment')
    installed = Path.home() / 'AppData/Local/hermes'
    sources = [(PROJECT / 'integrations/hermes/alpha_loop_daily.py', installed / 'scripts/alpha_loop_daily.py'),
               (PROJECT / 'integrations/hermes/alpha-loop-jp/SKILL.md', installed / 'skills/alpha-loop-jp/SKILL.md')]
    # Refuse an explicit override that would silently bypass the new default.
    if os.environ.get('ALPHA_LOOP_CONFIG'):
        raise RuntimeError('inspect explicit ALPHA_LOOP_CONFIG before changing the entry point')
    with exclusive(PROJECT / 'data/operations/.supervisor.lock'):
        live = status(PROJECT)
        if live['running'] or live['owner_unknown']:
            raise RuntimeError('live batch must be idle')
        jobs_path = installed / 'cron/jobs.json'
        jobs_hash = digest(jobs_path.read_bytes())
        job = next(j for j in read_json(jobs_path)['jobs'] if j['id'] == previous['job_id'])
        latest_path = PROJECT / 'data/operations/latest_service.json'
        latest_hash = digest(latest_path.read_bytes())
        manifest = _verified_manifest(PROJECT / 'outputs' / read_json(latest_path)['run_id'])
        checks = {'price_code_unchanged': _code_hash() == previous['price_code_hash'],
                  'price_m5_unchanged': price_hash() == previous['price_m5_engine_hash'],
                  'material_m5_unchanged': material_hash() == previous['material_engine_hash'],
                  'baseline_unchanged': digest((PROJECT / 'configs/baseline.json').read_bytes()) == '73b2d88676d143dc7006247ce05d72d38490958a971eb3ef6bbc67c53994f5c9',
                  'long_module_unchanged': digest((PROJECT / 'src/alpha_loop/material_long_text.py').read_bytes()) == 'f87ecad439fb6ee337fee8ce09621b5cfb6ea44b213d81c364631c83032d28ab',
                  'daily_schedule_unchanged': job['enabled'] is True and job['no_agent'] is True and job['schedule']['expr'] == '0 20 * * 1-5',
                  'synthetic_loop_complete': acceptance['site_packages_disabled'] and acceptance['next_generation_from_feedback'] and acceptance['automatic_rollback']['status'] == 'ROLLED_BACK' and acceptance['production_state']['version_id'] is None}
        old_config = read_json(PROJECT / 'configs/hermes_operations.json')
        new_config = read_json(PROJECT / 'configs/hermes_self_improving.json')
        del new_config['self_improvement_config']
        checks['original_operation_settings_preserved'] = old_config == new_config
        con = sqlite3.connect((PROJECT / 'data/state.sqlite').as_uri() + '?mode=ro', uri=True)
        try:
            rows = con.execute('SELECT experiment_id,registration_hash FROM m5_experiments ORDER BY experiment_id').fetchall()
        finally:
            con.close()
        checks['registered_experiments_unchanged'] = [r[0] for r in rows] == previous['registered_price_experiments']
        for experiment, sha in rows:
            path = PROJECT / 'data/research/experiments' / (experiment + '.json')
            if digest(path.read_bytes()) != sha:
                raise RuntimeError('registered experiment changed')
            verify_code(PROJECT, read_json(path)['code_bundle_id'])
        if not all(checks.values()):
            raise RuntimeError('protected checks failed: ' + json.dumps(checks))
        root = PROJECT / 'data/siw' / uuid.uuid4().hex[:8]
        root.mkdir(parents=True)
        shutil.copytree(PROJECT / 'src/alpha_loop', root / 'src/alpha_loop', ignore=shutil.ignore_patterns('__pycache__'))
        (root / 'configs').mkdir()
        for name in ('baseline.json', 'self_improvement.json'):
            shutil.copy2(PROJECT / 'configs' / name, root / 'configs' / name)
        fixture = create(root / 'input', without_turnover=True)
        # The legacy fixture calendar stops at D; forecasting also needs D+1.
        write_json(root / 'input/calendar.json', read_json(root / 'input/future.json')['calendar'])
        config = read_json(PROJECT / 'configs/hermes_demo.json')
        config.update(input_dir='input', self_improvement_config='configs/self_improvement.json')
        write_json(root / 'configs/operation.json', config)
        env = os.environ.copy()
        env.update(ALPHA_LOOP_ROOT=str(root), ALPHA_LOOP_CONFIG='configs/operation.json', ALPHA_LOOP_NO_AI='1',
                   ALPHA_LOOP_SESSION=fixture['target_session'])
        env.pop('ALPHA_LOOP_LIMIT', None)
        backups = []
        for source, target in sources:
            backup = root / ('old_' + target.name)
            shutil.copy2(target, backup)
            backups.append((backup, target))
        try:
            for source, target in sources:
                shutil.copy2(source, target)
            result = subprocess.run([sys.executable, str(sources[0][1])], cwd=root, env=env, capture_output=True, text=True, encoding='utf-8', timeout=120)
            (root / 'wrapper_stdout.json').write_text(result.stdout, encoding='utf-8')
            (root / 'wrapper_stderr.log').write_text(result.stderr, encoding='utf-8')
            if result.returncode:
                raise RuntimeError('isolated installed wrapper failed: ' + result.stderr[:1000])
            receipt = json.loads(result.stdout)
            daily = receipt['result']
            checks['installed_wrapper_synthetic_success'] = receipt['status'] == 'SUCCEEDED' and daily['candidate_count'] == 3 and daily['self_improvement']['status'] == 'SHADOW_SAVED'
            checks['installed_sources_match'] = all(digest(a.read_bytes()) == digest(b.read_bytes()) for a, b in sources)
            checks['live_receipt_and_jobs_unchanged'] = digest(latest_path.read_bytes()) == latest_hash and digest(jobs_path.read_bytes()) == jobs_hash
            _verified_manifest(PROJECT / 'outputs' / manifest['run_id'])
            if not all(checks.values()):
                raise RuntimeError('installed checks failed: ' + json.dumps(checks))
        except BaseException:
            for backup, target in backups:
                shutil.copy2(backup, target)
            raise
        proof = {'verified_at': now_iso(), 'checks': checks, 'tests': int(count[1]), 'test_seconds': float(count[2]),
                 'loop_engine_hash': loop_hash(), 'code_bundle_id': archive_code(PROJECT), 'wrapper_test_root': str(root),
                 'job_id': job['id'], 'next_run_at': job['next_run_at'], 'real_run_id': manifest['run_id'],
                 'default_config': 'configs/hermes_self_improving.json', 'new_live_scheduled_run_verified': False,
                 'real_market_quality_validated': False, 'external_ai_enabled': False, 'orders_enabled': False}
        write_json(PROJECT / 'data/operations/self_improvement_deployment.json', proof)
    print(json.dumps(proof, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
