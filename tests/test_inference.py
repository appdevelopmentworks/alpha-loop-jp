import copy
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from alpha_loop import inference, self_improvement as loop
from alpha_loop.common import digest, read_json, write_json
from alpha_loop.csv_input import derive_ranking
from alpha_loop.pipeline import run
from alpha_loop.reporting import LocalQwen, NoHypothesisEvidence, qwen_retrospective_report
from alpha_loop.retrospective import retrospective
from alpha_loop.self_improvement_fixture import SyntheticReasoner, input_day

PROJECT = Path(__file__).resolve().parents[1]
BASELINE = PROJECT / 'configs/baseline.json'


class TracedReasoner(SyntheticReasoner):
    def __init__(self, mutate=None):
        super().__init__()
        self.calls = []
        self.mutate = mutate

    def ask(self, facts):
        self.calls.append(copy.deepcopy(facts))
        if facts['report_kind'] == 'daily_constraints':
            return {'model': self.model_id, 'choices': [{'message': {'content': json.dumps({'note_codes': facts['required_note_codes']})}, 'finish_reason': 'stop'}]}
        response = super().ask(facts)
        return self.mutate(facts, response) if self.mutate else response


class InferenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        directory, _ = input_day(self.root, '2026-08-24')
        self.manifest = run(self.root, directory, BASELINE, '2026-08-24', '2026-08-24T20:00:00+09:00')
        ranking = derive_ranking(self.root, directory, '2026-08-24', .05, 50)
        self.study = retrospective(self.root, Path(ranking['ranking_input']), directory, BASELINE)
        self.provider = TracedReasoner()

    def tearDown(self):
        self.temp.cleanup()

    def report(self, provider=None, feedback=None):
        return qwen_retrospective_report(self.root, self.study['study_id'], provider or self.provider, feedback, analysis_required=True)

    def test_two_calls_analysis_precedes_hypothesis_and_raw_artifacts_unchanged(self):
        path = self.root / 'outputs' / self.manifest['run_id'] / 'candidates.csv'
        before = digest(path.read_bytes())
        report = self.report()
        self.assertEqual([f['report_kind'] for f in self.provider.calls], ['hypothesis_analysis', 'ranking_retrospective'])
        ref = report['inference']
        out = self.root / ref['directory']
        self.assertTrue((out / 'input.json').is_file())
        self.assertTrue((out / 'response.json').is_file())
        text = (out / 'report.md').read_text(encoding='utf-8')
        for expected in ('根拠 obs:', '代替説明:', '反証条件:', '因果関係ではない'):
            self.assertIn(expected, text)
        self.assertEqual(self.provider.calls[1]['pre_hypothesis_analysis']['reference'], ref)
        self.assertEqual(read_json(Path(report['report_path']))['inference'], ref)
        self.assertEqual(digest(path.read_bytes()), before)

    def test_repeat_uses_both_saved_responses(self):
        first = self.report()
        self.assertEqual(first, self.report())
        self.assertEqual(len(self.provider.calls), 2)

    def test_failed_hypothesis_retries_without_repeating_valid_analysis(self):
        failed = False
        def mutate(facts, response):
            nonlocal failed
            if facts['report_kind'] == 'ranking_retrospective' and not failed:
                failed = True
                raise OSError('temporary hypothesis failure')
            return response
        provider = TracedReasoner(mutate)
        with self.assertRaises(OSError):
            self.report(provider)
        self.report(provider)
        self.assertEqual([f['report_kind'] for f in provider.calls], ['hypothesis_analysis', 'ranking_retrospective', 'ranking_retrospective'])

    def test_insufficient_analysis_is_saved_and_does_not_call_hypothesis(self):
        def mutate(facts, response):
            response['choices'][0]['message']['content'] = '{"status":"INSUFFICIENT_EVIDENCE","analyses":[]}'
            return response
        provider = TracedReasoner(mutate)
        with self.assertRaisesRegex(NoHypothesisEvidence, 'insufficient evidence'):
            self.report(provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(list((self.root / 'outputs/retrospectives' / self.study['study_id'] / 'inference').glob('*/analysis.json'))), 1)

    def test_wrong_model_truncated_and_refusal_responses_cannot_generate_hypotheses(self):
        for field, value in (('model', 'wrong-model'), ('finish_reason', 'length'), ('finish_reason', 'content_filter')):
            def mutate(facts, response):
                if field == 'model':
                    response[field] = value
                else:
                    response['choices'][0][field] = value
                return response
            provider = TracedReasoner(mutate)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.report(provider)
            self.assertEqual(len(provider.calls), 1)

    def test_invented_values_unknown_ids_and_mismatched_feature_are_rejected(self):
        good = SyntheticReasoner().ask({'report_kind': 'hypothesis_analysis'})
        content = json.loads(good['choices'][0]['message']['content'])
        variants = []
        for field, value in (('value', 999), ('evidence_ids', ['invented']), ('evidence_ids', ['obs:ret_5d']), ('confidence', 'high')):
            bad = copy.deepcopy(content)
            bad['analyses'][0][field] = value
            variants.append(bad)
        for value in variants:
            provider = TracedReasoner(lambda facts, response: {**response, 'choices': [{'message': {'content': json.dumps(value)}, 'finish_reason': 'stop'}]})
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.report(provider)
            self.assertEqual(len(provider.calls), 1)

    def test_duplicate_keys_prose_and_malformed_json_are_rejected(self):
        for content in ('説明です。', '{"status":"ANALYZED","status":"ANALYZED","analyses":[]}', '{"status":NaN,"analyses":[]}'):
            provider = TracedReasoner(lambda facts, response: {**response, 'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]})
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.report(provider)

    def test_unknown_observation_cannot_ground_an_explanation(self):
        self.report()
        inputs = copy.deepcopy(self.provider.calls[0])
        inputs['evidence_index']['obs:rel_volume_20d']['usable'] = False
        raw = SyntheticReasoner().ask(inputs)['choices'][0]['message']['content']
        with self.assertRaisesRegex(ValueError, 'observed matching'):
            inference.validate(raw, inputs)

    def test_future_feedback_is_refused_before_any_model_call(self):
        feedback = {'cutoff_session': '2026-08-24', 'feedback': {}, 'source_days': [
            {'source_session': '2026-08-24', 'outcome_session': '2026-08-25'}]}
        with self.assertRaisesRegex(ValueError, 'future'):
            self.report(feedback=feedback)
        self.assertEqual(self.provider.calls, [])

    def test_protected_holdout_blocks_analysis_before_any_model_call(self):
        with patch('alpha_loop.self_improvement.protected', return_value=True):
            with self.assertRaisesRegex(NoHypothesisEvidence, 'protected holdout'):
                self.report()
        self.assertEqual(self.provider.calls, [])

    def test_model_runtime_and_reference_cannot_be_substituted(self):
        report = self.report()
        facts = self.provider.calls[1]
        groups = read_json(self.root / 'outputs/retrospectives' / self.study['study_id'] / 'comparison.json')['groups']
        args = (self.root, report['inference'], facts, groups, self.provider.model_id, self.provider.model_revision)
        with self.assertRaisesRegex(ValueError, 'model/version'):
            inference.verify(*args, {'runtime_config_version': 'different'})
        changed = dict(report['inference'], directory='../private')
        with self.assertRaisesRegex(ValueError, 'reference'):
            inference.verify(self.root, changed, facts, groups, self.provider.model_id, self.provider.model_revision, self.provider.runtime_config)

    def test_hypothesis_cannot_use_features_absent_from_analysis(self):
        def mutate(facts, response):
            if facts['report_kind'] == 'hypothesis_analysis':
                value = json.loads(response['choices'][0]['message']['content'])
                value['analyses'] = value['analyses'][:1]  # Only volume, but hypothesis also uses ret5.
                response['choices'][0]['message']['content'] = json.dumps(value)
            return response
        with self.assertRaisesRegex(ValueError, 'preceding analytical evidence'):
            self.report(TracedReasoner(mutate))

    def test_saved_analysis_modification_is_rejected_on_reuse(self):
        first = self.report()
        (self.root / first['inference']['directory'] / 'analysis.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'artifact changed'):
            self.report()
        self.assertEqual(len(self.provider.calls), 2)

    def test_generation_without_analysis_cannot_enter_new_loop(self):
        legacy = qwen_retrospective_report(self.root, self.study['study_id'], self.provider)
        with self.assertRaisesRegex(ValueError, 'preceding verified analysis'):
            loop.day(self.root, self.manifest['run_id'], PROJECT / 'configs/self_improvement.json', Path(legacy['report_path']))
        self.assertEqual(loop.status(self.root)['versions'], [])

    def test_analysis_not_available_at_forecast_time_is_rejected(self):
        report = self.report()
        with patch('alpha_loop.self_improvement.now_iso', return_value='2026-08-24T20:00:00+09:00'):
            with self.assertRaisesRegex(ValueError, 'not available'):
                loop.day(self.root, self.manifest['run_id'], PROJECT / 'configs/self_improvement.json', Path(report['report_path']))

    def test_refused_hypothesis_after_valid_analysis_is_not_accepted(self):
        def mutate(facts, response):
            if facts['report_kind'] == 'ranking_retrospective':
                response['choices'][0]['finish_reason'] = 'content_filter'
            return response
        with self.assertRaisesRegex(ValueError, 'incomplete hypothesis'):
            self.report(TracedReasoner(mutate))

    def test_native_failure_preserves_numerical_candidates(self):
        from alpha_loop.service import operate
        config = read_json(PROJECT / 'configs/hermes_demo.json')
        config.update(input_dir='input/2026-08-24', strategy_config=str(BASELINE), ranking_return_min=.05, qwen_enabled=True,
                      self_improvement_config=str(PROJECT / 'configs/self_improvement.json'),
                      qwen_model_id=self.provider.model_id, qwen_model_revision=self.provider.model_revision,
                      qwen_runtime_config=str(PROJECT / 'configs/qwen_runtime_rtx5090_v1.json'))
        write_json(self.root / 'operation.json', config)
        def mutate(facts, response):
            if facts['report_kind'] == 'hypothesis_analysis':
                raise OSError('analysis unavailable')
            return response
        provider = TracedReasoner(mutate)
        with patch('alpha_loop.service.LocalQwen', return_value=provider), patch('alpha_loop.model_session.local_model', return_value=nullcontext()):
            result = operate(self.root, self.root / 'operation.json')
        self.assertEqual(result['qwen_status'], 'FAILED')
        self.assertTrue(Path(result['candidate_csv']).is_file())
        self.assertEqual(loop.status(self.root)['versions'], [])

    def test_wire_prompt_requests_bounded_summary_and_does_not_enable_thinking(self):
        provider = LocalQwen('http://127.0.0.1:8000', 'Qwen/test', 'rev', {'runtime_config_version': 'test'})
        with patch.object(provider, '_request', side_effect=[{'data': [{'id': 'Qwen/test'}]}, {}]) as request:
            provider.ask({'report_kind': 'hypothesis_analysis'})
        body = request.call_args.args[1]
        self.assertEqual(body['max_tokens'], 1536)
        self.assertFalse(body['chat_template_kwargs']['enable_thinking'])
        self.assertIn('反証条件', body['messages'][0]['content'])


if __name__ == '__main__':
    unittest.main()
