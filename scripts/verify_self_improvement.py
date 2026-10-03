"""Run the complete authored self-improvement demo without API/GPU/packages."""
import json
import sys
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from alpha_loop.common import write_json
from alpha_loop.self_improvement_fixture import demo

if __name__ == '__main__':
    proof = demo(PROJECT / 'data/si' / uuid.uuid4().hex[:8], PROJECT / 'configs/baseline.json', PROJECT / 'configs/self_improvement.json')
    proof['site_packages_disabled'] = bool(sys.flags.no_site)
    write_json(PROJECT / 'data/operations/self_improvement_acceptance.json', proof)
    print(json.dumps(proof, ensure_ascii=False, indent=2))
