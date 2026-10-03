"""Isolated native Hermes entry-point check; never run the real-market batch."""
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from alpha_loop.common import digest, read_json, write_json
from alpha_loop.fixture import create


def main():
    installed = Path.home() / 'AppData/Local/hermes/scripts/alpha_loop_daily.py'
    source = PROJECT / 'integrations/hermes/alpha_loop_daily.py'
    if digest(installed.read_bytes()) != digest(source.read_bytes()):
        raise RuntimeError('installed Hermes entry differs from reviewed wrapper')
    root = PROJECT / 'data/material_m5_operation_demo' / uuid.uuid4().hex[:12]
    shutil.copytree(PROJECT / 'src/alpha_loop', root / 'src/alpha_loop', ignore=shutil.ignore_patterns('__pycache__'))
    create(root / 'input', without_turnover=True)
    config = read_json(PROJECT / 'configs/hermes_demo.json')
    config.update(input_dir='input', strategy_config=str(PROJECT / 'configs/baseline.json'),
                  materials_config=None, weekly_research_enabled=False)
    write_json(root / 'configs/operation.json', config)
    env = os.environ.copy()
    env.update(ALPHA_LOOP_ROOT=str(root), ALPHA_LOOP_CONFIG='configs/operation.json', ALPHA_LOOP_NO_AI='1')
    for key in ('ALPHA_LOOP_SESSION', 'ALPHA_LOOP_LIMIT'):
        env.pop(key, None)
    completed = subprocess.run([sys.executable, str(installed)], env=env, capture_output=True,
                               text=True, encoding='utf-8', timeout=120)
    (PROJECT / 'data/operations/material_m5_wrapper_stdout.json').write_text(completed.stdout, encoding='utf-8')
    (PROJECT / 'data/operations/material_m5_wrapper_stderr.log').write_text(completed.stderr, encoding='utf-8')
    if completed.returncode:
        raise RuntimeError('isolated Hermes wrapper failed: ' + completed.stderr[-500:])
    receipt = json.loads(completed.stdout)
    result = receipt['result']
    if result['candidate_count'] != 3 or result['material_m5']['status'] != 'NOT_REGISTERED':
        raise RuntimeError('Hermes material M5 side-output contract failed')
    output = root / 'outputs' / result['run_id']
    proof = {'status': 'SUCCEEDED', 'root': str(root), 'data_grade': 'synthetic',
             'installed_wrapper_matches': True, 'material_m5_status': result['material_m5']['status'],
             'candidate_count': result['candidate_count'], 'candidates_csv': str(output / 'candidates.csv'),
             'candidate_hash': digest((output / 'candidates.csv').read_bytes()),
             'no_ai': True, 'live_job_run_verified': False, 'real_market_quality_validated': False,
             'supervision_id': receipt['supervision_id']}
    write_json(PROJECT / 'data/operations/material_m5_wrapper_acceptance.json', proof)
    print(json.dumps(proof, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
