"""Exercise saved analysis -> hypotheses -> feedback -> release in synthetic isolation."""
import json
import sys
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from alpha_loop.common import write_json
from alpha_loop.self_improvement_fixture import demo

if __name__ == '__main__':
    proof = demo(PROJECT / 'data/ia' / uuid.uuid4().hex[:8], PROJECT / 'configs/baseline.json', PROJECT / 'configs/self_improvement.json')
    proof['site_packages_disabled'] = bool(sys.flags.no_site)
    proof['analysis_before_hypothesis'] = len(proof['pre_hypothesis_analysis']) == proof['versions']
    write_json(PROJECT / 'data/operations/inference_acceptance.json', proof)
    print(json.dumps(proof, ensure_ascii=False, indent=2))
