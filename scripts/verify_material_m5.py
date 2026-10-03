"""Run the complete M5 material fixture using only uv-managed Python's stdlib."""
import json
import sys
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from alpha_loop.common import digest, write_json
from alpha_loop.material_research_fixture import demo
from alpha_loop.pipeline import _code_hash
from alpha_loop.research import engine_hash as price_engine_hash


def main():
    before = {'price_code': _code_hash(), 'price_m5_engine': price_engine_hash(),
              'baseline': digest((PROJECT / 'configs/baseline.json').read_bytes())}
    root = PROJECT / 'data/material_m5_demo' / uuid.uuid4().hex[:12]
    result = demo(root, PROJECT / 'configs/baseline.json', PROJECT / 'configs/questions_v1.json')
    after = {'price_code': _code_hash(), 'price_m5_engine': price_engine_hash(),
             'baseline': digest((PROJECT / 'configs/baseline.json').read_bytes())}
    if before != after:
        raise RuntimeError('baseline/price engine changed during acceptance')
    result.update(unchanged_price_code_and_baseline=True, protected_hashes=after,
                  additional_packages_required=False, network_used=False, gpu_used=False)
    write_json(PROJECT / 'data/operations/material_m5_acceptance.json', result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
