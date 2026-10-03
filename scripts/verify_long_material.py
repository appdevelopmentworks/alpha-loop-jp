"""Offline long-text acceptance; use uv-managed Python with PYTHONPATH=src and -S."""
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from alpha_loop.common import digest, now_iso, read_json, write_json
from alpha_loop.fixture import create
from alpha_loop.material_long_text import analyze
from alpha_loop.materials import ingest
from alpha_loop.pipeline import evaluate_run, replay, run
from alpha_loop.semantic import MockSemanticProvider

PROJECT = Path(__file__).resolve().parents[1]


class SyntheticLongProvider(MockSemanticProvider):
    model_id = 'synthetic-long-text-v1'
    def __init__(self):
        super().__init__({})
        self.calls = 0

    def ask(self, state, questions):
        self.calls += 1
        answers = {}
        for key, question in questions.items():
            if question['type'] == 'noul':
                answers[key] = {'noul': int('末尾の訂正' in state)}
            else:
                selected = 'fragment' if 'fragment' in question['criteria'] else next(iter(question['criteria']))
                answers[key] = {'choice': selected, 'confidence': 1,
                                'probabilities': {c: int(c == selected) for c in question['criteria']}}
        return {'model': self.model_id, 'answers': answers}


def main():
    root = PROJECT / 'data/long_text_demo' / uuid.uuid4().hex[:12]
    fixture = create(root / 'input', without_turnover=True)
    config_dir = root / 'configs'
    config_dir.mkdir(parents=True)
    for name in ('baseline.json', 'questions_v1.json', 'materials_operations.json'):
        shutil.copyfile(PROJECT / 'configs' / name, config_dir / name)
    price = run(root, root / 'input', config_dir / 'baseline.json', fixture['target_session'], fixture['as_of'])
    before = digest((root / 'outputs' / price['run_id'] / 'candidates.csv').read_bytes())
    directory = root / 'incoming'
    directory.mkdir()
    text = ('当社の契約は正式締結。本業の製品。\r\n\r\n金額1億円を今期計上する。新株発行は行わない。\r\n' * 400) + '末尾の訂正: 希薄化について再確認が必要。'
    (directory / 'long.txt').write_bytes(text.encode('utf-8-sig'))
    write_json(directory / 'manifest.json', {'schema_version': 1, 'data_grade': 'synthetic', 'documents': [{
        'file': 'long.txt', 'instrument_id': 'TSE:0001', 'issuer_code': '0001', 'disclosure_id': 'authored-long-text',
        'revision_id': '1', 'source_url': 'synthetic://authored-long-text', 'published_at': fixture['target_session'] + 'T16:00:00+09:00',
        'synthetic_first_seen_at': fixture['target_session'] + 'T16:01:00+09:00', 'permission_confirmed': True,
        'permission_reference': 'project authored synthetic long text'}]})
    did = ingest(root, directory / 'manifest.json')['document_ids'][0]
    document = root / 'data/materials/documents' / (did + '.json')
    provider = SyntheticLongProvider()
    analyzed = analyze(root, document, config_dir / 'questions_v1.json', provider)
    calls = provider.calls
    if analyzed != analyze(root, document, config_dir / 'questions_v1.json', provider) or provider.calls != calls:
        raise AssertionError('saved response replay failed')
    if not analyzed['complete_source_coverage'] or analyzed['document_answers'] is not None or analyzed['m5_eligible']:
        raise AssertionError('long-text coverage/aggregation guard failed')
    plan = read_json(Path(analyzed['output_dir']) / 'plan.json')
    if plan['chunks'][-1]['end'] != len(text) or '末尾の訂正' not in plan['chunks'][-1]['text']:
        raise AssertionError('terminal disclosure lost')
    cli = subprocess.run([sys.executable, '-S', str(PROJECT / 'scripts/analyze_long_material.py'),
                          '--root', str(root), '--document-id', did], capture_output=True, text=True,
                         encoding='utf-8', timeout=30, check=True)
    plan_only = json.loads(cli.stdout)
    if plan_only['status'] != 'PLAN_ONLY':
        raise AssertionError('default CLI unexpectedly inferred')
    evaluated = evaluate_run(root, price['run_id'], root / 'input', fixture['next_session'])
    replayed = replay(root, price['run_id'])
    if digest((root / 'outputs' / price['run_id'] / 'candidates.csv').read_bytes()) != before:
        raise AssertionError('price candidates changed')
    result = dict(verified_at=now_iso(), root=str(root), data_grade='synthetic',
                  candidate_csv=str(root / 'outputs' / price['run_id'] / 'candidates.csv'),
                  outcomes_csv=str(Path(evaluated['path']) / 'outcomes.csv'), analysis=analyzed,
                  default_cli=plan_only, saved_response_reused=True, price_candidates_unchanged=True,
                  document_id=did, text_chars=len(text), replay=replayed,
                  site_packages_disabled=bool(sys.flags.no_site), network_used=False, gpu_used=False,
                  market_performance_validated=False)
    write_json(PROJECT / 'data/operations/long_text_acceptance.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
