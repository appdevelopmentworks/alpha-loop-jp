import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_loop.common import digest, read_json, write_json
from alpha_loop import self_improvement as loop
from alpha_loop.self_improvement_fixture import input_day, issue, synthetic_policy
from alpha_loop.pipeline import evaluate_run
from alpha_loop.research import compare, prepare_batch, register

PROJECT = Path(__file__).resolve().parents[1]
BASELINE = PROJECT / 'configs/baseline.json'
POLICY = PROJECT / 'configs/self_improvement.json'


class HypothesisLoopTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy = synthetic_policy(self.root, POLICY)
        self.m, self.day, self.following, _ = issue(self.root, BASELINE, self.policy, '2026-08-24')

    def tearDown(self):
        self.temp.cleanup()

    def evaluation(self, session=None):
        target = session or self.day['session']
        next_day = self.following
        value = evaluate_run(self.root, self.m['run_id'], self.root / 'input' / target, next_day)
        return {'run_id': self.m['run_id'], 'evaluation_id': value['evaluation_id'], 'future_ref': f'input/{target}/future.json'}

    def test_candidates_match_saved_conditions_and_keep_baseline(self):
        decisions = read_json(self.root / 'outputs' / self.m['run_id'] / 'decisions.json')
        selected = read_json(self.root / self.day['directory'] / 'selections.json')
        self.assertEqual(len(next(iter(selected.values()))), 10)
        self.assertEqual(sum(d['selected'] for d in decisions), 20)
        self.assertEqual(self.day['forecast_session'], '2026-08-25')
        self.assertEqual(self.day['operational_candidate_csv'], str(self.root / 'outputs' / self.m['run_id'] / 'candidates.csv'))

    def test_repeated_day_reuses_first_forecast(self):
        before = digest(Path(self.day['candidate_csv']).read_bytes())
        self.assertEqual(self.day, loop.day(self.root, self.m['run_id'], self.policy))
        self.assertEqual(before, digest(Path(self.day['candidate_csv']).read_bytes()))
        self.assertEqual(len(loop.status(self.root)['versions']), 1)

    def test_changed_policy_cannot_rewrite_day(self):
        policy = read_json(self.policy)
        policy['max_versions'] += 1
        write_json(self.policy, policy)
        with self.assertRaisesRegex(ValueError, 'sealed'):
            loop.day(self.root, self.m['run_id'], self.policy)

    def test_artifact_tampering_is_rejected(self):
        Path(self.day['candidate_csv']).write_text('modified', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'artifact'):
            loop.day(self.root, self.m['run_id'], self.policy)

    def test_unknown_cost_and_unfilled_do_not_become_trade_profit(self):
        result = loop.feedback(self.root, [self.evaluation()], self.following)[0]
        rows = read_json(self.root / result['directory'] / 'outcomes.json')
        selected = [r for r in rows if r['B_selected']]
        self.assertEqual(sum(r['discovery_hit'] is True for r in selected), 10)
        self.assertIsNone(next(r for r in rows if r['instrument_id'] == 'TSE:0001')['gross_proxy_return'])
        self.assertIsNone(next(r for r in rows if r['instrument_id'] == 'TSE:0002')['gross_proxy_return'])
        self.assertEqual(next(r for r in rows if r['instrument_id'] == 'TSE:0022')['result'], 'UNKNOWN')

    def test_hand_counts_capture_false_positive_and_miss(self):
        result = loop.feedback(self.root, [self.evaluation()], self.following)[0]
        metrics = next(iter(result['metrics'].values()))
        self.assertEqual(metrics['A']['hits'], 10)
        self.assertEqual(metrics['A']['false_positives'], 10)
        self.assertEqual(metrics['B']['hits'], 10)
        self.assertEqual(metrics['B']['false_positives'], 0)
        self.assertEqual(metrics['B']['capture_rate'], 1)
        self.assertEqual(loop.feedback(self.root, [self.evaluation()], self.following)[0], result)

    def test_future_label_rejected(self):
        with self.assertRaisesRegex(ValueError, 'future results'):
            loop.feedback(self.root, [self.evaluation()], self.day['session'])

    def test_original_future_hash_required(self):
        entry = self.evaluation()
        future = self.root / entry['future_ref']
        future.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'future evaluation input'):
            loop.feedback(self.root, [entry], self.following)

    def test_feedback_requires_original_input(self):
        entry = self.evaluation()
        del entry['future_ref']
        with self.assertRaisesRegex(ValueError, 'original future'):
            loop.feedback(self.root, [entry], self.following)

    def test_feedback_artifact_tampering_rejected(self):
        entry = self.evaluation()
        result = loop.feedback(self.root, [entry], self.following)[0]
        (self.root / result['directory'] / 'metrics.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'artifact'):
            loop.learning_facts(self.root, self.following)

    def test_feedback_drives_next_generation_and_duplicate_collapses(self):
        loop.feedback(self.root, [self.evaluation()], self.following)
        _, new, _, provider = issue(self.root, BASELINE, self.policy, '2026-09-03', ['ret5_le_0_05', 'volume_ge_2', 'ma25_gap_le_0_15'])
        self.assertTrue(provider.facts['learning_feedback']['feedback'])
        self.assertEqual(len(new['new_versions']), 1)
        _, repeated, _, _ = issue(self.root, BASELINE, self.policy, '2026-09-04', ['ret5_le_0_05', 'volume_ge_2', 'ma25_gap_le_0_15'])
        self.assertEqual(repeated['new_versions'], [])
        self.assertEqual(len(loop.status(self.root)['versions']), 2)

    def test_protected_holdout_generates_no_new_version(self):
        _, result, _, _ = issue(self.root, BASELINE, self.policy, '2026-08-26', ['ret5_le_0_05'])
        self.assertEqual(result['proposal_status'], 'PROTECTED_HOLDOUT')
        self.assertEqual(result['new_versions'], [])
        self.assertEqual(len(loop.status(self.root)['versions']), 1)

    def test_bad_proposal_response_cannot_become_conditions(self):
        _, result, _, _ = issue(self.root, BASELINE, self.policy, '2026-09-03')
        report = Path(result['source']['report_path'])
        value = read_json(report)
        value['hypothesis_plans'][0]['condition_ids'] = ['invented_condition']
        write_json(report, value)
        # A new day cannot bind an old discovery source even if its JSON is edited.
        directory, _ = input_day(self.root, '2026-09-04')
        from alpha_loop.pipeline import run
        with patch('alpha_loop.pipeline.now_iso', return_value='2026-09-04T20:00:00+09:00'):
            m = run(self.root, directory, BASELINE, '2026-09-04', '2026-09-04T20:00:00+09:00')
        with self.assertRaises(ValueError):
            loop.day(self.root, m['run_id'], self.policy, report)

    def test_journal_resumes_interrupted_publication(self):
        m, _, _, _ = issue(self.root, BASELINE, self.policy, '2026-09-03')
        # Simulate a different fresh day with no AI; old versions still issue forecasts.
        directory, _ = input_day(self.root, '2026-09-04')
        from alpha_loop.pipeline import run
        with patch('alpha_loop.pipeline.now_iso', return_value='2026-09-04T20:00:00+09:00'):
            m = run(self.root, directory, BASELINE, '2026-09-04', '2026-09-04T20:00:00+09:00')
        at = '2026-09-04T20:00:00+09:00'
        with patch('alpha_loop.self_improvement.now_iso', return_value=at), patch('alpha_loop.self_improvement.write_csv', side_effect=OSError('disk interruption')):
            with self.assertRaises(OSError):
                loop.day(self.root, m['run_id'], self.policy)
        with patch('alpha_loop.self_improvement.now_iso', return_value='2026-09-05T12:00:00+09:00'):
            recovered = loop.day(self.root, m['run_id'], self.policy)
        self.assertEqual(recovered['created_at'], at)
        self.assertTrue(recovered['forecast_eligible'])
        self.assertEqual(loop.day(self.root, m['run_id'], self.policy), recovered)

    def test_zero_hypothesis_emits_header_and_preserves_baseline(self):
        other = self.root / 'empty'
        policy = synthetic_policy(other, POLICY)
        directory, _ = input_day(other, '2026-08-24')
        from alpha_loop.pipeline import run
        m = run(other, directory, BASELINE, '2026-08-24', '2026-08-24T20:00:00+09:00')
        result = loop.day(other, m['run_id'], policy)
        self.assertEqual(result['shadow_count'], 0)
        self.assertEqual(len(Path(result['candidate_csv']).read_text(encoding='utf-8-sig').splitlines()), 1)
        self.assertFalse(result['forecast_eligible'])  # Late historical execution remains descriptive.

    def test_weekend_and_holiday_next_session(self):
        _, result, _, _ = issue(self.root, BASELINE, self.policy, '2026-09-18')
        self.assertEqual(result['forecast_session'], '2026-09-24')

    def test_global_version_budget_is_bounded(self):
        policy = read_json(self.policy)
        policy['max_versions'] = 1
        path = self.root / 'limited.json'
        write_json(path, policy)
        directory, _ = input_day(self.root, '2026-09-03')
        from alpha_loop.pipeline import run
        from alpha_loop.csv_input import derive_ranking
        from alpha_loop.retrospective import retrospective
        from alpha_loop.reporting import qwen_retrospective_report
        from alpha_loop.self_improvement_fixture import SyntheticReasoner
        m = run(self.root, directory, BASELINE, '2026-09-03', '2026-09-03T20:00:00+09:00')
        ranking = derive_ranking(self.root, directory, '2026-09-03', .05, 50)
        study = retrospective(self.root, Path(ranking['ranking_input']), directory, BASELINE)
        report = qwen_retrospective_report(self.root, study['study_id'], SyntheticReasoner(['ret5_le_0_05']), analysis_required=True)
        value = loop.day(self.root, m['run_id'], path, Path(report['report_path']))
        self.assertEqual(value['new_versions'], [])

    def test_simulation_policy_requires_marker(self):
        (self.root / 'data/self_improvement/SIMULATION.json').unlink()
        with self.assertRaisesRegex(ValueError, 'isolated'):
            loop.day(self.root, self.m['run_id'], self.policy)

    def test_policy_rejects_nan_and_invalid_bounds(self):
        policy = read_json(self.policy)
        policy['monitor']['max_unknown_rate'] = -1
        write_json(self.policy, policy)
        with self.assertRaises(ValueError):
            loop.settings(self.policy)

    def test_second_complete_evaluation_cannot_replace_first(self):
        entry = self.evaluation()
        loop.feedback(self.root, [entry], self.following)
        with self.assertRaisesRegex(ValueError, 'cannot substitute'):
            loop.feedback(self.root, [{**entry, 'evaluation_id': '0' * 20}], self.following)

    def test_replaying_known_labels_before_their_date_is_rejected(self):
        entry = self.evaluation()
        loop.feedback(self.root, [entry], self.following)
        with self.assertRaisesRegex(ValueError, 'future results'):
            loop.feedback(self.root, [entry], self.day['session'])

    def test_future_generations_not_exposed_to_past_learning(self):
        issue(self.root, BASELINE, self.policy, '2026-09-03', ['ret5_le_0_05'])
        facts = loop.learning_facts(self.root, '2026-08-25')
        self.assertEqual(len(facts['hypotheses']), 1)

    def test_changed_loop_engine_cannot_resume_sealed_forecast(self):
        with patch('alpha_loop.self_improvement.engine_hash', return_value='changed'):
            with self.assertRaisesRegex(RuntimeError, 'loop code changed'):
                loop.day(self.root, self.m['run_id'], self.policy)

    def test_interruption_recovered_after_open_is_not_a_timely_forecast(self):
        directory, following = input_day(self.root, '2026-09-03')
        from alpha_loop.pipeline import run
        at = '2026-09-03T20:00:00+09:00'
        with patch('alpha_loop.pipeline.now_iso', return_value=at):
            m = run(self.root, directory, BASELINE, '2026-09-03', at)
        with patch('alpha_loop.self_improvement.now_iso', return_value=at), patch('alpha_loop.self_improvement.write_csv', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                loop.day(self.root, m['run_id'], self.policy)
        with patch('alpha_loop.self_improvement.now_iso', return_value=following + 'T10:00:00+09:00'):
            result = loop.day(self.root, m['run_id'], self.policy)
        self.assertFalse(result['forecast_eligible'])
        self.assertGreaterEqual(result['completed_at'], following + 'T09:00:00+09:00')

    def test_feedback_paths_cannot_escape_root(self):
        with self.assertRaisesRegex(ValueError, 'inside the operation root'):
            loop.feedback(self.root, [{'run_id': self.m['run_id'], 'evaluation_id': 'a' * 20, 'future_ref': '../private.json'}], self.following)


class LoopServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        directory, _ = input_day(self.root, '2026-08-24')
        config = read_json(PROJECT / 'configs/hermes_demo.json')
        config.update(input_dir=str(directory), strategy_config=str(BASELINE),
                      self_improvement_config=str(POLICY))
        self.config = self.root / 'operation.json'
        write_json(self.config, config)

    def tearDown(self):
        self.temp.cleanup()

    def test_native_hook_no_ai_runs_and_duplicate_is_idempotent(self):
        from alpha_loop.service import operate
        result = operate(self.root, self.config, no_ai=True)
        self.assertEqual(result['self_improvement']['status'], 'SHADOW_SAVED')
        self.assertTrue(Path(result['candidate_csv']).is_file())
        again = operate(self.root, self.config, no_ai=True)
        self.assertEqual(result['self_improvement'], again['self_improvement'])

    def test_loop_failure_preserves_daily_and_recovers_on_retry(self):
        from alpha_loop.service import operate
        with patch('alpha_loop.self_improvement.day', side_effect=OSError('temporary disk failure')):
            result = operate(self.root, self.config, no_ai=True)
        self.assertEqual(result['status'], 'SUCCEEDED_WITH_SIDE_FAILURE')
        self.assertIn('self_improvement', result['side_failures'])
        before = digest(Path(result['candidate_csv']).read_bytes())
        again = operate(self.root, self.config, no_ai=True)
        self.assertNotIn('self_improvement', again['side_failures'])
        self.assertEqual(again['status'], 'SUCCEEDED')
        self.assertEqual(before, digest(Path(again['candidate_csv']).read_bytes()))

    def test_native_operation_rejects_simulation_policy(self):
        from alpha_loop.service import operate
        policy = synthetic_policy(self.root, POLICY)
        config = read_json(self.config)
        config['self_improvement_config'] = str(policy)
        write_json(self.config, config)
        with self.assertRaisesRegex(ValueError, 'simulation'):
            operate(self.root, self.config, no_ai=True)


class LoopReleaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.master = tempfile.TemporaryDirectory()
        cls.base = Path(cls.master.name)
        policy = synthetic_policy(cls.base, POLICY)
        cls.entries = []
        for session in ['2026-08-24', '2026-08-25', '2026-08-26', '2026-08-27', '2026-08-28', '2026-08-31', '2026-09-01']:
            loop.feedback(cls.base, cls.entries, session)
            m, _, following, _ = issue(cls.base, BASELINE, policy, session)
            e = evaluate_run(cls.base, m['run_id'], cls.base / 'input' / session, following)
            cls.entries.append({'run_id': m['run_id'], 'evaluation_id': e['evaluation_id'], 'future_ref': f'input/{session}/future.json'})
        cls.experiment = loop.status(cls.base)['registrations'][0]['experiment_id']
        compare(cls.base, cls.experiment, prepare_batch(cls.base, cls.experiment))

    @classmethod
    def tearDownClass(cls):
        cls.master.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'r'
        shutil.copytree(self.base, self.root)
        self.policy = self.root / 'policy.json'

    def tearDown(self):
        self.temp.cleanup()

    def activate(self):
        with patch('alpha_loop.self_improvement.now_iso', return_value='2026-09-02T20:00:00+09:00'):
            return loop.activate(self.root, self.experiment, self.policy, reviewer='authored human', human_reviewed=True)

    def test_first_release_needs_human_and_synthetic_cannot_activate_production(self):
        with self.assertRaisesRegex(ValueError, 'first ACTIVE'):
            loop.activate(self.root, self.experiment, self.policy, automatic=True)
        with self.assertRaisesRegex(ValueError, 'human review'):
            loop.activate(self.root, self.experiment, self.policy, reviewer='name')
        policy = read_json(self.policy)
        policy['mode'] = 'shadow'
        write_json(self.policy, policy)
        with self.assertRaisesRegex(ValueError, 'cannot activate production'):
            loop.activate(self.root, self.experiment, self.policy, reviewer='name', human_reviewed=True)
        self.assertIsNone(loop.status(self.root)['production']['version_id'])

    def test_accepted_release_and_manual_rollback_leave_baseline_bytes(self):
        baseline_before = digest(BASELINE.read_bytes())
        release = self.activate()
        self.assertTrue(release['version_id'])
        self.assertEqual(release, self.activate())
        result = loop.rollback(self.root, self.policy, 'manual revert')
        self.assertEqual(result['status'], 'ROLLED_BACK')
        self.assertIsNone(result['state']['version_id'])
        self.assertEqual(baseline_before, digest(BASELINE.read_bytes()))
        with self.assertRaisesRegex(ValueError, 'previously activated|retired'):
            self.activate()

    def test_human_consent_does_not_authorize_changed_policy(self):
        self.activate()
        policy = loop.settings(self.policy)
        with loop._db(self.root) as con:
            self.assertTrue(loop._human_policy_approved(con, policy))
            changed = read_json(self.policy)
            changed['monitor']['max_capture_drop'] = .9
            self.assertFalse(loop._human_policy_approved(con, changed))
            changed['mode'] = 'shadow'
            self.assertFalse(loop._human_policy_approved(con, changed))

    def test_bad_capture_automatically_rolls_back(self):
        self.activate()
        m, forecast, following, _ = issue(self.root, BASELINE, self.policy, '2026-09-03', bad=True)
        self.assertGreater(forecast['active_count'], 0)
        e = evaluate_run(self.root, m['run_id'], self.root / 'input/2026-09-03', following)
        loop.feedback(self.root, [{'run_id': m['run_id'], 'evaluation_id': e['evaluation_id'], 'future_ref': 'input/2026-09-03/future.json'}], following)
        result = loop.monitor(self.root, self.policy, following)
        self.assertEqual(result['status'], 'ROLLED_BACK')
        self.assertIn('capture_deteriorated', result['reason'])
        self.assertIsNone(loop.status(self.root)['simulation']['version_id'])

    def test_healthy_release_keeps_running_and_policy_is_frozen(self):
        self.activate()
        m, _, following, _ = issue(self.root, BASELINE, self.policy, '2026-09-03')
        e = evaluate_run(self.root, m['run_id'], self.root / 'input/2026-09-03', following)
        loop.feedback(self.root, [{'run_id': m['run_id'], 'evaluation_id': e['evaluation_id'], 'future_ref': 'input/2026-09-03/future.json'}], following)
        self.assertEqual(loop.monitor(self.root, self.policy, following)['status'], 'MONITOR_OK')
        policy = read_json(self.policy)
        policy['monitor']['max_capture_drop'] = .9
        write_json(self.policy, policy)
        with self.assertRaisesRegex(ValueError, 'policy is frozen'):
            loop.monitor(self.root, self.policy, following)

    def test_comparison_tampering_cannot_activate(self):
        from alpha_loop.research import _store
        con = _store(self.root)
        report_id = con.execute('SELECT report_id FROM m5_reports WHERE experiment_id=?', (self.experiment,)).fetchone()[0]
        con.close()
        path = self.root / 'outputs/research' / self.experiment / report_id / 'summary.json'
        path.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'artifact'):
            self.activate()

    def test_price_comparison_without_timely_hypothesis_forecasts_cannot_activate(self):
        with loop._db(self.root) as con:
            con.execute("DELETE FROM objects WHERE kind='day' AND id='2026-08-26'")
        with self.assertRaisesRegex(ValueError, 'timely sealed'):
            self.activate()

    def test_release_chain_corruption_rejected(self):
        self.activate()
        with loop._db(self.root) as con:
            con.execute("UPDATE events SET payload='{}' WHERE seq=1")
        with self.assertRaises((RuntimeError, KeyError)):
            loop.status(self.root)

    def test_lock_prevents_concurrent_release(self):
        from alpha_loop.runtime import exclusive
        with exclusive(self.root / 'data/self_improvement/.lock'):
            with self.assertRaisesRegex(RuntimeError, 'ALREADY_RUNNING'):
                self.activate()

    def test_revealed_holdout_cannot_register_another_version(self):
        original = read_json(next((self.root / 'data/self_improvement/plans').glob('*/plan.json')))
        original.update(hypothesis_id='H-NEW', family_id='UNUSED-FAMILY')
        original.pop('proposal_ref', None)
        path = self.root / 'another.json'
        write_json(path, original)
        with self.assertRaisesRegex(ValueError, 'revealed'):
            register(self.root, path)


if __name__ == '__main__':
    unittest.main()
