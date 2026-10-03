"""API/GPU-free complete material comparison, always explicitly synthetic."""
from pathlib import Path
from unittest.mock import patch
from .common import canonical, digest, read_json, write_json
from .material_fixture import CASES, FixtureMaterialProvider
from .materials import VERSION, batch, ingest
from .material_research import DEFAULT_POLICY, compare, register
from .research_fixture import create_research_fixture


def create(root: Path, baseline_path: Path, question_path: Path):
    if (root / 'data/state.sqlite').exists() or (root / 'outputs').exists():
        raise ValueError('use a new isolated synthetic output directory')
    price = create_research_fixture(root, baseline_path)
    write_json(root / 'questions.json', read_json(question_path))
    price_plan = read_json(root / 'plan.json')
    provider = FixtureMaterialProvider()
    identity = {'version': VERSION, 'extractor_version': 'utf8-paragraphs-v2',
                'question_hash': digest(canonical(read_json(root / 'questions.json'))), 'evidence_policy': 'source-paragraph-v2',
                'provider': provider.provider_id, 'model': provider.model_id, 'revision': provider.model_revision,
                'server_commit': provider.server_commit, 'quantization': provider.quantization_id,
                'runtime': getattr(provider, 'runtime_config', None)}
    plan = {'schema_version': 1, 'hypothesis_id': 'H-MATERIAL-SYNTHETIC', 'family_id': 'F-MATERIAL-SYNTHETIC',
            'version': 1, 'mode': 'synthetic', 'periods': price_plan['periods'], 'baseline_config_path': 'baseline.json',
            'calendar_path': 'calendar.json', 'question_set_path': 'questions.json', 'model_identity': identity,
            'max_documents': 100, 'policy': DEFAULT_POLICY, 'criteria': price_plan['criteria'], 'quality_evidence': None,
            'thesis': 'Synthetic formal-contract filter and separate material augmentation; no performance evidence'}
    write_json(root / 'material_plan.json', plan)
    entries = read_json(root / 'batch.json')['entries']
    for entry in entries:
        run = read_json(root / 'outputs' / entry['run_id'] / 'run_manifest.json')
        day = run['target_session']
        directory = root / 'material_input' / day
        documents = []
        for i in range(1, 23):
            directory.mkdir(parents=True, exist_ok=True)
            positive = i <= 5 or 11 <= i <= 15 or i == 21
            text = f'合成公表日{day} 発行者{i:04d}。' + CASES['positive' if positive else 'negative'][0]
            (directory / f'{i:04d}.txt').write_text(text, encoding='utf-8')
            documents.append({'file': f'{i:04d}.txt', 'instrument_id': f'TSE:{i:04d}', 'issuer_code': f'{i:04d}',
                              'disclosure_id': f'fixture-{day}-{i:04d}', 'revision_id': '1', 'source_url': 'synthetic://material-comparison',
                              'published_at': day + 'T16:00:00+09:00', 'synthetic_first_seen_at': day + 'T16:01:00+09:00',
                              'permission_confirmed': True, 'permission_reference': 'project authored synthetic text'})
        write_json(directory / 'manifest.json', {'schema_version': 1, 'data_grade': 'synthetic', 'documents': documents})
        ingest(root, directory / 'manifest.json')
        with patch('alpha_loop.materials.now_iso', return_value=day + 'T20:01:00+09:00'):
            batch(root, run['run_id'], provider, root / 'questions.json', max_documents=100)
    spec = register(root, root / 'material_plan.json')
    return {'data_grade': 'synthetic', 'experiment_id': spec['experiment_id'], 'root': str(root),
            'material_plan': str(root / 'material_plan.json'), 'price_fixture': price, 'performance_claim_allowed': False}


def demo(root, baseline_path, question_path):
    result = create(root, baseline_path, question_path)
    result['comparison'] = compare(root, result['experiment_id'])
    write_json(root / 'acceptance.json', result)
    return result
