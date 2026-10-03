"""Deploy the isolated tested additive module only while native daily work is idle."""
import json
import re
import sqlite3
from pathlib import Path

from alpha_loop.common import atomic_bytes, digest, now_iso, read_json, write_json
from alpha_loop.material_research import engine_hash as material_hash
from alpha_loop.pipeline import _code_hash
from alpha_loop.provenance import archive_code, verify_code
from alpha_loop.research import engine_hash as price_hash
from alpha_loop.runtime import exclusive, status

PROJECT = Path(__file__).resolve().parents[1]


def main():
    before = read_json(PROJECT / 'data/operations/material_m5_deployment.json')
    acceptance = read_json(PROJECT / 'data/operations/long_text_acceptance.json')
    log = (PROJECT / 'data/operations/long_text_regression_stderr.log').read_text(encoding='utf-8-sig')
    match = re.search(r'Ran (\d+) tests in ([\d.]+)s', log)
    if not match or not log.rstrip().endswith('OK') or int(match[1]) != 184:
        raise RuntimeError('expected full regression result missing')
    with exclusive(PROJECT / 'data/operations/.supervisor.lock'):
        state = status(PROJECT)
        if state['running'] or state['owner_unknown']:
            raise RuntimeError('daily work still active; source deployment refused')
        checks = dict(price_code_unchanged=_code_hash() == before['price_code_hash'],
                      price_m5_unchanged=price_hash() == before['price_m5_engine_hash'],
                      material_m5_unchanged=material_hash() == before['material_engine_hash'],
                      stdlib_synthetic_success=acceptance['site_packages_disabled'] and acceptance['saved_response_reused'])
        baseline = read_json(PROJECT / 'data/operations/material_m5_acceptance.json')
        checks['baseline_unchanged'] = digest((PROJECT / 'configs/baseline.json').read_bytes()) == baseline['protected_hashes']['baseline']
        con = sqlite3.connect((PROJECT / 'data/state.sqlite').as_uri() + '?mode=ro', uri=True)
        try:
            registrations = con.execute('SELECT experiment_id,registration_hash FROM m5_experiments ORDER BY experiment_id').fetchall()
        finally:
            con.close()
        checks['registered_experiments_unchanged'] = [r[0] for r in registrations] == before['registered_price_experiments']
        for eid, sha in registrations:
            file = PROJECT / 'data/research/experiments' / (eid + '.json')
            if digest(file.read_bytes()) != sha:
                raise RuntimeError('price registration changed')
            verify_code(PROJECT, read_json(file)['code_bundle_id'])
        installed = Path.home() / 'AppData/Local/hermes'
        jobs = read_json(installed / 'cron/jobs.json')
        job = next(j for j in jobs['jobs'] if j['id'] == before['job_id'])
        checks['daily_schedule_unchanged'] = job['enabled'] is True and job['no_agent'] is True and job['schedule']['expr'] == '0 20 * * 1-5'
        checks['installed_wrapper_unchanged'] = digest((installed / 'scripts/alpha_loop_daily.py').read_bytes()) == digest((PROJECT / 'integrations/hermes/alpha_loop_daily.py').read_bytes())
        if not all(checks.values()):
            raise RuntimeError('deployment checks failed: ' + json.dumps(checks))
        raw = (PROJECT / 'data/development/long_text/src/alpha_loop/material_long_text.py').read_bytes()
        target = PROJECT / 'src/alpha_loop/material_long_text.py'
        if target.exists() and target.read_bytes() != raw:
            raise RuntimeError('unexpected existing long-text source; inspect before editing')
        atomic_bytes(target, raw)
        proof = dict(verified_at=now_iso(), checks=checks, tests=int(match[1]), test_seconds=float(match[2]),
                     module_hash=digest(raw), code_bundle_id=archive_code(PROJECT),
                     registered_price_experiments=[r[0] for r in registrations],
                     live_daily_integration_added=False, external_ai_enabled=False, orders_enabled=False)
        write_json(PROJECT / 'data/operations/long_text_deployment.json', proof)
    print(json.dumps(proof, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
