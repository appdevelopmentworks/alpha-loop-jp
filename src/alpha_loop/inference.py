"""Grounded, bounded analytical summaries before numerical hypothesis proposals."""
from __future__ import annotations

import json
import math
import re
import uuid
from pathlib import Path

from .common import atomic_bytes, canonical, digest, now_iso, read_json, write_json
from .hypothesis_plans import CATALOG, _object
from .provenance import archive_code, verify_code

VERSION = 'pre-hypothesis-analysis-ja-v1'
INTERPRETATIONS = {
    'volume_attention': {'features': ['rel_volume_20d'], 'label': '前日の出来高の差が関心の変化を表し、翌日の急騰識別に役立つ可能性'},
    'high_position': {'features': ['high_distance_20d'], 'label': '前日の高値との位置関係が翌日の急騰識別に役立つ可能性'},
    'recent_overheat': {'features': ['ret_5d'], 'label': '前日までの上昇の進み方が翌日の急騰識別に役立つ可能性'},
    'trend_extension': {'features': ['ma25_gap'], 'label': '前日の移動平均との距離が翌日の急騰識別に役立つ可能性'},
}
ALTERNATIVES = {
    'market_wide_move': '市場全体や業種の変動でも同じ差が生じ得る',
    'small_sample': '少数標本による偶然や銘柄の偏りである可能性',
    'unobserved_material': '取得できていない材料など、別の要因による可能性',
    'selection_bias': '掲載・取得・欠損の選別によって見かけの差が生じた可能性',
}
FALSIFICATIONS = {
    'no_hit_gain': '未使用期間で基準より的中率が改善しなければ説明を支持しない',
    'capture_loss': '候補を絞って捕捉率が悪化するなら運用採用を支持しない',
    'unstable_across_days': '日や銘柄を変えると効果が維持されなければ説明を支持しない',
}


def build_input(facts, groups, manifest_hash):
    evidence = {}
    for feature in sorted({c['feature'] for c in CATALOG.values()}):
        a, b = (groups[g]['feature_medians'][feature] for g in ('listed', 'not_listed'))
        observed = {g: groups[g]['feature_observed_counts'][feature] for g in ('listed', 'not_listed')}
        valid = all(type(v) in (int, float) and math.isfinite(v) for v in (a, b)) and all(v > 0 for v in observed.values())
        evidence['obs:' + feature] = {'kind': 'group_medians', 'feature': feature, 'listed': a, 'not_listed': b,
                                      'observed_counts': observed, 'usable': valid,
                                      'direction': 'unknown' if not valid else 'higher' if a > b else 'lower' if a < b else 'equal'}
    feedback = facts.get('learning_feedback')
    if feedback is not None:
        if feedback['cutoff_session'] != facts['ranking_session'] or any(d['outcome_session'] > facts['ranking_session'] or d['source_session'] >= d['outcome_session'] for d in feedback['source_days']):
            raise ValueError('analysis feedback includes future or wrong-time evidence')
        for version, metrics in feedback['feedback'].items():
            evidence['feedback:' + version] = {'kind': 'development_outcomes', 'metrics': metrics,
                                               'usable': any(m['valid'] > 0 for m in metrics.values()),
                                               'note': 'unknown is not negative; price hits are not trade profit'}
    return {'report_kind': 'hypothesis_analysis', 'analysis_version': VERSION, 'study_id': facts['study_id'],
            'manifest_hash': manifest_hash, 'ranking_session': facts['ranking_session'],
            'feature_session': facts['feature_session'], 'feature_cutoff': facts['feature_cutoff'],
            'data_grade': facts['data_grade'], 'prediction_score_eligible': False,
            'comparison_scope': facts['comparison_scope'], 'evidence_index': evidence,
            'group_sizes': {g: groups[g]['feature_valid_eligible_count'] for g in ('listed', 'not_listed')},
            'limitations': facts['limitations'], 'material_semantics': facts['material_semantics'],
            'interpretation_catalog': INTERPRETATIONS, 'alternative_catalog': ALTERNATIVES,
            'falsification_catalog': FALSIFICATIONS,
            'note': 'associations only; confidence is an uncalibrated model judgement; summaries are not causal proof'}


def validate(content, inputs):
    from .reporting import json_content
    value = json.loads(json_content(content), object_pairs_hook=_object,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite analysis JSON')))
    if not isinstance(value, dict) or set(value) != {'status', 'analyses'} or value['status'] not in ('ANALYZED', 'INSUFFICIENT_EVIDENCE'):
        raise ValueError('invalid analytical summary schema')
    if not isinstance(value['analyses'], list):
        raise ValueError('analysis list required')
    if value['status'] == 'INSUFFICIENT_EVIDENCE':
        if value['analyses']:
            raise ValueError('insufficient evidence cannot have conclusions')
        return value
    if not 1 <= len(value['analyses']) <= 4:
        raise ValueError('one to four analytical summaries required')
    seen = set()
    for item in value['analyses']:
        if not isinstance(item, dict) or set(item) != {'evidence_ids', 'interpretation_code', 'alternative_codes', 'falsification_codes', 'confidence'}:
            raise ValueError('free text or unknown analysis fields rejected')
        code = item['interpretation_code']
        if not isinstance(code, str) or code not in INTERPRETATIONS or code in seen:
            raise ValueError('unknown or duplicate interpretation')
        seen.add(code)
        for field, catalog, limit in (('evidence_ids', inputs['evidence_index'], 4), ('alternative_codes', ALTERNATIVES, 2), ('falsification_codes', FALSIFICATIONS, 2)):
            ids = item[field]
            if not isinstance(ids, list) or not 1 <= len(ids) <= limit or any(not isinstance(k, str) or k not in catalog for k in ids) or len(set(ids)) != len(ids):
                raise ValueError('invalid analysis references: ' + field)
        if item['confidence'] not in ('low', 'moderate'):
            raise ValueError('unsupported analytical confidence')
        references = [inputs['evidence_index'][k] for k in item['evidence_ids']]
        if any(not r['usable'] for r in references) or not any(r.get('feature') in INTERPRETATIONS[code]['features'] for r in references):
            raise ValueError('interpretation requires observed matching evidence')
    return value


def response_summary(response, inputs, model_id):
    choices = response.get('choices') if isinstance(response, dict) else None
    if not isinstance(response, dict) or response.get('model') != model_id or not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError('invalid analysis model response')
    content = choices[0].get('message', {}).get('content')
    if choices[0].get('finish_reason') != 'stop' or not isinstance(content, str) or not content.strip() or len(content) > 10000:
        raise ValueError('incomplete or invalid analytical summary')
    return validate(content, inputs)


def render(inputs, summary):
    lines = ['# 仮説前の分析・推論要約', '', f"特徴日: {inputs['feature_session']} / ラベル日: {inputs['ranking_session']} / grade: {inputs['data_grade']}",
             '', '数値・観測事実はPython、説明の選択はQwen。以下は未検証の関連であり因果関係ではない。', '']
    for index, item in enumerate(summary['analyses'], 1):
        lines += [f'## 分析{index}', '', '説明: ' + INTERPRETATIONS[item['interpretation_code']]['label'],
                  '確信度: ' + item['confidence'] + '（モデルの判断、確率校正なし）', '']
        for key in item['evidence_ids']:
            lines.append('- 根拠 ' + key + ': ' + json.dumps(inputs['evidence_index'][key], ensure_ascii=False, sort_keys=True))
        lines += ['- 代替説明: ' + ALTERNATIVES[k] for k in item['alternative_codes']]
        lines += ['- 反証条件: ' + FALSIFICATIONS[k] for k in item['falsification_codes']]
        lines.append('')
    if not summary['analyses']:
        lines.append('根拠不足。新しい仮説を生成しない。')
    return '\n'.join(lines) + '\n'


def analyze(root, facts, groups, provider):
    from .retrospective import _verify
    output, _, _ = _verify(root, facts['study_id'])
    inputs = build_input(facts, groups, digest((output / 'study_manifest.json').read_bytes()))
    if len(canonical(inputs)) > 16000:
        raise ValueError('analysis input exceeds bounded size')
    identity = {'prompt_version': VERSION, 'input_hash': digest(canonical(inputs)), 'code_bundle_id': archive_code(root),
                'model_id': provider.model_id, 'model_revision': provider.model_revision, 'runtime_config': provider.runtime_config,
                'generation': {'temperature': 0, 'max_tokens': 1536, 'enable_thinking': False}}
    key = digest(canonical(identity))
    directory = f"outputs/retrospectives/{facts['study_id']}/inference/{key}"
    out = root / directory
    reference = {'analysis_id': key, 'directory': directory}
    if (out / 'manifest.json').is_file():
        reference['manifest_hash'] = digest((out / 'manifest.json').read_bytes())
        saved = verify(root, reference, facts, groups, provider.model_id, provider.model_revision, provider.runtime_config)
        if saved['identity'] != identity:
            raise RuntimeError('analysis identity changed')
        return reference, saved['summary']
    attempt_path = root / 'data/operations/qwen_attempts' / (uuid.uuid4().hex + '.json')
    attempt = {'stage': 'PRE_HYPOTHESIS_ANALYSIS', 'identity': identity, 'facts': inputs, 'started_at': now_iso(), 'status': 'REQUESTING'}
    write_json(attempt_path, attempt)
    try:
        response = provider.ask(inputs)
        attempt.update(status='RECEIVED', response=response)
        write_json(attempt_path, attempt)
        summary = response_summary(response, inputs, provider.model_id)
        for name, value in (('input.json', inputs), ('response.json', response), ('analysis.json', summary)):
            write_json(out / name, value)
        atomic_bytes(out / 'report.md', render(inputs, summary).encode('utf-8'))
        manifest = {'analysis_id': key, 'identity': identity, 'study_id': facts['study_id'], 'data_grade': inputs['data_grade'],
                    'created_at': now_iso(), 'artifacts': {n: digest((out / n).read_bytes()) for n in ('input.json', 'response.json', 'analysis.json', 'report.md')}}
        write_json(out / 'manifest.json', manifest)
        reference['manifest_hash'] = digest((out / 'manifest.json').read_bytes())
        attempt.update(status='VALIDATED', reference=reference, completed_at=now_iso())
        write_json(attempt_path, attempt)
        return reference, summary
    except Exception as error:
        attempt.update(status='FAILED', error=str(error)[:500], completed_at=now_iso())
        write_json(attempt_path, attempt)
        raise


def verify(root, reference, facts, groups, model_id, model_revision, runtime_config):
    from .retrospective import _verify
    key = reference['analysis_id']
    expected = f"outputs/retrospectives/{facts['study_id']}/inference/{key}"
    if not re.fullmatch('[a-f0-9]{64}', key) or reference['directory'] != expected:
        raise ValueError('invalid analysis reference')
    out = root / expected
    if digest((out / 'manifest.json').read_bytes()) != reference['manifest_hash']:
        raise RuntimeError('analysis manifest changed')
    manifest = read_json(out / 'manifest.json')
    identity = manifest['identity']
    if digest(canonical(identity)) != key or manifest['analysis_id'] != key or manifest['study_id'] != facts['study_id']:
        raise RuntimeError('analysis identity mismatch')
    for name in ('input.json', 'response.json', 'analysis.json', 'report.md'):
        if digest((out / name).read_bytes()) != manifest['artifacts'][name]:
            raise RuntimeError('analysis artifact changed: ' + name)
    output, _, _ = _verify(root, facts['study_id'])
    inputs = read_json(out / 'input.json')
    if inputs != build_input(facts, groups, digest((output / 'study_manifest.json').read_bytes())) or digest(canonical(inputs)) != identity['input_hash']:
        raise RuntimeError('analysis input/source binding mismatch')
    if identity['model_id'] != model_id or identity['model_revision'] != model_revision or identity['runtime_config'] != runtime_config or identity['prompt_version'] != VERSION:
        raise ValueError('analysis model/version mismatch')
    verify_code(root, identity['code_bundle_id'])
    summary = read_json(out / 'analysis.json')
    if summary != response_summary(read_json(out / 'response.json'), inputs, model_id) or (out / 'report.md').read_bytes() != render(inputs, summary).encode('utf-8'):
        raise RuntimeError('analysis summary differs from saved response')
    return {'identity': identity, 'summary': summary, 'created_at': manifest['created_at']}


def bind_plans(plans, summary):
    features = {feature for item in summary['analyses'] for feature in INTERPRETATIONS[item['interpretation_code']]['features']}
    if summary['status'] != 'ANALYZED' or any(c['feature'] not in features for p in plans for c in p['conditions']):
        raise ValueError('hypothesis conditions lack preceding analytical evidence')
