"""Use the pinned local Qwen on a saved retrospective; never collect or issue forecasts."""
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from alpha_loop.common import digest, now_iso, read_json, write_json
from alpha_loop.model_session import local_model
from alpha_loop.models import model_status
from alpha_loop.pipeline import _verified_manifest
from alpha_loop.reporting import LocalQwen, qwen_retrospective_report
from alpha_loop.retrospective import _verify
from alpha_loop.runtime import exclusive, status
from alpha_loop.self_improvement import learning_facts


class TracedQwen(LocalQwen):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stages = []

    def ask(self, facts):
        self.stages.append(facts['report_kind'])
        return super().ask(facts)


def main():
    with exclusive(PROJECT / 'data/operations/.supervisor.lock'):
        state = status(PROJECT)
        if state['running'] or state['owner_unknown']:
            raise RuntimeError('live batch active; manual model use refused')
        latest_path = PROJECT / 'data/operations/latest_service.json'
        latest_hash = digest(latest_path.read_bytes())
        latest = read_json(latest_path)
        manifest = _verified_manifest(PROJECT / 'outputs' / latest['run_id'])
        study_id = latest['qwen']['hypotheses']['study_id']
        output, study, _ = _verify(PROJECT, study_id)
        protected = {name: digest((output / name).read_bytes()) for name in ('study_manifest.json', 'features.json', 'comparison.json', 'decisions.json')}
        config = read_json(PROJECT / 'configs/hermes_self_improving.json')
        provider = TracedQwen('http://127.0.0.1:8000', config['qwen_model_id'], config['qwen_model_revision'],
                             read_json(PROJECT / config['qwen_runtime_config']))
        before = model_status()['containers']
        with local_model(PROJECT, 'qwen', 1200):
            report = qwen_retrospective_report(PROJECT, study_id, provider, learning_facts(PROJECT, study['ranking_session']), analysis_required=True)
        after = model_status()['containers']
        if before != after or digest(latest_path.read_bytes()) != latest_hash or any(digest((output / k).read_bytes()) != v for k, v in protected.items()):
            raise RuntimeError('model restoration or preserved research artifacts differ')
        _verified_manifest(PROJECT / 'outputs' / manifest['run_id'])
        proof = {'verified_at': now_iso(), 'model_id': provider.model_id, 'model_revision': provider.model_revision,
                 'study_id': study_id, 'ranking_session': study['ranking_session'], 'feature_session': study['feature_session'],
                 'data_grade': study['data_grade'], 'report': report, 'actual_model_calls': provider.stages,
                 'analysis_before_hypothesis_validated': True, 'numerical_artifacts_preserved': True,
                 'gpu_states_before': before, 'gpu_states_after': after, 'model_states_restored': True,
                 'new_forecasts_issued': False, 'market_performance_validated': False}
        write_json(PROJECT / 'data/operations/inference_local_acceptance.json', proof)
    print(json.dumps(proof, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
